## Context

See `proposal.md` for motivation and `specs/codex-models-enricher/spec.md` for the behavior contract.

The existing Go cache in `models-enricher/read_cache.go` stores response bodies in a fresh temporary directory with an in-memory index. Freshness is five minutes, retention is bounded by 32 MiB and 256 entries using last-access eviction, and only selected transient errors permit stale reuse. Other errors invalidate the entry. `main.go` removes the directory on shutdown. Forced refresh bypasses input fallback and can return an in-memory last-good rendered table.

The same cache is used by metadata sources, CPA native inventory, discovery/identity reads, CPA api-call wrappers, and AISIX raw membership. Changing all callers to unrestricted fallback would alter authorization and routing decisions. A channel inventory can also supply both metadata and candidate IDs, so caching it does not by itself establish permission to publish its IDs.

Read-only gateway inspection found repeated metadata/channel timeouts and cancellations followed by `channel enrichment skipped; using CPA baseline`. The diagnostic page did return HTTP 200 with 375 model rows during one probe, but other probes timed out. High host load was observed; its causal relationship to these timeouts is not established. `buildCatalog` currently performs discovery, identity, and channel collection before fetching shared metadata under one overall deadline, so a later stage can inherit exhausted time.

User decisions supersede the initial LRU plan: **100 MB of effective response payloads**, **FIFO**, **dedicated Docker volume**. Physical database/index/free-page/maintenance overhead is explicitly outside that limit. The supported module currently declares Go 1.24; the earlier bbolt selection is recorded in `PLAN/keeper-oom.md`.

## Goals / Non-Goals

**Goals:**
- Replace disposable enrichment-input storage with a small transactional disk KV, preserving request isolation, singleflight, validation, and low idle heap use.
- Separate metadata resilience from authority decisions at the shared read boundary, not through scattered status-code patches.
- Rebuild from current pinned authority and retained inputs after source outages, including forced refresh and restart.
- Keep payload accounting and FIFO order durable without a second mutable in-memory copy of the database index.

**Non-Goals:**
- Persist or replay finished manifests, rendered tables, routing snapshots, or per-user authorization decisions as outage recovery.
- Change the independent CPA model-sync cadence, existing membership precedence, entitlement checks, or front-proxy caches.
- Fix host load, Docker responsiveness, upstream availability, or general request scheduling as part of this change.
- Add a cache administration API, encryption/key management, backups, a general migration framework, automatic compaction service, a second cache library, or configurable eviction algorithms.
- Perform implementation, deployment, restarts, Git publication, or production fault injection during proposal creation.

## Decisions

### 1. Use bbolt for persistent metadata inputs

Use `go.etcd.io/bbolt`, selected previously for the constrained gateway host, with one database under `/var/cache/models-enricher/inputs.db`. bbolt v1.5.0 requires Go 1.25, while the image builds with Go 1.24, so pin v1.4.3 (Go 1.23) and `golang.org/x/sys` v0.40.0 instead of upgrading the toolchain. Only metadata inputs are persisted; authority reads (which may contain management credentials) stay in a bounded process-local FIFO memory cache with their existing policy.

Store only validated enrichment API response bytes plus their last-success time, original admission sequence, and payload length. Keep FIFO indexing and total-payload accounting in the same database. Small in-flight/singleflight coordination can remain in memory; persisted response bodies and the complete persistent index must not be eagerly loaded into heap.

Use short transactions. Copy a selected body into request-owned memory before its read transaction closes, then decode outside the transaction. Do not hold transactions across network calls or retain mmap-backed slices after a transaction. This limits idle heap, not the working set required to parse active responses.

**Alternatives:** Keep ad-hoc files and rebuild an in-memory index on startup, or use Badger. Files would retain bespoke accounting/recovery responsibilities; Badger's memtable/cache/compaction machinery is unnecessary here. Do not add either alternative alongside bbolt.

### 2. Classify metadata separately from authority

Replace the shared cacheable/not-cacheable distinction where necessary with a small explicit policy at each relevant read call site:

| Read purpose | Cache/fallback behavior |
| --- | --- |
| models.dev/modelparams.dev response, channel enrichment response, Ollama per-model metadata | Persistent metadata input; refresh failure can reuse last validated success regardless of age or failure status. |
| CPA native manifest, management discovery and identity, AISIX raw membership | Preserve existing authority validation, allowed transient fallback, denial invalidation, and process-local startup authority. Never apply relaxed metadata fallback to these decisions. |
| Mutating API requests and inference | Not admitted to the metadata cache. |
| Per-user entitlement | Preserve existing request-specific authorization; not served from this persistent cache. |

