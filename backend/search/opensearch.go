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
// When one file's chunks still crowd out the rest, the pool is doubled up to maxPool
// (OpenSearch's default index.max_result_window).
const (
	poolFactor = 5
	maxPool    = 10000
	// How many chunks of one file the reranker gets to judge.
	passagesPerFile = 3
)

type Params struct {
	Text   string    // BM25 leg
	Vector []float32 // text_embedding leg
	// ImageVector is the CLIP text-encoder embedding of the query; empty omits the image leg.
	ImageVector []float32
	Filters     *pb.Filters
	Limit       int
}

type Result struct {
	ID           string `json:"id"`
	Bucket       string `json:"bucket"`
	ObjectKey    string `json:"object_key"`
	ObjectName   string `json:"object_name"`
	Extension    string `json:"extension"`
	ContentType  string `json:"content_type"`
	SizeBytes    int64  `json:"size_bytes"`
	LastModified string `json:"last_modified"`
	Snippet      string `json:"snippet"`
	// Highlight is the sentence of Snippet that best matches the query; empty when none was picked.
	Highlight string  `json:"highlight,omitempty"`
	Score     float64 `json:"score"`
	// Passages are the file's best chunks by fused rank (the first is Snippet), for the reranker.
	Passages []string `json:"-"`
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

// BuildQuery returns the hybrid query: a BM25 leg and kNN legs on text_embedding and image_embedding.
// The filter is applied inside every leg; a filter on only one leg lets the others leak results.
func BuildQuery(p Params) map[string]any { return buildQuery(p, p.Limit*poolFactor) }

func buildQuery(p Params, pool int) map[string]any {
	filters := filterClauses(p.Filters)

	bm25 := map[string]any{"must": []any{map[string]any{"multi_match": map[string]any{
		"query": p.Text, "fields": []string{"filename^3", "title^2", "chunk_text"},
	}}}}
	if len(filters) > 0 {
		bm25["filter"] = filters
	}
	knn := func(field string, vector []float32) map[string]any {
		leg := map[string]any{"vector": vector, "k": pool}
		if len(filters) > 0 {
			leg["filter"] = map[string]any{"bool": map[string]any{"filter": filters}}
		}
		return map[string]any{"knn": map[string]any{field: leg}}
	}
	legs := []any{map[string]any{"bool": bm25}, knn("text_embedding", p.Vector)}
	if len(p.ImageVector) > 0 {
		legs = append(legs, knn("image_embedding", p.ImageVector))
	}
	return map[string]any{
		"size": pool,
		// image_embedding is kept: it is small, only images have it, and it gives their raw similarity.
		"_source": map[string]any{"excludes": []string{"text_embedding"}},
		"query":   map[string]any{"hybrid": map[string]any{"queries": legs}},
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
	for pool := p.Limit * poolFactor; ; pool *= 2 {
		pool = min(pool, maxPool)
		hits, err := c.fetch(ctx, buildQuery(p, pool))
		if err != nil {
			return nil, err
		}
		results := dedupe(hits, p)
		// Fewer hits than asked for means the index has nothing more to give.
		if len(results) == p.Limit || len(hits) < pool || pool == maxPool {
			return results, nil
		}
	}
}

type hit struct {
	ID     string  `json:"_id"`
	Score  float64 `json:"_score"`
	Source struct {
		Bucket         string    `json:"bucket"`
		ObjectKey      string    `json:"object_key"`
		Filename       string    `json:"filename"`
		Extension      string    `json:"extension"`
		MimeType       string    `json:"mime_type"`
		SizeBytes      int64     `json:"size_bytes"`
		UploadedAt     string    `json:"uploaded_at"`
		ChunkText      string    `json:"chunk_text"`
		ImageEmbedding []float32 `json:"image_embedding"`
	} `json:"_source"`
}

func (c *Client) fetch(ctx context.Context, query map[string]any) ([]hit, error) {
	body, err := json.Marshal(query)
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
			Hits []hit `json:"hits"`
		} `json:"hits"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, err
	}
	return parsed.Hits.Hits, nil
}

// dedupe keeps the best hit per object. An image's score becomes its raw cosine similarity to
// the query (both vectors are unit length), because the fused score says nothing absolute.
func dedupe(hits []hit, p Params) []Result {
	results := []Result{}
	seen := map[string]int{} // object -> index in results
	for _, h := range hits {
		s := h.Source
		object := s.Bucket + "/" + s.ObjectKey
		if i, ok := seen[object]; ok {
			// The chunk that fused best is not always the passage that answers the query.
			if len(results[i].Passages) < passagesPerFile {
				results[i].Passages = append(results[i].Passages, s.ChunkText)
			}
			continue
		}
		if len(results) == p.Limit {
			continue // keep collecting passages for the files already chosen
		}
		seen[object] = len(results)
		results = append(results, Result{
			ID: h.ID, Bucket: s.Bucket, ObjectKey: s.ObjectKey, ObjectName: s.Filename, Extension: s.Extension,
			ContentType: s.MimeType, SizeBytes: s.SizeBytes, LastModified: s.UploadedAt,
			Snippet: s.ChunkText, Score: h.Score, Passages: []string{s.ChunkText},
		})
		if len(s.ImageEmbedding) > 0 && len(s.ImageEmbedding) == len(p.ImageVector) {
			var dot float64
			for i, v := range s.ImageEmbedding {
				dot += float64(v) * float64(p.ImageVector[i])
			}
			results[len(results)-1].Score = dot
		}
	}
	return results
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
