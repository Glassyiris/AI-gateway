# Verification

## A — Lua last-known-good fallback (deployed early, operator-approved 2026-10-19)

- Pre-deploy prod files matched HEAD (config.yaml 1dcc817f…, routing_selector.lua 60b9e7ab…). Rollback copy + inspect + container list + compose hashes: `/root/AI-gateway/.rollback/apisix-models-lkg-20261019T111041Z/`.
- Synced only `apisix-models/config.yaml` (2cb97fc6…) and `routing_selector.lua` (e5fe09bb…); `docker compose config --hash` unchanged (bind-mounted files only).
- `docker compose restart apisix-models` 11:12:10Z → healthy 11:12:41Z; rendered nginx.conf contains `lua_shared_dict routing_lkg 1m;`; all other container IDs unchanged; apisix-models restarts=0, keeper unaffected (restarts 47, oom=false).
- First ~9 min after restart: selector 127-byte 503 = 0; last-known-good fallback used 14 times (transport/format failures previously had no LKG path); no error/emerg log lines. This is a short observation, not 24-hour stability evidence.
- Baseline for comparison: 92 selector 503s / 24h before deploy.
- Rollback: copy the two files back from the rollback dir and `docker compose restart apisix-models`.

## Pre-B/D rolling baseline — 2026-10-19 15:11:23Z

Window: 2026-10-18 15:11:23Z–2026-10-19 15:11:23Z. Read all four retained apisix-models JSON log files and keeper's retained JSON log; no files skipped. Earliest available records precede window start, so the rolling 24-hour window is covered. This window includes both pre/post-A operation and the CPA restart at approximately14:58Z; it is NOT 24 hours after A or a clean CPA before/after comparison.

- apisix-models Responses 127-byte503 signature count: **117**. All Responses503 access entries: **258**. Signature counts are not verified per-request root causes and may include multiple path records for a turn.
- LKG warning count: **291**.
- keeper WS: `previous=false` **6843**, `previous=true` **5318** (false share **56.27%**, all logged statuses). Not a count of reconnect-caused full histories.
- keeper logged503 count: **179** across transports.
- apisix-models Responses entries with upstream address exactly `127.0.0.1:9080`: **0**. Entries with request Host `127.0.0.1:9080`: **24614**. These fields differ: the existing keeper return hop appears as an incoming request to APISIX, not necessarily an APISIX self-upstream. A zero self-upstream count alone cannot prove single traversal after B; require per-turn correlation and topology validation.
- Full running-container ID inventory (50 containers) matches the post-CPA inventory at `/root/AI-gateway/.rollback/cpa-memory-20261019T145713Z/running-identities-after.json`; keeper remains `f21093a59491bbc5c0a6184328adbc30ad4ea14a3042cf925fad8853e51f95fe`, apisix-models `9cbc290889ab122914acb50306bc9c96969caa6d879d2fc2663c0af1fc6fc940`. Refresh immediately before B deployment.

## Core acceptance / release blocker

- Original reviewer approved F1/F2 on frozen binary `8a37d84d7114f7c4cee185af1754a60fa97b3a40f8d509a538675930312772d2`; report `/var/tmp/keeper-oom/stream-core-rereview.md`.
- Canonical33-file candidate matches reviewed manifest; functional/race/vet and unchanged384/128MiB workload pass. This is not full-chain/0.5CPU acceptance.
- Attempted commit failed with `gpg: signing failed: Timeout`; no commit or push.33 reviewed files remain staged, generated `response-keeper.test` excluded. No unsigned-commit/authentication workaround used.
- Therefore task0.1 remains incomplete: new keeper image is not published/deployed. B implementation is isolated at `/var/tmp/keeper-route-b/source`; no production topology switch.

## B keeper candidate — independent review and routed capacity

