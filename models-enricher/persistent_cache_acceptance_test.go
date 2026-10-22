package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// 验收：元数据成功一次后，缓存过期、边车重启、元数据源持续故障；CPA 同时新增模型。
// 重建的表格必须包含新模型，旧模型保留缓存中的元数据，不重放旧的最终目录。
func TestPersistentMetadataSurvivesRestartWithNewCPAModel(t *testing.T) {
	f := newForceRefreshFixture(t)
	warm := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
	if warm.Code != http.StatusOK || !strings.Contains(warm.Body.String(), "source-v1") {
		t.Fatalf("warm table lacks metadata: %d %.300s", warm.Code, warm.Body.String())
	}

	// 模拟重启：关闭并在同一目录重新打开缓存，时钟推进越过新鲜期。
	dir := f.pool.cache.dir
	if err := f.pool.cache.close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := newReadCache(dir, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reopened.close() })
	reopened.clock = func() time.Time { return time.Now().Add(time.Hour) }
	f.pool.cache = reopened

	f.sourceStatus.Store(http.StatusServiceUnavailable)
	f.extraModel.Store(true)
	catalogCacheTTL.Store(0) // 不复用进程内最终目录投影
	if err := f.owner.refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	got := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
	body := got.Body.String()
	if got.Code != http.StatusOK || !strings.Contains(body, "p/new") || !strings.Contains(body, "source-v1") {
		t.Fatalf("restart+outage lost metadata or new CPA model: HTTP %d %.600s", got.Code, body)
	}
}

// 验收：客户端在构建中途断开；共享构建继续完成并写入缓存，之后的请求直接得到元数据，
// 无需再次成功访问上游。
func TestClientDisconnectDoesNotAbortSharedBuild(t *testing.T) {
	f := newForceRefreshFixture(t)
	catalogCacheTTL.Store(0)
	f.blockSource.Store(true)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		f.handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/models-table", nil).WithContext(ctx))
	}()
	select {
	case <-f.sourceEntered:
	case <-time.After(2 * time.Second):
		t.Fatal("build did not reach metadata fetch")
	}
	cancel() // 客户端放弃
	time.Sleep(20 * time.Millisecond)
	f.blockSource.Store(false)
	close(f.releaseSource)
	<-done

	before := f.sourceCalls.Load()
	f.sourceStatus.Store(http.StatusServiceUnavailable)
	got := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
	if got.Code != http.StatusOK || !strings.Contains(got.Body.String(), "source-v1") {
		t.Fatalf("metadata from abandoned build was not cached: %d %.400s", got.Code, got.Body.String())
	}
	if f.sourceCalls.Load() != before {
		t.Fatalf("fresh cached metadata re-fetched: %d -> %d", before, f.sourceCalls.Load())
	}
}
