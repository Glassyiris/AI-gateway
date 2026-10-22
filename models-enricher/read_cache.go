package main

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	bolt "go.etcd.io/bbolt"
)

// readCacheTTL 只决定何时尝试刷新；过期不删除最后成功响应。
const readCacheTTL = 5 * time.Minute

// 只统计响应体字节；bbolt 索引、空闲页与维护文件不计入。
const readCachePayloadBudget int64 = 100_000_000

// 权威读取（管理接口可能含凭证）只留在进程内存，不落盘、不跨重启恢复。
const authorityCacheBudget int64 = 32 << 20

const defaultCacheDir = "/var/cache/models-enricher"

var (
	bucketMeta   = []byte("meta")   // key -> stored(8) | seq(8) | size(8)
	bucketBodies = []byte("bodies") // key -> 已校验的成功响应体
	bucketFIFO   = []byte("fifo")   // seq(大端) -> key，按入场顺序淘汰
	bucketStats  = []byte("stats")  // "bytes" 有效载荷总量，"seq" 最大入场序号
	statBytes    = []byte("bytes")
	statSeq      = []byte("seq")
)

// cachePolicy 区分读取用途：权威读取保持原有严格规则，元数据读取任何刷新失败都复用最后成功值。
type cachePolicy int

const (
	cacheNone cachePolicy = iota
	cacheAuthority
	cacheMetadata
)

type readStatusError struct{ status int }

func (e readStatusError) Error() string { return fmt.Sprintf("upstream status %d", e.status) }

type bypassReadCacheKey struct{}
type staleReadCounterKey struct{}

func withReadCacheBypass(ctx context.Context) context.Context {
	return context.WithValue(ctx, bypassReadCacheKey{}, true)
}

func bypassReadCache(ctx context.Context) bool {
	bypass, _ := ctx.Value(bypassReadCacheKey{}).(bool)
	return bypass
}

// withStaleReadCounter 统计一次构建中复用旧响应的次数，供诊断页提示。
func withStaleReadCounter(ctx context.Context, n *atomic.Int32) context.Context {
	return context.WithValue(ctx, staleReadCounterKey{}, n)
}

// 权威读取原有规则：仅临时故障可复用旧值。
func staleReadAllowed(err error) bool {
	if errors.Is(err, context.Canceled) {
		return false
	}
	var status readStatusError
	if errors.As(err, &status) {
		return status.status == 502 || status.status == 503 || status.status == 504
	}
	var network net.Error
	return errors.As(err, &network) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, io.EOF)
}

type readFlight struct {
	done chan struct{}
	body []byte
	err  error
}

// readCache 将成功的输入响应持久化到专用卷上的 bbolt；索引与计账都在库内，
// 内存只保留进行中的请求合并。事务短小，绝不跨越网络 IO。
type readCache struct {
	db      *bolt.DB
	dir     string
	budget  int64
	mu      sync.Mutex
	flights map[string]*readFlight
	mem     memCache
	clock   func() time.Time
	log     *slog.Logger
}

type memEntry struct {
	body   []byte
	stored time.Time
}

// memCache 按入场顺序淘汰的进程内权威读取缓存。
type memCache struct {
	sync.Mutex
	entries map[string]memEntry
	order   []string
	bytes   int64
}

func (m *memCache) get(key string) ([]byte, time.Time, bool) {
	m.Lock()
	defer m.Unlock()
	e, ok := m.entries[key]
	return e.body, e.stored, ok
}

func (m *memCache) put(key string, body []byte, now time.Time) error {
	m.Lock()
	defer m.Unlock()
	size := int64(len(body))
	if size > authorityCacheBudget {
		return errors.New("response exceeds authority cache budget")
	}
	old, exists := m.entries[key]
	m.bytes -= int64(len(old.body))
	for len(m.order) > 0 && m.bytes+size > authorityCacheBudget {
		victim := m.order[0]
		if victim == key {
			m.order = append(m.order[1:], victim)
			continue
		}
		m.order = m.order[1:]
		m.bytes -= int64(len(m.entries[victim].body))
		delete(m.entries, victim)
	}
	if !exists {
		m.order = append(m.order, key)
	}
	m.entries[key] = memEntry{body: body, stored: now}
	m.bytes += size
	return nil
}

func (m *memCache) remove(key string) {
	m.Lock()
	defer m.Unlock()
	e, ok := m.entries[key]
	if !ok {
		return
	}
	m.bytes -= int64(len(e.body))
	delete(m.entries, key)
	for i, k := range m.order {
		if k == key {
			m.order = append(m.order[:i], m.order[i+1:]...)
			break
		}
	}
}