- Isolated candidate `/var/tmp/keeper-route-b/source`; patch `/var/tmp/keeper-route-b/routing-b.patch`. Canonical/staged core unchanged.
- Independent reviewer `a041771c-4c5a-4e60-b07e-036faf6de3b7` approves2.1–2.5 and baseline-relative retention stripping, no new P1/P2. Report `/var/tmp/keeper-oom/keeper-routing-b-review.md`. Slow-drip lookup test confirms total1.6s deadline; focused race and HTTP/WS/owner/LKG tests pass.
- Task2.6 remains unchecked. Existing writer also consumes WS type, forces stream:true, wraps string input, maps null input to[], and normalizes outer keys/order/whitespace. Candidate adds no non-target value changes relative to base, but that does not satisfy literal original-wire preservation. Do not silently change this acceptance claim.
- Capacity frozen root `/var/tmp/keeper-b-capacity-jQlj9LPx`,39-file manifest, binary `3252f4979ae924620aba40d04cfc286cf2aeeaf649f7832ab5986bd7e056c047`. Parent independently reran `analyze_routed.py` and verified binary hash: pass.
- Routed mode active: near2/2, mixed208/208 including36initial+72replacement, dedup50/50.260 index and260 upstream receipts,73 successful pings; zero retries/errors/OOM. Original compiled fixture/source byte-identical; plumbing adds fake index/config and PID64 only. Report `/var/tmp/keeper-oom/keeper-routing-b-capacity.md`.
- Memory384MiB/no swap/GOMEMLIMIT256MiB/PID64. Peaks207.49/384.02/78.52MiB; mixed2591 limit events. Near peak+3.44MiB versus earlier accepted-core384MiB/128PID run; no observed completion regression, no statistical improvement/headroom claim.
- Index selected CPA for this load replay; AISIX/auth enforcement are functional tests, not separate load scenarios. CPU calibration still ineffective; no0.5CPU/full-production-chain acceptance.
- Gateway routing/integration3.1/3.3 underway only in `/var/tmp/gateway-route-b`; no production B topology change. Task0.1 publication/deployment remains blocked on signing.

## Task2.6 contract decision — bridge-writer semantics

Operator selected existing bridge semantics (2026-10-19). Supporting evidence, gathered locally after both research subagents failed on subscription quota:

- RFC 8259: a JSON object is an unordered collection of name/value pairs; key order/whitespace is not semantic.
- OpenAI WebSocket mode guide: `response.create` payload mirrors the HTTP create body "except that transport-specific fields like `stream` and `background` are not used". A WS→HTTP SSE bridge must therefore drop `type` and send `stream:true`.
- OpenAI prompt caching: hits require exact prompt prefix matches; caching depends on the rendered prompt, and keeper's deterministic writer gives the same output for the same input across turns.
- Sub2API ab6b00e `openai_gateway_request_body.go:1623–1680`: string `input` → `[{type:message,role:user,content}]`, empty string → `[]`, object → one-element array, forces `store:false` and `stream:true` on passthrough bodies.
- CPA `translator/codex/openai/responses/codex_openai-responses_request.go:19–20`: string input → user message with `input_text`; `codex_executor_stream.go:63–68`: deletes `previous_response_id`, `prompt_cache_retention`, `stream_options` and others.
- APISIX3.19 `ai-protocols/openai-responses.lua`: decodes body to Lua table and re-encodes (`core.json.encode`), so key order is not preserved; may append to string/array input.
- AISIX: serde typed request structures, re-serialized; not byte passthrough.
- LiteLLM (docs only, source not read): `docs.litellm.ai/docs/anthropic_unified/messages_to_responses_mapping` shows string user content mapped to `{type:message,role:user,content:[{type:input_text,text}]}`; `docs/completion/drop_params` documents `drop_params`/`additional_drop_params` removing unsupported OpenAI params from the request body. Consistent with the other gateways; LiteLLM source/byte behavior not independently verified.

Conclusion: keeper's rewrites are within what the deployed upstream gateways already do; Sub2API and CPA perform the same string-input wrap and field stripping downstream. Only `input:null`→`[]` is uncommon; Sub2API maps empty string to `[]`, and null input is not a meaningful request.

## Gateway wiring 3.1–3.3, deployment 6.1 — local state