Inspect all `readJSON` and CPA api-call consumers before changing the shared boundary. A read-only channel response can contribute metadata or candidates, but cached candidates must pass the existing current admission checks; it cannot re-enable a disabled channel or establish routing authority. Preserve exact request-key isolation over method, destination, authenticated identity/headers, and replayable inner request body. Persist hashed identities rather than credential-bearing request material.

The metadata cache publishes success only after the relevant outer envelope, inner status, JSON, and endpoint-specific required shape have been checked. Empty-but-valid data is a successful replacement; a missing match for a model is not invalid data. Parsing errors must not first overwrite last-good bytes and only then be discovered by the consumer.

**Alternative:** Relax `staleReadAllowed` globally. Rejected because cached native/management/AISIX reads participate in authority, unlike optional metadata.

### 3. Separate freshness, refresh attempt, and retention

Retain the existing five-minute ordinary freshness interval; it is not a retention deadline. Ordinary fresh reads return the stored input. A stale read or force-refresh attempts the network; on failure, eligible metadata uses its last validated response. A failed refresh preserves the last-success timestamp and FIFO position. Successful replacements update payload/timestamp atomically while retaining the key's original FIFO position.

Production logs showed fetch failures were mostly `context canceled` (169 vs 34 deadline errors) with HTTP 499 on `/models-table`: builds ran on the request context, so an abandoned client aborted the shared build and nothing was stored. Shared catalog/table builds and request-triggered snapshot refreshes therefore run on `context.WithoutCancel(r.Context())`, still bounded by their existing deadlines and deduplicated by the existing singleflight/build mutex; the disconnected client only stops waiting. Cancellation of an individual read remains cancellation and does not invalidate the stored success. A metadata refresh failure inside an otherwise live catalog build can fall back. Do not start network work with an expired operation context and then mistake absent time for proof that stored metadata is unusable. Check retained input availability at the appropriate metadata boundary, without extending a canceled caller or bypassing mandatory authority deadlines. Preserve bounded collection and existing singleflight; do not add indefinite retries.

If no cached response exists, use the existing channel-baseline/error contract. If a valid successful response cannot be stored because of I/O failure, retain the previous committed cache state and allow the already validated network result to serve the current build; log the persistence failure without credentials. Cache read failures must not be reported as cache hits. Database initialization failure remains explicit rather than silently switching to an empty volatile cache.

**Alternative:** Add a very long TTL. Rejected because it would delay refresh, still conflate age with retention, and not address restart or forced refresh.

### 4. Bound effective payloads at 100,000,000 bytes and evict FIFO

The only capacity limit is total retained response payload bytes, not entry count or age. In a successful write transaction, subtract any replaced body, calculate the new total, evict oldest admitted other entries only as needed, replace the body, and commit accounting/order together. Reads do not reorder. Replacement of an existing entry does not reorder. A removed-and-reinserted key receives a new admission sequence. This is the explicit interpretation of FIFO used by this plan.

If a body cannot fit by itself, skip persistence before evicting anything and preserve the previous entry. Existing endpoint response-size limits still apply before cache admission. Transaction failure must not leave victims deleted or counters updated without the new committed state.

No physical-disk quota or automatic compaction is added. bbolt reuses freed pages, but a database can keep its high-water allocation and physical usage can exceed the logical payload budget. Record that distinction in operations documentation. Volume/disk monitoring remains operational responsibility.

**Alternatives:** LRU and per-entry limits are superseded by the user's FIFO/100 MB-only decision. A hard total-file quota would require reserving metadata/maintenance space and was explicitly not selected.

### 5. Recompose catalogs; never substitute an old finished table

Keep the current immutable authority generation and generation/version-pinned ordinary projection cache. A new build combines that generation with newly fetched or eligible cached enrichment responses. Newly admitted CPA models therefore appear even when a metadata source is unavailable; absent metadata remains unknown or the CPA baseline. Removed/ineligible models cannot reappear from stored enrichment inputs.

