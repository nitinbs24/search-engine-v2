package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials/insecure"

	"search-engine-v2/backend/api"
	"search-engine-v2/backend/cache"
	pb "search-engine-v2/backend/proto"
	"search-engine-v2/backend/search"
)

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("%s is not set", key)
	}
	return v
}

// workerReady waits, within the caller's deadline, for the connection to be usable.
func workerReady(conn *grpc.ClientConn) func(context.Context) error {
	return func(ctx context.Context) error {
		conn.Connect() // a lazy connection stays idle until first use
		for s := conn.GetState(); s != connectivity.Ready; s = conn.GetState() {
			if !conn.WaitForStateChange(ctx, s) {
				return fmt.Errorf("connection %s", s)
			}
		}
		return nil
	}
}

// Cut-offs on absolute scores; override with RERANK_MIN_SCORE and IMAGE_MIN_SCORE.
// Rerank: the raw ms-marco cross-encoder logit (not a probability). Unrelated pairs fall to about
// -8 or lower, but relevant paraphrased questions often score well below 0 (eval: correct hits'
// median -0.8, 18 of 40 under -2), so the cut sits just above the unrelated cluster
// (see docs/model-evaluation.md, 2026-11-12).
// Image: cosine similarity between the query and the image. With CLIP a matching caption
// scores around 0.3 and an unrelated one around 0.17.
const (
	defaultMinRerankScore = -7.0
	defaultMinImageScore  = 0.24
)

// shortcut: nothing invalidates the cache, so results can be this stale after an upload or a
// delete; have the ingestion worker clear it if a longer lifetime is ever wanted.
const cacheTTL = 30 * time.Second

func envFloat(key string, fallback float64) float64 {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		log.Fatalf("%s: %v", key, err)
	}
	return f
}

func main() {
	conn, err := grpc.NewClient(mustEnv("SEARCH_WORKER_ADDR"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Close()

	opensearch := &search.Client{
		URL: mustEnv("OPENSEARCH_URL"), Index: mustEnv("OPENSEARCH_INDEX"),
		HTTP: &http.Client{Timeout: 30 * time.Second},
	}
	checks := map[string]func(context.Context) error{
		"opensearch":    opensearch.Ping,
		"search_worker": workerReady(conn),
	}

	log.Print("backend listening on :8080")
	cut := api.Cutoffs{
		Rerank: envFloat("RERANK_MIN_SCORE", defaultMinRerankScore),
		Image:  envFloat("IMAGE_MIN_SCORE", defaultMinImageScore),
	}
	store := cache.New(mustEnv("REDIS_ADDR"), cacheTTL)
	log.Fatal(http.ListenAndServe(":8080", api.New(pb.NewSearchWorkerClient(conn), opensearch, store, cut, checks)))
}
