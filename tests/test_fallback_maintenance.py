from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from maa_planner import fallback_maintenance as maintenance
from maa_planner.fallback_signals import revision, rules

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceTests(unittest.TestCase):
    def repo(self, temp):
        root = Path(temp) / 'repo'
        root.mkdir()
        remote = Path(temp) / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git(root, 'init', '-q', '-b', 'main')
        self.git(root, 'config', 'user.name', 'Test')
        self.git(root, 'config', 'user.email', 'test@example.invalid')
        (root / 'maa_planner').mkdir()
        rule = {'id': 'official-input-anr', 'version': 1, 'label': '官服输入 ANR',
                'requires': {'startup_failed': True, 'fresh_official_input_anr': True, 'game_offline': False},
                'action': 'restart-waydroid-session'}
        (root / 'maa_planner/fallback_rules.py').write_text('RULES = ' + repr([rule]))
        fixture = root / 'tests/fixtures/fallback/official-input-anr.json'
        fixture.parent.mkdir(parents=True)
        sample = {'startup_failed': True, 'fresh_official_input_anr': True,
                  'game_offline': False, 'native_motion_timeout': True}
        fixture.write_text(json.dumps({'positive': [sample], 'negative': [
            {**sample, 'fresh_official_input_anr': False},
            {**sample, 'fresh_official_input_anr': None}, {**sample, 'startup_failed': False}]}))
        (root / 'docs').mkdir()
        shutil.copy(ROOT / 'docs/fallback-maintenance.md', root / 'docs')
        (root / '.gitignore').write_text('/var/\n/.venv/\n')
        # A real, trusted fixed suite in the temporary release repository.
        (root / 'tests/test_contract.py').write_text('import unittest\n'
            'class FixedSuite(unittest.TestCase):\n'
            '    def test_replay_exists(self):\n'
            '        from pathlib import Path\n'
            '        self.assertTrue(Path("tests/fixtures/fallback/official-input-anr.json").is_file())\n')
        (root / '.venv/bin').mkdir(parents=True)
        (root / '.venv/bin/python').symlink_to(sys.executable)
        self.git(root, 'add', '.')
        self.git(root, 'commit', '-qm', 'baseline')
        self.git(root, 'remote', 'add', 'origin', str(remote))
        self.git(root, 'push', '-q', '-u', 'origin', 'main')
        return root

    def git(self, root, *argv):
        return subprocess.run(['git', *argv], cwd=root, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout.decode().strip()

    def assessment(self, root):
        now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        rule = rules(root)[0]
        return {'at': now.isoformat(), 'candidates': {'daily:startup-error': ['one', 'two', 'three']},
                'rules': {rule['id']: {'status': 'paused', 'revision': revision(rule)}},
                'samples': [{'run_id': str(i), 'at': (now - timedelta(days=day)).isoformat(),
                             'status': 'failed', 'action': None, 'outcome': None,
                             'signals': {'startup_failed': True, 'fresh_official_input_anr': True,
                                         'game_offline': False, 'native_motion_timeout': True}} for i, day in enumerate((20, 16, 12))]}

    def test_isolated_sdk_candidate_replays_tests_pr_squash_and_clean_main_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.repo(temp)
            base = self.git(root, 'rev-parse', 'HEAD')
            calls = []
            merged = False
            real_run = maintenance._run
            head = branch = None

            def sdk(request):
                self.assertEqual(request.sandbox, 'workspace-write')
                self.assertIn('sandbox_workspace_write.network_access=false', request.config_overrides)
                self.assertNotEqual(request.cwd, root)
                self.assertNotIn('DISPLAY', request.environment)
                self.assertIn('verify SHA-256', request.prompt)
                rule = rules(request.cwd)[0]
                rule.update(version=2, requires={**rule['requires'], 'native_motion_timeout': True})
                (request.cwd / 'maa_planner/fallback_rules.py').write_text('RULES = ' + repr([rule]))
                return json.dumps({'status': 'changed', 'summary': '用原生反例收紧识别。'})

            def command(cwd, *argv, **kwargs):
                nonlocal merged, head, branch
                calls.append(argv)
                if argv[0] != 'gh':
                    return real_run(cwd, *argv, **kwargs)
                if argv[1:3] == ('pr', 'create'):
                    head = self.git(cwd, 'rev-parse', 'HEAD')
                    branch = self.git(cwd, 'branch', '--show-current')
                    return 'https://github.com/example/repo/pull/7'
                if argv[1:3] == ('pr', 'view'):
                    return json.dumps({'state': 'MERGED' if merged else 'OPEN', 'headRefOid': head,
                        'headRefName': branch, 'baseRefName': 'main', 'isCrossRepository': False,
                        'mergeStateStatus': 'CLEAN', 'statusCheckRollup': []})
                if argv[1:3] == ('pr', 'merge'):
                    self.assertIn('--squash', argv)
                    self.assertIn('--match-head-commit', argv)
                    self.assertNotIn('--admin', argv)
                    commit = self.git(root, 'commit-tree', head + '^{tree}', '-p', base, '-m', 'squash')
                    self.git(root, 'push', 'origin', commit + ':refs/heads/main')
                    merged = True
                    return ''
                raise AssertionError(argv)

            with patch.object(maintenance, '_run', side_effect=command), \
                 patch('maa_planner.auto_fallback.notify', return_value=True) as notify:
                result = maintenance.improve_rules(root, self.assessment(root), sdk_runner=sdk)
            self.assertEqual(result['status'], 'deployed', result)
            self.assertEqual(rules(root)[0]['version'], 2)
            self.assertEqual(self.git(root, 'status', '--porcelain'), '')
            self.assertEqual(self.git(root, 'rev-list', '--left-right', '--count', 'main...origin/main'), '0\t0')
            self.assertEqual(self.git(root, 'rev-list', '--count', base + '..main'), '1')
            self.assertTrue(any(c[0:3] == ('gh', 'pr', 'create') for c in calls))
            self.assertIn('上线', notify.call_args.args[1])

    def test_generated_executable_code_or_out_of_scope_changes_never_publish(self):
        for change in ('executable', 'guard', 'ignored', 'committed'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = self.repo(temp)
                base = self.git(root, 'rev-parse', 'HEAD')

                def sdk(request):
                    if change == 'executable':
                        (request.cwd / 'maa_planner/fallback_rules.py').write_text('import os\nRULES=[]')
                    elif change in {'guard', 'committed'}:
                        (request.cwd / 'maa_planner/auto_fallback.py').write_text('unsafe guard change')
                        if change == 'committed':
                            self.git(request.cwd, 'add', '.')
                            self.git(request.cwd, 'commit', '-qm', 'unauthorized commit')
                    else:
                        (request.cwd / '.venv').mkdir()
                        (request.cwd / '.venv/hidden.py').write_text('hidden test injection')
                    return json.dumps({'status': 'changed', 'summary': 'candidate'})

                with patch('maa_planner.auto_fallback.notify', return_value=True):
                    result = maintenance.improve_rules(root, self.assessment(root), sdk_runner=sdk)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(self.git(root, 'rev-parse', 'HEAD'), base)
                self.assertEqual(self.git(root, 'status', '--porcelain'), '')
                self.assertEqual(self.git(root, 'ls-remote', '--heads', 'origin'), base + '\trefs/heads/main')

    def test_pending_or_failed_github_checks_cannot_release(self):
        pending = {'base': 'a' * 40, 'head': 'b' * 40, 'branch': 'auto/fallback-test',
                   'url': 'https://github.com/example/repo/pull/7'}
        for check, expected in (({'status': 'IN_PROGRESS'}, 'waiting-for-checks'),
                                ({}, 'waiting-for-checks'),
                                ({'status': 'COMPLETED', 'conclusion': 'FAILURE'}, 'error')):
            calls = []

            def command(root, *argv, **kwargs):
                calls.append(argv)
                if argv[0] == 'git':
                    return {'branch': 'main', 'status': '', 'rev-parse': 'a' * 40}.get(argv[1], '')
                return json.dumps({'state': 'OPEN', 'headRefOid': pending['head'],
                    'headRefName': pending['branch'], 'baseRefName': 'main', 'isCrossRepository': False,
                    'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [check]})

            with patch.object(maintenance, '_run', side_effect=command):
                if expected == 'error':
                    self.assertRaises(ValueError, maintenance._release, ROOT, pending)
                else:
                    self.assertEqual(maintenance._release(ROOT, pending)['status'], expected)
            self.assertFalse(any(c[:3] == ('gh', 'pr', 'merge') for c in calls))


if __name__ == '__main__':
    unittest.main()
