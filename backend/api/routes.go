// Package api holds the HTTP routes.
package api

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "search-engine-v2/backend/proto"
	"search-engine-v2/backend/search"
)

type Searcher interface {
	Search(ctx context.Context, p search.Params) ([]search.Result, error)
}

// New returns the router. checks are the dependency pings behind GET /health.
func New(worker pb.SearchWorkerClient, searcher Searcher, checks map[string]func(context.Context) error) http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /search", func(w http.ResponseWriter, r *http.Request) {
		query := strings.TrimSpace(r.URL.Query().Get("q"))
		if query == "" {
			writeJSON(w, http.StatusOK, map[string]any{"results": []search.Result{}})
			return
		}
		limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
		if err != nil {
			limit = 10
		}
		limit = min(max(limit, 1), 50)

		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()

		parsed, err := worker.ParseQuery(ctx, &pb.ParseQueryRequest{Query: query})
		if err != nil {
			log.Printf("search worker: %v", err)
			code := http.StatusServiceUnavailable
			if status.Code(err) == codes.InvalidArgument {
				code = http.StatusBadRequest
			}
			writeJSON(w, code, map[string]string{"error": "could not process the query"})
			return
		}
		text := strings.Join(parsed.Keywords, " ")
		if text == "" {
			text = query
		}
		results, err := searcher.Search(ctx, search.Params{Text: text, Vector: parsed.TextVector, Filters: parsed.Filters, Limit: limit})
		if err != nil {
			log.Printf("opensearch: %v", err)
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": "search failed"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"results": results, "total_hits": len(results), "extracted_keywords": parsed.Keywords,
		})
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

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
