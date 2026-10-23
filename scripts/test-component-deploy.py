#!/usr/bin/env python3
"""Fast guards; full isolated Ansible lifecycle: test-component-apply.py."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/ops' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMAGE = load('component-image')
LOCK = load('component-lock')
PIN = 'sha256:' + 'a' * 64
ID = 'sha256:' + 'b' * 64
REF = 'example.test/component:version@' + PIN


class ImageProof(unittest.TestCase):
    def test_manifest_digest_is_not_assumed_to_equal_config_id(self):
        self.assertEqual(IMAGE.verify(REF, {'Id': ID, 'RepoDigests': ['example.test/component@' + PIN]})['image_id'], ID)

    def test_mismatched_digest_is_refused(self):
        with self.assertRaisesRegex(AssertionError, 'pinned digest'):
            IMAGE.verify(REF, {'Id': ID, 'RepoDigests': ['example.test/component@' + ID]})

    def test_missing_image_after_skipped_pull_is_refused(self):
        with patch.object(IMAGE, 'run') as command:
            command.return_value.returncode = 1
            with self.assertRaisesRegex(AssertionError, 'not present'):
                IMAGE.inspect('docker', REF, 'local')
            self.assertEqual(command.call_count, 1)

    def test_plan_checks_registry_without_pull(self):
        with patch.object(IMAGE, 'run') as command:
            command.side_effect = [type('Result', (), {'returncode': 1})(), type('Result', (), {
                'returncode': 0, 'stdout': json.dumps({'schemaVersion': 2, 'config': {}})})()]
            self.assertEqual(IMAGE.inspect('docker', REF, 'plan')['source'], 'registry_manifest')
            self.assertEqual(command.call_args.args[0], ['docker', 'manifest', 'inspect', REF])

    def test_preload_never_contacts_registry(self):
        with patch.object(IMAGE, 'run') as command:
            command.return_value.returncode = 1
            with self.assertRaises(AssertionError):
                IMAGE.inspect('docker', 'localhost/image:preloaded', 'plan', True)
            self.assertEqual(command.call_count, 1)

    def test_unpinned_requires_preload_profile(self):
        with self.assertRaises(AssertionError):
            IMAGE.verify('localhost/image:tag', {'Id': ID})
        self.assertEqual(IMAGE.verify('localhost/image:tag', {'Id': ID}, True)['source'], 'local')


class OwnedLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='component-lock-test-', dir='/var/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.ai-ops-state').mkdir(mode=0o700)
        self.request = {'transaction': 'a' * 32, 'controller_host': socket.gethostname(),
                        'controller_pid': 99999999, 'service': 'models-enricher',
                        'operation': 'deploy', 'approval_digest': 'b' * 64}
        self.recovery = {'transaction': self.request['transaction'], 'controller_clear': True}

    def test_plan_leaves_no_lock_or_receipt(self):
        self.assertEqual(LOCK.info(self.root), {'held': False})
        self.assertEqual(list((self.root / '.ai-ops-state').iterdir()), [])

    def test_durable_progress_modes_and_owned_release(self):
        result = LOCK.acquire(self.root, self.request)
        LOCK.progress(self.root, {**self.recovery, 'stage': 'activating'})
        self.assertEqual(json.loads(Path(result['receipt']).read_text())['stage'], 'activating')
        self.assertEqual(Path(result['receipt']).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(AssertionError):
            LOCK.release(self.root, self.recovery)
        LOCK.progress(self.root, {**self.recovery, 'stage': 'complete', 'outcome': 'success'})
        self.assertTrue(LOCK.release(self.root, self.recovery)['released'])

    def test_other_writer_lock_is_never_removed(self):
        lock = self.root / '.ai-ops-state/front-component.lock'
        lock.mkdir(mode=0o700)
        with self.assertRaises(FileExistsError):
            LOCK.acquire(self.root, self.request)
        self.assertTrue(lock.exists())

    def test_recovery_refuses_recent_live_or_mismatched_owner(self):
        LOCK.acquire(self.root, self.request)
        with self.assertRaisesRegex(AssertionError, 'too recent'):
            LOCK.release(self.root, self.recovery, True)
        later = time.time() + 901
        with patch.object(LOCK.time, 'time', return_value=later), patch.object(LOCK, 'live_writers', return_value=[123]):
            with self.assertRaisesRegex(AssertionError, 'still alive'):
                LOCK.release(self.root, self.recovery, True)
        _, receipt, _, value = LOCK.owned(self.root, self.request['transaction'])
        value['owner']['controller_pid'] += 1
        LOCK.atomic_json(receipt, value)
        with self.assertRaisesRegex(AssertionError, 'mismatch'):
            LOCK.release(self.root, self.recovery, True)

    def test_killed_controller_can_be_recovered_only_with_receipt(self):
        LOCK.acquire(self.root, self.request)
        with patch.object(LOCK.time, 'time', return_value=time.time() + 901), patch.object(LOCK, 'live_writers', return_value=[]):
            self.assertTrue(LOCK.release(self.root, self.recovery, True)['released'])
        path = LOCK.paths(self.root, self.request['transaction'])[1]
        self.assertEqual(json.loads(path.read_text())['outcome'], 'recovery-required')

    def test_controller_must_be_dead_and_original_host(self):
        with self.assertRaises(AssertionError):
            LOCK.controller_clear({**self.request, 'controller_pid': os.getpid()})
        with self.assertRaises(AssertionError):
            LOCK.controller_clear({**self.request, 'controller_host': 'another-host'})

    def test_process_scan_does_not_match_quoted_source_but_does_match_writers(self):
        proc = self.root / 'proc'
        for pid, argv in [(1, ['python3', '-c', 'ansible-playbook docker compose']),
                          (2, ['docker', 'compose', 'pull']),
                          (3, ['python3', '/x/AnsiballZ_command.py'])]:
            p = proc / str(pid)
            p.mkdir(parents=True)
            (p / 'cmdline').write_bytes(b'\0'.join(s.encode() for s in argv))
        self.assertEqual(set(LOCK.live_writers(proc, set())), {2, 3})


class TaskOrdering(unittest.TestCase):
    def test_baseline_follows_readiness_and_group_readback(self):
        text = (ROOT / 'ansible/roles/ai_ops/tasks/front-component.yml').read_text()
        self.assertLess(text.index('Wait for selected component health'), text.index('Advance baselines only after'))
        self.assertLess(text.index('Refuse post-activation drift'), text.index('Advance baselines only after'))
        self.assertLess(text.index('Record stage before selected component activation'), text.index('Recreate only selected'))
        self.assertIn("'--no-deps', '--no-build', '--pull', 'never'", text)
        helper = (ROOT / 'ansible/roles/ai_ops/tasks/deploy-file.yml').read_text()
        self.assertNotIn('Adopt matching remote into baseline', helper)
        tail = helper.split('- name: "Record new baseline', 1)[1]
        self.assertIn('when: _ai_commit_baseline', tail)
        self.assertIn('ai_activate_cmd is defined and _ai_upload is changed', helper)
        self.assertIn('ai_defer_baseline', helper)
        self.assertIn('Read back activated file', helper)

    def test_generic_and_front_share_one_transaction(self):
        ops = (ROOT / 'ansible/ops.yml').read_text()
        self.assertIn("when: service | default('') in ai_ops_profiles", ops)
        self.assertNotIn('Pull exact image for {{ service }}', ops)
        self.assertNotIn('Assert {{ service }} appears in ps output', ops)

    def test_commands_have_bounded_async_time_and_keep_preload_identity(self):
        text = (ROOT / 'ansible/roles/ai_ops/tasks/front-component.yml').read_text()
        defaults = (ROOT / 'ansible/roles/ai_ops/defaults/main.yml').read_text()
        self.assertEqual(text.count('async: "{{ ai_ops_component_command_timeout }}"'), 2)
        self.assertIn('ai_ops_component_command_timeout: 600', defaults)
        self.assertIn('_front_candidate_image_id == _front_planned_image_id', text)
        self.assertIn('Validate candidate Compose explicitly', text)
        self.assertIn('Validate uploaded candidate using the exact apply Compose argv', text)

    def test_existing_native_probe_is_reused_not_reimplemented(self):
        runtime = (ROOT / 'ansible/roles/ai_ops/tasks/front-runtime.yml').read_text()
        self.assertIn('module.namespace_probe', runtime)
        self.assertIn('module.OBSERVE_HTTP', runtime)
        self.assertIn('module.worker_health', runtime)
        self.assertIn("return 'missing', False", runtime)


if __name__ == '__main__':
    unittest.main()
