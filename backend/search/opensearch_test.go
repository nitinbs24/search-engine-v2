package search

import (
	"context"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"testing"

	pb "search-engine-v2/backend/proto"
)

var allFilters = &pb.Filters{
	Extensions:   []string{"pdf", "ppt"},
	MimePrefixes: []string{"image/"},
	MinSizeBytes: 5 << 20,
	MaxSizeBytes: 1 << 30,
	UploadedFrom: "2026-03-04T00:00:00+05:30",
	UploadedTo:   "2026-03-05T00:00:00+05:30",
}

// asJSON round-trips through JSON so builder output and golden file compare structurally.
func asJSON(t *testing.T, v any) any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func legs(t *testing.T, q any) []any {
	t.Helper()
	return q.(map[string]any)["query"].(map[string]any)["hybrid"].(map[string]any)["queries"].([]any)
}

func TestBuildQueryMatchesGoldenFile(t *testing.T) {
	got := asJSON(t, BuildQuery(Params{Text: "kubernetes report", Vector: []float32{0.25, 0.75}, ImageVector: []float32{0.5, 0.5, 0}, Filters: allFilters, Limit: 10}))
	raw, err := os.ReadFile("testdata/hybrid_query.json")
	if err != nil {
		t.Fatal(err)
	}
	var want any
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		out, _ := json.MarshalIndent(got, "", "  ")
		t.Fatalf("query differs from testdata/hybrid_query.json; got:\n%s", out)
	}
}

func TestEveryLegCarriesTheSameFilter(t *testing.T) {
	q := asJSON(t, BuildQuery(Params{Text: "x", Vector: []float32{1}, ImageVector: []float32{1}, Filters: allFilters, Limit: 10}))
	l := legs(t, q)
	if len(l) != 3 {
		t.Fatalf("want 3 legs, got %d", len(l))
	}
	bm25 := l[0].(map[string]any)["bool"].(map[string]any)["filter"]
	knn := l[1].(map[string]any)["knn"].(map[string]any)["text_embedding"].(map[string]any)["filter"].(map[string]any)["bool"].(map[string]any)["filter"]
	image := l[2].(map[string]any)["knn"].(map[string]any)["image_embedding"].(map[string]any)["filter"].(map[string]any)["bool"].(map[string]any)["filter"]
	if bm25 == nil || !reflect.DeepEqual(bm25, knn) || !reflect.DeepEqual(bm25, image) {
		t.Fatalf("filters differ between legs:\nbm25:  %v\nknn:   %v\nimage: %v", bm25, knn, image)
	}
}

func TestEmptyImageVectorOmitsTheImageLeg(t *testing.T) {
	q := asJSON(t, BuildQuery(Params{Text: "x", Vector: []float32{1}, Filters: allFilters, Limit: 10}))
	if n := len(legs(t, q)); n != 2 {
		t.Fatalf("want 2 legs, got %d", n)
	}
}

func TestNoFiltersMeansNoFilterClauses(t *testing.T) {
	for _, f := range []*pb.Filters{nil, {}} {
		l := legs(t, asJSON(t, BuildQuery(Params{Text: "x", Vector: []float32{1}, ImageVector: []float32{1}, Filters: f, Limit: 10})))
		if _, ok := l[2].(map[string]any)["knn"].(map[string]any)["image_embedding"].(map[string]any)["filter"]; ok {
			t.Fatal("image leg has a filter")
		}
		if _, ok := l[0].(map[string]any)["bool"].(map[string]any)["filter"]; ok {
			t.Fatal("bm25 leg has a filter")
		}
		if _, ok := l[1].(map[string]any)["knn"].(map[string]any)["text_embedding"].(map[string]any)["filter"]; ok {
			t.Fatal("knn leg has a filter")
		}
	}
}

const cannedResponse = `{"hits":{"hits":[
 {"_id":"uploads/a.pdf#3","_score":0.03,"_source":{"bucket":"uploads","object_key":"a.pdf","filename":"a.pdf","extension":"pdf","mime_type":"application/pdf","size_bytes":10,"uploaded_at":"2026-01-01T00:00:00Z","chunk_text":"third chunk of a"}},
 {"_id":"uploads/a.pdf#1","_score":0.02,"_source":{"bucket":"uploads","object_key":"a.pdf","filename":"a.pdf","chunk_text":"first chunk of a"}},
 {"_id":"uploads/dir/b.txt#0","_score":0.01,"_source":{"bucket":"uploads","object_key":"dir/b.txt","filename":"b.txt","chunk_text":"b"}},
 {"_id":"uploads/c.txt#0","_score":0.005,"_source":{"bucket":"uploads","object_key":"c.txt","filename":"c.txt","chunk_text":"c"}}
]}}`

