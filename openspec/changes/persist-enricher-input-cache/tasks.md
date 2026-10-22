## 1. Establish the cache policy seam and first acceptance example

- [ ] 1.1 Inventory shared read-cache consumers in `read_cache.go`, `cpa.go`, `identity.go`, `aisix.go`, `sources.go`, `ollama.go`, and catalog/refresh handlers; classify metadata versus authority and verify a short caller-policy table matches design decision 2 without relaxing native inventory, identity, routing, or user authorization.
- [ ] 1.2 Add one existing-style HTTP acceptance test for a warmed metadata input followed by expiry, store close/reopen, source failure, and a newly eligible CPA model; run it before implementation and capture a failure for lost metadata or stale final-catalog replay, not fixture/setup failure.

## 2. Deliver persistent, bounded API input storage

- [ ] 2.1 Add a maintained Go-compatible bbolt dependency and replace disposable enrichment-input storage with a database holding payloads, last-success timestamps, FIFO order, and payload accounting; verify the module builds, the test from 1.2 progresses to green with the required call-policy integration, and close/reopen preserves the original metadata age without loading all bodies into heap.
- [ ] 2.2 Implement the 100,000,000-byte effective-payload budget with transactional FIFO eviction, no extra entry-count/age limit, and stable insertion order on reads/replacements; verify exact-fit, overflow, replacement growth/shrink, restart order, and oversized-body non-eviction using small test budgets plus a production-budget assertion.
- [ ] 2.3 Replace temporary-directory deletion with explicit persistent-store open/close, bounded exclusive-open failure, and coherent write-error handling; verify reopen retains data, a second writer fails predictably, and a failed write does not partially remove cached successes or corrupt accounting.

## 3. Deliver resilient metadata refresh without authority changes

- [ ] 3.1 Apply the metadata-specific policy at each classified enrichment call site while keeping authority calls on their existing policy; verify metadata timeouts, connection failures, 5xx, 401/403, and malformed bodies reuse the matching last successful input, while authority denial/invalid-body tests retain their prior strict outcomes.
- [ ] 3.2 Validate outer/inner API statuses and required endpoint response shape before cache publication, and preserve valid-empty replacement and ordinary missing-model metadata behavior; verify bad responses never replace last-good bytes and successful empty responses do not resurrect older data.
- [ ] 3.3 Preserve request-key isolation, response-size limits, singleflight, and cancellation while allowing metadata attempt failures to use retained inputs within a live build; verify distinct methods/headers/credentials/inner bodies remain isolated, concurrent refreshes coalesce, expired stage contexts do not silently discard available metadata, and actual caller cancellation still terminates the operation without deleting the cached success.

## 4. Deliver current-membership catalog and diagnostic recomposition

- [ ] 4.1 Rebuild catalogs from the pinned current authority generation and available raw API inputs, not a previous final models body; verify new eligible CPA models appear during metadata outages, removed or disabled models do not reappear, unknown new-model fields retain the baseline/unknown representation, and generation-race tests still pass.
- [ ] 4.2 Change forced refresh to bypass freshness but retain metadata fallback, remove old rendered-table replay as failure recovery, and surface stale input use in the existing diagnostic warning; verify concurrent force refresh remains singleflight, the current model set is rendered after source failure, and missing mandatory CPA authority produces the existing error instead of an old table.

## 5. Wire the dedicated Docker volume and current documentation

- [ ] 5.1 Declare the dedicated `models-enricher-cache` named volume, mount `/var/cache/models-enricher`, and configure the cache directory while keeping the root filesystem read-only; verify the redacted effective Compose configuration gives only the enricher this writable cache mount and that the actual container user can create/reopen the database in an isolated retained-volume rehearsal.
- [ ] 5.2 Update current README/operational/cache-verification documentation for five-minute freshness versus non-expiring retention, 100 MB payload-only accounting, FIFO, stale warnings, cold-cache limits, volume retention, and rollback; verify documentation no longer promises restart loss or LRU for this cache and does not claim a physical 100 MB disk quota or modify archived change history.

## 6. Verify the integrated change and prepare a review handoff

- [ ] 6.1 Run the focused cache/channel/catalog/force-refresh/routing tests, then `go test ./...`, `go test -race ./...`, and `go vet ./...` from `models-enricher` with build/test temporary files under `/var/tmp`; verify passing exit codes and record any environment limitation instead of marking a failed check complete.
- [ ] 6.2 Run effective Compose validation and existing relevant deployment-config checks without printing secrets, plus an isolated restart/outage/new-CPA-model acceptance rehearsal; verify the scenario-to-test evidence covers the delta requirements and no production service or independent model-sync process was modified.
- [ ] 6.3 Obtain fresh independent review of correctness, metadata/authority separation, persistence, FIFO accounting, and minimality; resolve accepted findings, rerun affected checks, and verify the final diff contains only the scoped change with no unauthorized commits, branches, pushes, or deployment. Present evidence for human acceptance; production rollout remains separately authorized.
