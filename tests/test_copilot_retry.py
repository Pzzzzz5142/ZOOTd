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
from maa_planner.copilot_run import experiment, acceptance_formation_failure, ExperimentError
from maa_planner.copilot_matcher import OperatorCatalog, OperatorIdentity
from maa_planner.operator_box import OperatorBox, Operator
from maa_planner.prts import PrtsError
from tests.test_copilot_run import candidate, raid_confirmation
from tests.test_copilot_proof import observations, support_helpers


def event(msg, **kw):
    return {'message': msg, 'details': dict(uuid='device', taskid=7, taskchain='Copilot', **kw)}


def stamp(records):
    for i, e in enumerate(records):
        e.update(run_id='attempt-A', sequence=i, recorded_ns=100+i)
    return records


def failed_events(kind='missing'):
    if kind == 'zero_star_mission_failed_complete':
        records = failed_events('zero_star_complete')
        records.insert(6, copy.deepcopy(failed_events('battle')[5]))
        return stamp(records)
    if kind == 'zero_star_complete':
        records = failed_events('two_star_complete')
        for record in records:
            detail = record['details'].get('details', {})
            if detail.get('task') == 'StageDrops-Stars-2':
                detail['task'] = 'StageDrops-Stars-0'
                detail['result']['template'] = 'StageDrops-Stars-0.png'
        return records
    if kind == 'two_star_auxiliary_complete':
        records = failed_events('two_star_complete')
        records[3:3] = formation_swipe_events()
        return stamp(records)
    if kind == 'two_star_complete':
        records = failed_events('two_star')
        completed = records.pop(-3)
        completed['message'] = 20002
        records.insert(5, completed)
        records[-2]['message'] = 10002
        return stamp(records)
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
    elif kind in ('battle', 'two_star', 'battle_unknown'):
        records += [event(20002, subtask='BattleFormationTask'), event(20001, subtask='BattleProcessTask')]
        if kind == 'battle':
            records.append(event(20002, subtask='ProcessTask', first=['Copilot@WaitUntilEndOfAction'],
                                 details={'task': 'FightMissionFailed', 'algorithm': 'OcrDetect',
                                          'action': 'ClickSelf', 'result': {'text': '任务失败', 'score': 0.98}}))
        if kind == 'two_star':
            records.append(event(20002, subtask='ProcessTask', first=['Copilot@WaitUntilEndOfAction'],
                                 details={'task': 'StageDrops-Stars-2', 'algorithm': 'MatchTemplate',
                                          'action': 'DoNothing', 'result': {
                                              'template': 'StageDrops-Stars-2.png', 'score': 0.98}}))
        records.append(event(20000, subtask='BattleProcessTask'))
    elif kind == 'navigation':
        records.pop()
    records += [event(10000), event(3, finished_tasks=[7])]
    return stamp(records)


def formation_swipe_events():
    return [{'message': message, 'details': {
        'uuid': 'device', 'taskid': 0, 'taskchain': 'Copilot', 'subtask': 'ProcessTask',
        'class': 'asst::ProcessTask', 'first': ['BattleQuickFormationSkill-SwipeToTheDown'],
        'pre_task': '', 'details': {'task': 'BattleQuickFormationSkill-SwipeToTheDown',
        'algorithm': 'JustReturn', 'action': 'Swipe', 'result': {}}}} for message in (20001, 20002)]


def classify(records, **kw):
    context = dict(run_id='attempt-A', started_ns=100, finished_ns=200,
                   task_id=7, stage='stage', filename='/attempt/execution.json', exit_code=0)
    context.update(kw)
    return classify_failure(records, **context)


