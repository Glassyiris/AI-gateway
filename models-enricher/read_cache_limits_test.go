package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
)

// cacheKeys 按 FIFO 入场顺序返回当前保留的键，并返回库内有效载荷计账。
func cacheKeys(t *testing.T, c *readCache) ([]string, int64) {
	t.Helper()
	var keys []string
	var total int64
	if err := c.db.View(func(tx *bolt.Tx) error {
		total = int64(u64(tx.Bucket(bucketStats).Get(statBytes)))
		return tx.Bucket(bucketFIFO).ForEach(func(_, k []byte) error {
			keys = append(keys, string(k))
			return nil
		})
	}); err != nil {
		t.Fatal(err)
	}
	return keys, total
}

func TestReadCacheSourceIsolationAndInvalidJSON(t *testing.T) {
	var broken atomic.Bool
	var otherCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/one" && broken.Load() {
			w.WriteHeader(503)
			return
		}
		if r.URL.Path == "/invalid" {
			fmt.Fprint(w, "not-json")
			return
		}
		if r.URL.Path == "/two" {
			otherCalls.Add(1)
		}
		fmt.Fprintf(w, `{"source":%q}`, r.URL.Path)
	}))
	defer server.Close()
	pool := cachedTestPool(t)
	now := time.Now()
	pool.cache.clock = func() time.Time { return now }
	read := func(path string) ([]byte, error) {
		r, _ := http.NewRequest("GET", server.URL+path, nil)
		return pool.readJSON(r, 1024, cacheMetadata, nil)
	}
	if _, err := read("/one"); err != nil {
		t.Fatal(err)
	}
	now = now.Add(4 * time.Minute)
	if _, err := read("/two"); err != nil {
		t.Fatal(err)
	}
	now = now.Add(2 * time.Minute)
	broken.Store(true)
	if b, err := read("/one"); err != nil || string(b) != `{"source":"/one"}` {
		t.Fatal("source stale fallback lost")
	}
	if b, err := read("/two"); err != nil || string(b) != `{"source":"/two"}` || otherCalls.Load() != 1 {
		t.Fatal("unrelated source cache affected")
	}
	if _, err := read("/invalid"); err == nil {
		t.Fatal("invalid JSON accepted")
	}
	if keys, _ := cacheKeys(t, pool.cache); len(keys) != 2 {
		t.Fatalf("invalid response cached: %v", keys)
	}
}

func TestReadCacheProductionPayloadBudget(t *testing.T) {
	if readCachePayloadBudget != 100_000_000 {
		t.Fatalf("payload budget = %d, want 100000000", readCachePayloadBudget)
	}
	if cachedTestPool(t).cache.budget != readCachePayloadBudget {
		t.Fatal("cache does not use production budget")
	}
}

// FIFO：读取与成功替换都不改变入场顺序；淘汰只针对其他完整条目；计账跨重启保持。
func TestReadCacheFIFOBudgetAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	open := func() *readCache {
		c, err := newReadCache(dir, testLog())
		if err != nil {
			t.Fatal(err)
		}
		c.budget = 10
		return c
	}
	c := open()
	put := func(key string, size int) {
		t.Helper()
		if err := c.put(key, make([]byte, size)); err != nil {
			t.Fatal(err)
		}
	}
	put("a", 4)
	put("b", 4)
	if _, _, ok := c.get("a"); !ok {
		t.Fatal("a missing")
	}
	put("a", 3) // 成功替换不移动位置
	put("c", 2) // 3+4+2=9，正好装下，无淘汰
	if keys, total := cacheKeys(t, c); fmt.Sprint(keys) != "[a b c]" || total != 9 {
		t.Fatalf("exact fit: keys=%v total=%d", keys, total)
	}
	if err := c.close(); err != nil {
		t.Fatal(err)
	}

	c = open()  // 重启后按原入场顺序淘汰
	put("d", 2) // 需要淘汰最早的 a（即使 a 被读取和替换过）
	if keys, total := cacheKeys(t, c); fmt.Sprint(keys) != "[b c d]" || total != 8 {
		t.Fatalf("FIFO after restart: keys=%v total=%d", keys, total)
	}
	put("b", 7) // 替换增长：2+2+7 超预算，其他条目中最早的 c 被淘汰，b 保留原位
	if keys, total := cacheKeys(t, c); fmt.Sprint(keys) != "[b d]" || total != 9 {
		t.Fatalf("replacement growth: keys=%v total=%d", keys, total)
	}
	if err := c.put("huge", make([]byte, 11)); err == nil {
		t.Fatal("oversized response stored")
	}
	if keys, total := cacheKeys(t, c); fmt.Sprint(keys) != "[b d]" || total != 9 {
		t.Fatalf("oversized response evicted entries: keys=%v total=%d", keys, total)
	}
	put("a", 1) // 已淘汰键重新进入时获得新位置
	if keys, _ := cacheKeys(t, c); fmt.Sprint(keys) != "[b d a]" {
		t.Fatalf("re-admitted key order: %v", keys)
	}
}