func newReadCache(dir string, log *slog.Logger) (*readCache, error) {
	if dir == "" {
		return nil, errors.New("read cache directory is required")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	db, err := bolt.Open(filepath.Join(dir, "inputs.db"), 0o600, &bolt.Options{Timeout: 5 * time.Second})
	if err != nil {
		return nil, fmt.Errorf("open read cache: %w", err)
	}
	if err := db.Update(func(tx *bolt.Tx) error {
		for _, name := range [][]byte{bucketMeta, bucketBodies, bucketFIFO, bucketStats} {
			if _, err := tx.CreateBucketIfNotExists(name); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		_ = db.Close()
		return nil, err
	}
	return &readCache{db: db, dir: dir, budget: readCachePayloadBudget, flights: map[string]*readFlight{}, mem: memCache{entries: map[string]memEntry{}}, clock: time.Now, log: log}, nil
}

func (c *readCache) close() error { return c.db.Close() }

type cacheMeta struct {
	stored time.Time
	seq    uint64
	size   int64
}

func encodeMeta(m cacheMeta) []byte {
	b := make([]byte, 24)
	binary.BigEndian.PutUint64(b[0:], uint64(m.stored.UnixNano()))
	binary.BigEndian.PutUint64(b[8:], m.seq)
	binary.BigEndian.PutUint64(b[16:], uint64(m.size))
	return b
}

func decodeMeta(b []byte) (cacheMeta, error) {
	if len(b) != 24 {
		return cacheMeta{}, fmt.Errorf("corrupt cache metadata (%d bytes)", len(b))
	}
	return cacheMeta{
		stored: time.Unix(0, int64(binary.BigEndian.Uint64(b[0:]))),
		seq:    binary.BigEndian.Uint64(b[8:]),
		size:   int64(binary.BigEndian.Uint64(b[16:])),
	}, nil
}

func be64(v uint64) []byte {
	b := make([]byte, 8)
	binary.BigEndian.PutUint64(b, v)
	return b
}

func u64(b []byte) uint64 {
	if len(b) != 8 {
		return 0
	}
	return binary.BigEndian.Uint64(b)
}

// URL不足以区分CPA api-call；方法、认证和其他请求头、内层请求体均参与哈希。
func readCacheKey(req *http.Request) (string, error) {
	h := sha256.New()
	if err := json.NewEncoder(h).Encode([]any{req.Method, req.URL.String(), req.Host, req.Header}); err != nil {
		return "", err
	}
	if req.Body != nil && req.Body != http.NoBody {
		if req.GetBody == nil {
			return "", errors.New("cache requires replayable request body")
		}
		body, err := req.GetBody()
		if err != nil {
			return "", err
		}
		defer body.Close()
		if _, err := io.Copy(h, body); err != nil {
			return "", err
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// get 把响应体拷贝出只读事务；不保留 mmap 切片。
func (c *readCache) get(key string) ([]byte, time.Time, bool) {
	var body []byte
	var stored time.Time
	err := c.db.View(func(tx *bolt.Tx) error {
		raw, data := tx.Bucket(bucketMeta).Get([]byte(key)), tx.Bucket(bucketBodies).Get([]byte(key))
		if raw == nil || data == nil {
			return nil
		}
		m, err := decodeMeta(raw)
		if err != nil {
			return err
		}
		stored, body = m.stored, append([]byte{}, data...)
		return nil
	})
	if err != nil {
		c.log.Warn("read cache lookup failed", "key", key, "err", err)
		return nil, time.Time{}, false
	}
	return body, stored, body != nil
}

// put 在一个写事务内替换载荷、按入场顺序淘汰其他条目并更新计账；任何失败整体回滚。
// 替换已有键保留原入场位置；超过整个预算的响应不写入、也不淘汰任何条目。
func (c *readCache) put(key string, body []byte) error {
	size := int64(len(body))
	if size > c.budget {
		return errors.New("response exceeds cache payload budget")
	}
	return c.db.Update(func(tx *bolt.Tx) error {
		meta, bodies, fifo, stats := tx.Bucket(bucketMeta), tx.Bucket(bucketBodies), tx.Bucket(bucketFIFO), tx.Bucket(bucketStats)
		k := []byte(key)
		total := int64(u64(stats.Get(statBytes)))
		var old cacheMeta
		exists := false
		if raw := meta.Get(k); raw != nil {
			var err error
			if old, err = decodeMeta(raw); err != nil {
				return err
			}
			exists = true
			total -= old.size
		}
		// 先收集再删除：游标删除后继续遍历会跳项。
		var victims []cacheMeta
		var victimKeys [][]byte
		cur := fifo.Cursor()
		for seq, victim := cur.First(); seq != nil && total+size > c.budget; seq, victim = cur.Next() {
			if string(victim) == key {
				continue
			}
			vm, err := decodeMeta(meta.Get(victim))
			if err != nil {
				return err
			}
			total -= vm.size
			victims = append(victims, vm)
			victimKeys = append(victimKeys, append([]byte(nil), victim...))
		}
		for i, v := range victimKeys {
			if err := errors.Join(meta.Delete(v), bodies.Delete(v), fifo.Delete(be64(victims[i].seq))); err != nil {
				return err
			}
		}
		seq := old.seq
		if !exists {
			seq = u64(stats.Get(statSeq)) + 1
			if err := errors.Join(stats.Put(statSeq, be64(seq)), fifo.Put(be64(seq), k)); err != nil {
				return err
			}
		}
		return errors.Join(
			meta.Put(k, encodeMeta(cacheMeta{stored: c.clock(), seq: seq, size: size})),
			bodies.Put(k, body),
			stats.Put(statBytes, be64(uint64(total+size))),
		)
	})
}

func (c *readCache) staleHit(ctx context.Context, key string, stored time.Time, err error) {
	if n, _ := ctx.Value(staleReadCounterKey{}).(*atomic.Int32); n != nil {
		n.Add(1)
	}
	c.log.Warn("read cache stale fallback", "key", key, "age_seconds", c.clock().Sub(stored).Seconds(), "err", err)
}

// lookup/store 按用途选择存储：元数据落盘，权威读取只在内存。
func (c *readCache) lookup(policy cachePolicy, key string) ([]byte, time.Time, bool) {
	if policy == cacheMetadata {
		return c.get(key)
	}
	return c.mem.get(key)
}

func (c *readCache) store(policy cachePolicy, key string, body []byte) error {
	if policy == cacheMetadata {
		return c.put(key, body)
	}
	return c.mem.put(key, body, c.clock())
}

func (c *readCache) load(ctx context.Context, key string, policy cachePolicy, fetch func() ([]byte, error)) ([]byte, error) {
	bypass := bypassReadCache(ctx)
	if err := ctx.Err(); err != nil {
		// 前序阶段耗尽期限时元数据仍可用最后成功值；真实取消照常终止。
		if policy == cacheMetadata && !errors.Is(err, context.Canceled) {
			if body, stored, ok := c.lookup(policy, key); ok {
				c.staleHit(ctx, key, stored, err)
				return body, nil
			}
		}
		return nil, err
	}
	if !bypass {
		if body, stored, ok := c.lookup(policy, key); ok && c.clock().Sub(stored) < readCacheTTL {
			return body, nil
		}
	}
	flightKey := key
	if bypass {
		flightKey += ":force"
	}
	c.mu.Lock()
	if f := c.flights[flightKey]; f != nil {
		c.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-f.done:
			return f.body, f.err
		}
	}
	f := &readFlight{done: make(chan struct{})}
	c.flights[flightKey] = f
	c.mu.Unlock()

	body, err := fetch()
	switch {
	case err == nil:
		if putErr := c.store(policy, key, body); putErr != nil {
			c.log.Warn("read cache store failed", "key", key, "err", putErr)
		}
	case errors.Is(err, context.Canceled):
		// 取消不改变缓存。
	case policy == cacheMetadata:
		if old, stored, ok := c.lookup(policy, key); ok {
			c.staleHit(ctx, key, stored, err)
			body, err = old, nil
		}
	case staleReadAllowed(err) && !bypass:
		if old, stored, ok := c.lookup(policy, key); ok {
			c.staleHit(ctx, key, stored, err)
			body, err = old, nil
		}
	default:
		// 权威读取被拒绝/响应无效：不借旧值掩盖，也不允许之后复活。
		c.mem.remove(key)
	}
	f.body, f.err = body, err
	c.mu.Lock()
	delete(c.flights, flightKey)
	c.mu.Unlock()
	close(f.done)
	return body, err
}

// 仅用于已知只读请求。先检查状态、完整body和业务封装，再发布缓存。
func (p *httpPool) readJSON(req *http.Request, limit int64, policy cachePolicy, validate func([]byte) error) ([]byte, error) {
	fetch := func() ([]byte, error) {
		resp, err := p.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, readStatusError{resp.StatusCode}
		}
		body, err := readBoundedBody(resp.Body, limit)
		if err != nil {
			return nil, err
		}
		if !json.Valid(body) {
			return nil, errors.New("invalid upstream JSON")
		}
		if validate != nil {
			if err := validate(body); err != nil {
				return nil, err
			}
		}
		return body, nil
	}
	if policy == cacheNone || p.cache == nil {
		return fetch()
	}
	key, err := readCacheKey(req)
	if err != nil {
		return nil, err
	}
	return p.cache.load(req.Context(), key, policy, fetch)
}