- 3.1/3.3: route patch `/var/tmp/gateway-route-b/gateway-routing-b.patch` applied to canonical `apisix-models/apisix.yaml` and `config.yaml`; files byte-identical to the integration-tested candidate. Final run `/var/tmp/gateway-route-b/run-kdan070w/`: 35 exchanges, both aliases HTTP/SSE/WS direct to keeper, no Responses self hop, owner isolation, per-turn backend switch, paused-index LKG and cold-start 503, authoritative 404, 20 MiB and exact 32 MiB accepted, other routes keep 16 MiB. nginx `client_max_body_size` is raised to 32m with a native global `client-control` 16 MiB rule for non-Responses routes; both files must deploy and roll back together. 15-min LKG expiry covered by unit test, not waited in integration.
- 3.2: keeper in `compose.yaml` and private compose now routed (`ROUTING_INDEX_URL`, `UPSTREAM_CPA_URL`, `UPSTREAM_AISIX_URL`, `CPA_API_KEY` from `.env`), named volume `response-keeper-cache:/var/tmp/response-keeper`, `STORE_DIR`, `GOMEMLIMIT` default 256MiB; `UPSTREAM_URL` removed; `read_only`, `pids_limit: 64`, `mem_limit: 384m` kept; stale loopback comments replaced. YAML diff of private compose against the live production file: only `response-keeper` and top-level `volumes` differ. Private compose CPA block was reset to `mem_limit: 480m` to equal the production base file; the effective CPA 1g comes from the identical `compose.override.yaml` (production container reads 1073741824/2147483648). No CPA limit change.
- Named volume first-mount: a fresh Docker/Podman volume copies ownership from the image path. Keeper image is `FROM scratch` without `/var/tmp/response-keeper`, so the volume would be root-owned and unwritable for 65534. B candidate `Dockerfile` now pre-creates that directory `65534:65534 0700` in the builder and copies it with `--chown`. Local Podman check with a fresh volume, read-only root, PID64, 384 MiB/no swap: health ok, keeper wrote `keeper.db`, `keeper.lock`, `.keeper-spools`. This Dockerfile change came after the B review/capacity freeze (39-file manifest); Go sources are unchanged. It must be committed as a separate, explained change.
- 6.1: `ansible/ops.yml` tag `deploy-responses-path` plus `roles/ai_ops/tasks/verify-responses-path.yml`. Plan: four drift-gated file diffs, desired digest, protected map of every other running service. Apply: approved-digest gate → pull → snapshot (four files, keeper image selector, identities) under `.rollback/responses-path-<UTC>` → upload → one `up -d --force-recreate --no-deps --no-build --pull never response-keeper apisix-models` → verify (both recreated, image/revision, OOMKilled false, store mount is a volume, health, both aliases answered by keeper 404/400, no Responses access line to 127.0.0.1:9080, protected IDs unchanged) → restore snapshot and recreate on failure. `--syntax-check` passes; `--list-tags` shows it; no sub2api task; legacy tag not used.
- 6.2 first plan run against production (read-only): stopped at the compose drift gate, nothing uploaded, `changed=0`. Production `compose.yaml` differs from the Ansible baseline only by `cli-proxy-api.environment.GOMEMLIMIT: 700MiB`, i.e. the manual CPA hotfix of 2026-10-19 14:58Z. Adopting that drift needs an explicit decision; do not edit the baseline or relax the gate silently.
- Release: operator decision 2026-10-20 "不需要 gpg 你先做" — commit without signing. response-keeper `main` pushed `b74c72e..2d269bc` (all three unsigned, `%G?`=N): `a186532` core (exactly the 33 reviewed files, manifest match), `47e97c4` B routing (index matched the 39-file reviewed/capacity manifest excluding Dockerfile), `2d269bc` Dockerfile STORE_DIR pre-create. Local gofmt/vet/`go test -race` on the combined tree passed (31.3 s). CI `ci` run 39348675023 in progress at record time.
- Compose drift adopted (operator-approved): production `.ai-ops-state/compose.yaml.baseline` replaced by the live file; previous `0048f979…` kept as `compose.yaml.baseline.pre-cpa-adopt-20261020T003700Z`, adopted `aca0c9d1…`, receipt `receipt-compose-adopt-20261020T003700Z.txt`. Only difference was CPA `GOMEMLIMIT: 700MiB`.
- Model-directory Sub2API fix: operator chose not to proceed now; todo stays open.