func TestReadCacheSecondWriterFails(t *testing.T) {
	dir := t.TempDir()
	first, err := newReadCache(dir, testLog())
	if err != nil {
		t.Fatal(err)
	}
	defer first.close()
	start := time.Now()
	if _, err := newReadCache(dir, testLog()); err == nil {
		t.Fatal("second writer opened the same cache")
	}
	if time.Since(start) > 10*time.Second {
		t.Fatal("second open did not time out promptly")
	}
}

func TestReadCacheNetworkFailureWithoutPreviousValue(t *testing.T) {
	pool := cachedTestPool(t)
	_, err := pool.cache.load(context.Background(), "missing", cacheMetadata, func() ([]byte, error) { return nil, io.ErrUnexpectedEOF })
	if err == nil {
		t.Fatal("cold failure fabricated cache")
	}
	if keys, _ := cacheKeys(t, pool.cache); len(keys) != 0 {
		t.Fatal("cold failure stored data")
	}
}

// 元数据：任何刷新失败（含401/403、坏JSON、校验失败）都保留并复用最后成功响应；
// 有效空响应正常替换；期限耗尽可复用，真实取消照常返回错误且不删除缓存。
func TestReadCacheMetadataFallbackPolicy(t *testing.T) {
	var status atomic.Int32
	var body atomic.Value
	var calls atomic.Int32
	status.Store(200)
	body.Store(`{"data":[{"id":"m"}]}`)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(int(status.Load()))
		io.WriteString(w, body.Load().(string))
	}))
	defer server.Close()
	pool := cachedTestPool(t)
	now := time.Now()
	pool.cache.clock = func() time.Time { return now }
	validate := func(b []byte) error {
		if string(b) == `{"broken":true}` {
			return errors.New("shape")
		}
		return nil
	}
	read := func(ctx context.Context) ([]byte, error) {
		r, _ := http.NewRequestWithContext(ctx, http.MethodGet, server.URL, nil)
		return pool.readJSON(r, 1024, cacheMetadata, validate)
	}
	original, err := read(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for _, failure := range []struct {
		status int
		body   string
	}{{401, `{}`}, {403, `{}`}, {500, `{}`}, {200, `not-json`}, {200, `{"broken":true}`}} {
		now = now.Add(time.Hour)
		status.Store(int32(failure.status))
		body.Store(failure.body)
		got, err := read(context.Background())
		if err != nil || string(got) != string(original) {
			t.Fatalf("%d %s: metadata fallback lost: %s %v", failure.status, failure.body, got, err)
		}
	}
	// 期限耗尽：直接复用最后成功值，不发起请求。
	before := calls.Load()
	expired, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	if got, err := read(expired); err != nil || string(got) != string(original) || calls.Load() != before {
		t.Fatalf("expired stage deadline discarded retained metadata: %s %v", got, err)
	}
	// 真实取消：返回取消错误，缓存保留。
	canceled, cancelNow := context.WithCancel(context.Background())
	cancelNow()
	if _, err := read(canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation hidden: %v", err)
	}
	status.Store(200)
	body.Store(`{"data":[]}`)
	if got, err := read(context.Background()); err != nil || string(got) != `{"data":[]}` {
		t.Fatalf("valid empty response did not replace: %s %v", got, err)
	}
	status.Store(503)
	if got, err := read(context.Background()); err != nil || string(got) != `{"data":[]}` {
		t.Fatalf("old metadata resurrected after empty replacement: %s %v", got, err)
	}
}
