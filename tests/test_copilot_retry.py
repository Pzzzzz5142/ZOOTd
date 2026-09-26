import copy
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from maa_planner.copilot_core import worker
from maa_planner.copilot_retry import RetryLimits, RetryBudget, classify_failure
from maa_planner.copilot_run import experiment
from maa_planner.copilot_matcher import OperatorCatalog, OperatorIdentity
from maa_planner.operator_box import OperatorBox, Operator
from maa_planner.prts import PrtsError
from tests.test_copilot_run import candidate
from tests.test_copilot_proof import observations


def event(msg, **kw):
    return {'message': msg, 'details': dict(uuid='device', taskid=7, taskchain='Copilot', **kw)}


def stamp(records):
    for i, e in enumerate(records):
        e.update(run_id='attempt-A', sequence=i, recorded_ns=100+i)
    return records


def failed_events(kind='missing'):
    records = [event(10001), event(20003, what='CopilotListLoadTaskFileSuccess',
               details={'stage_name': 'stage', 'file_name': '/attempt/execution.json'}),
               event(20001, subtask='BattleFormationTask')]
    if kind in ('missing', 'requirement', 'unchecked'):
        if kind == 'requirement':
            records.append(event(20003, subtask='BattleFormationTask', what='BattleFormationOperUnavailable',
                                 details={'oper_name': 'A', 'requirement_type': 'module'}))
        records.append(event(20000, subtask='BattleFormationTask', why='OperatorMissing',
                             details={'opers': {'slot': [{'name': 'A', 'reason': {
                                 'missing': 'Missing', 'requirement': 'Unavailable', 'unchecked': 'Unchecked'}[kind]}]}}))
        records.append(event(20000, subtask='BattleFormationTask'))
    elif kind == 'schema':
        records.append(event(20003, subtask='BattleFormationTask', what='BattleFormationParseFailed'))
        records.append(event(20000, subtask='BattleFormationTask'))
    elif kind == 'formation':
        records.append(event(20000, subtask='BattleFormationTask'))
    elif kind in ('battle', 'battle_unknown'):
        records += [event(20002, subtask='BattleFormationTask'), event(20001, subtask='BattleProcessTask')]
        if kind == 'battle':
            records.append(event(20002, subtask='ProcessTask', first=['Copilot@WaitUntilEndOfAction'],
                                 details={'task': 'FightMissionFailed', 'algorithm': 'OcrDetect',
                                          'action': 'ClickSelf', 'result': {'text': '任务失败', 'score': 0.98}}))
        records.append(event(20000, subtask='BattleProcessTask'))
    elif kind == 'navigation':
        records.pop()
    records += [event(10000), event(3, finished_tasks=[7])]
    return stamp(records)


def classify(records, **kw):
    context = dict(run_id='attempt-A', started_ns=100, finished_ns=200,
                   task_id=7, stage='stage', filename='/attempt/execution.json', exit_code=0)
    context.update(kw)
    return classify_failure(records, **context)