class RetryClassificationTests(unittest.TestCase):
    def test_native_support_search_allows_only_proven_formation_failure_retry(self):
        records = failed_events('missing')
        records[3:3] = support_helpers()
        outcome = classify(stamp(records))
        self.assertEqual(outcome['category'], 'formation_missing_operator')
        self.assertTrue(outcome['retryable'])
        self.assertEqual(outcome['sanity_outcome'], 'not_spent')
        for index in range(3, 3 + len(support_helpers())):
            for field, value in [('uuid', 'other'), ('taskid', 8), ('class', 'Other'), ('why', 'error')]:
                changed = copy.deepcopy(records)
                changed[index]['details'][field] = value
                self.assertFalse(classify(stamp(changed))['retryable'])
            for field, value in [('task', 'StageDrops-Stars-3'), ('action', 'Wrong'), ('algorithm', 'Wrong')]:
                changed = copy.deepcopy(records)
                changed[index]['details']['details'][field] = value
                self.assertFalse(classify(stamp(changed))['retryable'])
        for field, value in [('template', 'Warrior@SupportList-SelectRole.png'), ('score', float('nan'))]:
            changed = copy.deepcopy(records)
            changed[3]['details']['details']['result'][field] = value
            self.assertFalse(classify(stamp(changed))['retryable'])
        for index in (0, 2, 6, 7):
            changed = failed_events('missing')
            changed[index:index] = support_helpers()
            self.assertFalse(classify(stamp(changed))['retryable'])
        changed = failed_events('formation')
        changed[3:3] = support_helpers()
        self.assertFalse(classify(stamp(changed))['retryable'])

    def test_native_auxiliary_swipe_only_ignored_during_bound_formation(self):
        records = failed_events('two_star_auxiliary_complete')
        self.assertTrue(classify(records)['retryable'])
        for field, value in [('uuid', 'other-device'), ('taskid', 8), ('taskid', False),
                             ('first', ['Other']), ('pre_task', 'Other'), ('class', 'Other')]:
            changed = copy.deepcopy(records)
            changed[3]['details'][field] = value
            self.assertFalse(classify(changed)['retryable'])
        for field, value in [('action', 'ClickSelf'), ('algorithm', 'MatchTemplate'),
                             ('result', {'template': 'Other.png'})]:
            changed = copy.deepcopy(records)
            changed[3]['details']['details'][field] = value
            self.assertFalse(classify(changed)['retryable'])
        changed = copy.deepcopy(records)
        changed[3]['message'] = 20000
        self.assertFalse(classify(changed)['retryable'])
        for index in (0, 2, 5, 7, 9):
            changed = failed_events('two_star_complete')
            changed[index:index] = formation_swipe_events()
            self.assertFalse(classify(stamp(changed))['retryable'])

    def test_completed_zero_star_refunds_but_two_star_keeps_cost_with_bound_evidence(self):
        for stars, kind in ((0, 'zero_star_complete'), (2, 'two_star_complete')):
            with self.subTest(stars=stars):
                records = failed_events(kind)
                star_index = next(i for i, e in enumerate(records)
                                  if e['details'].get('details', {}).get('task') == f'StageDrops-Stars-{stars}')
                outcome = classify(records)
                self.assertEqual(outcome['category'], 'battle_failed')
                self.assertTrue(outcome['retryable'])
                self.assertEqual(outcome['sanity_outcome'], 'refunded' if stars == 0 else 'charged_or_unknown')
                for index in range(len(records)):
                    changed = copy.deepcopy(records)
                    del changed[index]
                    self.assertFalse(classify(stamp(changed))['retryable'])
                for field, value in [('uuid', 'other-device'), ('taskid', 8), ('first', ['Other'])]:
                    changed = copy.deepcopy(records)
                    changed[star_index]['details'][field] = value
                    self.assertFalse(classify(changed)['retryable'])
                for field, value in [('action', 'ClickSelf'), ('algorithm', 'JustReturn'),
                                     ('result', {'template': 'StageDrops-Stars-3.png', 'score': 0.99}),
                                     ('result', {'template': f'StageDrops-Stars-{stars}.png', 'score': 0.79})]:
                    changed = copy.deepcopy(records)
                    changed[star_index]['details']['details'][field] = value
                    self.assertFalse(classify(changed)['retryable'])
                changed = copy.deepcopy(records)
                contradictory = copy.deepcopy(records[star_index])
                contradictory['details']['details']['task'] = 'StageDrops-Stars-3'
                changed.insert(-2, contradictory)
                self.assertFalse(classify(stamp(changed))['retryable'])

    def test_raid_complete_zero_star_refund_requires_mode_and_rejects_two_star_contradiction(self):
        records = failed_events('zero_star_complete')
        self.assertFalse(classify(records, raid=True)['retryable'])
        records.insert(2, raid_confirmation())
        self.assertEqual(classify(stamp(records), raid=True)['sanity_outcome'], 'refunded')
        contradictory = copy.deepcopy(records[7])
        contradictory['details']['details'].update(task='StageDrops-Stars-2',
            result={'template': 'StageDrops-Stars-2.png', 'score': .99})
        records.insert(-2, contradictory)
        self.assertFalse(classify(stamp(records), raid=True)['retryable'])

    def test_complete_zero_star_defeat_with_explicit_failure_uses_refund_contract(self):
        records = failed_events('zero_star_mission_failed_complete')
        outcome = classify(records)
        self.assertEqual(outcome['category'], 'battle_failed')
        self.assertTrue(outcome['retryable'])
        self.assertEqual(outcome['sanity_outcome'], 'refunded')
        records.insert(2, raid_confirmation())
        self.assertEqual(classify(stamp(records), raid=True)['sanity_outcome'], 'refunded')
        records = failed_events('zero_star_mission_failed_complete')
        records[7]['details']['details'].update(task='StageDrops-Stars-2',
            result={'template': 'StageDrops-Stars-2.png', 'score': 0.98})
        self.assertFalse(classify(records)['retryable'])

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

    def test_optional_prts_probe_before_proven_formation_failure(self):
        probe = event(20000, subtask='ProcessTask', first=['NotUsePrts'],
                      pre_task='', details={}, **{'class': 'asst::ProcessTask'})
        records = failed_events('missing')
        records.insert(2, probe)
        self.assertEqual(classify(stamp(records))['category'], 'formation_missing_operator')
        for changes in [{'first': ['Other']}, {'pre_task': 'Other'},
                        {'details': {'error': 'failure'}}, {'why': 'error'},
                        {'class': 'Other'}]:
            changed = copy.deepcopy(records)
            changed[2]['details'].update(changes)
            self.assertFalse(classify(stamp(changed))['retryable'])
        for index in (0, 4):
            changed = failed_events('missing')
            changed.insert(index, probe)
            self.assertFalse(classify(stamp(changed))['retryable'])
        records.insert(2, copy.deepcopy(probe))
        self.assertFalse(classify(stamp(records))['retryable'])
        # Probe alone or followed by an unrelated navigation error cannot retry.
        records = failed_events('navigation')
        records.insert(2, probe)
        self.assertFalse(classify(stamp(records))['retryable'])

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
                               ('stage_locked', 'stage_locked'),
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


    def test_only_proven_zero_cost_failure_releases_sanity(self):
        outcome = classify(failed_events('two_star_complete'), exit_code=1, worker_phase='imperfect_result')
        self.assertEqual(outcome['category'], 'imperfect_result')
        self.assertFalse(outcome['retryable'])
        self.assertEqual(outcome['sanity_outcome'], 'charged_or_unknown')
        for kind, outcome in [('missing', 'not_spent'), ('requirement', 'not_spent'),
                              ('schema', 'not_spent'), ('battle', 'refunded'),
                              ('two_star', 'charged_or_unknown'),
                              ('battle_unknown', 'charged_or_unknown')]:
            self.assertEqual(classify(failed_events(kind))['sanity_outcome'], outcome)
        self.assertEqual(classify(failed_events('battle'), exit_code=124)['sanity_outcome'],
                         'charged_or_unknown')
        records = failed_events('battle')
        records.insert(-2, failed_events('two_star')[5])
        self.assertEqual(classify(stamp(records))['sanity_outcome'], 'charged_or_unknown')
        records = stamp(failed_events('battle')+[event(20003, what='GameOffline')])
        self.assertEqual(classify(records)['sanity_outcome'], 'charged_or_unknown')

    def test_raid_explicit_failure_refunds_and_requires_mode_evidence(self):
        records = failed_events('battle')
        records.insert(2, raid_confirmation())
        outcome = classify(stamp(records), raid=True)
        self.assertEqual(outcome['category'], 'battle_failed')
        self.assertTrue(outcome['retryable'])
        self.assertEqual(outcome['sanity_outcome'], 'refunded')
        self.assertFalse(classify(failed_events('battle'), raid=True)['retryable'])
        records = failed_events('missing')
        records.insert(2, raid_confirmation())
        self.assertEqual(classify(stamp(records), raid=True)['sanity_outcome'], 'not_spent')

    def test_settlement_is_once_per_dispatch_and_does_not_restore_attempt_count(self):
        budget = RetryBudget(RetryLimits(3, 2, 18), 18)
        self.assertTrue(budget.reserve_battle())
        self.assertEqual(budget.settle(1, zero_cost=True), 18)
        self.assertEqual(budget.sanity_reserved, 0)
        self.assertTrue(budget.reserve_battle())
        self.assertEqual(budget.settle(2, zero_cost=True), 18)
        self.assertEqual(budget.sanity_released, 36)
        self.assertFalse(budget.reserve_battle())
        for reservation in (0, 1, 2, 3, True):
            with self.assertRaises(ValueError):
                budget.settle(reservation, zero_cost=True)
        self.assertEqual(budget.sanity_reserved, 0)


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
        free = RetryBudget(RetryLimits(), 0)
        self.assertTrue(free.reserve_battle())
        self.assertFalse(free.reserve_battle())
        for cost in (None, -1, True):
            with self.assertRaises(ValueError):
                RetryBudget(RetryLimits(), cost)


