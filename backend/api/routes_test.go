package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"search-engine-v2/backend/cache"
	pb "search-engine-v2/backend/proto"
	"search-engine-v2/backend/search"
)

type stubWorker struct {
	calls       int
	err         error
	scores      []float32 // nil: zeros
	rerankErr   error
	rerankCalls int
	passages    []string
	rerankQuery string
}

func (w *stubWorker) Rerank(ctx context.Context, in *pb.RerankRequest, _ ...grpc.CallOption) (*pb.RerankResponse, error) {
	w.rerankCalls++
	w.passages = in.Passages
	w.rerankQuery = in.Query
	if w.rerankErr != nil {
		return nil, w.rerankErr
	}
	if w.scores != nil {
		return &pb.RerankResponse{Scores: w.scores}, nil
	}
	return &pb.RerankResponse{Scores: make([]float32, len(in.Passages))}, nil
}

func (w *stubWorker) ParseQuery(ctx context.Context, in *pb.ParseQueryRequest, _ ...grpc.CallOption) (*pb.ParseQueryResponse, error) {
	w.calls++
	if w.err != nil {
		return nil, w.err
	}
	return &pb.ParseQueryResponse{CleanQuery: "about kubernetes", Keywords: []string{"kubernetes"}, TextVector: []float32{1}, ImageVector: []float32{2, 3}, Filters: &pb.Filters{Extensions: []string{"pdf"}}}, nil
}

type stubSearcher struct {
	got     search.Params
	calls   int
	results []search.Result // nil: one result
}

func (s *stubSearcher) Search(ctx context.Context, p search.Params) ([]search.Result, error) {
	s.calls++
	s.got = p
	if s.results != nil {
		return s.results, nil
	}
	return []search.Result{{ObjectKey: "a.pdf"}}, nil
}

func get(t *testing.T, h http.Handler, url string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", url, nil))
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	return rec.Code, body
}

func TestSearchPassesParsedQueryToOpenSearch(t *testing.T) {
	s := &stubSearcher{}
	code, body := get(t, New(&stubWorker{}, s, nil, Cutoffs{}, nil), "/search?q=pdfs+about+kubernetes")
	if code != 200 || len(body["results"].([]any)) != 1 {
		t.Fatalf("got %d %v", code, body)
	}
	if s.got.Text != "kubernetes" || s.got.Filters.Extensions[0] != "pdf" || len(s.got.Vector) != 1 || len(s.got.ImageVector) != 2 || s.got.Limit != candidates(10) {
		t.Fatalf("unexpected params: %+v", s.got)
	}
}

func TestLimitIsClampedBetween1And50(t *testing.T) {
	for in, want := range map[string]int{"0": 1, "-3": 1, "7": 7, "999": 50, "abc": 10, "": 10} {
		s := &stubSearcher{}
		get(t, New(&stubWorker{}, s, nil, Cutoffs{}, nil), "/search?q=x&limit="+in)
		if s.got.Limit != candidates(want) {
			t.Errorf("limit=%q: got %d, want %d", in, s.got.Limit, want)
		}
	}
}

func TestBlankQueryReturnsNoResultsWithoutCallingTheWorker(t *testing.T) {
	w := &stubWorker{}
	code, body := get(t, New(w, &stubSearcher{}, nil, Cutoffs{}, nil), "/search?q=+++")
	if code != 200 || len(body["results"].([]any)) != 0 || w.calls != 0 {
		t.Fatalf("got %d %v, worker calls %d", code, body, w.calls)
	}
}

func TestWorkerFailureIs503(t *testing.T) {
	code, _ := get(t, New(&stubWorker{err: status.Error(codes.Unavailable, "down")}, &stubSearcher{}, nil, Cutoffs{}, nil), "/search?q=x")
	if code != http.StatusServiceUnavailable {
		t.Fatalf("got %d", code)
	}
}

func TestHealthOnlyRunsTheDependencyChecks(t *testing.T) {
	w, s := &stubWorker{}, &stubSearcher{}
	checks := map[string]func(context.Context) error{"opensearch": func(context.Context) error { return nil }}
	if code, _ := get(t, New(w, s, nil, Cutoffs{}, checks), "/health"); code != 200 {
		t.Fatalf("got %d", code)
	}
	if w.calls != 0 || s.calls != 0 {
		t.Fatal("health ran a search")
	}
}

func TestHealthIs503WhenADependencyIsDown(t *testing.T) {
	checks := map[string]func(context.Context) error{"opensearch": func(context.Context) error { return errors.New("refused") }}
	code, body := get(t, New(&stubWorker{}, &stubSearcher{}, nil, Cutoffs{}, checks), "/health")
	if code != http.StatusServiceUnavailable || body["opensearch"] != "refused" {
		t.Fatalf("got %d %v", code, body)
	}
}

