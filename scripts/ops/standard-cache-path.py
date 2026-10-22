#!/usr/bin/env python3
"""Guarded operations for the existing front-cache chain; never install networks."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

KEEPER = "openai-api-cache-keeper"
KEEPALIVE = "ai-sse-keepalive-proxy"
APPS = (KEEPER, "apisix-models", KEEPALIVE)
MUTABLE = set(APPS)
FILES = ("compose.yaml", "apisix/apisix.yaml", "apisix-models/apisix.yaml", "apisix-models/config.yaml", "apisix-models/routing_selector.lua")
GUARDS = FILES + ("compose.override.yaml", ".env")


class Refused(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def private_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)


class Operations:
    def __init__(self, root, project="ai-gateway", retries=20, delay=1, min_memory=200, min_disk=1024):
        self.root = Path(root).resolve()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project) or not 1 <= retries <= 60 or not 0 <= delay <= 10:
            raise Refused("invalid project or bounded verification settings")
        self.project, self.retries, self.delay = project, retries, delay
        if min_memory < 0 or min_disk < 0:
            raise Refused("capacity gates must not be negative")
        self.min_memory, self.min_disk = min_memory, min_disk

    def run(self, args, stdin=None, timeout=60):
        try:
            result = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Refused("runtime command unavailable or timed out") from error
        if result.returncode:
            # Docker diagnostics can include private environment/configuration.
            raise Refused("runtime command failed; inspect protected host diagnostics")
        return result.stdout

    def compose(self, *args, candidate=None, overlay=None):
        command = ["docker", "compose", "--project-directory", str(self.root), "--project-name", self.project,
                   "--env-file", str(self.root / ".env"), "-f", "-" if candidate is not None else str(self.root / "compose.yaml"),
                   "-f", str(self.root / "compose.override.yaml")]
        if overlay is not None:
            command += ["-f", str(overlay)]
        return self.run(command + list(args), stdin=candidate, timeout=180)

    def inspect(self, identity, image=False):
        args = ["docker", "image", "inspect", identity] if image else ["docker", "inspect", identity]
        return json.loads(self.run(args))[0]

    def ids(self):
        rows = self.run(["docker", "ps", "-a", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project,
                         "--format", '{{.Label "com.docker.compose.service"}} {{.ID}}']).decode().splitlines()
        pairs = [row.split() for row in rows]
        if any(len(row) != 2 for row in pairs) or len({row[0] for row in pairs}) != len(pairs):
            raise Refused("ambiguous project container identities")
        return dict(pairs)

    def networks(self):
        identities = self.run(["docker", "network", "ls", "--quiet", "--no-trunc", "--filter",
                               "label=com.docker.compose.project=" + self.project]).decode().split()
        if not identities:
            return {}
        records = json.loads(self.run(["docker", "network", "inspect", *identities]))
        names = [record.get("Labels", {}).get("com.docker.compose.network") for record in records]
        if not all(names) or len(set(names)) != len(records):
            raise Refused("ambiguous project network identities")
        return {name: {"id": record["Id"], "state": digest(encoded({key: record.get(key) for key in
                ("Driver", "Internal", "IPAM", "Options", "Containers")}))} for name, record in zip(names, records)}

    def files(self):
        if any((self.root / name).resolve() != self.root / name for name in GUARDS):
            raise Refused("managed configuration must not use symlinks")
        return {name: (self.root / name).read_bytes() for name in GUARDS}

    def baseline(self):
        files = self.files()
        ids = self.ids()
        if any(not ids.get(name) for name in (*APPS, "apisix", "ai-sse-keepalive-proxy-netns")):
            raise Refused("existing front-cache chain required; legacy migration and Operator installation are separate")
        if ids.get("response-keeper") and self.inspect(ids["response-keeper"])["State"]["Running"]:
            raise Refused("old inner keeper must be stopped")
        networks = self.networks()
        declared = json.loads(self.compose("config", "--format", "json")).get("networks", {})
        if set(declared) - set(networks):
            raise Refused("declared project networks must already exist; no network creation is allowed")
        states = {name: self.inspect(identity) for name, identity in ids.items()}
        return {"networks": networks, "files": {name: digest(data) for name, data in files.items()}, "containers": {
            name: {"id": identity, "image": states[name]["Image"],
                   "configuration": digest(encoded({key: states[name].get(key) for key in ("HostConfig", "Config", "Mounts")})),
                   "started": states[name]["State"].get("StartedAt"),
                   "running": states[name]["State"]["Running"], "restarts": states[name].get("RestartCount", 0)}
            for name, identity in ids.items()}}

    def specs(self, old, new):
        if {k: v for k, v in old.items() if k != "services"} != {k: v for k, v in new.items() if k != "services"}:
            raise Refused("network, namespace, volume or other top-level changes are out of scope")
        before, after = old["services"], new["services"]
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        if changed - {KEEPER, KEEPALIVE} or set(before) != set(after):
            raise Refused("only existing keeper/keepalive service definitions may change")
        if "response-keeper" in after:
            raise Refused("retired inner keeper is not supported")
        k, ka = after[KEEPER], after[KEEPALIVE]
        for name in (KEEPER, KEEPALIVE):
            if {key: value for key, value in before[name].items() if key not in {"image", "environment"}} != \
                    {key: value for key, value in after[name].items() if key not in {"image", "environment"}}:
                raise Refused("cache service mounts, namespace, lifecycle and isolation must remain unchanged")
        for service, memory, pids in ((k, 384, 64), (ka, 128, 128)):
            if str(service.get("mem_limit")).lower() not in (str(memory * 1024**2), str(memory) + "m") \
                    or float(service.get("cpus", 0)) != 0.5 or service.get("pids_limit") != pids \
                    or service.get("network_mode") != "service:ai-sse-keepalive-proxy-netns" \
                    or service.get("networks") or service.get("ports") or service.get("privileged") \
                    or not service.get("read_only") or set(service.get("cap_drop", [])) != {"ALL"} \
                    or service.get("cap_add") or "no-new-privileges:true" not in service.get("security_opt", []):
                raise Refused("cache service isolation or fixed resource limits changed")
        if str(k.get("memswap_limit")).lower() not in (str(384 * 1024**2), "384m") or str(k.get("user")) != "65534:65534":
            raise Refused("keeper must remain non-root with no swap")
        if ka.get("memswap_limit") != before[KEEPALIVE].get("memswap_limit"):
            raise Refused("keepalive swap policy must remain unchanged")
        ke, ae = k.get("environment", {}), ka.get("environment", {})
        if ke.get("UPSTREAM_URL") != "http://sub2api-ai-sse-relay:8080" \
                or ke.get("LISTEN_ADDR") != "127.0.0.1:8085" or ke.get("STORE_DIR") != "/var/tmp/openai-api-cache-keeper" \
                or str(ke.get("REQUIRE_NO_DEFAULT_ROUTE")) != "true" or str(ke.get("STORE_MAX_BYTES")) != "209715200" \
                or {"CPA_API_KEY", "ROUTING_INDEX_URL", "UPSTREAM_CPA_URL", "UPSTREAM_AISIX_URL"} & set(ke) \
                or ae.get("UPSTREAM_URL") != "http://127.0.0.1:8085" or ae.get("GOMEMLIMIT") != "96MiB" \
                or str(ae.get("MAX_INSPECT_BODY_BYTES")) != "8388608":
            raise Refused("standard shared chain or selected keepalive memory settings changed")
        if not any(v.get("type") == "volume" and v.get("target") == ke["STORE_DIR"] for v in k.get("volumes", [])):
            raise Refused("keeper requires a named disk volume")
        return sorted(changed)

    def images(self, config):
        result = {}
        for name in (KEEPER, KEEPALIVE):
            reference = config["services"][name]["image"]
            match = re.fullmatch(r"ghcr\.io/xz-dev/" + name + r":([0-9a-f]{40})@sha256:([0-9a-f]{64})", reference)
            if not match:
                raise Refused("both images require exact source-commit tags and index digests")
            result[name] = {"reference": reference, "revision": match[1]}
        return result

    def snapshot(self, name):
        if not re.fullmatch(r"standard-cache-\d{8}T\d{6}Z-[0-9a-f]{8}", name):
            raise Refused("select a snapshot created by this scoped tool")
        path = self.root / ".rollback" / name
        if path.resolve() != path:
            raise Refused("snapshot must not use symlinks")
        manifest = json.loads((path / "manifest.json").read_bytes())
        if manifest.get("version") != 1 or manifest.get("project") != self.project or manifest.get("root") != str(self.root):
            raise Refused("snapshot root/project/version mismatch")
        if set(manifest["old_files"]) != set(GUARDS) or set(manifest["post_files"]) != set(GUARDS) or set(manifest["old_images"]) != set(APPS):
            raise Refused("incomplete snapshot")
        for file in GUARDS:
            if digest((path / "old" / file).read_bytes()) != manifest["old_files"][file]:
                raise Refused("snapshot content checksum mismatch")
        if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in manifest["old_images"].values()):
            raise Refused("snapshot images must be exact local content identities")
        if (path / "images.json").read_bytes() != encoded({"services": {service: {"image": image} for service, image in manifest["old_images"].items()}}):
            raise Refused("snapshot image overlay checksum mismatch")
        return path, manifest

    def plan(self, operation, payload, snapshot=""):
        baseline = self.baseline()
        report = {"operation": operation, "baseline": baseline, "changed_files": [], "images": {}, "snapshot": snapshot,
                  "scope": "existing primary chain only; non-noop deploy recreates keeper/inner APISIX/keepalive; outer APISIX reload only",
                  "policy": {"retries": self.retries, "delay": self.delay, "min_memory_mib": self.min_memory, "min_disk_mib": self.min_disk}}
        if operation == "deploy":
            if set(payload) != set(GUARDS):
                raise Refused("adopt all guarded private files before planning")
            if any(digest(payload[name]) != baseline["files"][name] for name in (".env", "compose.override.yaml")):
                raise Refused("private environment and override must match live bytes")
            if b"__CPA_API_KEY__" in payload["apisix-models/apisix.yaml"]:
                raise Refused("render the private CPA placeholder before deployment")
            if any(b"/rk/" in payload[name] or b"upstream_id: response-keeper" in payload[name] for name in FILES[1:]):
                raise Refused("private recovery aliases and the old inner keeper are forbidden")
            old = json.loads(self.compose("config", "--format", "json"))
            new = json.loads(self.compose("config", "--format", "json", candidate=payload["compose.yaml"]))
            report["service_delta"] = self.specs(old, new)
            report["images"] = self.images(new)
            report["candidate_files"] = {name: digest(data) for name, data in payload.items()}
            report["changed_files"] = [name for name in FILES if report["candidate_files"][name] != baseline["files"][name]]
        elif operation == "rollback":
            _, manifest = self.snapshot(snapshot)
            if baseline["files"] != manifest["post_files"]:
                raise Refused("post-deploy files drifted; rollback would overwrite independent work")
            report["snapshot_hash"] = digest(encoded(manifest))
        elif operation != "restart":
            raise Refused("unknown operation")
        report["approved_plan"] = digest(encoded(report))
        return report

    def ready(self, name, identity):
        command = ["docker", "exec", identity]
        if name in ("apisix", "apisix-models"):
            path, status = ("/v1/models", "404") if name == "apisix" else ("/healthz", "200")
            command += ["/bin/bash", "-ec", "exec 3<>/dev/tcp/127.0.0.1/9080; printf 'GET " + path + " HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3; IFS=' ' read -r protocol code rest <&3; [[ \"$protocol\" == HTTP/* && \"$code\" == " + status + " ]]"]
        else:
            command += ["/" + name, "healthcheck"]
        for attempt in range(self.retries):
            state = self.inspect(identity)["State"]
            if state.get("OOMKilled"):
                raise Refused("cache-chain container OOM-killed")
            try:
                if state["Running"]:
                    self.run(command, timeout=5)
                    return
            except Refused:
                pass
            if attempt + 1 < self.retries:
                time.sleep(self.delay)
        raise Refused("bounded service readiness failed")

    def protected(self, baseline):
        return {name: state["id"] for name, state in baseline["containers"].items() if name not in MUTABLE}

    def verify(self, expected, protected, keepalive_swap, networks):
        if self.networks() != networks:
            raise Refused("protected network identity or attachment changed")
        ids = self.ids()
        if any(ids.get(name) != identity for name, identity in protected.items()):
            raise Refused("protected container identity changed")
        for name in APPS:
            state = self.inspect(ids[name])
            if state["Image"] != expected[name]:
                raise Refused("running image does not match exact approved identity")
            self.ready(name, ids[name])
        k, ka = (self.inspect(ids[name]) for name in (KEEPER, KEEPALIVE))
        for state, memory, pids in ((k, 384, 64), (ka, 128, 128)):
            host = state["HostConfig"]
            if host["Memory"] != memory * 1024**2 or host["NanoCpus"] != 500000000 or host["PidsLimit"] != pids:
                raise Refused("runtime resource readback mismatch")
        if k["HostConfig"]["MemorySwap"] != 384 * 1024**2 or ka["HostConfig"]["MemorySwap"] != keepalive_swap \
                or k["HostConfig"]["NetworkMode"] != "container:" + ids["ai-sse-keepalive-proxy-netns"] \
                or k["HostConfig"]["NetworkMode"] != ka["HostConfig"]["NetworkMode"]:
            raise Refused("runtime swap or shared namespace mismatch")
        ke, ae = ({item.split("=", 1)[0]: item.split("=", 1)[1] for item in state["Config"].get("Env", []) if "=" in item} for state in (k, ka))
        if ke.get("STORE_MAX_BYTES") != "209715200" or ke.get("UPSTREAM_URL") != "http://sub2api-ai-sse-relay:8080" \
                or ae.get("UPSTREAM_URL") != "http://127.0.0.1:8085" or ae.get("GOMEMLIMIT") != "96MiB" \
                or ae.get("MAX_INSPECT_BODY_BYTES") != "8388608":
            raise Refused("runtime chain or memory environment readback mismatch")
        mount = next((m for m in k["Mounts"] if m["Destination"] == "/var/tmp/openai-api-cache-keeper"), {})
        if mount.get("Type") != "volume" or not mount.get("RW"):
            raise Refused("keeper disk mount missing")
        filesystem = self.run(["stat", "-f", "-c", "%T", mount["Source"]]).decode().strip()
        if filesystem in ("tmpfs", "ramfs"):
            raise Refused("keeper cache must not be memory-backed")
        for name, identity in ids.items():
            if name != KEEPER and any(m.get("Source") == mount["Source"] for m in self.inspect(identity).get("Mounts", [])):
                raise Refused("keeper store is shared with another history owner")
        if ids.get("response-keeper") and self.inspect(ids["response-keeper"])["State"]["Running"]:
            raise Refused("old inner keeper became active")
        self.ready("apisix", ids["apisix"])

    def write_live(self, name, data, written):
        # Keep bind-mount inodes and account for our exact prefix on ENOSPC.
        descriptor = os.open(self.root / name, os.O_RDWR | os.O_NOFOLLOW)
        try:
            os.ftruncate(descriptor, 0)
            written[name] = digest(b"")
            offset = 0
            while offset < len(data):
                count = os.write(descriptor, data[offset:])
                if not count:
                    raise Refused("configuration write made no progress")
                offset += count
                written[name] = digest(data[:offset])
        finally:
            os.close(descriptor)

    def activate(self, overlay=None):
        for name in APPS:
            self.compose("up", "-d", "--force-recreate", "--no-deps", "--no-build", "--pull", "never", name, overlay=overlay)
            self.ready(name, self.ids()[name])

    def restore(self, path, manifest, protected, keepalive_swap, networks, written=None):
        if self.snapshot(path.name)[1] != manifest:
            raise Refused("snapshot changed before restoration")
        files = {name: digest(data) for name, data in self.files().items()}
        if files != (written if written is not None else manifest["post_files"]):
            raise Refused("rollback refused: independent file drift")
        current = self.ids()
        if self.networks() != networks:
            raise Refused("rollback refused: protected network drift")
        if any(current.get(name) != identity for name, identity in protected.items()):
            raise Refused("rollback refused: protected runtime drift")
        for image in manifest["old_images"].values():
            if self.inspect(image, image=True)["Id"] != image:
                raise Refused("exact rollback image unavailable locally")
        self.run(["docker", "stop", "--time", "10", self.ids()[KEEPALIVE]], timeout=30)
        for name in FILES:
            self.write_live(name, (path / "old" / name).read_bytes(), {})
        self.activate(overlay=path / "images.json")
        self.run(["docker", "exec", self.ids()["apisix"], "apisix", "reload"])
        self.verify(manifest["old_images"], protected, keepalive_swap, networks)
        if {name: digest(data) for name, data in self.files().items()} != manifest["old_files"]:
            raise Refused("restored file checksum mismatch")

    def execute(self, operation, mode, payload, approved="", snapshot=""):
        plan = self.plan(operation, payload, snapshot)
        if mode == "plan":
            return {"changed": False, "plan": plan}
        if mode != "apply" or not re.fullmatch(r"[0-9a-f]{64}", approved) or approved != plan["approved_plan"]:
            raise Refused("apply requires the exact reviewed approved_plan")
        # Plan never creates state or acquires a writable lock.
        with os.fdopen(os.open(self.root / ".standard-cache-operations.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.plan(operation, payload, snapshot) != plan:
                raise Refused("state changed after planning")
            protected = self.protected(plan["baseline"])
            networks = plan["baseline"]["networks"]
            ids = self.ids()
            swap = self.inspect(ids[KEEPALIVE])["HostConfig"]["MemorySwap"]
            if operation == "restart":
                identity = ids[KEEPER]
                image = self.inspect(identity)["Image"]
                self.run(["docker", "restart", "--time", "10", identity], timeout=40)
                self.ready(KEEPER, identity)
                if self.ids() != ids or self.inspect(identity)["Image"] != image or self.networks() != networks:
                    raise Refused("keeper-only restart identity changed")
                return {"changed": True, "operation": "restart", "keeper": identity, "cache_state": "invalidated"}
            if operation == "rollback":
                path, manifest = self.snapshot(snapshot)
                self.restore(path, manifest, protected, swap, networks)
                return {"changed": True, "operation": "rollback", "snapshot": snapshot}
            if not plan["changed_files"]:
                expected = {name: self.inspect(ids[name])["Image"] for name in APPS}
                for name, image in plan["images"].items():
                    found = self.inspect(image["reference"], image=True)
                    if found["Id"] != expected[name] or found["Config"]["Labels"].get("org.opencontainers.image.revision") != image["revision"]:
                        raise Refused("unchanged config has unexpected running image")
                self.verify(expected, protected, swap, networks)
                return {"changed": False, "operation": "deploy"}
            available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:")))
            if available < self.min_memory * 1024 or shutil.disk_usage(self.root).free < self.min_disk * 1024**2:
                raise Refused("host capacity below configured deployment gate")
            expected = {"apisix-models": self.inspect(ids["apisix-models"])["Image"]}
            for name, image in plan["images"].items():
                self.run(["docker", "pull", image["reference"]], timeout=300)
                found = self.inspect(image["reference"], image=True)
                if found["Config"]["Labels"].get("org.opencontainers.image.revision") != image["revision"]:
                    raise Refused("OCI source revision does not match approved commit tag")
                expected[name] = found["Id"]
            if self.plan(operation, payload, snapshot) != plan:
                raise Refused("state changed during image prefetch")
            name = "standard-cache-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
            path = self.root / ".rollback" / name
            if path.resolve() != path:
                raise Refused("snapshot must not use symlinks")
            frozen = self.files()
            if {file: digest(data) for file, data in frozen.items()} != plan["baseline"]["files"]:
                raise Refused("files changed before snapshot capture")
            path.mkdir(parents=True, mode=0o700)
            manifest = {"version": 1, "root": str(self.root), "project": self.project,
                        "old_files": plan["baseline"]["files"], "post_files": plan["candidate_files"],
                        "old_images": {service: self.inspect(ids[service])["Image"] for service in APPS}}
            for file, data in frozen.items():
                private_file(path / "old" / file, data)
            private_file(path / "manifest.json", encoded(manifest))
            private_file(path / "images.json", encoded({"services": {service: {"image": image} for service, image in manifest["old_images"].items()}}))
            private_file(path / "desired-images.json", encoded({"services": {service: {"image": image} for service, image in expected.items()}}))
            if self.plan(operation, payload, snapshot) != plan:
                raise Refused("state changed immediately before activation")
            written = dict(plan["baseline"]["files"])
            try:
                self.run(["docker", "stop", "--time", "10", ids[KEEPALIVE]], timeout=30)
                for file in FILES:
                    self.write_live(file, payload[file], written)
                for plane in ("apisix", "apisix-models"):
                    self.run(["docker", "exec", ids[plane], "apisix", "test"])
                self.activate(overlay=path / "desired-images.json")
                if "apisix/apisix.yaml" in plan["changed_files"]:
                    self.run(["docker", "exec", self.ids()["apisix"], "apisix", "reload"])
                self.verify(expected, protected, swap, networks)
                if {file: digest(data) for file, data in self.files().items()} != plan["candidate_files"]:
                    raise Refused("files changed during activation")
                receipt = {"changed": True, "operation": "deploy", "snapshot": name, "images": expected,
                           "protected_count": len(protected), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                private_file(path / "receipt.json", encoded(receipt))
                return receipt
            except Exception as error:
                try:
                    self.restore(path, manifest, protected, swap, networks, written=written)
                except Exception as rollback_error:
                    raise Refused("activation failed; rollback not verified; operator recovery snapshot=" + name) from rollback_error
                raise Refused("activation failed; exact snapshot rollback verified; snapshot=" + name) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("deploy", "restart", "rollback"))
    parser.add_argument("--mode", choices=("plan", "apply"), default="plan")
    parser.add_argument("--root", required=True)
    parser.add_argument("--project", default="ai-gateway")
    parser.add_argument("--approved-plan", default="")
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--retries", type=int, default=20)
    parser.add_argument("--delay", type=float, default=1)
    parser.add_argument("--min-memory", type=int, default=200)
    parser.add_argument("--min-disk", type=int, default=1024)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        data = json.load(sys.stdin) if args.operation == "deploy" else {}
        payload = {name: base64.b64decode(value, validate=True) for name, value in data.items()}
        ops = Operations(args.root, args.project, args.retries, args.delay, args.min_memory, args.min_disk)
        print(json.dumps(ops.execute(args.operation, args.mode, payload, args.approved_plan, args.snapshot), sort_keys=True))
    except Exception as error:
        print(json.dumps({"error": str(error) if isinstance(error, Refused) else type(error).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
