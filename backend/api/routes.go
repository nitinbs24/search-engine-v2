// Package api holds the HTTP routes.
package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"search-engine-v2/backend/cache"
	pb "search-engine-v2/backend/proto"
	"search-engine-v2/backend/search"
)

type Searcher interface {
	Search(ctx context.Context, p search.Params) ([]search.Result, error)
}

// Cutoffs are absolute scores below which a hit is dropped: the raw cross-encoder output
// for text, the raw cosine similarity to the query for images.
type Cutoffs struct{ Rerank, Image float64 }

// candidates is how many distinct files are fetched for a request. Reranking only the top
// `limit` could never promote a good document from just below the cut.
// highlightTop is how many of the top-scoring passages the model server also splits into sentences
// and scores (one extra cross-encoder batch of roughly 5 passages x a handful of sentences, against
// the ~150 passages of the main call). The top result's winning passage is always among them.
const highlightTop = 5

// answerSources is how many of the top text results the answer is written from.
const answerSources = 3

func parseLimit(r *http.Request) int {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil {
		limit = 10
	}
	return min(max(limit, 1), 50)
}

func candidates(limit int) int { return max(limit, min(limit*3, 50)) }

// New returns the router. store may be nil. checks are the dependency pings behind GET /health.
func New(worker pb.SearchWorkerClient, searcher Searcher, store *cache.Client, cut Cutoffs, checks map[string]func(context.Context) error) http.Handler {
	mux := http.NewServeMux()

	// retrieve is the whole search pipeline; code is 0 on success, else the HTTP status to return.
	retrieve := func(ctx context.Context, query string, limit int) (entry cache.Entry, code int, msg string) {
		if hit, ok := store.Get(ctx, query, limit); ok {
			return hit, 0, ""
		}
		parsed, err := worker.ParseQuery(ctx, &pb.ParseQueryRequest{Query: query})
		if err != nil {
			log.Printf("search worker: %v", err)
			code := http.StatusServiceUnavailable
			if status.Code(err) == codes.InvalidArgument {
				code = http.StatusBadRequest
			}
			return entry, code, "could not process the query"
		}
		text := strings.Join(parsed.Keywords, " ")
		if text == "" {
			text = query
		}
		results, err := searcher.Search(ctx, search.Params{Text: text, Vector: parsed.TextVector, ImageVector: parsed.ImageVector, Filters: parsed.Filters, Limit: candidates(limit)})
		if err != nil {
			log.Printf("opensearch: %v", err)
			return entry, http.StatusBadGateway, "search failed"
		}
		// Filter words ("pdf", "larger than 5MB") are not part of what the user is looking for.
		results, reranked := rank(ctx, worker, parsed.CleanQuery, results, cut, limit)
		entry = cache.Entry{Results: results, Keywords: parsed.Keywords}
		if reranked { // a fused-order fallback is a degraded answer: do not keep it
			store.Set(ctx, query, limit, entry)
		}
		return entry, 0, ""
	}

	mux.HandleFunc("GET /search", func(w http.ResponseWriter, r *http.Request) {
		query := strings.TrimSpace(r.URL.Query().Get("q"))
		if query == "" {
			writeJSON(w, http.StatusOK, map[string]any{"results": []search.Result{}})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()
		entry, code, msg := retrieve(ctx, query, parseLimit(r))
		if code != 0 {
			writeJSON(w, code, map[string]string{"error": msg})
			return
		}
		writeJSON(w, http.StatusOK, response(entry.Results, entry.Keywords))
	})

	// The UI asks for this after /search has rendered, with the same q and limit, so retrieval is
	// normally a cache hit; generation is slow and must never share a request with search.
	mux.HandleFunc("GET /answer", func(w http.ResponseWriter, r *http.Request) {
		query := strings.TrimSpace(r.URL.Query().Get("q"))
		if query == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "q is required"})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Minute)
		defer cancel()
		entry, code, msg := retrieve(ctx, query, parseLimit(r))
		if code != 0 {
			writeJSON(w, code, map[string]string{"error": msg})
			return
		}
		var sources []*pb.Source
		var names []string
		for _, res := range entry.Results {
			if len(sources) < answerSources && !strings.HasPrefix(res.ContentType, "image/") && res.Snippet != "" {
				sources = append(sources, &pb.Source{Filename: res.ObjectName, Passage: res.Snippet})
				names = append(names, res.ObjectName)
			}
		}
		if len(sources) == 0 {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "no text results to answer from"})
			return
		}
		resp, err := worker.Answer(ctx, &pb.AnswerRequest{Query: query, Sources: sources})
		if status.Code(err) == codes.Unavailable { // the optional model is off: not an error
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"available": false})
			return
		}
		if err != nil {
			log.Printf("answer: %v", err)
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "answer failed"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"answer": resp.Answer, "sources": names})
	})

	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		body, code := map[string]string{}, http.StatusOK
		for name, check := range checks {
			body[name] = "ok"
			if err := check(ctx); err != nil {
				body[name], code = err.Error(), http.StatusServiceUnavailable
			}
		}
		writeJSON(w, code, body)
	})

	return mux
}

