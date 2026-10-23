# Controlled gateway rollout — 2026-10-23

One production writer, no VM reboot. Order: bridge → models-enricher → recovery guard → keeper experiment. Each step requires fresh configuration/container identity reads, protected backup, narrow activation, readback, bounded real continuation checks, and exact-tip CI before the next step. Local ignored `ansible/private-config` is the push-model source; the host Git checkout is not pulled or reset.

## Bridge — deployed

- Source: `responses-ws-sse-bridge` `83bb9819a5afbda0dbf0c6c7af998d92de82adae`; index digest `sha256:9b5c28879f521bd52d13183a76fc7625271c317b07e571a5f5a945088c760fb6`. Running OCI revision matches. `PORT=8086` stays aligned with `LISTEN_ADDR=127.0.0.1:8086`; the new binary probes HTTP `/healthz`, not a shared-namespace TCP listener.
- Ansible front-component plan approved with digest `489423bb67fe1a9b8b477c1902d735582c3a1ea99b8c985d2fd4ad5d886cf815`. Apply succeeded; bridge-only replacement started **05:20:42 UTC**. All other 54 project containers retained their identities and start times. Existing sockets closed during this authorized interruption; a keeper `502 stored=false` at 05:20:41 is not part of the earlier 04:47–04:58 upstream-rate-limit event.
- Private snapshot: `/root/AI-gateway/.ai-ops-state/front-responses-ws-sse-bridge-jjkkoq9r` (0700 directory, 0600 files). Explicit guarded rollback uses a fresh `--tags rollback -e service=responses-ws-sse-bridge -e ai_ops_mode=plan` and separately approved digest. No production rollback drill was run.
- Readback **05:22:04 UTC**: bridge/keeper/keepalive/public APISIX healthy, no restart-count/OOM change; explicit bridge CLI healthcheck exit 0; `ai-gateway.service` active/success; full-stack Compose dry-run has no create/recreate/start/pull/build pending.
- Four real calls, zero retries: bare `gpt-6.1-sol` SSE marker plus parent+delta recall; slash `codex/gpt-6-sol` WS marker plus parent+delta recall on a **new socket**. All terminal statuses `completed`, both exact recalls passed. Keeper 05:22:47/49 HTTP and 05:22:51/05:23:01 WS records have `stored=true`, paired `previous=false/true`. Sub2API request IDs: `23b58601-e4d8-44fc-afe1-42f9652b241e`, `a3eca95c-4022-4b40-88b1-1bede6829118`, `9ed388f4-18f2-4b92-953d-d2aed51612b6`, `b52ec2d6-fa56-4e5d-9e1c-8ae7f26aa179`. Bare GPT avoided repeating the recently rate-limited Opus channel; no fallback/retry was used.
- Production/private-config SHA256: compose `b627271ab5fa8cb52588d2ffc3b8e109caec21afdf8e451656d9b06980f370bd`; override `b3f46dcb7c8284845ecf247b1f225c4e7dff08684aa4b09d9e68822249ae6521`; `.env` `0934ac93dee1028d0198fefdda6fd546f5d7e8ab67bf7cb020cc4f657827607b`. This three-file equality does not claim that every managed file was checked.
- Local topology regression 11/11 and `git diff --check` pass. First smoke harness launch lacked Python `websockets` and made **zero** calls; rerun used the existing cached dependency via `uv run --offline --with websockets`, not a production retry. Private evidence: `/var/tmp/deploy-batch-20261023/bridge-*`.

## Models-enricher — deployed

