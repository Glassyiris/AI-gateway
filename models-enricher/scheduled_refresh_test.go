package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// 冷页不等采集：上游仍阻塞时，GET 已返回可操作的加载页。
func TestModelsTableColdEntryDoesNotWaitForCollection(t *testing.T) {
	f := newForceRefreshFixture(t)
	f.blockSource.Store(true)
	defer close(f.releaseSource)
	done := make(chan *httptest.ResponseRecorder, 1)
	go func() { done <- requestForceRefresh(f.handler, http.MethodGet, "/models-table") }()
	select {
	case response := <-done:
		if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "加载中") || !strings.Contains(response.Body.String(), `action="/models-table/refresh"`) {
			t.Fatalf("cold entry: HTTP %d %.500s", response.Code, response.Body.String())
		}
	case <-time.After(150 * time.Millisecond):
		t.Fatal("cold GET waited for the blocked upstream")
	}
	select {
	case <-f.sourceEntered:
	case <-time.After(time.Second):
		t.Fatal("cold entry did not start background collection")
	}
}

// 仅本地假上游，记录旧/新请求路径差异；不代表生产延迟。
func TestModelsTableFakeUpstreamLatency(t *testing.T) {
	f := newForceRefreshFixture(t)
	f.blockSource.Store(true)
	go func() {
		<-f.sourceEntered
		time.Sleep(200 * time.Millisecond)
		close(f.releaseSource)
	}()
	started := time.Now()
	cold := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
	coldElapsed := time.Since(started)
	started = time.Now()
	manual := requestForceRefresh(f.handler, http.MethodPost, "/models-table/refresh")
	manualElapsed := time.Since(started)
	started = time.Now()
	warm := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
	warmElapsed := time.Since(started)
	if cold.Code != 200 || manual.Code != 200 || warm.Code != 200 {
		t.Fatalf("unexpected status: %d %d %d", cold.Code, manual.Code, warm.Code)
	}
	t.Logf("fake_source_delay=200ms cold_GET=%s manual_POST=%s warm_GET=%s", coldElapsed, manualElapsed, warmElapsed)
}

func cancelRefreshOnCleanup(t *testing.T, h *modelsHandler) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	h.refreshCtx = ctx
	t.Cleanup(func() {
		cancel()
		h.refreshMu.Lock()
		f := h.refresh
		h.refreshMu.Unlock()
		if f != nil {
			<-f.done
		}
	})
}

func TestModelsTableEntryWindowAndManualJoin(t *testing.T) {
	f := newForceRefreshFixture(t)
	if got := requestForceRefresh(f.handler, http.MethodPost, "/models-table/refresh"); got.Code != http.StatusOK {
		t.Fatal(got.Code)
	}
	before := f.nativeCalls.Load()
	for range 3 {
		page := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
		if !strings.Contains(page.Body.String(), "p/m1") || strings.Contains(page.Body.String(), "尚无成功刷新") {
			t.Fatalf("warm page: %s", page.Body.String())
		}
	}
	if f.nativeCalls.Load() != before {
		t.Fatal("entry inside window forced a refresh")
	}

	f.models.refreshMu.Lock()
	f.models.lastSuccess = time.Now().Add(-f.models.cfg.EntryRefreshWindow - time.Second)
	f.models.refreshMu.Unlock()
	f.version.Store(2)
	f.blockNative.Store(true)
	var release sync.Once
	defer release.Do(func() { close(f.releaseNative) })
	entryDone := make(chan *httptest.ResponseRecorder, 1)
	go func() { entryDone <- requestForceRefresh(f.handler, http.MethodGet, "/models-table") }()
	select {
	case page := <-entryDone:
		if !strings.Contains(page.Body.String(), "p/m1") {
			t.Fatal("expired entry lost the existing table")
		}
	case <-time.After(150 * time.Millisecond):
		t.Fatal("expired entry waited for upstream")
	}
	select {
	case <-f.nativeEntered:
	case <-time.After(time.Second):
		t.Fatal("expired entry did not refresh")
	}

	manualDone := make(chan *httptest.ResponseRecorder, 1)
	go func() { manualDone <- requestForceRefresh(f.handler, http.MethodPost, "/models-table/refresh") }()
	select {
	case <-manualDone:
		t.Fatal("manual refresh did not wait for the existing job")
	case <-time.After(20 * time.Millisecond):
	}
	var wg sync.WaitGroup
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			requestForceRefresh(f.handler, http.MethodGet, "/models-table")
		}()
	}
	wg.Wait()
	release.Do(func() { close(f.releaseNative) })
	select {
	case page := <-manualDone:
		if page.Code != http.StatusOK || !strings.Contains(page.Body.String(), "p/m2") {
			t.Fatalf("joined refresh: HTTP %d %.300s", page.Code, page.Body.String())
		}
	case <-time.After(2 * time.Second):
		t.Fatal("manual waiter did not finish")
	}
	if got := f.nativeCalls.Load() - before; got != 1 {
		t.Fatalf("entry/manual/concurrent triggers made %d collections, want 1", got)
	}
}

