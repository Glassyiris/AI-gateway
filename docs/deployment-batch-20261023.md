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

## Remaining steps

Models-enricher image publication/deployment, updated recovery guard, and keeper `24h / 10000 / 0` observation are still pending at this checkpoint. Their source CI success is not production acceptance.

## Limits

No second VM boot, real rollback drill, formal 0.5-core CPU acceptance, long soak, or standards certification. Messages remains disabled; Sub2API, account modes, slash routing and keeper LRU are unchanged. Keeper startup still clears aliases; later long TTL will not survive a process restart. The production observation must have stop thresholds and an explicit owner before being armed.
