## MODIFIED Requirements

### Requirement: Metadata fallback with cached data sources

When a model's metadata fields are missing from the channel response, the sidecar SHALL look them up from the configured models.dev and/or modelparams.dev sources. Validated successful enrichment inputs, including read-only channel metadata responses and configured per-model metadata lookups, SHALL be cached as API responses rather than as final model catalogs. Ordinary reads SHALL reuse fresh responses for five minutes; expiry SHALL make an input eligible for refresh without deleting its last successful response or imposing a maximum fallback age. After a failed metadata refresh, the sidecar SHALL reuse that request's last successful cached response when present and SHALL NOT replace or invalidate it because the refresh failed. Metadata failures include timeouts, connection failures, non-success HTTP statuses including 401/403, and invalid response bodies. A client disconnecting from a catalog or table request SHALL only stop that client's wait; the shared build and snapshot refresh SHALL continue within their existing deadlines and publish successful inputs to the cache. An enrichment stage whose deadline is already exhausted SHALL still use the retained last successful input. Without a usable cached input, the sidecar SHALL retain its existing affected-channel baseline or error behavior rather than fabricate metadata. These rules SHALL NOT relax membership, authorization, or routing authority.

#### Scenario: Missing max_tokens filled

- **WHEN** a channel model lacks `max_tokens` and a data source knows the model's max output tokens
- **THEN** the emitted entry carries that value

#### Scenario: Source cache honored

- **WHEN** two ordinary enrichment runs request the same successful metadata input within five minutes
- **THEN** that metadata endpoint is fetched at most once in the freshness window

#### Scenario: Expired input survives a source outage

- **GIVEN** a successful metadata input remains cached after its five-minute freshness period, even after a prolonged outage
- **WHEN** refreshing that input times out, loses its connection, or returns a non-success HTTP status
- **THEN** enrichment reuses that input's last successful response without an age-based cutoff
- **AND** the failed attempt does not advance its last-successful-refresh time

#### Scenario: Metadata rejection or malformed response retains last-good data

- **GIVEN** a metadata-only request has a validated successful cached response
- **WHEN** a refresh returns 401, 403, malformed JSON, or a response that fails the endpoint's required validation
- **THEN** the sidecar preserves and reuses the previous validated response for metadata
- **AND** neither the rejected response nor the invalid body replaces the cached success

#### Scenario: Successful empty input replaces old metadata

- **GIVEN** an endpoint has a previously populated cached response
- **WHEN** it successfully returns a valid empty response under that endpoint's contract
- **THEN** the sidecar replaces the previous response with the valid empty response
- **AND** it does not resurrect old metadata merely because the new response is empty

#### Scenario: Client disconnect does not abort the shared build

- **WHEN** a client disconnects while a shared catalog or table build is fetching metadata
- **THEN** that client stops waiting, while the build continues within its existing deadline and stores successful metadata responses
- **AND** a later request uses the stored metadata without another successful upstream fetch

#### Scenario: Cancellation does not delete cached metadata

- **WHEN** an individual metadata read is cancelled
- **THEN** that read returns cancellation rather than reporting a successful refresh
- **AND** the last successful cached metadata input remains stored

### Requirement: Snapshot lifecycle is initialized and refreshed independently of catalog traffic

The sidecar SHALL initialize the routing snapshot at startup and refresh it on a bounded periodic cadence aligned with the existing cache TTL, reusing the existing collector and singleflight discipline, independent of any catalog-client or lookup traffic. Failed periodic refreshes SHALL retry on the same bounded cadence without tight-looping. Assembled routing snapshots SHALL remain process-local; the persistent enrichment-input cache SHALL NOT restore routing authority at startup. Collection SHALL keep the CPA mandatory phase and optional AISIX phase isolated, with their existing authority-specific eligible last-good policy, so a failed optional AISIX collection preserves successful CPA output and known CPA routing decisions. Publication SHALL be an immutable raw-membership/source-availability generation: known CPA decisions are preserved when only AISIX collection fails, and no hot-path lookup triggers upstream collection. Catalog projection caches SHALL key each cached body to the raw generation plus client_version/format: a completed projection of a stale generation cannot be served as current, and a late old-generation build cannot overwrite the current entry; in-flight readers may finish serving their pinned generation, with no global simultaneous multi-request guarantee. Cold readiness is truthful: before the first successful authority snapshot the index returns `unavailable`, and recovery occurs via the bounded retry cadence even when only inference traffic exists.

