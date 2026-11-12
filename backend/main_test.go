package main

import (
	"context"
	"net"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func dial(t *testing.T, addr string) *grpc.ClientConn {
	t.Helper()
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

func TestWorkerReadyFailsWhenNothingIsListening(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	if err := workerReady(dial(t, "127.0.0.1:1"))(ctx); err == nil {
		t.Fatal("an unreachable worker was reported ready")
	}
}

func TestWorkerReadySucceedsAgainstARunningServer(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := grpc.NewServer()
	go srv.Serve(lis)
	defer srv.Stop()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := workerReady(dial(t, lis.Addr().String()))(ctx); err != nil {
		t.Fatalf("a running worker was reported down: %v", err)
	}
}