func ids(body map[string]any) (out []string) {
	for _, r := range body["results"].([]any) {
		out = append(out, r.(map[string]any)["object_key"].(string))
	}
	return
}

var three = []search.Result{
	{ObjectKey: "a", Snippet: "sa", Score: 0.03}, {ObjectKey: "b", Snippet: "sb", Score: 0.02}, {ObjectKey: "c", Snippet: "sc", Score: 0.01},
}

func TestResultsFollowTheRerankerAndDropBelowTheThreshold(t *testing.T) {
	w := &stubWorker{scores: []float32{-6, 4.5, -1.5}}
	code, body := get(t, New(w, &stubSearcher{results: three}, nil, Cutoffs{Rerank: -2}, nil), "/search?q=x")
	if code != 200 || !reflect.DeepEqual(ids(body), []string{"b", "c"}) {
		t.Fatalf("got %d %v", code, body)
	}
	if !reflect.DeepEqual(w.passages, []string{"sa", "sb", "sc"}) {
		t.Fatalf("reranked %v", w.passages)
	}
	first := body["results"].([]any)[0].(map[string]any)
	if first["score"] != 4.5 || body["total_hits"] != float64(2) {
		t.Fatalf("score must be the reranker's: %v", body)
	}
}

func TestRerankFailureKeepsTheFusedOrderAndIsNotCached(t *testing.T) {
	c := cache.New(miniredis.RunT(t).Addr(), time.Minute)
	w := &stubWorker{rerankErr: status.Error(codes.Unavailable, "down")}
	s := &stubSearcher{results: three}
	h := New(w, s, c, Cutoffs{}, nil)
	code, body := get(t, h, "/search?q=x")
	if code != 200 || !reflect.DeepEqual(ids(body), []string{"a", "b", "c"}) {
		t.Fatalf("got %d %v", code, body)
	}
	get(t, h, "/search?q=x")
	if s.calls != 2 {
		t.Fatal("a degraded response was cached")
	}
}

func TestNoCandidatesMeansNoRerankCall(t *testing.T) {
	w := &stubWorker{}
	code, body := get(t, New(w, &stubSearcher{results: []search.Result{}}, nil, Cutoffs{}, nil), "/search?q=x")
	if code != 200 || len(ids(body)) != 0 || w.rerankCalls != 0 {
		t.Fatalf("got %d %v, rerank calls %d", code, body, w.rerankCalls)
	}
}

func TestSecondIdenticalSearchIsServedFromCache(t *testing.T) {
	c := cache.New(miniredis.RunT(t).Addr(), time.Minute)
	w, s := &stubWorker{scores: []float32{1, 2, 3}}, &stubSearcher{results: three}
	h := New(w, s, c, Cutoffs{}, nil)
	_, first := get(t, h, "/search?q=Kubernetes+Report")
	_, second := get(t, h, "/search?q=kubernetes++report")
	if w.calls != 1 || s.calls != 1 || !reflect.DeepEqual(first, second) {
		t.Fatalf("worker %d, search %d, %v vs %v", w.calls, s.calls, first, second)
	}
	get(t, h, "/search?q=kubernetes+report&limit=3")
	if s.calls != 2 {
		t.Fatal("a different limit hit the cache")
	}
}

func TestEverythingBelowTheThresholdIsNotCached(t *testing.T) {
	c := cache.New(miniredis.RunT(t).Addr(), time.Minute)
	s := &stubSearcher{results: three}
	h := New(&stubWorker{scores: []float32{-9, -9, -9}}, s, c, Cutoffs{}, nil)
	get(t, h, "/search?q=x")
	get(t, h, "/search?q=x")
	if s.calls != 2 {
		t.Fatal("an empty result set was cached")
	}
}

func TestSearchWorksWhenRedisIsDown(t *testing.T) {
	code, body := get(t, New(&stubWorker{}, &stubSearcher{}, cache.New("127.0.0.1:1", time.Minute), Cutoffs{}, nil), "/search?q=x")
	if code != 200 || len(ids(body)) != 1 {
		t.Fatalf("got %d %v", code, body)
	}
}

func TestMoreCandidatesThanTheLimitAreRerankedAndTheBestAreKept(t *testing.T) {
	five := []search.Result{{ObjectKey: "a"}, {ObjectKey: "b"}, {ObjectKey: "c"}, {ObjectKey: "d"}, {ObjectKey: "e"}}
	s := &stubSearcher{results: five}
	w := &stubWorker{scores: []float32{1, 2, 3, 4, 9}}
	_, body := get(t, New(w, s, nil, Cutoffs{}, nil), "/search?q=x&limit=2")
	if s.got.Limit != 6 || len(w.passages) != 5 {
		t.Fatalf("asked for %d candidates, reranked %d", s.got.Limit, len(w.passages))
	}
	if got := ids(body); !reflect.DeepEqual(got, []string{"e", "d"}) {
		t.Fatalf("got %v", got)
	}
}