The diagnostic force-refresh action SHALL be the sole exception to request-independent collection: it SHALL explicitly initiate a bounded, singleflight refresh of raw memberships and catalog inputs and bypass freshness reuse for that network attempt. Bypassing freshness SHALL NOT disable last-successful-input fallback for metadata. The action SHALL publish a new generation only according to the same source-authority and atomic-publication rules and SHALL rebuild its diagnostic output using those authority inputs and the newly fetched or eligible cached enrichment responses. It SHALL NOT return an old rendered table or final models catalog as the fallback for a failed refresh. Normal `GET /models-table`, catalog requests, and routing-index lookups SHALL retain their existing non-forcing behavior.

#### Scenario: Startup initialization without catalog traffic

- **WHEN** the sidecar starts and no client requests any catalog
- **THEN** the routing snapshot is initialized and refreshed on the bounded cadence
- **AND** inference classification does not depend on a catalog request having occurred

#### Scenario: Optional AISIX failure preserves CPA decisions

- **WHEN** an AISIX collection attempt fails while CPA collection succeeds
- **THEN** the published generation preserves known CPA membership decisions
- **AND** IDs decidable only with AISIX membership return `unavailable`

#### Scenario: Generation-pinned projection caching

- **WHEN** a new raw generation is published while a catalog projection build is in flight
- **THEN** the in-flight build may complete for readers pinned to its generation
- **AND** its result does not overwrite the current entry, and stale completed projections are not served as current

#### Scenario: Inference-only cold start and recovery

- **WHEN** the sidecar restarts and only inference (routing-index) traffic arrives
- **THEN** lookups return `unavailable` until the first successful bounded-cadence authority collection completes, then answer per membership
- **AND** refresh retries observe the bounded cadence without a tight loop
- **AND** persisted metadata alone does not make routing ready

#### Scenario: Hot-path lookup never collects

- **WHEN** a routing-index lookup arrives between refreshes
- **THEN** it is answered from the published generation without triggering upstream collection

#### Scenario: Normal catalog and table reads do not force collection

- **WHEN** a client requests `GET /v1/models` or `GET /models-table`
- **THEN** the request uses the current generation and eligible caches without bypassing the periodic refresh cadence

#### Scenario: Diagnostic force refresh uses existing publication rules

- **WHEN** an operator submits the diagnostic force-refresh action
- **THEN** the sidecar performs one bounded refresh attempt that bypasses input freshness reuse
- **AND** concurrent force-refresh submissions share the active attempt
- **AND** any newly published generation follows the existing atomic source-authority rules

#### Scenario: Forced refresh recomposes from cached API inputs

- **GIVEN** CPA successfully publishes a new model and a metadata source has an older successful cached response
- **WHEN** the operator forces refresh and that metadata source fails
- **THEN** the rebuilt table includes the newly published eligible CPA model and uses the cached API response for matching models' metadata
- **AND** it reports cached metadata fallback through the diagnostic warning rather than labeling the inputs as freshly fetched
- **AND** it does not replay the old table or final model list

#### Scenario: Missing mandatory authority is not hidden by a saved table

- **WHEN** a forced refresh cannot obtain usable mandatory CPA authority under the existing authority policy
- **THEN** the diagnostic request reports the existing unavailable/error outcome
- **AND** it does not substitute a previously rendered table or treat metadata cache contents as authority

## ADDED Requirements

### Requirement: Enrichment inputs survive sidecar restart on a dedicated volume

The sidecar SHALL retain its validated successful enrichment API responses on a dedicated Docker data volume mounted as its cache path. Normal process restart and container recreation with the same volume SHALL preserve cached payloads, last-successful-refresh times, capacity accounting, and FIFO order. Cache age alone SHALL NOT remove persisted inputs, and startup SHALL NOT pretend recovered stale data was newly fetched. Persistent cache contents SHALL consist of metadata API inputs and their cache metadata, not assembled models catalogs, rendered tables, authority reads (native inventory, management discovery/identity, AISIX membership), or authorization snapshots; authority reads SHALL remain process-local. Cache request identities SHALL isolate method, destination, relevant authentication and headers, and any inner read request body; stored keys, logs, and diagnostics SHALL NOT expose credentials.

#### Scenario: Restart during an external metadata outage

