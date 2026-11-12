package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials/insecure"

	"search-engine-v2/backend/api"
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
		"opensearch": opensearch.Ping,
		"search_worker": func(context.Context) error {
			conn.Connect() // a lazy connection stays idle until first use
			if s := conn.GetState(); s == connectivity.TransientFailure || s == connectivity.Shutdown {
				return fmt.Errorf("connection %s", s)
			}
			return nil
		},
	}

	log.Print("backend listening on :8080")
	log.Fatal(http.ListenAndServe(":8080", api.New(pb.NewSearchWorkerClient(conn), opensearch, checks)))
}
