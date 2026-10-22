## Why

External metadata and channel-enrichment failures currently strip enriched fields from `/models-table` by falling back to CPA baselines; the last successful API inputs can also disappear on restart or be discarded by refresh policy. Operators need metadata availability through source outages without freezing the final model catalog and hiding newly published CPA models.

## What Changes

- Persist validated successful enrichment API responses in a Go disk-backed cache, using the previously selected bbolt library and a dedicated Docker named volume.
- Keep the existing five-minute freshness cadence, but never delete a response because it has aged. Failed metadata refreshes, including 401/403 and invalid responses, preserve and reuse the last successful response when present.
- Cap effective cached response payloads at **100 MB (100,000,000 bytes)**. Evict entries **FIFO**, not LRU; do not add a separate entry-count or maximum-stale-age limit. Database overhead and temporary storage are outside this payload cap.
- Make normal and forced refreshes rebuild from current model membership plus available API input responses, rather than replaying an old rendered table or final models catalog after an enrichment failure.
- Preserve source validation, request-key isolation, cancellation, singleflight, current authority rules, and user entitlement checks. Cached enrichment inputs cannot authorize models, revive removed members, or relax routing eligibility.
- Update tests and current operational documentation to cover persistence, outage recovery, FIFO capacity, fresh CPA membership, and security-policy separation.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-models-enricher`: Persist validated enrichment inputs across restart; define non-expiring stale fallback, a 100 MB FIFO payload budget, force-refresh recomposition, and isolation from membership/authorization authority.

## Impact

- Go `models-enricher` read-cache storage and lifecycle, shared read-call policies, source/channel enrichment, catalog construction, and diagnostic refresh handling.
- `models-enricher/go.mod` and `go.sum` gain bbolt with a version compatible with the supported Go toolchain; no unrelated toolchain change.
- Compose gains a dedicated persistent cache volume and writable mount for the enricher; operational documentation describes ownership, limits, stale behavior, and deliberate volume removal.
- Existing cache, channel fallback, routing, catalog-generation, and force-refresh fixtures require corresponding behavioral updates. HTTP endpoint paths and successful response formats remain unchanged.
- Production deployment, host resource remediation, upstream API fixes, and changes to the independent model-sync service are outside this planning change. Implementation requires a later explicit apply request.
