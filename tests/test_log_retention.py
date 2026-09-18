import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest

from maa_planner.log_retention import RETENTION_SECONDS, retain
from maa_planner.util import canonical_json, sha256_bytes


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1_800_000_000
        self.old = self.now - RETENTION_SECONDS - 1

    def file(self, name, value='evidence', stamp=None):
        path = self.root / 'var/state' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        os.utime(path, (self.old if stamp is None else stamp,) * 2)
        return path

    def run_audit(self, number, *, stamp=None, success=False, extra=None, finished=True):
        rid = f'20260101T00000{number}.000000Z-12345678'
        payloads = [('run-started', {'mode': 'full', **(extra or {})})]
        if finished:
            payloads.append(('run-finished', {'status': 'success' if success else 'failed'}))
        previous = None
        for seq, (kind, payload) in enumerate(payloads):
            event = {'schema_version': 1, 'run_id': rid, 'sequence': seq,
                     'event_type': kind, 'previous_event_sha256': previous,
                     'recorded_at': '2026-01-01T00:00:00Z', 'payload': payload}
            previous = sha256_bytes(canonical_json(event))
            event['event_sha256'] = previous
            self.file(f'supervisor/runs/{rid}/events/{seq:04d}-{kind}.json', json.dumps(event), stamp)
        return rid

    def test_boundary_state_and_dry_run(self):
        expired = self.file('host/old.log')
        boundary = self.file('host/boundary.log', stamp=self.now - RETENTION_SECONDS)
        state = self.file('planner/inventory.json', '{}')
        receipt = self.file('runtime/receipt.json', '{}')
        decision = self.file('planner/launcher-old.json', '{}')
        snapshot = expired.read_bytes()
        result = retain(self.root, dry_run=True, now=self.now)
        self.assertIn('var/state/host/old.log', result['deleted'])
        self.assertEqual(expired.read_bytes(), snapshot)
        retain(self.root, now=self.now)
        self.assertFalse(expired.exists())
        self.assertFalse(decision.exists())
        self.assertTrue(all(p.exists() for p in (boundary, state, receipt)))

    def test_whole_run_removal_and_retained_reference(self):
        old = self.run_audit(1)
        self.file(f'recovery/{old}/screen.png')
        evidence = self.file('host/referenced.log')
        recent = self.run_audit(2, stamp=self.now, extra={'evidence': 'var/state/host/referenced.log'})
        retain(self.root, now=self.now)
        self.assertFalse((self.root / f'var/state/supervisor/runs/{old}').exists())
        self.assertFalse((self.root / f'var/state/recovery/{old}').exists())
        self.assertTrue((self.root / f'var/state/supervisor/runs/{recent}').exists())
        self.assertTrue(evidence.exists())

    def test_parent_links_latest_index_success_baseline_and_incomplete(self):
        parent = self.run_audit(1)
        self.run_audit(2, stamp=self.now, extra={'parent_run_id': parent})
        baseline = self.run_audit(3, success=True)
        latest = self.run_audit(4)
        incomplete = self.run_audit(5, finished=False)
        self.file('supervisor/latest-run.json', json.dumps({'run_id': latest}))
        retain(self.root, now=self.now)
        for rid in (parent, baseline, latest, incomplete):
            self.assertTrue((self.root / f'var/state/supervisor/runs/{rid}').exists())

    def test_rotation_preserves_bytes_and_mtime_then_expires(self):
        log = self.file('debug/asst.log', 'original log', self.now - 10)
        retain(self.root, now=self.now)
        archives = list((log.parent / 'archive').iterdir())
        self.assertFalse(log.exists())
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_text(), 'original log')
        self.assertEqual(archives[0].stat().st_mtime, self.now - 10)
        retain(self.root, now=self.now + RETENTION_SECONDS)
        self.assertFalse(archives[0].exists())

    def test_locks_skip_mutation(self):
        old = self.file('host/old.log')
        locks = self.root / 'var/run'
        locks.mkdir()
        for name in ('maa-daily.lock', 'zootd.lock', 'codex-sdk.lock'):
            with (locks / name).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertEqual(retain(self.root, now=self.now)['status'], 'busy')
                self.assertTrue(old.exists())

    def test_no_symlink_traversal(self):
        external = self.file('unmanaged/keep.log')
        debug = self.root / 'var/state/debug'
        debug.symlink_to(external.parent, target_is_directory=True)
        retain(self.root, now=self.now)
        self.assertTrue(external.exists())

    def test_invalid_audit_prevents_partial_cleanup(self):
        expired = self.file('host/old.log')
        rid = self.run_audit(1)
        self.file(f'supervisor/runs/{rid}/events/0001-run-finished.json', '{}')
        result = retain(self.root, now=self.now)
        self.assertEqual(result['status'], 'deferred')
        self.assertTrue(expired.exists())

    def test_unfinished_recovery_defers_cleanup(self):
        old = self.file('host/old.log')
        rid = self.run_audit(1)
        directory = self.root / f'var/state/supervisor/runs/{rid}/events'
        previous = json.loads((directory / '0001-run-finished.json').read_text())
        event = {'schema_version': 1, 'run_id': rid, 'sequence': 2,
                 'event_type': 'recovery-started', 'payload': {},
                 'previous_event_sha256': previous['event_sha256']}
        event['event_sha256'] = sha256_bytes(canonical_json(event))
        self.file(f'supervisor/runs/{rid}/events/0002-recovery-started.json', json.dumps(event))
        self.assertEqual(retain(self.root, now=self.now)['status'], 'deferred')
        self.assertTrue(old.exists())

    def test_repair_worktree_is_not_deleted(self):
        rid = self.run_audit(1)
        gitfile = self.file(f'recovery/{rid}/repair-worktree/.git', 'gitdir: elsewhere')
        retain(self.root, now=self.now)
        self.assertTrue(gitfile.exists())


if __name__ == '__main__':
    unittest.main()
