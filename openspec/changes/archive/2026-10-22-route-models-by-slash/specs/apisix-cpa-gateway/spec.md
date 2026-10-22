## ADDED Requirements

### Requirement: Slash-rule passthrough to CPA or AISIX

The internal instance SHALL forward classified model-bearing JSON HTTP inference requests (at minimum `POST /v1/responses` and `POST /v1/chat/completions`, including Sub2API `http_bridge` output) by the model ID slash rule: an ID containing `/` goes to the CPA upstream with the CPA service credential, and every other ID goes to the AISIX upstream with the caller credential. Request semantics, including SSE streaming, SHALL be preserved. Classification SHALL happen before inference with no cross-backend retry. Malformed classified payloads SHALL fail explicitly without forwarding. Requests outside the classified scope, including direct WebSocket upgrades and endpoints without a model-bearing JSON body, keep forwarding to CPA. Raw source discovery traffic never traverses this entrance; client catalog delivery through the entrance is retained.

#### Scenario: Slash ID chat completion reaches CPA
- **WHEN** Sub2API sends `POST /v1/chat/completions` naming an ID that contains `/`
- **THEN** the request reaches CPA and CPA's response body and status are returned unchanged

#### Scenario: Bare ID reaches AISIX
- **WHEN** a classified request names an ID without `/`
- **THEN** it is forwarded to the AISIX upstream once, with no CPA attempt

#### Scenario: SSE streaming passes through
- **WHEN** Sub2API sends a streaming `POST /v1/responses` request
- **THEN** SSE chunks flow from the selected backend to Sub2API incrementally without full-response buffering

#### Scenario: Direct WebSocket passes through to CPA
- **WHEN** a client opens a direct WebSocket upgrade on `/v1/responses`
- **THEN** the upgrade and frames pass through to CPA without classification
- **AND** bare IDs on this path are not supported; Sub2API accounts use `http_bridge` instead

#### Scenario: Chosen backend failure does not re-route
- **WHEN** the selected backend returns 429/5xx, times out, or errors mid-stream
- **THEN** the failure surfaces without any attempt on the other backend

#### Scenario: Malformed classified payload does not fall through
- **WHEN** a classified path receives a malformed body or one without an extractable model ID
- **THEN** the entrance returns an explicit protocol-shaped error without forwarding to any backend

## REMOVED Requirements

### Requirement: Transparent CPA passthrough
**Reason**: It required catalog-based selection and local model-not-found/503 outcomes, which the slash rule replaces.
**Migration**: Use "Slash-rule passthrough to CPA or AISIX".
