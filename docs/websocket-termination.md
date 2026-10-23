# Decision: terminate WebSocket in front of Sub2API

Status: **decided 2026-10-23, not yet deployed.** Production still uses Sub2API `http_bridge`; see [model-routing.md](model-routing.md).

Read this before you move the keeper, change Sub2API WebSocket mode, or remove the ws→sse bridge.

## Decision

WebSocket ends at our own layer, in front of Sub2API. Sub2API receives only HTTP/SSE.

```text
client ─WS/HTTP─► public APISIX ─► keepalive ─► openai-api-cache-keeper
       ─WS/HTTP─► responses-ws-sse-bridge        (each WS turn → POST + SSE)
       ─HTTP/SSE─► Sub2API                       (auth only; account WS mode off)
       ─► apisix-models slash selector ─► AISIX | CPA
```

| Component | Responsibility | Must not |
| --- | --- | --- |
| keeper (single-upstream, `UPSTREAM_URL` = bridge, `IGNORE_CLIENT_STORE_FALSE=true`) | Pure cache. No `previous_response_id`: forward and record. Parent in cache: expand to full input, remove the id, forward, record. Parent not in cache: forward unchanged, record nothing. | Isolate per account, add routing, retry |
| responses-ws-sse-bridge (`UPSTREAM_URL` = Sub2API) | Stateless. Every WS `response.create` becomes `POST /v1/responses` with `stream:true`. Every SSE event becomes one WS frame. Any `previous_response_id` is answered with `previous_response_not_found`. `generate:false` is answered locally. Plain HTTP passes through. | Store anything |
| Sub2API | Auth, quota, accounts | Handle WebSocket |

## Why not Sub2API passthrough with the keeper behind it

We tried `Sub2API passthrough → keeper → bridge`. A local test with the real production Sub2API image (`/var/tmp/sub2api-seam-20261022/RESULT.md`, Sub2API source `ab6b00ed`) showed that passthrough is not transparent:

1. **It rewrites every frame.** `normalizeOpenAIResponsesWebSocketCompatibilityBody` (`openai_gateway_request_body.go`) forces `store:false` and normalizes `stream`, model and tool names. "Passthrough" is therefore not byte-transparent. With `IGNORE_CLIENT_STORE_FALSE=true` the keeper still stores such turns; the first test run reported lost reconnect history, but that came from a stale keeper process that did not have the flag set.
2. **A spec-correct `error` frame poisons the socket.** OpenAI WebSocket mode returns request-scoped errors as `{"type":"error","status":400,...}`, and the connection stays usable. Sub2API ends a turn only on `response.*` terminal events (`openAIWSPassthroughIsTerminalOutput`, `openai_ws_v2_passthrough_adapter.go`). The next `response.create` is closed with 1008 `overlapping response.create is not supported`.
3. **A WS-created id cannot be continued over HTTP.** The HTTP `previous_response_id` owner check (`openai_gateway_handler.go`, `ValidateOpenAIHTTPResponseOwner`) only knows ids that Sub2API bound from HTTP responses. No setting turns it off. Unknown ids get a non-standard 400 `previous_response_id is not available for this user`.
4. Passthrough also needs `GATEWAY_OPENAI_WS_MODE_ROUTER_V2_ENABLED=true` and account concurrency above 0. The startup probe can pin an account to chat/completions.

Fixing 2 or 3 means patching Sub2API, which the architecture rule forbids for our own requirements.

The front placement was locally verified on 2026-10-23 with the same real Sub2API image and a stub model upstream: the report lists 22 scenario rows and reports 44 passing assertions, with keeper `30c0e5a` and bridge `24326a4` (`/var/tmp/sub2api-seam2-20261023/RESULT.md`). This is not production or installed-Pi acceptance. Reconnect continuation, the socket surviving `previous_response_not_found`, WS↔HTTP continuation, key authentication through both hops, and the startup responses probe all passed.

## Why not Sub2API `http_bridge` (the current production setting)

On parent plus delta turns, `http_bridge` rebuilds the history from the same socket only:

- It drops the previous assistant message and reasoning. Upstream PR [#7799](https://github.com/Wei-Shaw/sub2api/pull/7799) fixes this.
- It silently drops a parent it cannot resolve. Our upstream PR [#8302](https://github.com/Wei-Shaw/sub2api/pull/8302) fixes this.

Production acceptance on 2026-10-22 (`/var/tmp/keeper-prod-acceptance-20261022/REPORT.md`) found HTTP B3 continuation: 0 pass, 9 fail, 2 inconclusive among 11 usable models. That failure has a separate cause: the keeper then recorded nothing in single-upstream mode.

## Why WebSocket must become HTTP before the inner gateway

- AISIX supports WebSocket only on `/v1/realtime`, not Responses.
- The `apisix-models` selector reads `model` from an HTTP body. APISIX 3.19 frame phases can read frames, but cannot pick an upstream per frame.
- CPA's XL upstream and the egress proxy accept only POST.

Every hop after the client is HTTP anyway. One conversion point, in our own code, is the least lossy.

## What this costs

- An upstream's native WS connection cache cannot be used. CPA's Codex OAuth path loses that connection-local speed-up. Full-history HTTP replay is not semantically identical to native WS caching: IDs, retention and connection-local state differ. Keeper retains supported output items, including available `reasoning.encrypted_content`; it cannot recreate encrypted reasoning that upstream omitted. `generate:false` is a local bridge prewarm, not an upstream prewarm.
- Each turn resends full history to Sub2API and further upstream: more bandwidth. Any upstream prompt-cache benefit is separate and not guaranteed by this topology.
- Keeper retains one bounded turn's output until terminal (1 MiB/event, 16 MiB shared working bound); it is not an unbounded, fully streaming history store. Late storage failure can leave a stable `resp_` alias without a continuable entry. These are bounded known issues, not waived protocol or leak failures.
- An HTTP request whose parent the keeper has never seen still reaches Sub2API's owner check, which answers with the non-standard 400. Such a parent cannot be continued in any case.

## Do not

- Put Sub2API in front of the keeper or the bridge again.
- Set Sub2API accounts to `passthrough` or `ctx_pool` for this chain.
- Remove `IGNORE_CLIENT_STORE_FALSE=true`. Codex sends `store:false` on every turn.
- Let the bridge store state or retry. The keeper is the only history owner.
