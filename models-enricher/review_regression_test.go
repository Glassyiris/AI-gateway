package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// 自身列表决定成员的渠道（gemini/interactions）：内层403不能用旧列表恢复成员。
func TestMembershipInventoryKeepsAuthorityPolicy(t *testing.T) {
	for _, kind := range []string{"gemini-api-key", "interactions-api-key"} {
		t.Run(kind, func(t *testing.T) {
			var status atomic.Int32
			status.Store(200)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				if status.Load() != 200 {
					_ = json.NewEncoder(w).Encode(map[string]any{"status_code": status.Load(), "body": "denied"})
					return
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"status_code": 200, "body": map[string]any{"models": []map[string]any{{"name": "models/m"}}}})
			}))
			defer server.Close()
			pool := cachedTestPool(t)
			cpa := newCPAClient(server.URL, "m", "c", pool, testLog())
			cfg := &Config{}
			ch := Channel{Type: kind, Prefix: "p", BaseURL: "https://p"}
			if models, err := fetchOne(context.Background(), cpa, cfg, ch, "1"); err != nil || len(models) != 1 {
				t.Fatalf("warm: %v %v", models, err)
			}
			// 重启后缓存为空（权威读取不落盘），403 不得复活旧列表。
			dir := pool.cache.dir
			_ = pool.cache.close()
			reopened, err := newReadCache(dir, testLog())
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = reopened.close() })
			reopened.clock = func() time.Time { return time.Now().Add(time.Hour) }
			pool.cache = reopened
			status.Store(403)
			if models, err := fetchOne(context.Background(), cpa, cfg, ch, "1"); err == nil {
				t.Fatalf("membership inventory resurrected after 403: %v", models)
			}
		})
	}
}

// 端点形状校验在发布缓存前完成：形状不对的 200 响应不覆盖最后成功值。
func TestEndpointShapeValidatedBeforeCache(t *testing.T) {
	var body atomic.Value
	body.Store(`{"p":{"models":{"m":{"id":"m"}}}}`)
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, body.Load().(string))
	}))
	defer source.Close()
	old := modelsDevAPIURL
	modelsDevAPIURL = source.URL + "/api.json"
	t.Cleanup(func() { modelsDevAPIURL = old })
	pool := cachedTestPool(t)
	now := time.Now()
	pool.cache.clock = func() time.Time { return now }
	good, err := sourceGet(context.Background(), pool, modelsDevAPIURL)
	if err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Hour)
	body.Store(`[]`)
	if got, err := sourceGet(context.Background(), pool, modelsDevAPIURL); err != nil || string(got) != string(good) {
		t.Fatalf("invalid bulk shape replaced last success: %s %v", got, err)
	}
	now = now.Add(time.Hour)
	body.Store(`{}`)
	if got, err := sourceGet(context.Background(), pool, modelsDevAPIURL); err != nil || string(got) != `{}` {
		t.Fatalf("valid empty bulk response rejected: %s %v", got, err)
	}

	var channelBody atomic.Value
	channelBody.Store(map[string]any{"data": []map[string]any{{"id": "m"}}})
	cpaServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status_code": 200, "body": channelBody.Load()})
	}))
	defer cpaServer.Close()
	cpa := newCPAClient(cpaServer.URL, "m", "c", pool, testLog())
	ch := Channel{Type: "openai-compatibility", Prefix: "p", BaseURL: "https://p"}
	if _, err := fetchOne(context.Background(), cpa, &Config{}, ch, "1"); err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Hour)
	channelBody.Store(map[string]any{"data": "broken"})
	if models, err := fetchOne(context.Background(), cpa, &Config{}, ch, "1"); err != nil || len(models) != 1 || models[0].ID != "m" {
		t.Fatalf("invalid channel inventory replaced last success: %v %v", models, err)
	}
}

// 脱离客户端的构建在排队等待时也受期限约束。
func TestDetachedBuildQueueIsBounded(t *testing.T) {
	catalogBuildGate <- struct{}{}
	defer func() { <-catalogBuildGate }()
	h := &modelsHandler{cfg: &Config{OverallDeadline: 50 * time.Millisecond}, log: testLog()}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(context.Background()), h.cfg.OverallDeadline)
	defer cancel()
	start := time.Now()
	status, _, _ := h.build(httptest.NewRequest(http.MethodGet, "/models-table", nil).WithContext(ctx), "1", nil, catalogTable)
	if status != http.StatusServiceUnavailable || time.Since(start) > time.Second {
		t.Fatalf("queued build not bounded: status=%d after %s", status, time.Since(start))
	}
}

// 新鲜度检查与认领 flight 原子：已提交的新结果不会再触发重复抓取。
func TestFreshnessRecheckedWhenClaimingFlight(t *testing.T) {
	pool := cachedTestPool(t)
	var calls atomic.Int32
	fetch := func() ([]byte, error) {
		n := calls.Add(1)
		time.Sleep(5 * time.Millisecond)
		return []byte(fmt.Sprintf(`{"n":%d}`, n)), nil
	}
	var wg sync.WaitGroup
	for i := range 64 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			time.Sleep(time.Duration(i%8) * time.Millisecond)
			if _, err := pool.cache.load(context.Background(), "k", cacheMetadata, fetch); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatalf("concurrent readers fetched %d times", calls.Load())
	}
}

// 权威内存缓存：替换不改变入场位置，下一次淘汰仍从最早入场的条目开始。
func TestAuthorityMemoryCacheReplacementKeepsFIFO(t *testing.T) {
	m := memCache{entries: map[string]memEntry{}}
	mib := func(n int) []byte { return make([]byte, n<<20) }
	now := time.Now()
	for _, k := range []string{"a", "b", "c"} {
		if err := m.put(k, mib(8), now); err != nil {
			t.Fatal(err)
		}
	}
	if err := m.put("a", mib(20), now); err != nil { // 8+8+20>32：淘汰 b
		t.Fatal(err)
	}
	if fmt.Sprint(m.order) != "[a c]" || m.bytes != 28<<20 {
		t.Fatalf("replacement changed FIFO order: %v bytes=%d", m.order, m.bytes)
	}
	if err := m.put("d", mib(8), now); err != nil { // 28+8>32：淘汰最早的 a
		t.Fatal(err)
	}
	if fmt.Sprint(m.order) != "[c d]" {
		t.Fatalf("next eviction order: %v", m.order)
	}
}
