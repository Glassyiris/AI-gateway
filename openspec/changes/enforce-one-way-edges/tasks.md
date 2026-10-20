## 1. Repository

- [x] 1.1 Replace the four direct AISIX networks with `*-aisix-relay` source/target pairs (public and private compose), preserving `aisix`/`cpa` names
- [x] 1.2 Add `bind=` to every host-ingress relay and `aisix-status-relay`
- [x] 1.3 AISIX `proxy.listeners` on 172.30.61.2/65.2/71.2:3000, metrics on 172.30.68.2:9090 (example and private config); verified with AISIX 1.6.0 that no wildcard listener remains
- [x] 1.4 APISIX `node_listen` pinned to relay targets plus loopback and `ssl.enable: false` for both data planes; catalog bridge listens on 172.30.41.3
- [x] 1.5 `validate.sh`: remove direct-edge exception; require relay `bind=`, explicit AISIX listener set and rendered APISIX listeners; negative cases rejected (no-bind relay, wildcard AISIX, TLS/wildcard APISIX)
- [x] 1.6 `test-socat-boundary.sh`: target cannot reach the relay listener
- [x] 1.7 README security model and validation contract updated
- [x] 1.8 Replace private `0.0.0.0:9083:9080` publication with loopback plus the explicit `TAILSCALE_IP`; validate effective short/long-form bindings and reject wildcard, omitted-host and public-IP variants

## 2. Production (separately approved window)

Operator decision (2026-10-20): include the 9083 binding fix in the whole-stack rollout; do not deploy the diagnostics host namespace/relay separately. All repository checkmarks above mean implemented/tested, not live.

- [ ] 2.1 Snapshot compose, override, `.env`, `aisix/config.yaml`, both APISIX configs and the catalog template into `.rollback/one-way-edges-<ts>/`, and record the container ID map
- [ ] 2.2 Upload files, then run one coupled `up -d --force-recreate --remove-orphans` (including the provider-sidecar trio); remove the four obsolete networks afterwards
- [ ] 2.3 Verify: all services healthy, 0 OOM; the all-pairs reachability script shows only forward relay edges plus the documented residual; Responses go through keeper; one Sub2API→AISIX request succeeds; status page and metrics badge work; host 9083 listens only on loopback and the chosen Tailscale IP, never wildcard
- [ ] 2.4 Rollback on failure: restore the snapshot and run the coupled recreate again