func response(results []search.Result, keywords []string) map[string]any {
	return map[string]any{"results": results, "total_hits": len(results), "extracted_keywords": keywords}
}

// rank reranks the text hits, keeps the image hits that are similar enough to the query (the text
// reranker cannot judge an image), and interleaves the two lists. reranked is false
// when the reranker failed and the text hits are still in fused order.
func rank(ctx context.Context, worker pb.SearchWorkerClient, query string, results []search.Result, cut Cutoffs, limit int) ([]search.Result, bool) {
	var texts, images []search.Result
	for _, r := range results {
		if !strings.HasPrefix(r.ContentType, "image/") {
			texts = append(texts, r)
		} else if r.Score >= cut.Image {
			images = append(images, r)
		}
	}
	sort.SliceStable(images, func(i, j int) bool { return images[i].Score > images[j].Score })
	texts, reranked := rerank(ctx, worker, query, texts, cut.Rerank)

	// The two score scales cannot be compared, so the lists are interleaved. Text leads unless
	// its best hit is not clearly relevant (a reranker score under 0) and an image matched.
	first, second := texts, images
	if len(images) > 0 && (len(texts) == 0 || texts[0].Score < 0) {
		first, second = images, texts
	}
	out := []search.Result{}
	for i := 0; len(out) < limit && (i < len(first) || i < len(second)); i++ {
		if i < len(first) {
			out = append(out, first[i])
		}
		if i < len(second) && len(out) < limit {
			out = append(out, second[i])
		}
	}
	return out, reranked
}

// rerank scores every passage of every result with the cross-encoder. A result takes the score
// of its best passage, which also becomes its snippet (with that passage's highlight); results under minScore are dropped and the
// rest ordered by score. On any failure it logs and returns the fused order (reranked=false).
func rerank(ctx context.Context, worker pb.SearchWorkerClient, query string, results []search.Result, minScore float64) ([]search.Result, bool) {
	if len(results) == 0 {
		return results, true
	}
	var passages []string
	var owner []int // owner[i] is the result passages[i] belongs to
	for i, r := range results {
		own := r.Passages
		if len(own) == 0 {
			own = []string{r.Snippet}
		}
		for _, p := range own {
			passages, owner = append(passages, p), append(owner, i)
		}
	}
	resp, err := worker.Rerank(ctx, &pb.RerankRequest{Query: query, Passages: passages, HighlightTop: highlightTop})
	if err == nil && len(resp.Scores) != len(passages) {
		err = fmt.Errorf("got %d scores for %d passages", len(resp.Scores), len(passages))
	}
	if err != nil {
		log.Printf("rerank: %v", err)
		return results, false
	}
	highlights := resp.Highlights
	if len(highlights) != len(passages) { // an old worker: no highlights, not a failure
		highlights = make([]string, len(passages))
	}
	scored := make([]bool, len(results))
	for i, score := range resp.Scores {
		r := &results[owner[i]]
		if s := float64(score); !scored[owner[i]] || s > r.Score {
			r.Score, r.Snippet, r.Highlight, scored[owner[i]] = s, passages[i], highlights[i], true
		}
	}
	kept := make([]search.Result, 0, len(results))
	for _, r := range results {
		if r.Score >= minScore {
			kept = append(kept, r)
		}
	}
	sort.SliceStable(kept, func(i, j int) bool { return kept[i].Score > kept[j].Score })
	return kept, true
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
