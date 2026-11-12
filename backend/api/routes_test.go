package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "search-engine-v2/backend/proto"
	"search-engine-v2/backend/search"
)

type stubWorker struct {
	calls int
	err   error
}

func (w *stubWorker) ParseQuery(ctx context.Context, in *pb.ParseQueryRequest, _ ...grpc.CallOption) (*pb.ParseQueryResponse, error) {
	w.calls++
	if w.err != nil {
		return nil, w.err
	}
	return &pb.ParseQueryResponse{Keywords: []string{"kubernetes"}, TextVector: []float32{1}, Filters: &pb.Filters{Extensions: []string{"pdf"}}}, nil
}

type stubSearcher struct {
	got   search.Params
	calls int
}

func (s *stubSearcher) Search(ctx context.Context, p search.Params) ([]search.Result, error) {
	s.calls++
	s.got = p
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
	code, body := get(t, New(&stubWorker{}, s, nil), "/search?q=pdfs+about+kubernetes")
	if code != 200 || len(body["results"].([]any)) != 1 {
		t.Fatalf("got %d %v", code, body)
	}
	if s.got.Text != "kubernetes" || s.got.Filters.Extensions[0] != "pdf" || len(s.got.Vector) != 1 || s.got.Limit != 10 {
		t.Fatalf("unexpected params: %+v", s.got)
	}
}

func TestLimitIsClampedBetween1And50(t *testing.T) {
	for in, want := range map[string]int{"0": 1, "-3": 1, "7": 7, "999": 50, "abc": 10, "": 10} {
		s := &stubSearcher{}
		get(t, New(&stubWorker{}, s, nil), "/search?q=x&limit="+in)
		if s.got.Limit != want {
			t.Errorf("limit=%q: got %d, want %d", in, s.got.Limit, want)
		}
	}
}

func TestBlankQueryReturnsNoResultsWithoutCallingTheWorker(t *testing.T) {
	w := &stubWorker{}
	code, body := get(t, New(w, &stubSearcher{}, nil), "/search?q=+++")
	if code != 200 || len(body["results"].([]any)) != 0 || w.calls != 0 {
		t.Fatalf("got %d %v, worker calls %d", code, body, w.calls)
	}
}

func TestWorkerFailureIs503(t *testing.T) {
	code, _ := get(t, New(&stubWorker{err: status.Error(codes.Unavailable, "down")}, &stubSearcher{}, nil), "/search?q=x")
	if code != http.StatusServiceUnavailable {
		t.Fatalf("got %d", code)
	}
}

func TestHealthOnlyRunsTheDependencyChecks(t *testing.T) {
	w, s := &stubWorker{}, &stubSearcher{}
	checks := map[string]func(context.Context) error{"opensearch": func(context.Context) error { return nil }}
	if code, _ := get(t, New(w, s, checks), "/health"); code != 200 {
		t.Fatalf("got %d", code)
	}
	if w.calls != 0 || s.calls != 0 {
		t.Fatal("health ran a search")
	}
}

func TestHealthIs503WhenADependencyIsDown(t *testing.T) {
	checks := map[string]func(context.Context) error{"opensearch": func(context.Context) error { return errors.New("refused") }}
	code, body := get(t, New(&stubWorker{}, &stubSearcher{}, checks), "/health")
	if code != http.StatusServiceUnavailable || body["opensearch"] != "refused" {
		t.Fatalf("got %d %v", code, body)
	}
}
