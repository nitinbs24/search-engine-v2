// Package cache keeps search responses in Redis. Redis is an optimisation only:
// every failure is logged and treated as a miss, never as a search error.
package cache

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"search-engine-v2/backend/search"
)

// opTimeout bounds each Redis call so a dead Redis costs a search at most this much.
const opTimeout = 200 * time.Millisecond

// Entry is what a search response is rebuilt from.
type Entry struct {
	Results  []search.Result `json:"results"`
	Keywords []string        `json:"keywords"`
}

type Client struct {
	rdb *redis.Client
	ttl time.Duration
}

func New(addr string, ttl time.Duration) *Client {
	return &Client{ttl: ttl, rdb: redis.NewClient(&redis.Options{
		Addr: addr, DialTimeout: opTimeout, ReadTimeout: opTimeout, WriteTimeout: opTimeout, MaxRetries: -1,
	})}
}

// Key hashes the normalised query (trimmed, lower-cased, whitespace collapsed) with the limit.
func Key(query string, limit int) string {
	q := strings.Join(strings.Fields(strings.ToLower(query)), " ")
	return fmt.Sprintf("search:%x", sha256.Sum256([]byte(fmt.Sprintf("%d\x00%s", limit, q))))
}

// Get is safe on a nil Client.
func (c *Client) Get(ctx context.Context, query string, limit int) (Entry, bool) {
	var e Entry
	if c == nil {
		return e, false
	}
	ctx, cancel := context.WithTimeout(ctx, opTimeout)
	defer cancel()
	raw, err := c.rdb.Get(ctx, Key(query, limit)).Bytes()
	if err != nil {
		if err != redis.Nil {
			log.Printf("cache get: %v", err)
		}
		return e, false
	}
	if err := json.Unmarshal(raw, &e); err != nil {
		log.Printf("cache decode: %v", err)
		return e, false
	}
	return e, true
}

// Set stores a non-empty entry; empty result sets are never cached. Safe on a nil Client.
func (c *Client) Set(ctx context.Context, query string, limit int, e Entry) {
	if c == nil || len(e.Results) == 0 {
		return
	}
	raw, err := json.Marshal(e)
	if err != nil {
		log.Printf("cache encode: %v", err)
		return
	}
	ctx, cancel := context.WithTimeout(ctx, opTimeout)
	defer cancel()
	if err := c.rdb.Set(ctx, Key(query, limit), raw, c.ttl).Err(); err != nil {
		log.Printf("cache set: %v", err)
	}
}