- **GIVEN** the sidecar has successfully cached metadata and its dedicated data volume is retained
- **WHEN** the sidecar restarts or is recreated and the metadata endpoint remains unavailable
- **THEN** a catalog build with usable current CPA authority reuses the recovered API response for metadata
- **AND** recovery preserves the input's original freshness age rather than resetting it at startup

#### Scenario: Separate requests cannot borrow another request's cache

- **WHEN** otherwise similar metadata requests differ in authenticated identity, target, method, relevant headers, or inner request body
- **THEN** their successful responses are isolated
- **AND** one request's failure cannot return another request identity's cached response

### Requirement: Persistent enrichment cache has a 100 MB FIFO payload budget

The sidecar SHALL limit the sum of retained enrichment-response payload bytes to **100,000,000 bytes (100 MB)**. Before admitting a response that would exceed this limit, it SHALL evict complete cached entries in first-in-first-out admission order until the new retained payload fits; when replacing an existing key, eviction SHALL consider other entries after subtracting that key's previous payload size. Reads and successful replacement of an existing entry SHALL NOT make that entry newer in FIFO order; a removed entry admitted again SHALL receive a new position. The budget and admission order SHALL survive restart. Freshness expiry SHALL NOT cause eviction, and the sidecar SHALL NOT impose an additional entry-count limit. Database indexes, reusable free pages, and temporary maintenance files SHALL be outside this effective-payload limit. A successful response too large to fit by itself SHALL NOT flush the cache or replace an existing retained response; the existing per-endpoint response limits SHALL remain enforced.

#### Scenario: Admission at the exact capacity boundary

- **GIVEN** retained response payloads plus a new valid response total exactly 100,000,000 bytes
- **WHEN** the new response is admitted
- **THEN** the sidecar retains it without capacity eviction

#### Scenario: FIFO differs from LRU

- **GIVEN** entries A and B were admitted in that order, and A was later read or successfully refreshed
- **WHEN** admitting C requires one older entry to be evicted
- **THEN** A is evicted before B
- **AND** retained payload bytes do not exceed 100,000,000

#### Scenario: FIFO survives restart

- **GIVEN** A was admitted before B and both remain persisted
- **WHEN** the sidecar restarts and admitting C requires eviction
- **THEN** eviction preserves A-before-B order rather than ordering entries by startup, last access, or last refresh

#### Scenario: Oversized response does not destroy retained successes

- **WHEN** a successful response cannot fit within the entire effective-payload budget by itself
- **THEN** it is not retained and the sidecar does not evict other entries merely to attempt its admission
- **AND** any previous retained successful response for the same request remains intact

### Requirement: Cached metadata does not freeze catalog membership or grant authority

For every newly built catalog, the sidecar SHALL compose output from the pinned current authority generation and available enrichment inputs rather than reuse an old final catalog to conceal an enrichment failure. Cached enrichment data SHALL only contribute metadata or model candidates that pass the existing current admission rules. Current CPA additions SHALL remain visible when eligible; current removals, disabled channels, authorization denials, and absent routing eligibility SHALL NOT be reversed by old enrichment inputs. Native CPA membership, management discovery and identity reads, AISIX routing membership, and per-user authorization SHALL retain their existing authority-specific failure policy, including denial and invalid-response handling. The relaxed metadata fallback policy SHALL NOT be applied to those authority decisions. A valid input without metadata for a new model SHALL remain an ordinary metadata miss, not grounds to replay a prior catalog.

#### Scenario: New CPA model appears while old metadata is reused

- **GIVEN** CPA's current eligible membership contains existing model A and newly added model B, and the retained source response describes only A
- **WHEN** that source refresh fails and a catalog is rebuilt
- **THEN** A retains its matching cached metadata and B appears under current admission rules
- **AND** fields unknown for B remain unknown or at their CPA baseline rather than suppressing B

#### Scenario: Removed model is not resurrected

- **GIVEN** a persisted channel-enrichment response mentions model A but current admission no longer permits A
- **WHEN** a source failure causes that response to be reused for enrichment
- **THEN** the output does not regain A from cached metadata

#### Scenario: Authority denial stays strict despite metadata cache

- **GIVEN** retained metadata exists for a model
- **WHEN** an authority read returns an authentication denial or invalid authority response, or a user is not entitled to the model
- **THEN** the existing authority failure or entitlement rejection behavior remains in force
- **AND** cached metadata does not grant catalog visibility or routing readiness
