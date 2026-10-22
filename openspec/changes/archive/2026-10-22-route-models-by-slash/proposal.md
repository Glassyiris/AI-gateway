## Why

On 2026-10-22 bare aggregate model IDs (for example `claude-opus-5-5`, `gpt-6.1-sol`) failed over WebSocket. The 08:53 UTC front-keeper cutover (`b112be3`) removed the old inner response-keeper. That keeper had classified every WebSocket `response.create` turn. Its replacement selector was POST-only, so Sub2API's WebSocket upgrade fell through the inner `catch-all` to CPA. CPA does not know bare aggregate IDs, so the connection died with `Connection error`.

The catalog-based selector added its own failure modes too: routing-index stalls produced 503s, and IDs missing from a snapshot were rejected locally with 404 before reaching any backend.

The operator defined the routing rule directly: **a model ID without `/` goes to AISIX; a model ID containing `/` goes to CPA.** No catalog lookup, no provider registration check, no exceptions for where the slash is.

## What Changes

- `apisix-models/routing_selector.lua` classifies by literal `/` only. It removes the routing-index lookup, the last-known-good cache and the local 404/503 outcomes. Body, size and model-field validation stay. CPA key injection still happens only on the CPA branch.
- WebSocket from Sub2API reaches the selector as HTTP. Every Sub2API OpenAI API-key account uses the existing `http_bridge` mode and `base_url` `http://cli-proxy-api:8317/v1`. This uses existing Sub2API configuration. Sub2API code is not modified.
- The inner `catch-all` keeps forwarding direct WebSocket upgrades to CPA unclassified. That path is only correct for IDs containing `/`.
- Specs are updated so the catalog-precedence rule cannot be restored by following stale requirements.

## Impact

- `apisix-models/routing_selector.lua`, `apisix-models/routing_selector_test.lua`
- Specs: `cpa-first-catalog-routing`, `apisix-cpa-gateway`
- Sub2API account configuration (database, not in this repository)
- Unknown bare IDs now reach AISIX and fail there. Unknown slash IDs reach CPA and fail there. The entrance no longer returns its own `model_not_found`.
- `models-enricher` `/routing-index` is no longer consulted by inference routing.
- Deployed 2026-10-22 14:29:57 UTC (selector) and 14:30:52 UTC (accounts) without container restarts.
