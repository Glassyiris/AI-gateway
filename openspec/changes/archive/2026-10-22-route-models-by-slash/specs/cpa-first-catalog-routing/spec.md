## ADDED Requirements

### Requirement: Backend selection follows the model ID slash rule

The entrance SHALL select the backend from the literal top-level `model` string only: a model ID that contains the character `/` anywhere SHALL route to CPA; every other model ID SHALL route to AISIX. The entrance SHALL NOT consult a catalog, routing index, provider registry or cache, and SHALL NOT validate the text on either side of the slash. The model ID SHALL be forwarded unchanged. Whether the model exists is decided by the selected backend.

#### Scenario: Bare aggregate ID routes to AISIX
- **WHEN** a classified request names `claude-opus-5-5` or `gpt-6.1-sol`
- **THEN** it is forwarded to AISIX with the caller credential unchanged

#### Scenario: Unknown bare ID still routes to AISIX
- **WHEN** a classified request names a bare ID that no catalog lists
- **THEN** it is forwarded to AISIX, and any error comes from AISIX

#### Scenario: Any slash routes to CPA
- **WHEN** a classified request names `xl/claude-opus-5-5`, `unknown-provider/unknown-model`, `/model`, `provider/` or `/`
- **THEN** it is forwarded to CPA with the CPA service credential

#### Scenario: Missing CPA credential fails closed
- **WHEN** a slash ID is classified and no CPA service credential is configured
- **THEN** the entrance returns a 503-class error without forwarding

### Requirement: Sub2API WebSocket ingress reaches the selector as HTTP

Every Sub2API OpenAI API-key account SHALL set `extra.openai_apikey_responses_websockets_v2_mode` to `http_bridge` and `credentials.base_url` to `http://cli-proxy-api:8317/v1`, so client WebSocket turns arrive at the entrance as `POST /v1/responses` and are classified. The retired `/rk/v1` base URL SHALL NOT be used. This requirement SHALL be met with existing Sub2API configuration; gateway routing SHALL NOT be implemented by modifying Sub2API code.

#### Scenario: Bare ID over client WebSocket
- **WHEN** a client opens a Responses WebSocket through Sub2API and sends a turn naming a bare ID
- **THEN** Sub2API sends `POST /v1/responses` to the entrance and the request reaches AISIX

#### Scenario: Account left on ctx_pool or /rk
- **WHEN** an account uses `ctx_pool` or a `/rk/v1` base URL
- **THEN** this is a configuration defect: bare IDs over WebSocket reach CPA directly, and `/rk` requests receive 404

### Requirement: Classification scope is the model-bearing HTTP seam

Classification SHALL apply only to the enumerated model-bearing JSON HTTP inference paths of the unified entrance: at minimum `POST /v1/responses` and `POST /v1/chat/completions`, including requests Sub2API's `http_bridge` emits as HTTP inference calls. The selector routes SHALL take precedence over the CPA-direct routes for those paths, so the original requested model ID reaches the selected backend untouched. A direct WebSocket upgrade to the entrance SHALL NOT be classified; it keeps forwarding to CPA and is therefore correct only for model IDs containing `/`. A request on a classified path whose JSON body is malformed or whose model field cannot be extracted SHALL fail explicitly with a protocol-shaped error, never silently fall through to a backend.

#### Scenario: Direct WebSocket stays outside the selector
- **WHEN** a client opens a direct WebSocket upgrade to the entrance
- **THEN** it is forwarded to CPA without model classification or frame inspection

#### Scenario: Malformed body on a classified path
- **WHEN** a classified path receives a body that is not valid JSON or lacks an extractable model ID
- **THEN** the entrance returns an explicit protocol-shaped error
- **AND** no backend receives the request

#### Scenario: Non-classified endpoint unchanged
- **WHEN** a request arrives on an endpoint outside the enumerated classification scope
- **THEN** it keeps its current matching and forwarding behavior, distinct from a failed classification

## REMOVED Requirements

### Requirement: Classification covers a bounded model-bearing HTTP route seam
**Reason**: Its scenarios assumed legacy alias routes and an unspecified direct-WebSocket behavior; that ambiguity let bare IDs reach CPA over WebSocket.
**Migration**: Use "Classification scope is the model-bearing HTTP seam" together with "Sub2API WebSocket ingress reaches the selector as HTTP".

### Requirement: Backend selection follows raw-catalog precedence
**Reason**: Replaced by the operator-defined slash rule. Catalog precedence rejected valid bare aggregate IDs whenever a snapshot lacked them.
**Migration**: Use "Backend selection follows the model ID slash rule".

### Requirement: Routing-index lookup returns one of four outcomes
**Reason**: Inference routing no longer performs a lookup, so `not_found` and `unavailable` are no longer entrance outcomes.
**Migration**: Backends return their own model errors. The enricher may keep serving `/routing-index` for diagnostics; routing does not read it.

### Requirement: Source failure is distinct from empty membership
**Reason**: Routing no longer depends on catalog snapshots.
**Migration**: None for routing. Catalog delivery behavior is owned by `codex-models-enricher`.

### Requirement: Membership lookup is bounded and raw collection is non-recursive
**Reason**: There is no per-request membership lookup.
**Migration**: Raw source collection rules remain with `codex-models-enricher`.
