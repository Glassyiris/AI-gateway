## Context

Direction on a Docker internal network is decided by two things: which process holds a listening socket, and on which interface. An edge is one-way only when the initiator holds no listener reachable from the target, and the relay's listener cannot be reached from the target side.

## Decisions

- **Reuse the existing relay shape.** Each AISIX edge copies `apisix-models-cpa-relay`: `alpine/socat` running as 65534, read-only, no capabilities, `no-new-privileges`, with a source network and a target network. New subnets are .60/.61 (Sub2API), .64/.65 (enricher), .70/.71 (apisix-models) and .62/.63 (AISIX→CPA). The source subnets and source-side addresses are unchanged, so `aisix:3000` still resolves to 172.30.60.3/64.3/70.3. Those addresses now belong to relays.
- **Bind every listener explicitly** instead of filtering with firewall rules. This keeps the existing portability constraint: no host firewall and no engine-specific options. Verified with rootless Podman (published port to a socat bound to a specific IP) and with AISIX 1.6.0, the production digest.
- **AISIX** uses `proxy.listeners` (supported in both 1.5.0 and 1.6.0). `proxy.addr` stays because it is required, but it is ignored when `listeners` is set. Metrics move from the upstream default `0.0.0.0:9090` to `172.30.68.2:9090`. `aisix-status` already derives `<admin host>:9090`, so it needs no change. The pprof listener stays on its upstream default, loopback `127.0.0.1:9091`.
- **APISIX** pins `node_listen` to the relay-target addresses plus `127.0.0.1` (used by healthchecks and the `self` upstream), and sets `ssl.enable: false`. The offline `apisix test` gets `net.ipv4.ip_nonlocal_bind=1` because those addresses exist only in production networks. Validation renders `nginx.conf` and rejects any listener that is not loopback, a 172.30 :9080 address, or a unix socket.

## Residual risk (accepted)

A relay can connect to anything on its own target network: the backend it fronts. It can also reach the listeners its own source-side endpoint exposes on that source subnet. Those are now only the explicitly bound service addresses, which in practice means the backend port of a peer the relay already serves. Relays run a fixed socat command with no shell entrypoint, so exploiting this requires compromising socat itself.

## Rollout

Namespace-owner network membership changes for AISIX, Sub2API, CPA, the enricher and apisix-models. Per README Operations, this requires one coupled full-stack recreation, not per-service recreation, followed by removal of the obsolete direct networks.