func TestModelsTableFailedAndDegradedRefreshDoNotAdvanceSuccess(t *testing.T) {
	f := newForceRefreshFixture(t)
	requestForceRefresh(f.handler, http.MethodPost, "/models-table/refresh")
	f.models.refreshMu.Lock()
	lastSuccess := f.models.lastSuccess
	f.models.refreshMu.Unlock()
	if lastSuccess.IsZero() {
		t.Fatal("successful collection was not recorded")
	}
	f.sourceStatus.Store(http.StatusBadGateway)
	degraded := requestForceRefresh(f.handler, http.MethodPost, "/models-table/refresh")
	if degraded.Code != http.StatusOK || !strings.Contains(degraded.Body.String(), "最后成功的响应") || !strings.Contains(degraded.Body.String(), lastSuccess.UTC().Format(time.RFC3339)) {
		t.Fatal("degraded collection did not preserve the stale-input notice")
	}
	f.nativeStatus.Store(http.StatusBadGateway)
	failed := requestForceRefresh(f.handler, http.MethodPost, "/models-table/refresh")
	if failed.Code != http.StatusBadGateway {
		t.Fatalf("authority failure status=%d", failed.Code)
	}
	f.models.refreshMu.Lock()
	got := f.models.lastSuccess
	f.models.refreshMu.Unlock()
	if !got.Equal(lastSuccess) {
		t.Fatal("failed or degraded attempt advanced the successful-refresh window")
	}
	page := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
	if !strings.Contains(page.Body.String(), "p/m1") || !strings.Contains(page.Body.String(), "上次刷新失败") || !strings.Contains(page.Body.String(), lastSuccess.UTC().Format(time.RFC3339)) {
		t.Fatal("entry did not retain the table and last-success timestamp after failure")
	}
}

func TestModelsTableStartupScheduledAndShutdown(t *testing.T) {
	f := newForceRefreshFixture(t)
	f.models.cfg.ScheduledRefreshInterval = 40 * time.Millisecond
	f.models.cfg.EntryRefreshWindow = time.Hour
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	before := f.nativeCalls.Load()
	done := f.models.start(ctx)
	// No table request may be responsible for starting the startup collection.
	for deadline := time.Now().Add(time.Second); f.nativeCalls.Load() == before; {
		if time.Now().After(deadline) {
			t.Fatal("startup collection required an entry request")
		}
		time.Sleep(time.Millisecond)
	}
	waitForModel := func(model string) {
		t.Helper()
		for deadline := time.Now().Add(2 * time.Second); ; {
			page := requestForceRefresh(f.handler, http.MethodGet, "/models-table")
			body := page.Body.String()
			if strings.Contains(body, model) {
				if !strings.Contains(body, "下次定时刷新：") || strings.Contains(body, "未启动或已停止") || strings.Contains(body, "尚无成功刷新") {
					t.Fatal("running schedule lacks completion/next-tick timestamps")
				}
				return
			}
			if time.Now().After(deadline) {
				t.Fatalf("scheduled table never contained %s", model)
			}
			time.Sleep(time.Millisecond)
		}
	}
	waitForModel("p/m1")
	f.version.Store(2)
	waitForModel("p/m2")
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("scheduler did not stop")
	}
	before = f.nativeCalls.Load()
	time.Sleep(80 * time.Millisecond)
	requestForceRefresh(f.handler, http.MethodGet, "/models-table")
	if got := f.nativeCalls.Load(); got != before {
		t.Fatalf("stopped scheduler/entry collected again: %d -> %d", before, got)
	}
}

func TestModelsTableShutdownCancelsInFlightRefresh(t *testing.T) {
	f := newForceRefreshFixture(t)
	f.blockNative.Store(true)
	defer close(f.releaseNative)
	ctx, cancel := context.WithCancel(context.Background())
	done := f.models.start(ctx)
	select {
	case <-f.nativeEntered:
	case <-time.After(time.Second):
		t.Fatal("startup did not begin collection")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("shutdown did not cancel the active refresh")
	}
	if page := requestForceRefresh(f.handler, http.MethodPost, "/models-table/refresh"); page.Code != http.StatusServiceUnavailable {
		t.Fatalf("stopped handler accepted new refresh: HTTP %d", page.Code)
	}
}

func TestRefreshConfigDurations(t *testing.T) {
	for _, tc := range []struct {
		name, config string
		wantErr      bool
	}{
		{"defaults", "", false},
		{"independent", "scheduled_refresh_interval: 1m\nentry_refresh_window: 2h\n", false},
		{"zero schedule", "scheduled_refresh_interval: 0s\n", true},
		{"negative schedule", "scheduled_refresh_interval: -1s\n", true},
		{"zero window", "entry_refresh_window: 0s\n", true},
		{"negative window", "entry_refresh_window: -1s\n", true},
		{"invalid duration", "entry_refresh_window: not-a-duration\n", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.yaml")
			if err := os.WriteFile(path, []byte("cpa_base_url: http://cpa.invalid\n"+tc.config), 0600); err != nil {
				t.Fatal(err)
			}
			cfg, err := loadConfig(path)
			if (err != nil) != tc.wantErr {
				t.Fatalf("loadConfig error=%v, wantErr=%t", err, tc.wantErr)
			}
			if tc.name == "defaults" && (cfg.ScheduledRefreshInterval != 30*time.Minute || cfg.EntryRefreshWindow != 10*time.Minute) {
				t.Fatalf("wrong defaults: %s/%s", cfg.ScheduledRefreshInterval, cfg.EntryRefreshWindow)
			}
			if tc.name == "independent" && (cfg.ScheduledRefreshInterval != time.Minute || cfg.EntryRefreshWindow != 2*time.Hour) {
				t.Fatalf("durations coupled or ignored: %s/%s", cfg.ScheduledRefreshInterval, cfg.EntryRefreshWindow)
			}
		})
	}
}