## Production rollout — 2026-10-20

- Image: `ghcr.io/xz-dev/response-keeper:2d269bc11ec8c1b58040a8f03f3cd5763f5c41c1@sha256:74b3213b791952994785502357024fc79ed138ee68a541978a853efc9c1708de`. CI `ci` run 39348675023 success (gofmt, vet, race, amd64+arm64 image). Index digest verified by registry header and local hash; OCI revision label `2d269bc`, user `65534:65534`.
- Private `.env` equalled production except `RESPONSE_KEEPER_IMAGE` (old `b74c72e@sha256:4d83acbe…`).
- 6.2 plan (`/var/tmp/keeper-oom/responses-path-plan2.log`): rc=0, changed=0. Diffs only keeper block + top-level volume, apisix routes/body limits, nginx 32m; `routing_selector.lua` unchanged; 47 protected services.
- 6.3 apply (`/var/tmp/keeper-oom/responses-path-apply.log`): rc=0, ok=68 changed=8 failed=0 rescued=0. Snapshot `/root/AI-gateway/.rollback/responses-path-20261020T005738Z/` (compose, three apisix-models files, keeper image selector, identities). Receipt `.ai-ops-state/receipt-responses-path-1792458280.txt`: keeper `f21093a5…`→`32fb409d…`, apisix-models `9cbc2908…`→`edd50910…`, verified image/revision, OOMKilled false, store mount type volume, 47 protected IDs unchanged. Sub2API not recreated.
- Independent post-check: both healthy, restarts 0, OOM false, started 00:58:52Z. Volume `ai-gateway_response-keeper-cache` on ext4, root dir `65534:65534`, contains `keeper.db`, `keeper.lock`, `.keeper-spools`.
- First ~6 min of real traffic: apisix-models logged 28 Responses lines, all upstream `127.0.0.1:8085` (keeper), **zero to 127.0.0.1:9080**. Keeper 59 turns: WS 43×2xx + 2×4xx, HTTP 14×2xx, no 5xx. The two 404s are the deploy probes. 15 × 429 on `/rk/v1/responses` from the CPA backend; 22 × 429 in the previous 2 h before deploy, so not introduced by this change.
- `previous=false` is 45/45 WS turns here: the deployed Pi plugin does not yet carry D, and keeper state was cleared by the recreate. Not an outcome measure for 6.5.
- Rollback: restore the four files and `RESPONSE_KEEPER_IMAGE` from the snapshot, then `up -d --force-recreate --no-deps --no-build --pull never response-keeper apisix-models`.
- D: `openai-api-pi-extension` main `75f8e03..9b2684e` (unsigned per operator decision). Independent review: reviewer subagent failed on subscription quota; parent reviewed the diff and ran the full suite on the identical copy, 310/310 pass. Host-first load in the user's Pi requested from the plugin owner session; real HTTP/WS turns pending there.
- Not claimed: 24 h stability (6.4), `WS/full` ratio change (6.5 needs plugin rollout), CPU acceptance, model-directory latency.

## Acceptance check — 2026-10-21 02:08Z (read-only)