class RetryClassificationTests(unittest.TestCase):
    def test_explicit_candidate_failures(self):
        for kind, category in [('missing', 'formation_missing_operator'),
                               ('requirement', 'formation_requirement_unsatisfied'),
                               ('schema', 'copilot_schema_failure'), ('battle', 'battle_failed')]:
            with self.subTest(kind=kind):
                result = classify(failed_events(kind))
                self.assertEqual(result['category'], category)
                self.assertTrue(result['retryable'])
        self.assertEqual(classify(failed_events('requirement'))['evidence'],
                         [{'oper_name': 'A', 'requirement_type': 'module'}])

    def test_unknown_navigation_and_generic_failures_stop(self):
        for kind, category in [('unchecked', 'formation_other_failure'),
                               ('formation', 'formation_other_failure'),
                               ('battle_unknown', 'unknown_execution_failure'),
                               ('navigation', 'navigation_failure')]:
            self.assertEqual(classify(failed_events(kind))['category'], category)
            self.assertFalse(classify(failed_events(kind))['retryable'])

    def test_system_failure_overrides_candidate_failure(self):
        for added, category in [
            (event(20003, what='GameOffline'), 'runtime_failure'),
            (event(20003, what='Disconnect'), 'adb_failure'),
            (event(20003, what='Reconnecting'), 'adb_failure'),
            (event(0), 'runtime_failure'), (event(10004), 'runtime_failure'),
            (event(10000, details={'error': 'OpenCVException'}), 'runtime_failure'),
        ]:
            records = stamp(failed_events()+[added])
            self.assertEqual(classify(records)['category'], category)
            self.assertFalse(classify(records)['retryable'])
        for phase, category in [('adb', 'adb_failure'), ('navigation', 'navigation_failure'),
                                ('execution', 'runtime_failure')]:
            self.assertEqual(classify([], exit_code=1, worker_phase=phase)['category'], category)
        self.assertFalse(classify(failed_events(), exit_code=124)['retryable'])

    def test_old_wrong_or_missing_evidence_cannot_authorize_retry(self):
        for key, value in [('run_id', 'attempt-B'), ('task_id', 8), ('stage', 'other'),
                           ('filename', '/other'), ('started_ns', 102), ('exit_code', 1)]:
            self.assertFalse(classify(failed_events(), **{key: value})['retryable'])
        for index in (0, 1, 2, 3, 5, 6):
            records = failed_events()
            del records[index]
            self.assertFalse(classify(stamp(records))['retryable'], index)
        for index in range(len(failed_events())):
            records = failed_events()
            records[index]['details']['uuid'] = 'other'
            self.assertFalse(classify(records)['retryable'], index)
        records = failed_events()
        records[2], records[3] = records[3], records[2]
        self.assertFalse(classify(stamp(records))['retryable'])
        for invalid in (None, [], {'what': []}):
            records = failed_events()
            records[2]['details'] = invalid
            self.assertFalse(classify(records)['retryable'])

    def test_unmet_requirement_info_alone_does_not_authorize_retry(self):
        records = failed_events('requirement')
        records[4]['details'].pop('why')
        self.assertFalse(classify(records)['retryable'])
        records = failed_events('requirement')
        records[3]['details']['details']['requirement_type'] = 'unknown'
        self.assertFalse(classify(records)['retryable'])

    def test_battle_failure_requires_actual_recognition(self):
        for field, value in [('algorithm', 'JustReturn'), ('result', {}), ('task', 'Other')]:
            records = failed_events('battle')
            records[5]['details']['details'][field] = value
            self.assertFalse(classify(records)['retryable'])
        records = failed_events('battle')
        records[5]['details']['first'] = ['other']
        self.assertFalse(classify(records)['retryable'])

    def test_worker_failure_receipt_is_safe_and_phase_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / 'attempt-A'
            run.mkdir()
            def broken(root, attempt, address, progress):
                progress['phase'] = 'adb'
                raise RuntimeError('private account/URL')
            with patch('maa_planner.copilot_core._worker', side_effect=broken):
                self.assertEqual(worker(Path(directory), run, 'device'), 1)
            payload = json.loads((run / 'worker-result.json').read_text())
            self.assertEqual(payload, {'run_id': 'attempt-A', 'phase': 'adb', 'exit_code': 1})


    def test_bounds_and_reservations(self):
        for values in [dict(max_candidates=6), dict(max_candidates=True), dict(max_battles=4),
                       dict(max_battles=2), dict(sanity_budget=-1), dict(sanity_budget=True)]:
            with self.assertRaises(ValueError):
                RetryLimits(**values)
        budget = RetryBudget(RetryLimits(3, 3, 35), 18)
        self.assertTrue(budget.reserve_battle())
        self.assertFalse(budget.reserve_battle())
        self.assertEqual(budget.sanity_reserved, 18)
        for _ in range(3):
            self.assertTrue(budget.take_candidate())
        self.assertFalse(budget.take_candidate())
        for cost in (None, 0, -1, True):
            with self.assertRaises(ValueError):
                RetryBudget(RetryLimits(), cost)


class RetryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        root = self.root
        (root / 'config').mkdir()
        (root / 'config/copilot.toml').write_text(
            '[navigation.NL-8]\nactivity="长夜临光"\nmap_marker="NL-"\n')
        for name, payload in (
            ('var/state/runtime/maa-resource.json', {}),
            ('var/data/resource/stages.json', [{'code': 'NL-8', 'stageId': 'stage', 'apCost': 18}]),
            ('var/data/resource/battle_data.json', {}),
        ):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
        def mock(name, **kw):
            return self.stack.enter_context(patch('maa_planner.copilot_run.' + name, **kw))
        self.mock = mock
        mock('load_policy', return_value={'allow_support': False})
        self.commands = mock('command', side_effect=lambda args: '' if 'status' in args else 'fixture-head')
        mock('validate_runtime_receipt', return_value='fixture')
        mock('load_secret', return_value={})
        mock('SklandClient')
        box = OperatorBox('fixture', 'now', 'private', {'A': Operator('A', 2, 90, 6, 7, None, None)})
        self.box_fetch = mock('SklandBoxProvider').return_value.fetch_box
        self.box_fetch.return_value = box
        self.catalog_fetch = mock('fetch_catalog', return_value=(
            OperatorCatalog([OperatorIdentity('A', 'A', {1: 'sA'})]), {}))
        self.provider = mock('PrtsCopilotClient').return_value
        self.provider.query.return_value = {'candidates': [candidate(i).to_dict() for i in (1, 2, 3)], 'page': 1}
        self.content = {'stage_name': 'stage', 'opers': candidate().operators, 'actions': [{'type': 'SkillDaemon'}]}
        self.provider.get.return_value = self.content
        @contextmanager
        def fake_device(*_):
            yield 'fixture-device'
        self.dev = mock('device', side_effect=fake_device)
        self.outcomes = ['missing', 'success']
        self.paths = []
        self.execute = mock('execute', side_effect=self.fake_execute)

    def fake_execute(self, root, run, address):
        self.paths.append(run)
        params = json.loads((run / 'params.json').read_text())
        self.assertEqual(params['loop_times'], 1)
        self.assertFalse(params['use_sanity_potion'])
        self.assertFalse(params['ignore_requirements'])
        selection = json.loads((run / 'selection.json').read_text())
        self.assertGreaterEqual(selection['budget']['sanity_reserved'], len(self.paths)*18)
        kind = self.outcomes.pop(0)
        if kind == 'interrupt':
            raise KeyboardInterrupt
        records = observations() if kind == 'success' else failed_events('missing' if kind in ('offline', 'timeout', 'old') else kind)
        if kind == 'offline':
            records.append(event(20003, what='GameOffline'))
        for i, record in enumerate(records):
            record.update(run_id=run.name if kind != 'old' else 'old-attempt', sequence=i,
                          recorded_ns=time.monotonic_ns())
        records[1]['details']['details']['file_name'] = str(run / 'execution.json')
        (run / 'callbacks.jsonl').write_text('\n'.join(json.dumps(e) for e in records))
        (run / 'task-id.json').write_text('7')
        return 124 if kind == 'timeout' else 0

    def run_experiment(self, **kw):
        return experiment(self.root, 'NL-8', None, limits=RetryLimits(**dict(
            {'max_candidates': 3, 'max_battles': 2, 'sanity_budget': 36}, **kw)))

    def test_candidate_A_fails_B_succeeds_with_one_snapshot(self):
        result = self.run_experiment()
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual([a['copilot_id'] for a in result['attempts']], [1, 2])
        self.assertEqual(result['attempts'][0]['failure']['category'], 'formation_missing_operator')
        self.assertTrue(result['attempts'][1]['battle_proof']['three_star'])
        self.assertEqual(result['budget']['sanity_reserved'], 36)
        self.box_fetch.assert_called_once()
        self.provider.query.assert_called_once()
        self.catalog_fetch.assert_called_once()
        self.dev.assert_called_once()
        self.assertEqual(self.provider.get.call_count, 2)
        resets = [c for c in self.commands.call_args_list if 'force-stop' in c.args[0]]
        self.assertEqual(len(resets), 1)
        self.assertNotEqual(self.paths[0].name, self.paths[1].name)
        for i, path in enumerate(self.paths):
            self.assertEqual(json.loads((path / 'result.json').read_text()), result['attempts'][i])
        self.assertNotIn('private', json.dumps(result))
        self.assertFalse((self.root / 'var/state/planner/capabilities.json').exists())

    def test_system_timeout_old_evidence_and_interrupt_never_try_B(self):
        for kind in ('offline', 'timeout', 'old', 'interrupt', 'battle_unknown', 'navigation'):
            with self.subTest(kind=kind):
                self.outcomes = [kind, 'success']
                self.paths = []
                result = self.run_experiment()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(len(result['attempts']), 1, result)
                self.assertEqual(self.outcomes, ['success'])

    def test_budget_prevents_second_worker(self):
        for limits in ({'max_candidates': 1}, {'max_battles': 1}, {'sanity_budget': 35}):
            with self.subTest(limits=limits):
                self.outcomes = ['missing', 'success']
                self.paths = []
                result = self.run_experiment(**limits)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(len(self.paths), 1)
                self.assertEqual(result['budget']['sanity_reserved'], 18)
        self.paths = []
        self.outcomes = ['success']
        result = self.run_experiment(sanity_budget=0)
        self.assertEqual(result['stop_reason'], 'budget_exhausted')
        self.assertEqual(self.paths, [])

    def test_changed_candidate_skips_without_spending_or_reset(self):
        changed = copy.deepcopy(self.content)
        changed['opers'] = candidate(names=('B',)).operators
        self.provider.get.side_effect = [changed, self.content]
        self.outcomes = ['success']
        result = self.run_experiment(max_battles=1)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        self.assertEqual(result['attempts'][0]['failure']['category'], 'copilot_schema_failure')
        self.assertFalse(any('force-stop' in c.args[0] for c in self.commands.call_args_list))

    def test_protocol_or_network_error_stops_before_device(self):
        for category in ('network', 'schema_error', 'stage_mismatch'):
            self.provider.get.side_effect = PrtsError(category, 'sensitive URL')
            result = self.run_experiment()
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(len(result['attempts']), 1)
            self.assertNotIn('sensitive URL', json.dumps(result))
        self.dev.assert_not_called()

    def test_missing_stage_cost_stops_before_box_or_device(self):
        path = self.root / 'var/data/resource/stages.json'
        path.write_text(json.dumps([{'code': 'NL-8', 'stageId': 'stage'}]))
        result = self.run_experiment()
        self.assertEqual(result['failure_phase'], 'readiness')
        self.box_fetch.assert_not_called()
        self.dev.assert_not_called()

    def test_reset_failure_stops_without_second_worker(self):
        def command(args):
            if 'force-stop' in args:
                raise RuntimeError('reset failed')
            return '' if 'status' in args else 'head'
        self.commands.side_effect = command
        result = self.run_experiment()
        self.assertEqual(result['stop_reason'], 'adb_failure')
        self.assertEqual(len(self.paths), 1)
        self.assertEqual(result['attempts'][1]['failure_phase'], 'reset')

    def test_default_still_runs_only_one_candidate(self):
        result = experiment(self.root, 'NL-8', None)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['budget']['sanity_limit'], 18)

    def test_exhausted_candidates_keep_all_failures(self):
        self.outcomes = ['missing', 'requirement', 'battle']
        result = self.run_experiment(max_battles=3, sanity_budget=54)
        self.assertEqual(result['stop_reason'], 'candidates_exhausted')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['attempts']), 3)
        self.assertTrue(all(a['status'] == 'failed' for a in result['attempts']))
        self.assertEqual(result['budget']['sanity_reserved'], 54)


    def test_no_resume_and_no_reuse_of_previous_attempt_artifacts(self):
        first = self.run_experiment()
        before = [(path / 'result.json').read_bytes() for path in self.paths]
        self.paths = []
        self.outcomes = ['offline']
        second = self.run_experiment()
        self.assertNotEqual(first['run_dir'], second['run_dir'])
        self.assertEqual(second['status'], 'failed')
        for old, expected in zip(first['attempts'], before):
            self.assertEqual((Path(old['run_dir']) / 'result.json').read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()
