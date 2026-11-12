// Package search builds and runs the hybrid OpenSearch query.
package search

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	pb "search-engine-v2/backend/proto"
)

// Chunks fetched per requested result: several chunks of one file collapse into one result.
const poolFactor = 5

type Params struct {
	Text    string // BM25 leg
	Vector  []float32
	Filters *pb.Filters
	Limit   int
}

type Result struct {
	ID           string  `json:"id"`
	Bucket       string  `json:"bucket"`
	ObjectKey    string  `json:"object_key"`
	ObjectName   string  `json:"object_name"`
	Extension    string  `json:"extension"`
	ContentType  string  `json:"content_type"`
	SizeBytes    int64   `json:"size_bytes"`
	LastModified string  `json:"last_modified"`
	Snippet      string  `json:"snippet"`
	Score        float64 `json:"score"`
}

func filterClauses(f *pb.Filters) []any {
	var clauses []any
	var kinds []any
	if len(f.GetExtensions()) > 0 {
		kinds = append(kinds, map[string]any{"terms": map[string]any{"extension": f.Extensions}})
	}
	for _, p := range f.GetMimePrefixes() {
		kinds = append(kinds, map[string]any{"prefix": map[string]any{"mime_type": p}})
	}
	if len(kinds) > 0 {
		clauses = append(clauses, map[string]any{"bool": map[string]any{"should": kinds, "minimum_should_match": 1}})
	}
	size := map[string]any{}
	if f.GetMinSizeBytes() > 0 {
		size["gt"] = f.MinSizeBytes
	}
	if f.GetMaxSizeBytes() > 0 {
		size["lt"] = f.MaxSizeBytes
	}
	if len(size) > 0 {
		clauses = append(clauses, map[string]any{"range": map[string]any{"size_bytes": size}})
	}
	uploaded := map[string]any{}
	if f.GetUploadedFrom() != "" {
		uploaded["gte"] = f.UploadedFrom
	}
	if f.GetUploadedTo() != "" {
		uploaded["lt"] = f.UploadedTo
	}
	if len(uploaded) > 0 {
		clauses = append(clauses, map[string]any{"range": map[string]any{"uploaded_at": uploaded}})
	}
	return clauses
}

// BuildQuery returns the hybrid query: a BM25 leg and a kNN leg on text_embedding.
// The filter is applied inside every leg; a filter on only one leg lets the other leak results.
func BuildQuery(p Params) map[string]any {
	filters := filterClauses(p.Filters)
	pool := p.Limit * poolFactor

	bm25 := map[string]any{"must": []any{map[string]any{"multi_match": map[string]any{
		"query": p.Text, "fields": []string{"filename^3", "title^2", "chunk_text"},
	}}}}
	knn := map[string]any{"vector": p.Vector, "k": pool}
	if len(filters) > 0 {
		bm25["filter"] = filters
		knn["filter"] = map[string]any{"bool": map[string]any{"filter": filters}}
	}
	return map[string]any{
		"size":    pool,
		"_source": map[string]any{"excludes": []string{"text_embedding"}},
		"query": map[string]any{"hybrid": map[string]any{"queries": []any{
			map[string]any{"bool": bm25},
			map[string]any{"knn": map[string]any{"text_embedding": knn}},
		}}},
	}
}

type Client struct {
	URL   string
	Index string
	HTTP  *http.Client
}

// Search runs the query and returns at most p.Limit results, one per object, best chunk first.
// (collapse is ignored on hybrid queries, so the deduplication is done here.)
func (c *Client) Search(ctx context.Context, p Params) ([]Result, error) {
	body, err := json.Marshal(BuildQuery(p))
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.URL+"/"+c.Index+"/_search", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return nil, fmt.Errorf("opensearch %d: %s", resp.StatusCode, msg)
	}

	var parsed struct {
		Hits struct {
			Hits []struct {
				ID     string  `json:"_id"`
				Score  float64 `json:"_score"`
				Source struct {
					Bucket     string `json:"bucket"`
					ObjectKey  string `json:"object_key"`
					Filename   string `json:"filename"`
					Extension  string `json:"extension"`
					MimeType   string `json:"mime_type"`
					SizeBytes  int64  `json:"size_bytes"`
					UploadedAt string `json:"uploaded_at"`
					ChunkText  string `json:"chunk_text"`
				} `json:"_source"`
			} `json:"hits"`
		} `json:"hits"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, err
	}

	results := []Result{}
	seen := map[string]bool{}
	for _, h := range parsed.Hits.Hits {
		s := h.Source
		object := s.Bucket + "/" + s.ObjectKey
		if seen[object] {
			continue
		}
		seen[object] = true
		results = append(results, Result{
			ID: h.ID, Bucket: s.Bucket, ObjectKey: s.ObjectKey, ObjectName: s.Filename, Extension: s.Extension,
			ContentType: s.MimeType, SizeBytes: s.SizeBytes, LastModified: s.UploadedAt,
			Snippet: s.ChunkText, Score: h.Score,
		})
		if len(results) == p.Limit {
			break
		}
	}
	return results, nil
}

// Ping is the cheap health check.
func (c *Client) Ping(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.URL+"/", nil)
	if err != nil {
		return err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("opensearch %d", resp.StatusCode)
	}
	return nil
}
