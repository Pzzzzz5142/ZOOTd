import copy
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from maa_planner.copilot_core import terminal_result
from maa_planner.copilot_matcher import OperatorCatalog, OperatorIdentity, match_candidate
from maa_planner.copilot_run import (bind_formation, device_lock, experiment, load_policy,
                                     select_candidate, ExperimentError)
from maa_planner.copilot_static import build_catalog
from maa_planner.operator_box import Operator, OperatorBox
from maa_planner.prts import CopilotCandidate, StageCatalog, PrtsCopilotClient
from tests.test_prts import Opener, page, row


def candidate(id=1, names=('A',)):
    return CopilotCandidate(id, 'stage', 'test',
                            [{'name': n, 'skill': 1, 'role': None, 'requirements': {}} for n in names], [], None, {})


def events():
    def e(msg, **kw):
        return {'message': msg, 'details': dict(uuid='device', taskid=7, taskchain='Copilot', **kw)}
    return [e(10001), e(20003, what='CopilotListLoadTaskFileSuccess',
                       details={'stage_name': 'stage', 'file_name': '/run/execution.json'}),
            e(20002, subtask='BattleFormationTask'), e(20002, subtask='BattleProcessTask'),
            e(10002), {'message': 3, 'details': {'finished_tasks': [6, 7]}}]


def result(records):
    return terminal_result(records, task_id=7, stage='stage', filename='/run/execution.json')


