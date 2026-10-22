#!/usr/bin/env python3
"""Offline transaction regressions; no Docker daemon, SSH or inference."""
import base64
import copy
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cache_ops", ROOT / "scripts/ops/standard-cache-path.py")
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


def reference(name, revision):
    return "ghcr.io/xz-dev/" + name + ":" + revision * 40 + "@sha256:" + revision * 64


class Runtime:
    def __init__(self, root, config):
        self.root, self.commands, self.images, self.containers = root, [], {}, {}
        self.failed_health, self.fail_rollback, self.drift_on_pull = False, False, False
        self.generations = 0
        self.networks = {name: {"Id": "net-" + name, "Labels": {"com.docker.compose.network": name}, "Driver": "bridge", "Internal": True,
                                "IPAM": {}, "Options": {}, "Containers": {}} for name in config.get("networks", {})}
        for name in config["services"]:
            ref = config["services"][name].get("image", "image-" + name)
            self.add_image(ref, "a" if name in (ops.KEEPER, ops.KEEPALIVE) else "b")
            self.create(name, ref, config)
        for name in (ops.KEEPER, ops.KEEPALIVE):
            self.add_image(reference(name, "c"), "c")
        for state in self.containers.values():
            state["HostConfig"]["NetworkMode"] = "container:" + self.containers["ai-sse-keepalive-proxy-netns"]["Id"]

    def add_image(self, ref, revision):
        identity = "sha256:" + ops.digest((ref + revision).encode())
        image = {"Id": identity, "Config": {"Labels": {"org.opencontainers.image.revision": revision * 40}}}
        self.images[ref] = self.images[identity] = image

    def create(self, name, ref, config):
        self.generations += 1
        identity = name + "-" + str(self.generations)
        memory = 384 if name == ops.KEEPER else 128
        state = {"Id": identity, "Image": self.images[ref]["Id"], "Config": {"Image": ref, "Env": [key + "=" + str(value) for key, value in config["services"][name].get("environment", {}).items()]}, "RestartCount": 0,
                 "State": {"Running": True, "OOMKilled": False, "StartedAt": identity},
                 "HostConfig": {"Memory": memory * 1024**2, "MemorySwap": (384 if name == ops.KEEPER else 256) * 1024**2,
                                "NanoCpus": 500000000, "PidsLimit": 64 if name == ops.KEEPER else 128, "NetworkMode": "container:" + self.containers.get("ai-sse-keepalive-proxy-netns", {}).get("Id", "pending")},
                 "Mounts": [{"Destination": "/var/tmp/openai-api-cache-keeper", "Source": "/disk/cache", "Type": "volume", "RW": True}] if name == ops.KEEPER else []}
        self.containers[name] = state

    def __call__(self, args, stdin=None, timeout=60):
        self.commands.append(list(args))
        if args[0] == "stat":
            return b"ext2/ext3\n"
        if args[:2] == ["docker", "version"]:
            return b"29.0.0\n"
        if args[:3] == ["docker", "network", "ls"]:
            return "\n".join(network["Id"] for network in self.networks.values()).encode()
        if args[:3] == ["docker", "network", "inspect"]:
            return json.dumps([network for network in self.networks.values() if network["Id"] in args[3:]]).encode()
        if args[:2] == ["docker", "ps"]:
            return "\n".join(name + " " + state["Id"] for name, state in self.containers.items()).encode()
        if args[:3] == ["docker", "image", "inspect"]:
            if args[3] not in self.images:
                raise ops.Refused("missing exact image")
            return json.dumps([self.images[args[3]]]).encode()
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([next(state for state in self.containers.values() if state["Id"] == args[2])]).encode()
        if args[:2] == ["docker", "pull"]:
            if self.drift_on_pull:
                (self.root / ".env").write_bytes(b"changed-by-other-owner")
            return b""
        if args[:2] in (["docker", "stop"], ["docker", "restart"]):
            state = next(state for state in self.containers.values() if state["Id"] == args[-1])
            state["State"]["Running"] = args[1] == "restart"
            if args[1] == "restart":
                state["RestartCount"] += 1
            return b""
        if args[:2] == ["docker", "exec"]:
            if "healthcheck" in args and (self.failed_health or self.fail_rollback):
                self.failed_health = False
                raise ops.Refused("fixture health failure")
            return b""
        if args[:2] == ["docker", "compose"]:
            files = [args[i + 1] for i, value in enumerate(args) if value == "-f"]
            config = json.loads(stdin if files[0] == "-" else (self.root / "compose.yaml").read_bytes())
            if "config" in args:
                return json.dumps(config).encode()
            if "up" in args:
                name = args[-1]
                ref = config["services"][name]["image"]
                if len(files) == 3:
                    ref = json.loads(Path(files[-1]).read_bytes())["services"][name]["image"]
                self.create(name, ref, config)
                return b""
        raise AssertionError("unexpected command: " + repr(args))


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        k, ka = ops.KEEPER, ops.KEEPALIVE
        common = {"network_mode": "service:ai-sse-keepalive-proxy-netns", "cpus": "0.5", "read_only": True,
                  "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]}
        self.config = {"services": {
            k: {**common, "image": reference(k, "a"), "mem_limit": 384 * 1024**2, "memswap_limit": 384 * 1024**2,
                "pids_limit": 64, "user": "65534:65534", "volumes": [{"type": "volume", "target": "/var/tmp/" + k}],
                "environment": {"UPSTREAM_URL": "http://sub2api-ai-sse-relay:8080", "LISTEN_ADDR": "127.0.0.1:8085",
                                "STORE_DIR": "/var/tmp/" + k, "REQUIRE_NO_DEFAULT_ROUTE": "true", "STORE_MAX_BYTES": "209715200"}},
            ka: {**common, "image": reference(ka, "a"), "mem_limit": 128 * 1024**2, "pids_limit": 128,
                 "environment": {"UPSTREAM_URL": "http://127.0.0.1:8085", "GOMEMLIMIT": "96MiB", "MAX_INSPECT_BODY_BYTES": "8388608"}},
            "apisix-models": {"image": "inner-immutable"}, "apisix": {"image": "front-immutable"},
            "models-enricher": {"image": "enricher-unchanged", "environment": {"SECRET": "do-not-print-credential"}},
            "sub2api": {"image": "sub2api-unchanged"}, "ai-sse-keepalive-proxy-netns": {"image": "owner-unchanged"},
        }, "networks": {"unchanged": {}}, "volumes": {k + "-cache": {}, "models-enricher-cache": {}}}
        for name in ops.GUARDS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"initial-private-data")
        (self.root / "compose.yaml").write_bytes(json.dumps(self.config).encode())
        self.payload = {name: (self.root / name).read_bytes() for name in ops.GUARDS}
        candidate = copy.deepcopy(self.config)
        for name in (k, ka):
            candidate["services"][name]["image"] = reference(name, "c")
        self.payload["compose.yaml"] = json.dumps(candidate).encode()
        self.runtime = Runtime(self.root, self.config)
        self.subject = ops.Operations(self.root, retries=1, delay=0, min_memory=0, min_disk=0)
        self.subject.run = self.runtime

    def planned(self, operation="deploy", snapshot=""):
        return self.subject.execute(operation, "plan", self.payload, snapshot=snapshot)["plan"]

    def apply(self):
        plan = self.planned()
        return self.subject.execute("deploy", "apply", self.payload, plan["approved_plan"])

    def test_plan_is_read_only_and_redacts_private_values(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        report = self.planned()
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))
        self.assertNotIn("do-not-print-credential", json.dumps(report))
        self.assertEqual(set(report["service_delta"]), {ops.KEEPER, ops.KEEPALIVE})
        self.assertFalse(any(any(word in command for word in ("pull", "up", "stop", "restart", "exec")) for command in self.runtime.commands))

    def test_approval_and_drift_reject_before_mutation(self):
        for approval in ("", "0" * 64):
            with self.subTest(approval=approval), self.assertRaises(ops.Refused):
                self.subject.execute("deploy", "apply", self.payload, approval)
        self.assertFalse((self.root / ".standard-cache-operations.lock").exists())
        plan = self.planned()
        self.runtime.containers[ops.KEEPALIVE]["HostConfig"]["MemorySwap"] += 1024
        with self.assertRaises(ops.Refused):
            self.subject.execute("deploy", "apply", self.payload, plan["approved_plan"])
        self.runtime.containers[ops.KEEPALIVE]["HostConfig"]["MemorySwap"] -= 1024
        (self.root / ".env").write_bytes(b"concurrent-CPA-change")
        with self.assertRaises(ops.Refused):
            self.subject.execute("deploy", "apply", self.payload, plan["approved_plan"])
        self.assertFalse(any("pull" in command for command in self.runtime.commands))

    def test_forbidden_changes_and_unpinned_images(self):
        for kind in ("protected-service", "network", "owner", "swap", "image", "CPA-key", "heap", "override"):
            data = copy.deepcopy(self.payload)
            config = json.loads(data["compose.yaml"])
            if kind == "protected-service":
                config["services"]["sub2api"]["image"] = "forbidden"
            elif kind == "network":
                config["networks"]["new-operator"] = {}
            elif kind == "owner":
                config["services"][ops.KEEPER]["network_mode"] = "service:new-owner"
            elif kind == "swap":
                config["services"][ops.KEEPALIVE]["memswap_limit"] = 512 * 1024**2
            elif kind == "image":
                config["services"][ops.KEEPER]["image"] = "ghcr.io/xz-dev/openai-api-cache-keeper:latest"
            elif kind == "CPA-key":
                config["services"][ops.KEEPER]["environment"]["CPA_API_KEY"] = "secret"
            elif kind == "heap":
                config["services"][ops.KEEPALIVE]["environment"]["GOMEMLIMIT"] = "192MiB"
            else:
                data["compose.override.yaml"] = b"stale"
            data["compose.yaml"] = json.dumps(config).encode()
            with self.subTest(kind=kind), self.assertRaises(ops.Refused):
                self.subject.plan("deploy", data)

    def test_wrong_revision_and_prefetch_drift_do_not_activate(self):
        image = self.runtime.images[reference(ops.KEEPER, "c")]
        image["Config"]["Labels"]["org.opencontainers.image.revision"] = "wrong"
        with self.assertRaises(ops.Refused):
            self.apply()
        self.assertFalse(any("stop" in command for command in self.runtime.commands))
        image["Config"]["Labels"]["org.opencontainers.image.revision"] = "c" * 40
        self.runtime.drift_on_pull = True
        with self.assertRaises(ops.Refused):
            self.apply()
        self.assertFalse(any("stop" in command for command in self.runtime.commands))
        self.assertFalse((self.root / ".rollback").exists())

    def test_apply_is_scoped_and_snapshot_is_private(self):
        old = {name: state["Id"] for name, state in self.runtime.containers.items()}
        result = self.apply()
        self.assertTrue(result["changed"])
        for name, identity in old.items():
            self.assertEqual(self.runtime.containers[name]["Id"] == identity, name not in ops.APPS)
        for command in self.runtime.commands:
            if "up" in command:
                for flag in ("--no-deps", "--no-build", "never"):
                    self.assertIn(flag, command)
                self.assertIn(command[-1], ops.APPS)
        snapshot = self.root / ".rollback" / result["snapshot"]
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        for path in snapshot.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / ".env").read_bytes(), b"initial-private-data")

    def test_inner_apisix_keeps_running_image_despite_local_tag_drift(self):
        original = self.runtime.containers["apisix-models"]["Image"]
        self.runtime.add_image("inner-immutable", "d")
        self.assertNotEqual(self.runtime.images["inner-immutable"]["Id"], original)
        self.apply()
        self.assertEqual(self.runtime.containers["apisix-models"]["Image"], original)

    def test_missing_declared_network_cannot_be_created(self):
        self.runtime.networks.clear()
        with self.assertRaises(ops.Refused):
            self.apply()
        self.assertFalse(any("up" in command or "pull" in command for command in self.runtime.commands))

    def test_keeper_only_restart_and_unchanged_deploy(self):
        plan = self.planned("restart")
        before = self.subject.ids()
        self.subject.execute("restart", "apply", {}, plan["approved_plan"])
        self.assertEqual(before, self.subject.ids())
        mutations = [c for c in self.runtime.commands if any(w in c for w in ("up", "pull", "stop", "restart"))]
        self.assertEqual(mutations, [["docker", "restart", "--time", "10", before[ops.KEEPER]]])
        self.payload = self.subject.files()
        self.runtime.commands.clear()
        self.assertFalse(self.apply()["changed"])
        self.assertFalse(any("up" in c or "pull" in c for c in self.runtime.commands))

    def test_failure_rolls_back_exact_old_images_and_files(self):
        before = {name: state["Image"] for name, state in self.runtime.containers.items()}
        self.runtime.failed_health = True
        with self.assertRaisesRegex(ops.Refused, "rollback verified"):
            self.apply()
        for name in ops.APPS:
            self.assertEqual(self.runtime.containers[name]["Image"], before[name])
        self.assertEqual(json.loads((self.root / "compose.yaml").read_bytes()), self.config)
        self.assertFalse(any("latest" in str(c) for c in self.runtime.commands))

    def test_manual_rollback_exact_images_and_stale_snapshot_guard(self):
        old = {name: state["Image"] for name, state in self.runtime.containers.items()}
        result = self.apply()
        name = result["snapshot"]
        plan = self.planned("rollback", name)
        self.subject.execute("rollback", "apply", {}, plan["approved_plan"], name)
        for service in ops.APPS:
            self.assertEqual(self.runtime.containers[service]["Image"], old[service])
        (self.root / ".env").write_bytes(b"later-independent-change")
        with self.assertRaises(ops.Refused):
            self.planned("rollback", name)
        self.assertEqual((self.root / ".env").read_bytes(), b"later-independent-change")

    def test_rollback_failure_is_not_reported_as_success(self):
        self.runtime.fail_rollback = True
        with self.assertRaisesRegex(ops.Refused, "rollback not verified"):
            self.apply()

    def test_apisix_health_shell_uses_existing_role_contracts(self):
        paths = []
        class Handler(BaseHTTPRequestHandler):
            misleading = False
            def do_GET(self):
                paths.append(self.path)
                self.send_response(503 if self.misleading else (404 if self.path == "/v1/models" else 200), "200 wrong" if self.misleading else None)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        real_run = self.subject.run
        def run(args, **kwargs):
            if args[:2] == ["docker", "exec"] and "/bin/bash" in args:
                command = args[-1].replace("127.0.0.1/9080", "127.0.0.1/" + str(server.server_port))
                result = subprocess.run(["/bin/bash", "-ec", command], capture_output=True, timeout=5)
                if result.returncode:
                    raise ops.Refused("fixture HTTP health failed")
                return result.stdout
            return real_run(args, **kwargs)
        self.subject.run = run
        for name in ("apisix", "apisix-models"):
            self.subject.ready(name, self.runtime.containers[name]["Id"])
        self.assertEqual(paths, ["/v1/models", "/healthz"])
        Handler.misleading = True
        with self.assertRaises(ops.Refused):
            self.subject.ready("apisix-models", self.runtime.containers["apisix-models"]["Id"])

    def test_lock_contention_and_approval_policy_binding(self):
        plan = self.planned()
        self.subject.min_disk += 1
        with self.assertRaisesRegex(ops.Refused, "approved_plan"):
            self.subject.execute("deploy", "apply", self.payload, plan["approved_plan"])
        self.subject.min_disk -= 1
        path = self.root / ".standard-cache-operations.lock"
        with path.open("w") as lock:
            ops.fcntl.flock(lock, ops.fcntl.LOCK_EX | ops.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.subject.execute("deploy", "apply", self.payload, plan["approved_plan"])
        self.assertFalse(any("pull" in command for command in self.runtime.commands))

    def test_partial_write_failure_restores_exact_snapshot(self):
        real_write = os.write
        counter = 0
        def partial(descriptor, data):
            nonlocal counter
            counter += 1
            if counter == 1:
                return real_write(descriptor, data[:7])
            if counter == 2:
                raise OSError("ENOSPC fixture")
            return real_write(descriptor, data)
        with mock.patch.object(os, "write", side_effect=partial), self.assertRaisesRegex(ops.Refused, "rollback verified"):
            self.apply()
        self.assertEqual(json.loads((self.root / "compose.yaml").read_bytes()), self.config)

    def test_snapshot_image_tamper_and_wrong_filesystem_fail(self):
        result = self.apply()
        overlay = self.root / ".rollback" / result["snapshot"] / "images.json"
        overlay.write_bytes(b'{"services":{}}')
        with self.assertRaisesRegex(ops.Refused, "overlay checksum"):
            self.planned("rollback", result["snapshot"])
        real_run = self.subject.run
        self.subject.run = lambda args, **kwargs: b"tmpfs\n" if args[0] == "stat" else real_run(args, **kwargs)
        with self.assertRaisesRegex(ops.Refused, "memory-backed"):
            self.subject.verify({name: self.runtime.containers[name]["Image"] for name in ops.APPS}, {}, 256 * 1024**2, self.subject.networks())

    def test_protected_identity_change_fails_before_restore(self):
        original = self.subject.verify
        def changed(*args):
            self.runtime.containers["sub2api"]["Id"] = "concurrent-recreate"
            return original(*args)
        self.subject.verify = changed
        with self.assertRaisesRegex(ops.Refused, "rollback not verified"):
            self.apply()

    @unittest.skipUnless(shutil.which("ansible-playbook"), "Ansible executable unavailable; install it to verify the complete entry point")
    def test_ansible_entrypoint_plan_apply_restart_rollback(self):
        workspace = self.root.parent / (self.root.name + "-ansible")
        workspace.mkdir()
        self.addCleanup(shutil.rmtree, workspace)
        candidate = workspace / "candidate"
        for name, data in self.payload.items():
            target = candidate / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        state = workspace / "runtime.json"
        state.write_text(json.dumps({**self.runtime.__dict__, "root": str(self.root)}))
        fakebin = workspace / "bin"
        fakebin.mkdir()
        wrapper = '''#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("fixture", os.environ["CACHE_OPS_TEST_MODULE"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
path = Path(os.environ["CACHE_OPS_FAKE_STATE"])
runtime = module.Runtime.__new__(module.Runtime)
runtime.__dict__.update(json.loads(path.read_text()))
runtime.root = Path(runtime.root)
try:
    output = runtime([Path(sys.argv[0]).name] + sys.argv[1:], stdin=sys.stdin.buffer.read())
finally:
    path.write_text(json.dumps({**runtime.__dict__, "root": str(runtime.root)}))
sys.stdout.buffer.write(output)
'''
        for name in ("docker", "stat"):
            target = fakebin / name
            target.write_text(wrapper)
            target.chmod(0o700)
        inventory = workspace / "hosts.ini"
        inventory.write_text("[gateway]\nfixture ansible_connection=local ansible_python_interpreter=" + sys.executable + "\n")
        environment = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"], "CACHE_OPS_FAKE_STATE": str(state),
                       "CACHE_OPS_TEST_MODULE": str(Path(__file__).resolve()), "ANSIBLE_NOCOLOR": "1", "ANSIBLE_STDOUT_CALLBACK": "default",
                       "ANSIBLE_LOCAL_TEMP": str(workspace / "local-tmp"), "ANSIBLE_REMOTE_TEMP": str(workspace / "remote-tmp")}
        def play(tag, mode, approved="", snapshot="", expected=0):
            variables = workspace / "vars.json"
            variables.write_text(json.dumps({"ai_ops_runtime": "docker", "ai_ops_compose_cmd": "docker compose", "ai_ops_mode": mode,
                "ai_ops_deploy_root": str(self.root), "ai_ops_config_dir": str(candidate), "ai_ops_min_mem_mb": 0, "ai_ops_min_disk_mb": 0,
                "ai_ops_openai_api_cache_keeper_approved_plan": approved, "ai_ops_openai_api_cache_keeper_snapshot": snapshot,
                "ai_ops_openai_api_cache_keeper_verify_retries": 1, "ai_ops_openai_api_cache_keeper_verify_delay_seconds": 0}))
            process = subprocess.run(["ansible-playbook", "-i", str(inventory), "ops.yml", "--tags", tag, "-e", "@" + str(variables)],
                                     cwd=ROOT / "ansible", env=environment, capture_output=True, text=True, timeout=90)
            text = process.stdout + process.stderr
            self.assertNotIn("do-not-print-credential", text)
            self.assertEqual(process.returncode == 0, expected == 0, text)
            return text
        def approval(text):
            return re.search(r'approved_plan["\s:]+([0-9a-f]{64})', text).group(1)
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        plan = play("deploy-standard-cache-path", "plan")
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))
        play("deploy-standard-cache-path", "apply", expected=1)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))
        result = play("deploy-standard-cache-path", "apply", approval(plan))
        snapshot = re.search(r'snapshot["\s:]+(standard-cache-[0-9TZ]+-[0-9a-f]+)', result).group(1)
        shutil.rmtree(candidate)
        restart = play("restart-openai-api-cache-keeper", "plan")
        play("restart-openai-api-cache-keeper", "apply", approval(restart))
        rollback = play("rollback-standard-cache-path", "plan", snapshot=snapshot)
        play("rollback-standard-cache-path", "apply", approval(rollback), snapshot)
        self.assertEqual(json.loads((self.root / "compose.yaml").read_bytes()), self.config)
        commands = json.loads(state.read_text())["commands"]
        self.assertTrue(any("restart" in command for command in commands))
        self.assertFalse((self.root / ".ai-ops-state").exists())
        previous = json.loads(state.read_text())["commands"]
        for tag in ("deploy-responses-path", "all", "deploy-standard-cache-path,deploy-recovery", "restart-openai-api-cache-keeper,rollback-standard-cache-path"):
            play(tag, "plan", expected=1)
            self.assertEqual(previous, json.loads(state.read_text())["commands"])

    def test_cli_never_prints_private_runtime_errors(self):
        with mock.patch.object(sys, "argv", ["standard-cache-path", "deploy", "--root", str(self.root)]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps({n: base64.b64encode(v).decode() for n, v in self.payload.items()}))), \
                mock.patch.object(ops.Operations, "execute", side_effect=ValueError("do-not-print-credential")), \
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(ops.main(), 1)
            self.assertNotIn("do-not-print-credential", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