func TestSearchReturnsOneResultPerObjectBestChunkFirst(t *testing.T) {
	var path string
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		_, _ = w.Write([]byte(cannedResponse))
	}))
	defer srv.Close()

	c := &Client{URL: srv.URL, Index: "docs", HTTP: srv.Client()}
	got, err := c.Search(context.Background(), Params{Text: "x", Vector: []float32{1}, Limit: 2})
	if err != nil {
		t.Fatal(err)
	}
	if path != "/docs/_search" || body["query"] == nil {
		t.Fatalf("unexpected request: %s %v", path, body)
	}
	if len(got) != 2 || got[0].ObjectKey != "a.pdf" || got[1].ObjectKey != "dir/b.txt" {
		t.Fatalf("want a.pdf then dir/b.txt, got %+v", got)
	}
	if got[0].Snippet != "third chunk of a" || got[0].ObjectName != "a.pdf" || got[0].ContentType != "application/pdf" {
		t.Fatalf("first result not built from the best chunk: %+v", got[0])
	}
}

func TestSearchReportsOpenSearchErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"error":"boom"}`, http.StatusBadRequest)
	}))
	defer srv.Close()
	c := &Client{URL: srv.URL, Index: "docs", HTTP: srv.Client()}
	if _, err := c.Search(context.Background(), Params{Text: "x", Vector: []float32{1}, Limit: 2}); err == nil {
		t.Fatal("expected an error")
	}
}

func hitJSON(id, key string) string {
	return `{"_id":"` + id + `","_score":0.01,"_source":{"bucket":"uploads","object_key":"` + key + `","chunk_text":"t"}}`
}

// One file's chunks fill the first pool; the second file only appears when the pool grows.
func TestSearchWidensThePoolUntilThereAreEnoughDistinctFiles(t *testing.T) {
	var sizes []int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct{ Size int }
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		sizes = append(sizes, body.Size)
		hits := ""
		for i := 0; i < body.Size-1; i++ {
			hits += hitJSON("a#"+string(rune('a'+i%26)), "a.pdf") + ","
		}
		last := "a.pdf"
		if body.Size > 2*poolFactor {
			last = "b.pdf"
		}
		_, _ = w.Write([]byte(`{"hits":{"hits":[` + hits + hitJSON("last", last) + `]}}`))
	}))
	defer srv.Close()

	c := &Client{URL: srv.URL, Index: "docs", HTTP: srv.Client()}
	got, err := c.Search(context.Background(), Params{Text: "x", Vector: []float32{1}, Limit: 2})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[1].ObjectKey != "b.pdf" || len(sizes) != 2 || sizes[1] <= sizes[0] {
		t.Fatalf("got %+v with request sizes %v", got, sizes)
	}
}

func TestSearchStopsWideningWhenTheIndexIsExhausted(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		_, _ = w.Write([]byte(`{"hits":{"hits":[` + hitJSON("a#0", "a.pdf") + `]}}`))
	}))
	defer srv.Close()
	c := &Client{URL: srv.URL, Index: "docs", HTTP: srv.Client()}
	got, err := c.Search(context.Background(), Params{Text: "x", Vector: []float32{1}, Limit: 5})
	if err != nil || len(got) != 1 || calls != 1 {
		t.Fatalf("got %v %v after %d calls", got, err, calls)
	}
}

func TestSameKeyInDifferentBucketsAreDifferentObjects(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"hits":{"hits":[` +
			`{"_id":"1","_source":{"bucket":"uploads","object_key":"a.txt","chunk_text":"x"}},` +
			`{"_id":"2","_source":{"bucket":"archive","object_key":"a.txt","chunk_text":"y"}}]}}`))
	}))
	defer srv.Close()
	c := &Client{URL: srv.URL, Index: "docs", HTTP: srv.Client()}
	got, err := c.Search(context.Background(), Params{Text: "x", Vector: []float32{1}, Limit: 5})
	if err != nil || len(got) != 2 {
		t.Fatalf("got %+v %v", got, err)
	}
}

func TestImageHitsAreScoredByRawCosineSimilarityToTheQuery(t *testing.T) {
	const body = `{"hits":{"hits":[
 {"_id":"uploads/cat.png#0","_score":0.03,"_source":{"bucket":"uploads","object_key":"cat.png","mime_type":"image/png","image_embedding":[0.6,0.8]}},
 {"_id":"uploads/a.txt#0","_score":0.02,"_source":{"bucket":"uploads","object_key":"a.txt","mime_type":"text/plain","chunk_text":"a"}}
]}}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(body)) }))
	defer srv.Close()
	c := &Client{URL: srv.URL, Index: "docs", HTTP: srv.Client()}
	got, err := c.Search(context.Background(), Params{Text: "x", Vector: []float32{1}, ImageVector: []float32{0.8, 0.6}, Limit: 5})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || math.Abs(got[0].Score-0.96) > 1e-6 || got[1].Score != 0.02 {
		t.Fatalf("want cosine 0.96 for the image and the fused score for the text, got %+v", got)
	}
}

func TestSeveralChunksPerFileAreKeptAsPassagesForReranking(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(cannedResponse)) }))
	defer srv.Close()
	c := &Client{URL: srv.URL, Index: "docs", HTTP: srv.Client()}
	got, err := c.Search(context.Background(), Params{Text: "x", Vector: []float32{1}, Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	// a.pdf has two chunks in the response; the limit of one file must not cut its second chunk off.
	if len(got) != 1 || !reflect.DeepEqual(got[0].Passages, []string{"third chunk of a", "first chunk of a"}) {
		t.Fatalf("got %+v", got)
	}
}
