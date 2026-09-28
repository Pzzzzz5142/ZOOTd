import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_planner.copilot_capability import complete_proof, record, prepare, safe_proxy_tasks, scope
from maa_planner.capability import CapabilityLedger, CapabilityKey
from maa_planner.util import sha256_bytes
from tests.test_copilot_proof import observations, prove, stamp


def custom(taskid, task, result, *, first=None, algorithm='OcrDetect', action='DoNothing'):
    base = dict(taskchain='Custom', uuid='device', taskid=taskid)
    return [dict(message=10001, details=base.copy()),
            dict(message=20002, details=dict(base, subtask='ProcessTask', first=[first or task],
                 details=dict(task=task, result=result, algorithm=algorithm, action=action))),
            dict(message=10002, details=base.copy()),
            dict(message=3, details=dict(base, finished_tasks=[taskid]))]


def fixture():
    pre = custom(1, 'ZootdCopilotUID', {'text': 'UID:12345678'})
    battle = observations()
    post = custom(8, 'ZootdCopilotUID', {'text': 'UID:12345678'})
    post += custom(9, 'ZootdCopilotProofStage', {'text': 'NL-8'}, action='ClickSelf')
    post += custom(10, 'UsePrtsSuccessCheck', {'template': 'UsePrtsSuccess.png'},
                   first='StageQueue@CheckPrts', algorithm='MatchTemplate')
    events = stamp(pre + battle + post)
    proof = prove(events[:len(pre) + len(battle)])
    context = {'client': 'Official', 'account': 'fixture-account', 'expected_uid': '12345678',
               'uid_sha256': sha256_bytes(b'Official:12345678'), 'battle_stages': ['stage'],
               'stage': 'stage', 'stage_code': 'NL-8', 'activity_instance': 'a' * 24, 'enroll': True}
    return events, proof, context


def complete(events, proof, context):
    return complete_proof(events, proof, context, started_ns=100, finished_ns=200)


