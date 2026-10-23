# Decision: terminate WebSocket in front of Sub2API

Status: **deployed 2026-10-23.** Bridge started 02:05:00 UTC, keeper cut over 02:07:35 UTC, nine account WS modes changed to `off` at 02:09:54 UTC. Correlated production and installed-Pi acceptance completed afterwards; bounded limitations are recorded below. See [model-routing.md](model-routing.md).

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

## Why not Sub2API `http_bridge` (the previous production setting)

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

## Production validation, 2026-10-23

Immutable registry images (no production source build):

| Component | Source commit | Index digest |
| --- | --- | --- |
| keeper | `0b4e92695e33c9c2b20d55a286514faaf88712c4` | `sha256:e2b80ae2980bfaf0865666e4dfc3da684ae47c6280343f97fa3f93711cbca4e2` |
| responses-ws-sse-bridge | `6af0a3ef95c6a6f1e1cd8fe353b974196fdc2d44` | `sha256:3b23fd0963d415d32beefa783fc50d83d03342d3c95d2551554afbc5dd006caa` |

Keeper's reviewed `30c0e5a` received only a gofmt correction. Bridge's published graph squashes reviewed `24326a4`, removes generated binary/log artifacts, fixes topology documentation, and applies gofmt; Go source is otherwise unchanged. CI passed for both images and gateway config (`742c226`, documentation `222da19`). Sub2API's image/code was not changed; 53 preexisting containers other than keeper retained IDs, start times and restart counts.

- 140/220 capped inference/WS-create attempts counted; no failed inference retried. Fresh evidence: `/var/tmp/ws-front-acceptance-20261023/`; deployment/rollback receipt: `/var/tmp/ws-front-deploy-20261023/REPORT.md`.
- HTTP B3 marker recall: **9 pass / 1 fail / 2 inconclusive**, versus **0 / 9 / 2** before. The remaining failed sample returned `incomplete: max_output_tokens` at the 64-token cap with no visible text; it does not demonstrate lost history and was not retried. Ten first-turn channel/auth/balance/model failures remain, not silently relabelled as cache passes.
- Branch rollback/forward: **6/6**; WS same/new-socket/store:false/recovery: **20/20**; HTTP post-abort completed-parent recovery: **2/2**; independent WS recovery/store:false fixtures: **9/9**. Unknown-parent WS error leaves the same socket usable. Both bare/slash chat/completions streams reached `[DONE]`.
- Installed Pi `1.3.0-xz.357.1.g99aba2a8`, plugin `0dfd75d`: **4/4** turns with normal HOME, task-only strict `websocket-cached` settings, retries off. Wire observation proved second turns sent parent plus **one input item on a new socket**, not full-history replay. Both `codex/gpt-6-sol` and bare `claude-opus-5-5` recalled assistant-invented markers. The test guard capped output, requested low effort/store:true, and deliberately closed the first completed socket. No global client settings/model IDs were changed.
- Server correlation: keeper previous-hit records, Sub2API POST records, AISIX exact response IDs and bare input models, and inner APISIX upstream addresses (CPA `172.30.35.3:8317`, AISIX `172.30.70.3:3000`). CPA stdout had no request records under existing logging settings; direct CPA model-log coverage is not claimed.
- Actual process `/proc` VmRSS: keeper **11096 → 9360 KiB**, FDs **11 → 11**; bridge **8240 → 7784 KiB**, FDs **9 → 9**. CPU deltas over 769.6 seconds: **1.76 s / 0.67 s** respectively (includes ordinary live traffic). Both healthy, restart count **0**, no OOM, no remaining spools. These are bounded observations, not an exhaustive leak proof or a production CPU profile.

Additional known limitations:

- Pi's first catalog discovery timed out. One read-only warm of public versioned `/v1/models` returned **200 in 10.055 seconds**; real Pi then discovered models normally, without a fixture. That route serially fetches Sub2API entitlement through the front chain and metadata through the catalog sidecar. Catalog latency remains a separate preexisting issue, not hidden as successful inference.
- Production `/v1/messages` returned **403**, `This group does not allow /v1/messages dispatch`, for both tested IDs; this credential cannot establish end-to-end Anthropic inference. No group permissions were changed. Local byte-transparency tests remain the evidence for those streams.
- Keeper WS `logTurn` runs when the client frame is prepared, before upstream terminal storage. Its `stored=false`/short duration are not final storage outcomes. Client `store:true`, exact delta-only reconnect recall, and subsequent hit records establish retention; HTTP completion logs also show `stored=true`.
- Unknown HTTP parents retain the non-standard 400; aborted ids are not required to be retained. Bare Opus's explicit tool-choice rejection and channel inventory/credit failures remain upstream issues. No exhaustive fuzz/standards certification, TTL-long soak or all-account traffic exercise is claimed.

## VM reboot incident and recovery

On the 2026-10-23 03:06:22 UTC VM boot, the new bridge exposed a cold-start health dependency bug: `LISTEN_ADDR=127.0.0.1:8086` was set, but its image healthcheck reads a separate `PORT` variable and defaults to 8080. While keepalive on 8080 was already running, the bridge check falsely passed against keepalive. At cold start keepalive waits for keeper, keeper for bridge health, and bridge incorrectly checks keepalive; `ai-gateway.service` fails with `dependency failed to start: container ai-gateway-responses-ws-sse-bridge-1 is unhealthy`. The gateway unit failed on three start attempts and then hit systemd's start limit (`Start request repeated too quickly`, 03:10:23 UTC). This was introduced by the front-chain deployment, not explained away as an upstream outage.

Read-only checks at 03:16:42 UTC proved bridge `/healthz` on 8086 returned 200 while keeper on 8085 and keepalive on 8080 refused connections. Docker/containerd were enabled/active and netns target IDs existed; no Docker warning-level boot failure was found. Recovery timer ran successfully but skipped stopped keepalive/public APISIX, still allowlisted retired `response-keeper`, and classified bridge as unsupported. A successful timer run did not mean the public chain was available. Do not equate warm startup or a TCP probe on a shared port with a tested cold boot. Detailed local diagnosis: `/var/tmp/keeper-log-fix-20261023/reboot-diagnosis.md`; key cause and health evidence are preserved here independently of that path.

### Supervisor recovery and acceptance

The supervisor took sole production write ownership and completed recovery:

1. **03:17 UTC:** started the six original stopped ingress containers: keeper, keepalive, `apisix-ai-sse-relay`, public APISIX, `cloudflared-apisix-relay`, and cloudflared. These were starts of existing containers, not a new image rollout. Bridge's incorrect port-8080 check then went green, corroborating the false-positive diagnosis.
2. **03:19 UTC:** backed up/fresh-read the host configuration, added bridge `PORT=8086` alongside `LISTEN_ADDR=127.0.0.1:8086`, and recreated **only bridge** from its unchanged `6af0a3e` digest. The healthcheck now probes its own listener, not keepalive. Keeper remains `0b4e926`; Sub2API and other image/configuration values are unchanged. `systemctl reset-failed` plus starting `ai-gateway.service` returned **active(exited)/success**.
3. **Post-recovery acceptance:** bare Opus SSE assistant-marker continuation **2 calls PASS**; strict WS slash Codex continuation on a **new socket**, **2 calls PASS**. Four inference calls total, zero retries. These are recovery smoke checks, not a repeat of the earlier 140-attempt matrix or a fresh VM reboot drill.
4. Backups/rollback receipts remain private on the host: `/var/tmp/ai-gateway-reboot-recovery-20261023T031704Z` and `/var/tmp/ai-gateway-bridge-health-20261023T031916Z`. Supervisor owns synchronization of production compose/override/.env into the private-config checkout; no key-bearing snapshot is published here.

## Do not

- Put Sub2API in front of the keeper or the bridge again.
- Set Sub2API accounts to `passthrough` or `ctx_pool` for this chain.
- Remove `IGNORE_CLIENT_STORE_FALSE=true`. Codex sends `store:false` on every turn.
- Let the bridge store state or retry. The keeper is the only history owner.