- Published on main `68a37627e1649b50c966c0c886a17679bd308f77` by [39905857374](https://github.com/xz-dev/AI-gateway/actions/runs/39905857374). Its `models-enricher` tree equals the approved `af3376d` tree (`79b3c30e75d4285be051970a06269cbdd89bd60b`); workflow blob also matches (`0ba4e3c495e4d24a3616009fd71150cea4f22c3b`). Build context is only `./models-enricher`; no other source tree is copied. No extra release refs were created.
- Pin: `ghcr.io/xz-dev/ai-gateway-models-enricher:scheduled-refresh-af3376dc95c5@sha256:432e88475f0131475caac054a9d8731bf1aedc119f40ab1389739c8294949310`. GHCR index, linux/amd64 manifest/config SHA256 and source revision were independently read back. This workflow publishes amd64, not a newly claimed dual-architecture image.
- Existing generic Ansible path used; only this container changed. Readback **06:00:38 UTC**: all 54 neighbours unchanged; health and actual `/readyz`-based CLI probe pass; running OCI revision is `68a3762`. Existing production YAML preserved with SHA256 `8a71b24832525e4964e3266b58954e3193dfb4b6a9b6bf0d745c59ed85e4ad2e`; defaults 30m schedule / 10m entry window apply without overwriting private settings. Three-file equality verified: compose/override unchanged from bridge; `.env` now `dbba913363c27346fb44d3befb612a44566fc770842f7548dcf5639dbd3021d6`.
- Single local `:9083/models-table` GET at **05:59:42 UTC**: HTTP200, 334.894ms, last success `05:58:54Z`, next schedule `06:28:44Z`. This is a warm-page production measurement, not a cold-page or scheduled-tick acceptance. Cold/nonblocking/manual/single-flight behaviour has source-level integration evidence in the Lane B receipt; no extra refresh POST was sent here.
- Three public `/v1/models` GETs, zero retries: **4.525s / 0.390s / 0.702s**, all HTTP200 / 361 models. A short checkpoint, not p95 or proof every Pi discovery meets its deadline.
- Four real inference calls, zero retries: same bare GPT SSE and slash Codex WS new-socket marker/parent-delta pairs, all `completed` with exact recall. Keeper HTTP 06:00:46/48 and WS 06:00:51/56 are `stored=true`, paired `previous=false/true`; Sub2API request IDs `8ea020f1-db3f-4171-b4e5-823ec1b87f8e`, `5ad26c10-4faf-4ecf-a0a3-3ecb2c7dae93`, `be93057b-0fcd-40d3-ac86-cb9a8526c9b5`, `ba15f5b7-2eb4-4564-8a0c-915b82675953`.

### Two stopped apply attempts, restored before activation

First apply uploaded the candidate `.env` but failed before pull: a space-bearing `-e key=value` command override was parsed as `env` alone. CAS proved all 55 containers unchanged. Only `.env` was restored; its prematurely updated generic baseline was backed up and restored under separate supervisor approval. The second apply used the correct default `docker compose`, but existing `pull_policy: never` made `compose pull` return success without fetching; `up --pull never` correctly refused the missing image. Again all 55 containers were unchanged, and `.env` plus its baseline were CAS-restored before continuing. Neither attempt sent inference smoke or recreated a service.

Supervisor-approved minimal fixes: `9e9a8b0` forbids implicit pulls during generic activation (CI [39906312680](https://github.com/xz-dev/AI-gateway/actions/runs/39906312680) success); `c50ac22` forces the explicit pull branch to `pull --policy always` (CI [39908152120](https://github.com/xz-dev/AI-gateway/actions/runs/39908152120) success). The preloaded/`pull:false` branch still requires an existing local image and never pulls. Production Compose v2.40.3 supports `--policy`. Regression was red before each fix; topology 12/12 and full `scripts/validate.sh .env.example` pass with an explicit empty test override, leaving ignored operator override untouched. Initial local validate hit that unrelated private override's missing variable; it was not a source failure.

Third apply, explicitly capped as the last allowed attempt, succeeded after a fresh Ansible `config --quiet` argv proof, plan and external hash/container CAS. The legacy profile has no approval digest; no front-profile digest is invented for it. Group backup and receipts: `/var/tmp/deploy-batch-20261023-enricher-before` (0700/0600); local evidence `/var/tmp/deploy-batch-20261023/enricher-*`.

### Generic profile follow-ups (not fixed here)

- Plan does not exercise/validate the actual pull/activation command; a valid plan alone did not catch a malformed command override.
- File baselines advance before pull/recreate succeed; failed apply can leave a baseline describing undeployed configuration.
- Group-transaction CAS, automatic readiness/digest/image-present checks and all-neighbour verification are still absent from this legacy profile. This batch supplied external CAS, grouped private backups, image/revision/readiness and neighbour readback; they are not newly guaranteed by generic Ansible.

## Recovery guard — deployed

- `deploy-recovery` plan changed only `gateway-recovery.py`; apply succeeded. Private pre-operation four-file plus Compose snapshot: `/var/tmp/deploy-batch-20261023-recovery-before` (0700/0600), local receipt `/var/tmp/deploy-batch-20261023/recovery-rollback-receipt.json`. Guard SHA256 `8235b2b9c60ee5c7c56313ef0722f36e0bec29676caaaf8fde277c7c6a7cee90`; shared role probes SHA256 unchanged `3db2597b01e5a43218c769e17d526c314aca58543d8efbfd30fb222e115ebc8b`; unit files unchanged.
- Actual timer cycle **06:09:29 UTC** reports eight application checks, all `ok=true`, including bridge8086. `startup_unit` reports `active`, `ok=true`, `action=observe_only`; timer is enabled/active. Coverage is 55 running / 55 observed, including the standalone relay; this count is not a claim that provider dependencies are all ready. No restart action was taken.
- At 06:09:53 all 55 project-container identities and all three Compose hashes are unchanged; production/private-config still equal. Recovery regression 22/22 passes.
- Four real smoke calls, zero retries, all completed with exact SSE/WS new-socket recall. Keeper HTTP 06:10:01/03 and WS 06:10:06/08 each show `stored=true`, `previous=false/true`; correlated Sub2API IDs `863f2aec-37f4-4947-bc83-e21888396b20`, `c54639cc-974a-407e-9aff-dff200ac1ff1`, `9bd10322-a7aa-4c77-b152-93dcfbebd30f`, `162a5822-52df-490d-a065-f2b172637941`. Concurrent unrelated traffic was not counted as smoke.

## Remaining steps

Keeper `24h / 10000 / 0` deployment and remote observation are still pending at this checkpoint. The local notifier was explicitly dropped before implementation; none was built or armed. Source CI success is not production acceptance.

## Limits

No second VM boot, real rollback drill, formal 0.5-core CPU acceptance, long soak, or standards certification. Messages remains disabled; Sub2API, account modes, slash routing and keeper LRU are unchanged. Keeper startup still clears aliases; later long TTL will not survive a process restart. The production observation must have stop thresholds and an explicit owner before being armed.