class CapabilityProofTests(unittest.TestCase):
    def test_complete_account_battle_proxy_chain(self):
        events, battle, context = fixture()
        result = complete(events, battle, context)
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(result['account_binding'], 'verified')
        self.assertEqual(result['saved_proxy'], 'verified')
        self.assertEqual(result['first_clear'], 'unknown')
        self.assertFalse(result['ledger_recorded'])

    def test_each_account_and_proxy_event_required(self):
        events, battle, context = fixture()
        for i in list(range(4)) + list(range(11, len(events))):
            changed = copy.deepcopy(events)
            del changed[i]
            # Retain recorder numbering; missing evidence cannot be replayed.
            self.assertNotEqual(complete(changed, battle, context)['status'], 'verified', i)
        for index in (1, 12, 16, 20):
            changed = copy.deepcopy(events)
            changed[index]['details']['details']['result'] = {'text': 'wrong', 'template': 'wrong'}
            self.assertNotEqual(complete(changed, battle, context)['status'], 'verified', index)

    def test_wrong_scope_account_support_and_stale_evidence(self):
        events, battle, context = fixture()
        for key, value in [('expected_uid', '99999999'), ('client', 'Bilibili'),
                           ('battle_stages', ['other-stage'])]:
            changed = dict(context, **{key: value})
            self.assertNotEqual(complete(events, battle, changed)['status'], 'verified')
        self.assertNotEqual(complete(events, dict(battle, support_used=True), context)['status'], 'verified')
        for index in (0, 11, 15, 19):
            changed = copy.deepcopy(events)
            changed[index]['details']['uuid'] = 'other-device'
            self.assertNotEqual(complete(changed, battle, context)['status'], 'verified')
        changed = copy.deepcopy(events)
        changed[-1]['run_id'] = 'old'
        self.assertNotEqual(complete(changed, battle, context)['status'], 'verified')

    def test_chain_completion_without_all_tasks_is_not_proof(self):
        events, battle, context = fixture()
        for i in (3, 14, 18, 22):
            changed = copy.deepcopy(events)
            changed[i]['message'] = 20003
            self.assertNotEqual(complete(changed, battle, context)['status'], 'verified', i)
        changed = copy.deepcopy(events)
        changed[-1]['details']['finished_tasks'] = [9]
        self.assertNotEqual(complete(changed, battle, context)['status'], 'verified')

    def test_shared_ledger_enrollment_idempotency_and_manual_quarantine(self):
        events, battle, context = fixture()
        proof = complete(events, battle, context)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = type('Config', (), {'client_type': 'Official', 'account': 'fixture-account'})()
            with patch('maa_planner.copilot_capability.scope', return_value='a' * 24), \
                 patch('maa_planner.copilot_capability.load_config', return_value=config):
                self.assertTrue(record(root, context, proof))
                self.assertFalse(record(root, context, proof))
                path = root / 'var/state/planner/capabilities.json'
                ledger = CapabilityLedger.load(path)
                key = CapabilityKey('Official', 'fixture-account', 'a' * 24, 'NL-8')
                self.assertTrue(ledger.is_verified(key))
                self.assertEqual(ledger.query(key).success_count, 1)
                binding = root / 'var/state/copilot-account-binding.json'
                self.assertNotIn('12345678', binding.read_text())
                self.assertEqual(binding.stat().st_mode & 0o777, 0o600)
                ledger.mark_quarantined(key, 'manual operator decision')
                ledger.save(path)
                with self.assertRaises(ValueError):
                    record(root, context, dict(proof, run_id='new-run'))

    def test_enrollment_requires_explicit_flag_and_never_rebinds(self):
        from types import SimpleNamespace
        from maa_planner.prts import StageCatalog
        config = SimpleNamespace(client_type='Official', account='fixture-account')
        box = SimpleNamespace(account_id='Official:12345678')
        catalog = StageCatalog([{'stageId': 'stage', 'code': 'NL-8'}])
        with tempfile.TemporaryDirectory() as tmp, \
             patch('maa_planner.copilot_capability.load_config', return_value=config), \
             patch('maa_planner.copilot_capability.scope', return_value='a' * 24), \
             patch('maa_planner.copilot_capability.StageCatalog.load', return_value=catalog):
            root = Path(tmp)
            with self.assertRaises(ValueError):
                prepare(root, box, 'stage', 'NL-8')
            context = prepare(root, box, 'stage', 'NL-8', enroll=True)
            binding = root / 'var/state/copilot-account-binding.json'
            self.assertFalse(binding.exists())
            binding.parent.mkdir(parents=True)
            binding.write_text(json.dumps({k: context[k] for k in ('client', 'account', 'uid_sha256')}))
            self.assertEqual(prepare(root, box, 'stage', 'NL-8')['account'], 'fixture-account')
            box.account_id = 'Official:87654321'
            with self.assertRaises(ValueError):
                prepare(root, box, 'stage', 'NL-8', enroll=True)

    def test_record_rechecks_scope_and_deduplicates_after_newer_observation(self):
        events, battle, context = fixture()
        proof = complete(events, battle, context)
        config = type('Config', (), {'client_type': 'Official', 'account': 'fixture-account'})()
        with tempfile.TemporaryDirectory() as tmp, \
             patch('maa_planner.copilot_capability.scope', return_value='a' * 24) as current_scope, \
             patch('maa_planner.copilot_capability.load_config', return_value=config):
            root = Path(tmp)
            self.assertTrue(record(root, context, proof))
            self.assertTrue(record(root, context, dict(proof, run_id='new-run')))
            self.assertFalse(record(root, context, proof))
            ledger = CapabilityLedger.load(root / 'var/state/planner/capabilities.json')
            self.assertEqual(ledger.entries()[0].success_count, 2)
            current_scope.return_value = 'b' * 24
            with self.assertRaises(ValueError):
                record(root, context, dict(proof, run_id='third-run'))

    def test_unproven_never_creates_ledger(self):
        events, battle, context = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(record(root, context, battle))
            self.assertFalse((root / 'var/state/planner/capabilities.json').exists())

    def test_proxy_overlay_disables_every_spending_entry(self):
        overlay = safe_proxy_tasks('NL-8')
        for name in ('GoLastBattle', 'StartButton1', 'StartButton2', 'MedicineConfirm',
                     'ExpiringMedicineConfirm', 'StoneConfirm'):
            self.assertEqual(overlay[name]['action'], 'Stop')
            for edge in ('next', 'sub', 'onErrorNext', 'exceededNext'):
                self.assertEqual(overlay[name][edge], [])

    def test_permanent_scope_requires_both_installed_catalogs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resource = root / 'var/data/resource'
            tiles = resource / 'Arknights-Tile-Pos'
            tiles.mkdir(parents=True)
            (resource / 'stages.json').write_text(json.dumps([
                {'stageId': 'act13side_08_perm', 'code': 'NL-8'}]))
            (tiles / 'overview.json').write_text(json.dumps({'tile': {
                'stageId': 'act13side_08', 'code': 'NL-8', 'levelId': 'level'}}))
            self.assertEqual(len(scope(root, 'act13side_08_perm', 'NL-8')), 24)
            with self.assertRaises(ValueError):
                scope(root, 'other', 'NL-8')
            (tiles / 'overview.json').write_text('{}')
            with self.assertRaises((ValueError, OSError)):
                scope(root, 'act13side_08_perm', 'NL-8')
