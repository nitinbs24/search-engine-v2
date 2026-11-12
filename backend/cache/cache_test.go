package cache

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"

	"search-engine-v2/backend/search"
)

var entry = Entry{Results: []search.Result{{ObjectKey: "a.pdf", Score: 2.5}}, Keywords: []string{"kubernetes"}}

func newCache(t *testing.T) *Client {
	t.Helper()
	return New(miniredis.RunT(t).Addr(), time.Minute)
}

func TestHitAfterSet(t *testing.T) {
	c := newCache(t)
	ctx := context.Background()
	if _, ok := c.Get(ctx, "kubernetes", 10); ok {
		t.Fatal("hit before set")
	}
	c.Set(ctx, "kubernetes", 10, entry)
	got, ok := c.Get(ctx, "kubernetes", 10)
	if !ok || got.Results[0].ObjectKey != "a.pdf" || got.Results[0].Score != 2.5 || got.Keywords[0] != "kubernetes" {
		t.Fatalf("got %+v %v", got, ok)
	}
}

func TestDifferentLimitsDoNotShareAnEntry(t *testing.T) {
	c := newCache(t)
	c.Set(context.Background(), "kubernetes", 3, entry)
	if _, ok := c.Get(context.Background(), "kubernetes", 20); ok {
		t.Fatal("limit 20 served limit 3's entry")
	}
}

func TestSpacingAndCaseDoNotChangeTheKey(t *testing.T) {
	c := newCache(t)
	c.Set(context.Background(), "Kubernetes   Report", 10, entry)
	if _, ok := c.Get(context.Background(), "  kubernetes report\t", 10); !ok {
		t.Fatal("normalised query missed")
	}
}

func TestEmptyResultsAreNotStored(t *testing.T) {
	c := newCache(t)
	c.Set(context.Background(), "nothing", 10, Entry{Results: []search.Result{}})
	if _, ok := c.Get(context.Background(), "nothing", 10); ok {
		t.Fatal("empty result set was cached")
	}
}

func TestDeadRedisNeitherFailsNorBlocks(t *testing.T) {
	c := New("127.0.0.1:1", time.Minute)
	start := time.Now()
	c.Set(context.Background(), "x", 10, entry)
	if _, ok := c.Get(context.Background(), "x", 10); ok {
		t.Fatal("hit from a dead redis")
	}
	if d := time.Since(start); d > time.Second {
		t.Fatalf("took %v", d)
	}
}

func TestNilCacheIsANoOp(t *testing.T) {
	var c *Client
	c.Set(context.Background(), "x", 10, entry)
	if _, ok := c.Get(context.Background(), "x", 10); ok {
		t.Fatal("hit from nil cache")
	}
}