class RetryIntegrationTests(unittest.TestCase):
    def test_verified_leak_abort_retries_with_refund_without_claiming_clear(self):
        self.outcomes = ['two_star_complete', 'success']
        self.provider.get.side_effect = [self.content,
            dict(self.content, actions=[{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}])]
        original = self.fake_execute
        def execute_abort(root, run, address):
            status = original(root, run, address)
            if len(self.paths) == 1:
                (run / 'worker-result.json').write_text(json.dumps({
                    'run_id': run.name, 'phase': 'battle_aborted', 'exit_code': status}))
            return status
        self.execute.side_effect = execute_abort
        with patch('maa_planner.copilot_run.abort_proof', return_value={
                'status': 'verified', 'reason': 'leak_abandoned', 'evidence': [], 'sanity_outcome': 'refunded'}) as proof:
            audit = self.run_experiment(sanity_budget=18)
        self.assertEqual(audit['status'], 'success', audit)
        self.assertEqual(audit['budget']['sanity_reserved'], 18)
        self.assertEqual(audit['attempts'][0]['status'], 'failed')
        self.assertEqual(audit['attempts'][0]['sanity_settlement']['released'], 18)
        self.assertFalse(audit['attempts'][0]['battle_proof']['three_star'])
        proof.assert_called_once()
        params = json.loads((self.paths[0] / 'params.json').read_text())
        self.assertTrue(params['abort_on_leak'])
        tasks = json.loads((self.paths[0] / 'navigation/resource/tasks/tasks.json').read_text())
        self.assertEqual(tasks['Copilot@StageDrops-Stars-2']['action'], 'Stop')

    def test_semiautomatic_candidate_is_excluded_before_any_device_reservation(self):
        rows = self.provider.query.return_value['candidates']
        rows[0]['title'] = '【自用/半自动/改良】test'
        self.outcomes = ['success']
        result = self.run_experiment(max_candidates=1, max_battles=1, sanity_budget=18)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['copilot_id'], 2)
        self.assertEqual(result['excluded_candidates'], [{'copilot_id': 1, 'reason': 'semiautomatic_title'}])
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['budget']['battle_reservations'], 1)
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        for row in rows:
            row['title'] = '【半自动】test'
        self.paths = []
        result = self.run_experiment()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.paths, [])
        self.assertEqual(result['budget']['battle_reservations'], 0)

    def test_downloaded_semiautomatic_title_is_rejected_before_device_use(self):
        changed = copy.deepcopy(self.content)
        changed['doc'] = {'title': '【半自动】test'}
        self.provider.get.side_effect = [changed, self.content]
        self.outcomes = ['success']
        result = self.run_experiment(max_battles=1, sanity_budget=18)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['copilot_id'], 2)
        self.assertEqual(len(self.paths), 1)
        self.assertEqual(result['attempts'][0]['failure']['sanity_outcome'], 'not_spent')

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        root = self.root
        (root / 'config').mkdir()
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
        from tests.test_navigation import pipeline_catalog
        mock('load_navigation', side_effect=pipeline_catalog)
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
        self.confirm_raid = True
        self.paths = []
        self.execute = mock('execute', side_effect=self.fake_execute)

    def fake_execute(self, root, run, address):
        self.paths.append(run)
        params = json.loads((run / 'params.json').read_text())
        self.assertEqual(params['loop_times'], 1)
        self.assertFalse(params['use_sanity_potion'])
        self.assertFalse(params['ignore_requirements'])
        selection = json.loads((run / 'selection.json').read_text())
        self.assertGreaterEqual(selection['budget']['sanity_reserved'], 18)
        self.assertLessEqual(selection['budget']['sanity_reserved'], selection['budget']['sanity_limit'])
        kind = self.outcomes.pop(0)
        if kind == 'interrupt':
            raise KeyboardInterrupt
        if kind == 'raid_preflight':
            from tests.test_copilot_preflight import preflight_events
            records = preflight_events()
            del records[2]
            for i, record in enumerate(records):
                record.update(run_id=run.name, sequence=i, recorded_ns=time.monotonic_ns())
            (run / 'callbacks.jsonl').write_text('\n'.join(json.dumps(e) for e in records))
            (run / 'worker-result.json').write_text(json.dumps({
                'run_id': run.name, 'phase': 'raid_preflight', 'exit_code': 1}))
            return 1
        records = observations() if kind == 'success' else failed_events('missing' if kind in ('offline', 'timeout', 'old') else kind)
        if params['copilot_list'][0]['is_raid'] and self.confirm_raid:
            records.insert(2, raid_confirmation())
        if kind == 'offline':
            records.append(event(20003, what='GameOffline'))
        for i, record in enumerate(records):
            record.update(run_id=run.name if kind != 'old' else 'old-attempt', sequence=i,
                          recorded_ns=time.monotonic_ns())
        records[1]['details']['details']['file_name'] = str(run / 'execution.json')
        records[1]['details']['details']['stage_name'] = json.loads((run / 'execution.json').read_text())['stage_name']
        (run / 'callbacks.jsonl').write_text('\n'.join(json.dumps(e) for e in records))
        (run / 'task-id.json').write_text('7')
        return 124 if kind == 'timeout' else 0

    def run_experiment(self, **kw):
        return experiment(self.root, 'NL-8', None, limits=RetryLimits(**dict(
            {'max_candidates': 3, 'max_battles': 2, 'sanity_budget': 36}, **kw)))

    def raid_catalog(self):
        from maa_planner.navigation_catalog import NavigationCatalog
        from tests.test_navigation import fixture
        self.mock('load_navigation', side_effect=lambda root, raid=False:
                  NavigationCatalog(*fixture(), now=150, evidence={'sha256': 'fixture'}, raid=raid))

    def test_raid_pipeline_filters_difficulty_binds_map_and_preserves_original(self):
        self.raid_catalog()
        for difficulty in (None, 0, 1, 2, 3):
            with self.subTest(difficulty=difficulty):
                self.dev.reset_mock()
                self.provider.get.reset_mock()
                c = dict(candidate().to_dict(), difficulty=difficulty, stage='act13d5_ex07#f#')
                self.provider.query.return_value = {'candidates': [c], 'page': 1}
                content = dict(self.content, stage_name='act13d5_ex07', difficulty=difficulty)
                self.provider.get.return_value = content
                self.outcomes = ['success']
                audit = experiment(self.root, 'MN-EX-7', None, raid=True)
                self.assertTrue(audit['authorization']['raid'])
                self.assertEqual(audit['stage'], 'act13d5_ex07#f#')
                if difficulty not in (2, 3):
                    self.assertEqual(audit['status'], 'failed')
                    self.dev.assert_not_called()
                    self.provider.get.assert_not_called()
                    continue
                self.assertEqual(audit['status'], 'success', audit)
                attempt = audit['attempts'][0]
                path = Path(attempt['run_dir'])
                params = json.loads((path / 'params.json').read_text())
                self.assertTrue(params['copilot_list'][0]['is_raid'])
                self.assertEqual(params['copilot_list'][0]['stage_name'], 'MN-EX-7')
                self.assertEqual(json.loads((path / 'source.json').read_text()), content)
                self.assertEqual(json.loads((path / 'execution.json').read_text())['stage_name'], audit['stage'])
                self.assertNotEqual(attempt['copilot_sha256'], attempt['execution_sha256'])
                self.assertTrue(attempt['execution']['raid_confirmed'])
                self.assertFalse(attempt['battle_proof']['ledger_recorded'])
                self.assertEqual(attempt['battle_proof']['reason'], 'raid_saved_proxy_not_supported')
                self.assertEqual(audit['budget']['stage_cost'], 18)
                self.assertFalse((self.root / 'var/state/planner/capabilities.json').exists())

    def test_raid_download_difficulty_drift_never_dispatches(self):
        self.raid_catalog()
        c = dict(candidate().to_dict(), difficulty=2, stage='act13d5_ex07#f#')
        self.provider.query.return_value = {'candidates': [c], 'page': 1}
        self.provider.get.return_value = dict(self.content, stage_name='act13d5_ex07', difficulty=1)
        audit = experiment(self.root, 'MN-EX-7', None, raid=True)
        self.assertEqual(audit['status'], 'failed')
        self.assertEqual(audit['attempts'][0]['failure_phase'], 'download_recheck')
        self.dev.assert_not_called()
        self.execute.assert_not_called()

    def test_raid_zero_exit_without_confirmation_is_not_success(self):
        self.raid_catalog()
        self.provider.query.return_value = {'candidates': [dict(candidate().to_dict(), difficulty=3)], 'page': 1}
        self.provider.get.return_value = dict(self.content, stage_name='act13d5_ex07', difficulty=3)
        self.outcomes = ['success']
        self.confirm_raid = False
        audit = experiment(self.root, 'MN-EX-7', None, raid=True)
        self.assertEqual(audit['status'], 'failed')
        self.assertFalse(audit['attempts'][0]['execution']['raid_confirmed'])

    def test_raid_preflight_failure_stops_without_next_candidate_or_battle_task(self):
        self.raid_catalog()
        rows = [dict(candidate(i).to_dict(), stage='act13d5_ex07#f#', difficulty=2) for i in (1, 2)]
        self.provider.query.return_value = {'candidates': rows, 'page': 1}
        self.content.update(stage_name='act13d5_ex07', difficulty=2)
        self.outcomes = ['raid_preflight', 'success']
        audit = experiment(self.root, 'MN-EX-7', None, raid=True, limits=RetryLimits(2, 2, 36))
        self.assertEqual(audit['status'], 'failed')
        self.assertEqual(audit['stop_reason'], 'raid_unconfirmed')
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(len(audit['attempts']), 1)
        self.assertEqual(audit['attempts'][0]['worker_phase'], 'raid_preflight')
        self.assertFalse((self.paths[0] / 'task-id.json').exists())
        self.assertIn('启动作业前停止', audit['message'])

    def test_raid_retry_releases_budget_after_proven_battle_failure(self):
        self.raid_catalog()
        self.provider.query.return_value = {'candidates': [dict(candidate(i).to_dict(), difficulty=3)
                                                         for i in (1, 2)], 'page': 1}
        self.provider.get.return_value = dict(self.content, stage_name='act13d5_ex07', difficulty=3)
        self.provider.get.side_effect = [self.provider.get.return_value,
            dict(self.provider.get.return_value, actions=[{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}])]
        self.outcomes = ['battle', 'success']
        audit = experiment(self.root, 'MN-EX-7', None, raid=True, limits=RetryLimits(2, 2, 18))
        self.assertEqual(audit['status'], 'success', audit)
        self.assertEqual(len(self.paths), 2)
        self.assertEqual(audit['budget']['sanity_reserved'], 18)
        self.assertEqual(audit['attempts'][0]['sanity_settlement']['released'], 18)

    def test_raid_cannot_request_normal_proxy_proof_or_acceptance_check(self):
        for options in ({'prove_capability': True}, {'acceptance_failure': True}):
            with self.assertRaises(ExperimentError):
                experiment(self.root, 'MN-EX-7', None, raid=True, **options)
        self.box_fetch.assert_not_called()
        self.dev.assert_not_called()

    def test_acceptance_injects_only_A_preserves_sources_and_uses_real_retry_path(self):
        (self.root / 'var/data/resource/battle_data.json').write_text(json.dumps({'chars': {'A': {'rarity': 4}}}))
        self.outcomes = ['requirement', 'success']
        result = experiment(self.root, 'NL-8', None, limits=RetryLimits(2, 2, 18), acceptance_failure=True)
        self.assertTrue(result['acceptance_passed'], result)
        self.assertEqual(result['status'], 'success')
        first, second = result['attempts']
        self.assertEqual(first['acceptance_injection']['oper_name'], 'A')
        self.assertNotIn('acceptance_injection', second)
        self.assertEqual(first['sanity_settlement']['outcome'], 'not_spent')
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        a, b = self.paths
        self.assertEqual(json.loads((a / 'source.json').read_text()), self.content)
        self.assertEqual(json.loads((b / 'execution.json').read_text()), self.content)
        self.assertEqual(json.loads((a / 'execution.json').read_text())['opers'][0]['requirements'],
                         {'elite': 2, 'level': 90})
        self.box_fetch.assert_called_once()
        self.provider.query.assert_called_once()

    def test_acceptance_missing_must_identify_injected_operator(self):
        (self.root / 'var/data/resource/battle_data.json').write_text(json.dumps({'chars': {'A': {'rarity': 4}}}))
        for injected_name, accepted in [('A', True), ('B', False)]:
            with self.subTest(injected_name=injected_name):
                self.outcomes = ['missing', 'success']
                from maa_planner.copilot_run import acceptance_formation_failure as inject
                def renamed(*args):
                    content, evidence = inject(*args)
                    evidence['oper_name'] = injected_name
                    return content, evidence
                with patch('maa_planner.copilot_run.acceptance_formation_failure', side_effect=renamed):
                    result = experiment(self.root, 'NL-8', None,
                                        limits=RetryLimits(2, 2, 18), acceptance_failure=True)
                self.assertEqual(result['acceptance_passed'], accepted, result)

    def test_acceptance_never_labels_direct_success_as_retry_acceptance(self):
        (self.root / 'var/data/resource/battle_data.json').write_text(json.dumps({'chars': {'A': {'rarity': 4}}}))
        self.outcomes = ['success']
        result = experiment(self.root, 'NL-8', None, limits=RetryLimits(2, 2, 18), acceptance_failure=True)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['acceptance_passed'])
        self.assertEqual(len(result['attempts']), 1)

    def test_acceptance_rejects_wrong_budget_and_no_safe_injection_before_device(self):
        with self.assertRaises(ExperimentError):
            experiment(self.root, 'NL-8', None, limits=RetryLimits(2, 2, 36), acceptance_failure=True)
        (self.root / 'var/data/resource/battle_data.json').write_text(json.dumps({'chars': {'A': {'rarity': 6}}}))
        result = experiment(self.root, 'NL-8', None, limits=RetryLimits(2, 2, 18), acceptance_failure=True)
        self.assertEqual(result['status'], 'failed')
        self.dev.assert_not_called()
        self.execute.assert_not_called()

    def test_acceptance_does_not_mutate_input_and_skips_unknown_rarity(self):
        original = copy.deepcopy(self.content)
        box = self.box_fetch.return_value
        catalog = self.catalog_fetch.return_value[0]
        for rarity in (6, None, True, 0, 7):
            with self.assertRaises(ExperimentError):
                acceptance_formation_failure(original, box, catalog, {'chars': {'A': {'rarity': rarity}}})
        changed, evidence = acceptance_formation_failure(original, box, catalog, {'chars': {'A': {'rarity': 4}}})
        self.assertEqual(original, self.content)
        self.assertNotEqual(changed, original)
        self.assertEqual(evidence['original_requirements'], {})

    def test_candidate_A_fails_B_succeeds_with_one_snapshot(self):
        result = self.run_experiment()
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual([a['copilot_id'] for a in result['attempts']], [1, 2])
        self.assertEqual(result['attempts'][0]['failure']['category'], 'formation_missing_operator')
        self.assertTrue(result['attempts'][1]['battle_proof']['three_star'])
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        self.assertEqual(result['budget']['sanity_released'], 18)
        self.box_fetch.assert_called_once()
        self.provider.query.assert_called_once()
        self.catalog_fetch.assert_called_once()
        self.dev.assert_called_once()
        self.assertEqual(self.provider.get.call_count, 2)
        resets = [c for c in self.commands.call_args_list if 'force-stop' in c.args[0]]
        self.assertEqual(len(resets), 0)
        self.assertEqual(result['attempts'][1]['reset_strategy'], 'fresh_core_startup_navigation')
        self.assertNotEqual(self.paths[0].name, self.paths[1].name)
        for i, path in enumerate(self.paths):
            self.assertEqual(json.loads((path / 'result.json').read_text()), result['attempts'][i])
        self.assertNotIn('private', json.dumps(result))
        self.assertFalse((self.root / 'var/state/planner/capabilities.json').exists())

    def test_explicit_local_source_uses_same_budget_navigation_and_battle_proof(self):
        content = dict(self.content, doc={'title': 'Local strategy'}, difficulty=3)
        source = self.root / 'local.json'
        source.write_text(json.dumps(content))
        self.outcomes = ['success']
        result = experiment(self.root, 'NL-8', None, copilot_file=source)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['source']['kind'], 'local')
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        self.assertEqual(result['budget']['battle_reservations'], 1)
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['attempts'][0]['source'], result['source'])
        self.assertTrue(result['attempts'][0]['battle_proof']['three_star'])
        self.assertEqual(Path(result['source']['snapshot']).read_bytes(), source.read_bytes())
        self.provider.query.assert_not_called()
        self.provider.get.assert_not_called()
        self.execute.assert_called_once()

    def test_local_source_cannot_bypass_difficulty_or_owned_requirements(self):
        source = self.root / 'local.json'
        for changes in ({'difficulty': 2}, {'opers': [dict(self.content['opers'][0], skill=3)]}):
            with self.subTest(changes=changes):
                source.write_text(json.dumps(dict(self.content, doc={'title': 'Local strategy'}, **changes)))
                result = experiment(self.root, 'NL-8', None, copilot_file=source)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['budget']['sanity_reserved'], 0)
                self.dev.assert_not_called()
                self.execute.assert_not_called()

    def test_local_file_changed_after_matching_stops_before_battle_reservation(self):
        from maa_planner.copilot_local import LocalCopilotClient
        source = self.root / 'local.json'
        source.write_text(json.dumps(dict(self.content, doc={'title': 'Local strategy'})))
        original_get = LocalCopilotClient.get

        def changed(client, *args, **kwargs):
            source.write_text(source.read_text() + '\n')
            return original_get(client, *args, **kwargs)

        with patch.object(LocalCopilotClient, 'get', changed):
            result = experiment(self.root, 'NL-8', None, copilot_file=source)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['attempts'][0]['failure_phase'], 'download_recheck')
        self.assertEqual(result['budget']['battle_reservations'], 0)
        self.assertEqual(result['budget']['sanity_reserved'], 0)
        self.execute.assert_not_called()

    def test_failed_battle_then_success_needs_only_one_clear_sanity_budget(self):
        self.outcomes = ['battle', 'success']
        self.provider.get.side_effect = [self.content,
            dict(self.content, actions=[{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}])]
        result = self.run_experiment(sanity_budget=18)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(result['budget']['battle_reservations'], 2)
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        self.assertEqual(result['budget']['sanity_released'], 18)
        self.assertEqual(result['attempts'][0]['sanity_settlement'],
                         {'reservation': 1, 'outcome': 'refunded', 'released': 18})
        self.assertEqual(result['attempts'][1]['sanity_settlement']['released'], 0)

    def test_formation_failure_then_success_also_fits_one_clear(self):
        result = self.run_experiment(sanity_budget=18)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['attempts'][0]['sanity_settlement']['outcome'], 'not_spent')
        self.assertEqual(result['budget']['sanity_reserved'], 18)

    def test_unknown_or_interrupted_battle_keeps_full_reservation(self):
        for kind in ('offline', 'timeout', 'old', 'interrupt', 'battle_unknown'):
            self.outcomes = [kind]
            self.paths = []
            result = self.run_experiment(sanity_budget=18)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['budget']['sanity_reserved'], 18)
            self.assertEqual(result['budget']['sanity_released'], 0)


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
                self.outcomes = ['two_star', 'success']
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

    def test_completed_two_star_result_tries_B_without_releasing_sanity(self):
        self.outcomes = ['two_star_auxiliary_complete', 'success']
        self.provider.get.side_effect = [self.content,
            dict(self.content, actions=[{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}])]
        result = self.run_experiment(sanity_budget=36)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(result['attempts'][0]['execution']['errors'], ['non_three_star_result'])
        self.assertEqual(result['attempts'][0]['failure']['category'], 'battle_failed')
        self.assertEqual(result['budget']['sanity_reserved'], 36)
        self.assertEqual(result['budget']['sanity_released'], 0)
        for path in self.paths:
            tasks = json.loads((path / 'navigation/resource/tasks/tasks.json').read_text())
            self.assertIn('Copilot@StageDrops-Stars-2', tasks['Copilot@EndOfAction']['next'])

    def test_zero_star_result_retries_with_template_and_refund(self):
        self.outcomes = ['zero_star_complete', 'success']
        self.provider.get.side_effect = [self.content,
            dict(self.content, actions=[{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}])]
        result = self.run_experiment(sanity_budget=18)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(result['attempts'][0]['execution']['errors'], ['non_three_star_result'])
        self.assertEqual(result['attempts'][0]['failure']['category'], 'battle_failed')
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        self.assertEqual(result['budget']['sanity_released'], 18)
        for path in self.paths:
            tasks = json.loads((path / 'navigation/resource/tasks/tasks.json').read_text())
            self.assertIn('Copilot@StageDrops-Stars-0', tasks['Copilot@EndOfAction']['next'])
            template = path / 'navigation/resource/template/StageDrops-Stars-0.png'
            self.assertTrue(template.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))

    def test_complete_zero_star_mission_failure_retries_with_single_refund(self):
        self.outcomes = ['zero_star_mission_failed_complete', 'success']
        self.provider.get.side_effect = [self.content,
            dict(self.content, actions=[{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}])]
        result = self.run_experiment(sanity_budget=18)
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(result['attempts'][0]['failure']['category'], 'battle_failed')
        self.assertEqual(result['attempts'][0]['failure']['sanity_outcome'], 'refunded')
        self.assertEqual(result['budget']['sanity_reserved'], 18)
        self.assertEqual(result['budget']['sanity_released'], 18)
        self.assertEqual(result['budget']['battle_reservations'], 2)

    def test_duplicate_failed_execution_skips_before_reservation_and_worker(self):
        self.provider.get.side_effect = [self.content, copy.deepcopy(self.content),
            dict(self.content, actions=[{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}])]
        self.outcomes = ['battle', 'success']
        result = self.run_experiment()
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual([a['copilot_id'] for a in result['attempts']], [1, 2, 3])
        first, duplicate, third = result['attempts']
        self.assertEqual(first['failure']['category'], 'battle_failed')
        self.assertEqual(duplicate['failure']['category'], 'duplicate_execution')
        self.assertEqual(duplicate['duplicate_of'], 1)
        self.assertEqual(first['execution_sha256'], duplicate['execution_sha256'])
        self.assertNotEqual(first['execution_sha256'], third['execution_sha256'])
        self.assertEqual(duplicate['failure']['sanity_outcome'], 'not_spent')
        self.assertNotIn('sanity_settlement', duplicate)
        self.assertNotIn('worker_exit_code', duplicate)
        self.assertEqual(len(self.paths), 2)
        self.assertEqual(result['budget']['battle_reservations'], 2)
        self.assertEqual(result['budget']['candidates_considered'], 3)
        self.assertEqual(third['reset_strategy'], 'fresh_core_startup_navigation')

    def test_duplicate_guard_is_bounded_to_the_current_snapshot(self):
        self.outcomes = ['battle']
        first = self.run_experiment()
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(first['stop_reason'], 'candidates_exhausted')
        self.assertEqual(first['budget']['battle_reservations'], 1)
        self.assertEqual(first['budget']['candidates_considered'], 3)
        self.assertEqual(len(self.paths), 1)
        self.outcomes = ['success']
        self.paths = []
        second = self.run_experiment()
        self.assertEqual(second['status'], 'success', second)
        self.assertEqual(len(second['attempts']), 1)
        self.assertEqual(len(self.paths), 1)

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

    def test_retry_navigation_failure_stops_without_third_worker(self):
        self.outcomes = ['missing', 'navigation', 'success']
        result = self.run_experiment(max_candidates=3, max_battles=3)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.paths), 2)
        self.assertFalse(result['attempts'][1]['failure']['retryable'])
        self.assertEqual(result['attempts'][1]['reset_strategy'], 'fresh_core_startup_navigation')

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
        self.assertEqual(result['budget']['sanity_reserved'], 0)
        self.assertEqual(result['budget']['sanity_released'], 54)


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
