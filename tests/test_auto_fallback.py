from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from maa_planner import auto_fallback as fallback
from maa_planner.fallback_maintenance import validate_change, validate_replays
from maa_planner.fallback_signals import anr_evidence, matches, revision, rules, startup_signals
from maa_planner.supervisor import RUN_MODES, describe_evidence_files, finish_run, record_phase, start_run
from maa_planner.util import atomic_write_json, canonical_json, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
SIGNALS = {'startup_failed': True, 'fresh_official_input_anr': True, 'game_offline': False,
           'native_motion_timeout': True, 'native_key_timeout': False, 'depot_failed': False}
BASE_RULE = {'id': 'official-input-anr', 'version': 1, 'label': '官服输入 ANR',
             'requires': {'startup_failed': True, 'fresh_official_input_anr': True, 'game_offline': False},
             'action': 'restart-waydroid-session'}
LOG = b'[2026-11-08 06:00:00 INFO ] StartUp Start\n[2026-11-08 06:01:00 ERROR] StartUp Error\n'


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'maa_planner').mkdir()
        (self.root / 'maa_planner/fallback_rules.py').write_text('RULES = ' + repr([BASE_RULE]))
        fixture = self.root / 'tests/fixtures/fallback/official-input-anr.json'
        fixture.parent.mkdir(parents=True)
        fixture.write_text(json.dumps({'positive': [SIGNALS], 'negative': [
            {**SIGNALS, 'fresh_official_input_anr': False},
            {**SIGNALS, 'fresh_official_input_anr': None}, {**SIGNALS, 'startup_failed': False}]}))
        self.git = patch('maa_planner.supervisor._git_snapshot', return_value={'head': 'a' * 40, 'dirty': False})
        self.git.start(); self.addCleanup(self.git.stop)
        self.rule = rules(self.root)[0]
        self.native = self.root / 'android-anr'
        self.native.mkdir()
        self.since = NOW - timedelta(minutes=10)
        self.notify = patch.object(fallback, 'notify', return_value=True)
        self.messages = self.notify.start(); self.addCleanup(self.notify.stop)

    def anr(self, *, at=None, package='com.hypergryph.arknights', unknown=False):
        path = self.native / 'anr_current'
        at = at or NOW - timedelta(minutes=1)
        path.write_text(f'Subject: Input dispatching timed out (abc {package}/com.u8.sdk.U8UnityContext '
                        '(server) is not responding. Waited 5003ms for MotionEvent)\n'
                        + ('unknown timestamp\n' if unknown else f'----- pid 298 at {at.isoformat()} -----\n'))
        return path

    def log(self, rid, name='daily.log', raw=LOG):
        path = self.root / 'var/state/host' / (rid + '-' + name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        return path

    def run_record(self, at, *, success=False, finish=True, extra=None, retry=None, daily_success=None,
                   retry_phase='daily'):
        with patch.dict('os.environ', extra or {}):
            rid = start_run(self.root, 'full', now=at)
        if finish:
            for phase in RUN_MODES['full']:
                accepted = success or phase != 'daily'
                if phase == 'daily' and daily_success is not None:
                    accepted = daily_success
                evidence = describe_evidence_files(self.root, [retry]) if phase == retry_phase and retry else []
                record_phase(self.root, rid, phase=phase, result='succeeded' if accepted else 'failed',
                             outcome='accepted' if accepted else 'startup-error', evidence_files=evidence,
                             now=at + timedelta(seconds=1))
            finish_run(self.root, rid, process_status=0 if success else 1, supervisor_enabled=False,
                       supervisor_required=False, command=(), timeout_seconds=1, now=at + timedelta(seconds=2))
        return rid

    def observation(self, rid, at, *, action=None, stage=True, retry=None, phase='daily'):
        directory = self.root / 'var/state/fallback/observations' / rid
        fallback._record(directory / 'detected.json', {'run_id': rid, 'at': at.isoformat(), 'signals': SIGNALS})
        if action:
            detected = fallback._observation(directory / 'detected.json')
            fallback._record(directory / 'action-started.json', {
                'run_id': rid, 'at': at.isoformat(), 'rule_id': self.rule['id'],
                'phase': phase,
                'revision': action, 'observation_sha256': detected['record_sha256']})
            if stage is not None:
                started = fallback._observation(directory / 'action-started.json')
                reference = describe_evidence_files(self.root, [retry])[0] if retry else {}
                fallback._record(directory / 'action-finished.json', {'run_id': rid,
                    'stage_accepted': stage, 'retry_log': reference, 'action_sha256': started['record_sha256']})

    def baseline(self):
        for age in (20, 16, 12):
            at = NOW - timedelta(days=age)
            rid = self.run_record(at)
            self.observation(rid, at)

    def test_anr_is_bound_to_exact_package_native_time_and_pristine_startup(self):
        self.anr()
        self.assertIsNotNone(anr_evidence(self.native, since=self.since, now=NOW))
        for options in ({'at': self.since - timedelta(seconds=1)},
                        {'at': NOW + timedelta(seconds=1)}, {'package': 'other.game'}, {'unknown': True}):
            self.anr(**options)
            self.assertIsNone(anr_evidence(self.native, since=self.since, now=NOW))
        self.assertTrue(startup_signals(LOG)['startup_failed'])
        for marker in (b'Infrast Completed', b'Mission started', b'StartUp Completed'):
            self.assertFalse(startup_signals(LOG + marker)['startup_failed'])
        self.assertFalse(matches(self.rule, {**SIGNALS, 'fresh_official_input_anr': None}))

    def test_rule_source_cannot_execute_code_or_add_host_actions(self):
        validate_replays(ROOT)  # Production rule revisions may evolve independently of this fixture.
        validate_replays(self.root)
        source = self.root / 'maa_planner/fallback_rules.py'
        for text in ('import os\nRULES=[]', 'RULES = __import__("os").system("false")',
                     'RULES=[]\nprint("side effect")'):
            source.write_text(text)
            self.assertRaises(ValueError, rules, self.root)
        for change in ({'action': 'restart-host'}, {'requires': {'startup_failed': True}},
                       {'requires': {'unknown_signal': True}}):
            source.write_text('RULES = ' + repr([{**self.rule, **change}]))
            self.assertRaises(ValueError, rules, self.root)

    def test_recurring_requires_independent_game_days_and_week_long_span(self):
        for days in ((0, 0, 0), (0, 1, 2)):
            rows = [{'run_id': str(i), 'at': (NOW - timedelta(days=d)).isoformat()} for i, d in enumerate(days)]
            self.assertFalse(fallback.recurring(rows))
        self.baseline()
        result = fallback.monitor(self.root, now=NOW, dry_run=True)
        self.assertEqual(result['rules'][self.rule['id']]['status'], 'trial')
        self.assertFalse((self.root / 'var/state/fallback/policy.json').exists())
        self.assertEqual(result['rules'][self.rule['id']]['baseline_cases'], 3)

    def test_action_needs_fresh_policy_owned_session_and_executes_once(self):
        rid = self.run_record(self.since, finish=False)
        log = self.log(rid)
        self.anr()
        policy = {'schema_version': 1, 'at': NOW.isoformat(), 'rules': {
            self.rule['id']: {'status': 'trial', 'revision': revision(self.rule)}}}
        history = {'schema_version': 1, 'runs': {}}
        atomic_write_json(self.root / 'var/state/fallback/history.json', history)
        policy.update(history_sha256=sha256_bytes(canonical_json(history)), invalid_runs=[])
        atomic_write_json(self.root / 'var/state/fallback/policy.json', policy)
        self.assertFalse(fallback.probe(self.root, rid, str(log), owned=False, anr_dir=self.native, now=NOW))
        self.assertTrue(fallback.probe(self.root, rid, str(log), owned=True, anr_dir=self.native, now=NOW))
        self.assertFalse(fallback.probe(self.root, rid, str(log), owned=True, anr_dir=self.native, now=NOW))
        fallback.finish_action(self.root, rid, str(log), True)
        original = (self.root / f'var/state/fallback/observations/{rid}/action-finished.json').read_bytes()
        fallback.finish_action(self.root, rid, str(log), False)
        self.assertEqual((self.root / f'var/state/fallback/observations/{rid}/action-finished.json').read_bytes(), original)

    def test_stale_policy_unknown_revision_and_recovery_children_never_act(self):
        self.anr()
        for at, digest in ((NOW - timedelta(hours=37), revision(self.rule)), (NOW, '0' * 64)):
            rid = self.run_record(self.since, finish=False)
            log = self.log(rid)
            atomic_write_json(self.root / 'var/state/fallback/policy.json', {
                'schema_version': 1, 'at': at.isoformat(), 'rules': {
                    self.rule['id']: {'status': 'trial', 'revision': digest}}})
            self.assertFalse(fallback.probe(self.root, rid, str(log), owned=True, anr_dir=self.native, now=NOW))
        parent = rid
        child = self.run_record(self.since, finish=False, extra={'MAA_RECOVERY_ACTIVE': 'true',
            'MAA_RECOVERY_PARENT_RUN_ID': parent, 'MAA_RECOVERY_ATTEMPT_ID': 'b' * 32, 'MAA_RECOVERY_SLOT': 'morning'})
        self.assertFalse(fallback.probe(self.root, child, str(self.log(child)), owned=True, anr_dir=self.native, now=NOW))

    def test_old_log_or_changed_history_cannot_authorize_an_intervention(self):
        self.baseline()
        rid = self.run_record(self.since, finish=False)
        log = self.log(rid)
        self.anr()
        fallback.monitor(self.root, now=NOW)
        os.utime(log, ((self.since - timedelta(seconds=1)).timestamp(),) * 2)
        self.assertFalse(fallback.probe(self.root, rid, str(log), owned=True, anr_dir=self.native, now=NOW))
        os.utime(log, (NOW.timestamp(),) * 2)
        history = self.root / 'var/state/fallback/history.json'
        value = json.loads(history.read_text())
        value['unexpected_change'] = True
        atomic_write_json(history, value)
        self.assertFalse(fallback.probe(self.root, rid, str(log), owned=True, anr_dir=self.native, now=NOW))
        self.assertFalse((self.root / f'var/state/fallback/observations/{rid}/action-started.json').exists())

    def test_only_future_bound_full_successes_validate_and_silence_does_not(self):
        self.baseline()
        fallback.monitor(self.root, now=NOW)
        waiting = fallback.monitor(self.root, now=NOW + timedelta(days=1))
        self.assertEqual(waiting['rules'][self.rule['id']]['status'], 'trial')
        self.assertEqual(waiting['rules'][self.rule['id']]['successes'], 0)
        for day in (1, 2, 3):
            at = NOW + timedelta(days=day)
            retry = self.log('prospective' + str(day))
            rid = self.run_record(at, success=True, retry=retry)
            self.observation(rid, at, action=revision(self.rule), retry=retry)
        result = fallback.monitor(self.root, now=NOW + timedelta(days=4))
        self.assertEqual(result['rules'][self.rule['id']]['status'], 'effective')
        self.assertEqual(result['rules'][self.rule['id']]['successes'], 3)

    def test_two_failed_or_unconfirmed_attempts_pause_without_agent_success_credit(self):
        for stage in (False, None, True):
            with self.subTest(stage=stage):
                for day in (1, 2):
                    at = NOW - timedelta(days=day, minutes=1 if stage is None else 2 if stage else 3)
                    rid = self.run_record(at)
                    self.observation(rid, at, action=revision(self.rule), stage=stage)
                result = fallback.monitor(self.root, now=NOW)
                self.assertEqual(result['rules'][self.rule['id']]['status'], 'paused')
                self.assertEqual(result['rules'][self.rule['id']]['successes'], 0)

    def test_poor_success_rate_pauses_even_without_consecutive_failures(self):
        self.baseline()
        fallback.monitor(self.root, now=NOW)
        for day in range(1, 6):
            at = NOW + timedelta(days=day)
            ok = day not in (1, 3)
            retry = self.log('rate' + str(day))
            rid = self.run_record(at, success=ok, retry=retry)
            self.observation(rid, at, action=revision(self.rule), stage=ok, retry=retry)
        result = fallback.monitor(self.root, now=NOW + timedelta(days=6))
        state = result['rules'][self.rule['id']]
        self.assertEqual((state['attempts'], state['successes'], state['status']), (5, 3, 'paused'))

    def test_other_phase_failure_is_separate_and_corrupt_audit_disables_authority(self):
        retry = self.log('unrelated-failure')
        rid = self.run_record(NOW - timedelta(days=1), retry=retry, daily_success=True)
        self.observation(rid, NOW - timedelta(days=1), action=revision(self.rule), retry=retry)
        result = fallback.monitor(self.root, now=NOW)
        self.assertEqual(result['rules'][self.rule['id']]['other_phase_failures'], 1)
        event = next((self.root / f'var/state/supervisor/runs/{rid}/events').glob('*run-finished.json'))
        event.write_text('{}')
        result = fallback.monitor(self.root, now=NOW)
        self.assertEqual(result['invalid_runs'], [rid])
        self.assertEqual(result['rules'][self.rule['id']]['status'], 'evidence-invalid')

    def test_history_survives_source_log_retention_and_device_lock_defers(self):
        self.baseline()
        fallback.monitor(self.root, now=NOW)
        shutil.rmtree(self.root / 'var/state/supervisor/runs')
        result = fallback.monitor(self.root, now=NOW + timedelta(days=1))
        self.assertEqual(result['rules'][self.rule['id']]['baseline_cases'], 3)
        with (self.root / 'var/run/zootd.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(fallback.monitor(self.root)['status'], 'busy')

    def test_depot_success_must_bind_to_depot_and_abandoned_trials_are_unconfirmed(self):
        for day, correct in ((1, True), (2, False)):
            at = NOW - timedelta(days=day)
            retry = self.log('depot' + str(day))
            rid = self.run_record(at, success=True, retry=retry, retry_phase='depot' if correct else 'daily')
            self.observation(rid, at, action=revision(self.rule), retry=retry, phase='depot')
        result = fallback.monitor(self.root, now=NOW)
        state = result['rules'][self.rule['id']]
        self.assertEqual((state['successes'], state['unconfirmed']), (1, 1))
        for day in (1, 2):
            at = NOW - timedelta(days=day, minutes=10)
            rid = self.run_record(at, finish=False)
            self.observation(rid, at, action=revision(self.rule), stage=None)
        result = fallback.monitor(self.root, now=NOW)
        self.assertEqual(result['rules'][self.rule['id']]['unconfirmed'], 3)

    def test_candidate_change_needs_new_logic_version_and_recurring_primary_evidence(self):
        candidate = self.root / 'candidate'
        shutil.copytree(self.root / 'maa_planner', candidate / 'maa_planner')
        shutil.copytree(self.root / 'tests', candidate / 'tests')
        new = {**self.rule, 'version': 2,
               'requires': {**self.rule['requires'], 'native_motion_timeout': True}}
        (candidate / 'maa_planner/fallback_rules.py').write_text('RULES = ' + repr([new]))
        samples = [{'run_id': str(i), 'at': (NOW - timedelta(days=d)).isoformat(),
                    'signals': SIGNALS, 'status': 'failed', 'action': None, 'outcome': None}
                   for i, d in enumerate((20, 16, 12))]
        self.assertEqual(validate_change(self.root, candidate, {'samples': samples, 'at': NOW.isoformat()})[0]['version'], 2)
        self.assertRaises(ValueError, validate_change, self.root, candidate, {'samples': samples[:1], 'at': NOW.isoformat()})
        samples[0].update(action={'revision': 'old'}, outcome='failed')
        self.assertRaises(ValueError, validate_change, self.root, candidate, {'samples': samples, 'at': NOW.isoformat()})


if __name__ == '__main__':
    unittest.main()