func TestImageHitsSkipTheRerankerAndUseTheirOwnCutoff(t *testing.T) {
	s := &stubSearcher{results: []search.Result{
		{ObjectKey: "doc.pdf", ContentType: "application/pdf", Snippet: "text"},
		{ObjectKey: "far.png", ContentType: "image/png", Score: 0.10},
		{ObjectKey: "near.png", ContentType: "image/png", Score: 0.30},
	}}
	w := &stubWorker{scores: []float32{5}}
	_, body := get(t, New(w, s, nil, Cutoffs{Rerank: -2, Image: 0.24}, nil), "/search?q=x")
	if !reflect.DeepEqual(w.passages, []string{"text"}) {
		t.Fatalf("reranker saw %v", w.passages)
	}
	if got := ids(body); !reflect.DeepEqual(got, []string{"doc.pdf", "near.png"}) {
		t.Fatalf("got %v", got)
	}
}

func TestTextAndImageHitsAreInterleavedTextFirst(t *testing.T) {
	s := &stubSearcher{results: []search.Result{
		{ObjectKey: "i2", ContentType: "image/jpeg", Score: 0.3},
		{ObjectKey: "t2", ContentType: "text/plain"},
		{ObjectKey: "i1", ContentType: "image/jpeg", Score: 0.4},
		{ObjectKey: "t1", ContentType: "text/plain"},
	}}
	w := &stubWorker{scores: []float32{3, 5}}
	_, body := get(t, New(w, s, nil, Cutoffs{}, nil), "/search?q=x&limit=3")
	if got := ids(body); !reflect.DeepEqual(got, []string{"t1", "i1", "t2"}) {
		t.Fatalf("got %v", got)
	}
}

func TestOnlyImageHitsMeansNoRerankCall(t *testing.T) {
	s := &stubSearcher{results: []search.Result{{ObjectKey: "i", ContentType: "image/png", Score: 0.5}}}
	w := &stubWorker{}
	_, body := get(t, New(w, s, nil, Cutoffs{}, nil), "/search?q=x")
	if w.rerankCalls != 0 || len(ids(body)) != 1 {
		t.Fatalf("rerank calls %d, results %v", w.rerankCalls, ids(body))
	}
}

func TestAFileIsScoredByItsBestPassageWhichBecomesTheSnippet(t *testing.T) {
	s := &stubSearcher{results: []search.Result{
		{ObjectKey: "a", Snippet: "a weak", Passages: []string{"a weak", "a strong"}},
		{ObjectKey: "b", Snippet: "b only", Passages: []string{"b only"}},
	}}
	w := &stubWorker{scores: []float32{-5, 4, 1}}
	_, body := get(t, New(w, s, nil, Cutoffs{Rerank: -2}, nil), "/search?q=x")
	if !reflect.DeepEqual(w.passages, []string{"a weak", "a strong", "b only"}) {
		t.Fatalf("reranker saw %v", w.passages)
	}
	results := body["results"].([]any)
	first := results[0].(map[string]any)
	if got := ids(body); !reflect.DeepEqual(got, []string{"a", "b"}) || first["snippet"] != "a strong" || first["score"] != 4.0 {
		t.Fatalf("got %v, first %v", got, first)
	}
}

func TestImagesLeadWhenNoTextHitIsClearlyRelevant(t *testing.T) {
	s := &stubSearcher{results: []search.Result{
		{ObjectKey: "t", ContentType: "text/plain"},
		{ObjectKey: "i", ContentType: "image/jpeg", Score: 0.3},
	}}
	_, body := get(t, New(&stubWorker{scores: []float32{-3}}, s, nil, Cutoffs{Rerank: -7}, nil), "/search?q=x")
	if got := ids(body); !reflect.DeepEqual(got, []string{"i", "t"}) {
		t.Fatalf("got %v", got)
	}
}

func TestRerankerGetsTheQueryWithoutFilterPhrases(t *testing.T) {
	w := &stubWorker{}
	get(t, New(w, &stubSearcher{}, nil, Cutoffs{}, nil), "/search?q=pdfs+larger+than+5MB+about+kubernetes")
	if w.rerankQuery != "about kubernetes" {
		t.Fatalf("reranked against %q", w.rerankQuery)
	}
}