For `POST /models-table/refresh`, bypass input freshness for the attempt, not the ability to fall back to validated metadata. Continue refreshing authority under its existing strict policy. Remove old rendered-table replay as the force-refresh recovery path. Render the newly built result and surface metadata fallback through the existing diagnostic warning mechanism rather than saying all data was freshly fetched. If mandatory CPA authority is unavailable, return the existing error/unavailable result instead of replaying a saved table. Ordinary GET remains non-forcing; this change does not run the independent model-sync service.

**Alternative:** Persist or retain last-good final models/table output as the fallback. Rejected because it hides current CPA additions and removals, even when CPA itself is healthy.

### 6. Use one dedicated Docker named volume

Declare `models-enricher-cache` and mount it at `/var/cache/models-enricher` for the enricher only. Use one cache-directory environment setting (for example `CACHE_DIR`) with the mounted directory as its production value; avoid new capacity or eviction-policy configuration. Keep the existing read-only root filesystem and unrelated tmpfs use. Ensure the actual container UID/GID can create and exclusively open the database without making the volume world-writable or mounting it into other services.

Startup opens the existing database without refreshing timestamps or replaying authority. Shutdown closes the database; it does not remove the cache directory. Retaining the named volume across container recreation retains the cache. Intentional volume deletion is a destructive operator action, not an application cleanup step. A bounded database-open timeout reports a conflicting writer rather than hanging startup.

**Alternatives:** `/tmp` tmpfs, the container writable layer, a shared service volume, or a host bind directory. Rejected because they either lose cache on recreation, retain heap/tmpfs pressure, broaden access, or add host-path management without a need.

### 7. Verify through existing HTTP and cache fixtures

Use existing Go test structure rather than a new framework. Begin apply with one failing external-behavior example. Exercise catalog/table handlers with controlled CPA/source endpoints, close/reopen the real persistent store on a temporary disk path, and assert outputs and source request counts. Test FIFO with small injected test budgets while separately asserting the production budget is 100,000,000 bytes; avoid allocating 100 MB per ordinary test.

Required scenarios cover expiry/outage; 401/403 and malformed metadata; successful empty replacement; restart with stale age preserved; new CPA model and removed-model behavior; forced refresh with diagnostic warning; authority denial; request-key isolation; FIFO read/replacement/restart order; exact-fit and oversized admission; transactional write failure where supported; cancellation and concurrent singleflight. Run focused checks, full Go tests, race detection and vet, plus redacted effective Compose validation and existing relevant deployment-config checks. Tests alone are evidence, not production acceptance.

## Risks / Trade-offs

- **Old metadata can remain wrong indefinitely during an outage** → preserve actual last-success times and diagnostic warnings; only successful validated responses replace it.
- **FIFO can evict a frequently used source** → intentional user choice; document that an evicted input has no fallback on its next failure.
- **Physical database usage can exceed 100 MB** → the confirmed cap is payload-only; rely on page reuse and operational disk monitoring, not an implied file quota.
- **Persisted metadata can contain private source fields** → keep the volume private to the enricher, avoid logging payloads/credentials, and preserve hashed request isolation; encryption is not introduced.
- **Restored enrichment mistaken for authority** → explicit read-purpose separation plus denial, disabled-channel, removed-model, and cold-routing tests.
- **Disk-backed storage does not eliminate active parse heap** → retain existing bounded responses/concurrency; do not claim this change fixes all host memory pressure.
- **Cold first deployment has no last-good data** → first successful fetch is still required; outages before that retain existing degraded/error behavior.
- **Caller versions or credential changes produce different cache identities** → intentional isolation; do not borrow a previous identity's data to fabricate fallback.
- **Existing main spec mentions a ten-minute source cache and uncached channel responses** → the delta explicitly replaces those rules with the current five-minute freshness cadence and persistent enrichment-input retention; archived history stays untouched.

## Migration Plan

1. After a new explicit apply request, implement the input-store seam and call-policy separation with tests, then add the dedicated volume and update current docs.
2. Validate a cold store, warmed store, close/reopen with source failure, and a new CPA generation under outage using isolated fixtures. Validate non-root volume permissions without mutating production.
3. Obtain independent review and parent verification. Present implementation evidence before seeking separate production deployment authorization.
4. When deployment is authorized, record the current image/config, deploy only the scoped enricher/volume wiring, warm successful inputs, and verify live catalog/diagnostic behavior without destructive production fault injection.
5. For rollback, restore the recorded previous image/config and retain the named volume. The old implementation ignores the new store; it returns to its former disposable-cache behavior. Do not delete the persistent volume as part of rollback.