class CopilotRunTests(unittest.TestCase):
    def setUp(self):
        self.catalog = OperatorCatalog([OperatorIdentity(n, n, {1: 's' + n}) for n in ('A', 'B', 'C')])
        self.box = OperatorBox('fixture', 'now', 'private',
                               {'A': Operator('A', 2, 90, 6, 7, None, None)})

    def test_policies_exact_preferred_even_when_support_allowed(self):
        options = [candidate(1, ('B',)), candidate(2)]
        for support in (False, True):
            self.assertEqual(select_candidate(self.box, options, self.catalog, support)[0].copilot_id, 2)

    def test_support_policy_and_unknown_are_enforced(self):
        self.assertIsNone(select_candidate(self.box, [candidate(1, ('B',))], self.catalog, False)[0])
        self.assertEqual(select_candidate(self.box, [candidate(1, ('B',))], self.catalog, True)[0].support_operator, 'B')
        for support in (False, True):
            self.assertIsNone(select_candidate(self.box, [candidate(1, ('unknown',))], self.catalog, support)[0])
            self.assertIsNone(select_candidate(self.box, [candidate(1, ('B', 'C'))], self.catalog, support)[0])

    def test_profiles(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse(load_policy(root, None)['allow_support'])
        self.assertTrue(load_policy(root, 'allow-support')['allow_support'])

    def test_global_assignment_is_bound_into_executable_groups(self):
        c = candidate()
        c.groups.append({'name': 'healer', 'operators': candidate(names=('A', 'B')).operators})
        matched = match_candidate(self.box, c, self.catalog)
        self.assertEqual(matched.support_operator, 'B')
        original = {'opers': c.operators, 'groups': [{'name': 'healer', 'opers': c.groups[0]['operators']}],
                    'actions': [{'name': 'healer'}]}
        bound = bind_formation(original, matched, self.catalog)
        self.assertEqual([o['name'] for o in bound['groups'][0]['opers']], ['B'])
        self.assertEqual(bound['actions'], original['actions'])
        self.assertEqual(len(original['groups'][0]['opers']), 2)

    def test_complete_terminal_required(self):
        self.assertEqual(result(events())['status'], 'success')
        for i in range(len(events())):
            records = events()
            del records[i]
            self.assertEqual(result(records)['status'], 'failed', i)
        self.assertEqual(result([])['status'], 'failed')

    def test_wrong_task_stage_file_device_cannot_prove_success(self):
        for change in ('task', 'device', 'file', 'stage'):
            records = events()
            if change in ('task', 'device'):
                records[3]['details']['taskid' if change == 'task' else 'uuid'] = 99
            else:
                records[1]['details']['details']['file_name' if change == 'file' else 'stage_name'] = 'wrong'
            self.assertEqual(result(records)['status'], 'failed')

    def test_failure_after_completion_vetoes_terminal(self):
        for message in (0, 1, 10000, 10004):
            self.assertEqual(result(events() + [{'message': message, 'details': {}}])['status'], 'failed')
        self.assertEqual(result(events() + [{'message': 20003, 'details': {'what': 'GameOffline'}}])['status'], 'failed')

    def test_reordered_or_duplicate_chains_fail(self):
        records = events()
        records[2], records[3] = records[3], records[2]
        self.assertEqual(result(records)['status'], 'failed')
        self.assertEqual(result(events() + events())['status'], 'failed')

    def test_lock_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with device_lock(Path(tmp)):
                with self.assertRaises(ExperimentError):
                    with device_lock(Path(tmp)):
                        self.fail('concurrent device access')

    def test_static_indices_not_box_or_array_order(self):
        catalog = build_catalog({'char_a': {'name': 'A', 'skills': [{'skillId': 's2'}, {'skillId': 's1'}]},
                                 'token_a': {'name': 'T', 'skills': [{'skillId': None}]}},
                                {'charEquip': {'char_a': ['mod2', 'mod1']}, 'equipDict': {
                                    'mod1': {'charEquipOrder': 1}, 'mod2': {'charEquipOrder': 2}}},
                                {'chars': {'char_a': {'name': 'A'}, 'token_a': {'name': 'T'}}})
        self.assertEqual(catalog.resolve('A').skills, {1: 's2', 2: 's1'})
        self.assertEqual(catalog.resolve('A').modules, {1: 'mod1', 2: 'mod2'})
        self.assertIsNone(catalog.resolve('T'))

    def test_permanent_alias_requires_both_catalogs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'stages.json').write_text(json.dumps([{'code': 'NL-8', 'stageId': 'act13side_08_perm'}]))
            (root / 'Arknights-Tile-Pos').mkdir()
            path = root / 'Arknights-Tile-Pos/overview.json'
            path.write_text(json.dumps({'tile': {'code': 'NL-8', 'stageId': 'act13side_08'}}))
            catalog = StageCatalog.load(root / 'stages.json')
            self.assertEqual(catalog.resolve('act13side_08'), catalog.resolve('NL-8'))
            self.assertEqual(catalog.query_ids['act13side_08_perm'], 'act13side_08')
            path.write_text(json.dumps({'tile': {'code': 'NL-7', 'stageId': 'act13side_08'}}))
            self.assertEqual(StageCatalog.load(root / 'stages.json').query_ids, {})

    def test_video_guides_not_executable(self):
        video = dict(row(), type='VIDEO')
        client = PrtsCopilotClient(StageCatalog([{'code': '1-7', 'stageId': 'main_01-07'}]),
                                  opener=Opener(page([video])))
        self.assertEqual(client.query('1-7')['candidates'], [])

    def test_preflight_failure_records_reason_without_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('maa_planner.copilot_run.load_policy', return_value={'allow_support': False}), \
                 patch('maa_planner.copilot_run.command', return_value=' M tracked.py'), \
                 patch('maa_planner.copilot_run.device') as device:
                audit = experiment(root, 'NL-8', None)
            self.assertEqual(audit['failure_phase'], 'readiness')
            self.assertEqual(audit['status'], 'failed')
            self.assertTrue((Path(audit['run_dir']) / 'result.json').exists())
            device.assert_not_called()

    def test_full_pipeline_binds_parameters_and_rejects_download_drift(self):
        for drift in (False, True):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp, ExitStack() as mocks:
                root = Path(tmp)
                (root / 'config').mkdir()
                (root / 'config/copilot.toml').write_text(
                    '[navigation.NL-8]\nactivity="长夜临光"\nmap_marker="NL-"\n')
                for name, payload in (
                    ('var/state/runtime/maa-resource.json', {}),
                    ('var/data/resource/stages.json', [{'code': 'NL-8', 'stageId': 'stage'}]),
                    ('var/data/resource/battle_data.json', {}),
                ):
                    p = root / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(payload))
                def mock(name, **kw):
                    return mocks.enter_context(patch('maa_planner.copilot_run.' + name, **kw))
                mock('load_policy', return_value={'allow_support': True})
                mock('command', side_effect=['', 'fixture-head'])
                mock('validate_runtime_receipt', return_value='fixture')
                mock('load_secret', return_value={})
                mock('SklandClient')
                mock('SklandBoxProvider').return_value.fetch_box.return_value = self.box
                mock('fetch_catalog', return_value=(self.catalog, {}))
                provider = mock('PrtsCopilotClient').return_value
                provider.query.return_value = {'candidates': [candidate().to_dict()], 'page': 1}
                provider.get.return_value = {'stage_name': 'stage', 'opers': candidate().operators,
                                             'actions': [{'type': 'SkillDaemon'}]}
                if drift:
                    provider.get.return_value['opers'] = candidate(names=('B',)).operators
                @contextmanager
                def fake_device(*_):
                    yield 'fixture-device'
                dev = mock('device', side_effect=fake_device)
                def fake_execute(root, run, address):
                    params = json.loads((run / 'params.json').read_text())
                    self.assertEqual(params['loop_times'], 1)
                    self.assertFalse(params['use_sanity_potion'])
                    self.assertFalse(params['ignore_requirements'])
                    self.assertEqual(params['support_unit_usage'], 0)
                    self.assertEqual(len(params['copilot_list']), 1)
                    self.assertFalse(params['copilot_list'][0]['is_raid'])
                    records = events()
                    records[1]['details']['details']['file_name'] = str(run / 'execution.json')
                    (run / 'callbacks.jsonl').write_text('\n'.join(json.dumps(e) for e in records))
                    (run / 'task-id.json').write_text('7')
                    return 0
                mock('execute', side_effect=fake_execute)
                audit = experiment(root, 'NL-8', None)
                self.assertEqual(audit['status'], 'failed' if drift else 'success')
                if drift:
                    dev.assert_not_called()
                    self.assertEqual(audit['failure_phase'], 'download_recheck')
                self.assertNotIn('private', json.dumps(audit))


if __name__ == '__main__':
    unittest.main()