- 0.1 base: production keeper now runs `ghcr.io/xz-dev/response-keeper:df45c7f…@sha256:badb1a6c74e75341842774293b82361c6ea6c1d9cf153cbc73fde007098f386c`, the direct successor of reviewed `2d269bc` (`fix: serve turns uncached on storage failure and end failed turns`, pinned by AI-gateway `5784f95`). Named volume `ai-gateway_response-keeper-cache` mounted at `/var/tmp/response-keeper` (47 MB). Started 2026-10-20T04:34:57Z, restarts 0, OOMKilled false; cgroup `memory.events` max/oom/oom_kill all 0, `memory.peak` ≈ 60.7 MiB of the 384 MiB limit (GOMEMLIMIT 256 MiB). `sock_throttled` 2713 (socket memory pressure, not OOM).
- 5.1: independent reviewer `a041771c` approved keeper routing/credentials/owner with no P1/P2 (`/var/tmp/keeper-oom/keeper-routing-b-review.md`); gateway route patch is byte-identical to the integration-tested candidate.
- 6.4 interim (keeper logs 04:35Z→02:08Z ≈ 21.5 h; apisix-models logs only since its 13:19Z recreate ≈ 12.8 h, so not yet a full 24 h):
  - apisix-models: 4978 Responses requests, **all** upstream `127.0.0.1:8085` (keeper), **0** to `127.0.0.1:9080`; 5 × 503, **0** with the 127-byte selector signature (baseline 117/24 h).
  - keeper: 1 × 503 (baseline 179). Other errors: WS 400 × 91, 429 × 42, 502 × 52, 403 × 2; HTTP 502 × 3. 429/502 are upstream statuses passed through.
  - Host now: load 2.61/2.22/1.97, swap used 1472/4480 MiB, memory PSI some≈10 / full≈4, CPU PSI some≈35. No sysstat history, so no 24 h swap/load curve.
- 6.5 **not demonstrated**: WS `previous=false` share is 63.93% (4654 false / 2626 true) versus 56.27% baseline; the last three hours (2026-10-21T00–02Z) are 85–97% false. This cannot be attributed to D: plugin `9b2684e` was installed 2026-10-20 04:35Z, but five local Pi processes still running were started 2026-10-19 07:20Z and keep the pre-D plugin (`480f11b`); only later processes load D. Keeper logs carry no client identity and include other gateway clients, so the aggregate share measures a client mix, not D. `0dfd75d` was installed 2026-10-21 02:08Z and no running process has loaded it. 6.5 is **not measurable yet**, not shown to have failed. Measuring it needs all Pi processes restarted on the current plugin, then client-side `WS/full` vs `WS/continuation` transport-label counts (or a client-distinguishing keeper log field).

## Keeper production acceptance — 2026-10-21 02:25Z (read-only)

Verdict: **accepted with observations** for the keeper-oom production fix (image `df45c7f`, revision label matches, user `65534:65534`).

- Memory: cgroup `memory.max` 384 MiB, `memory.swap.max` 384 MiB; `memory.peak` 60.7 MiB, `memory.swap.peak` 27.8 MiB over ~21.8 h of real traffic; `memory.events` max/oom/oom_kill 0; restarts 0. Previous incident: 47 OOM kills in ~8.5 h at 96 MiB.
- Disk cache: named volume on host ext4, owned `nobody:nogroup`; `keeper.db` 43.5 MB with deduplicated shared content; store hit the configured 200 MiB live cap once, otherwise ~21 MB. Host disk 8.0 GB free.
- Load: 16 847 turns; duration p50 6.4 s, p95 32.8 s, p99 50.1 s, max 60.0 s (includes upstream generation time). `pids` 11/64.
- Bounded admission works as designed: 9 explicit `working-set wait exceeded` rejections (8 HTTP, 1 WS), no process death.
- Observation to explain (not a demonstrated fault): 2 660 `http relay ended early: context canceled` lines (~27% of HTTP turns), logged with status 200. apisix-models shows only 200/502/503 for the same path and no 499, so the cancellations did not surface as client-visible failures in gateway logs. Likely upstream-side cancellation of the keeper's relay; root cause not investigated.
- Not covered here: 0.5 CPU acceptance (out of scope), live disk-full/slow-disk behaviour (offline test #49).

## Authorization update

Existing-scope delivery approval verified from direct user messages `5aab6f6d`/`7f1db647` in session01a15472. Repeated per-step consent is no longer required for these existing tasks; verification, rollback, inference scope and authentication restrictions remain. D plugin repository currently has another release writer; wait for explicit ownership handoff before editing it.
