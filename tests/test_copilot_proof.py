import copy
import unittest

from maa_planner.copilot_proof import battle_proof
from tests.test_copilot_run import events


def observations():
    records = events()
    records.insert(4, {'message': 20002, 'details': {
        'uuid': 'device', 'taskid': 7, 'taskchain': 'Copilot',
        'subtask': 'ProcessTask', 'first': ['Copilot@WaitUntilEndOfAction'],
        'details': {'task': 'StageDrops-Stars-3', 'action': 'DoNothing',
                    'algorithm': 'MatchTemplate',
                    'result': {'template': 'StageDrops-Stars-3.png', 'score': 0.95}}}})
    records[-1]['details'].update(uuid='device', taskchain='Copilot', taskid=7)
    return stamp(records)


def stamp(records):
    for i, e in enumerate(records):
        e.update(run_id='current-run', sequence=i, recorded_ns=100 + i)
    return records


def prove(records, **changes):
    context = dict(run_id='current-run', task_id=7, stage='stage',
                   filename='/run/execution.json', copilot_id=123,
                   copilot_sha256='a' * 64, execution_sha256='b' * 64,
                   started_ns=100, finished_ns=200, exit_code=0, support_used=False)
    context.update(changes)
    return battle_proof(records, **context)


class CopilotProofTests(unittest.TestCase):
    def assert_unproven(self, records, **kw):
        result = prove(records, **kw)
        self.assertFalse(result['three_star'], result)
        self.assertFalse(result['ledger_recorded'])

    def test_fresh_stars_are_not_account_or_proxy_proof(self):
        for support in (False, True):
            result = prove(observations(), support_used=support)
            self.assertTrue(result['three_star'])
            self.assertEqual(result['status'], 'observed')
            self.assertEqual(result['saved_proxy'], 'unknown')
            self.assertEqual(result['account_binding'], 'unknown')
            self.assertEqual(result['activity_binding'], 'unknown')
            self.assertFalse(result['ledger_recorded'])
            self.assertEqual(result['copilot_id'], 123)
            self.assertEqual(result['execution_sha256'], 'b' * 64)
            self.assertEqual(result['run_id'], 'current-run')

    def test_every_required_event_is_necessary(self):
        records = observations()
        for i in range(len(records)):
            with self.subTest(missing=i):
                self.assert_unproven(stamp(records[:i] + records[i + 1:]))
        self.assert_unproven(stamp(events()))
        self.assert_unproven([])

    def test_historical_callbacks_and_broken_recorder_sequence_fail(self):
        self.assert_unproven(events())
        for field, value in [('run_id', 'old-run'), ('sequence', 0),
                             ('sequence', True), ('recorded_ns', 99),
                             ('recorded_ns', 201), ('recorded_ns', True)]:
            records = observations()
            records[3][field] = value
            with self.subTest(field=field, value=value):
                self.assert_unproven(records)
        self.assert_unproven(observations(), started_ns=108)
        self.assert_unproven(observations(), exit_code=124)
        self.assert_unproven(observations(), exit_code=False)

    def test_wrong_identity_and_order_fail(self):
        for index in range(6):
            for field, value in [('uuid', 'other-device'), ('taskid', 8), ('taskid', True)]:
                records = observations()
                records[index]['details'][field] = value
                self.assert_unproven(records)
        for index in (1, 2, 3, 5, 6):
            records = observations()
            records[4], records[index] = records[index], records[4]
            self.assert_unproven(stamp(records))
        for index in (0, 1, 2, 3, 4, 5):
            records = observations()
            records.insert(index, copy.deepcopy(records[index]))
            self.assert_unproven(stamp(records))
        for field, value in [('uuid', 'wrong-device'), ('taskid', 8),
                             ('taskchain', 'Fight'), ('finished_tasks', [True, 7])]:
            records = observations()
            records[-1]['details'][field] = value
            self.assert_unproven(records)
        records = observations()
        records.append(copy.deepcopy(records[-1]))
        self.assert_unproven(stamp(records))
        self.assert_unproven(observations(), stage='wrong-stage')
        self.assert_unproven(observations(), filename='/old/execution.json')

    def test_template_and_recognition_metadata_required(self):
        for field, value in [('task', 'StageDrops-Stars-2'), ('algorithm', 'JustReturn'),
                             ('action', 'ClickSelf'), ('result', None)]:
            records = observations()
            records[4]['details']['details'][field] = value
            self.assert_unproven(records)
        for field, value in [('template', 'StageDrops-Stars-Adverse.png'),
                             ('score', float('nan')), ('score', True),
                             ('score', 0), ('score', 1.1)]:
            records = observations()
            records[4]['details']['details']['result'][field] = value
            self.assert_unproven(records)
        for field, value in [('subtask', 'Custom'), ('first', ['OtherTask'])]:
            records = observations()
            records[4]['details'][field] = value
            self.assert_unproven(records)

    def test_failures_even_after_terminal_veto_observation(self):
        for msg in (0, 1, 10000, 10004, 20000, 20004):
            self.assert_unproven(stamp(observations() + [{'message': msg, 'details': {}}]))
        for task in ('FightMissionFailed', 'StageDrops-Stars-2', 'EndOfAction-Sandbox'):
            records = observations()
            failure = copy.deepcopy(records[4])
            failure['details']['details']['task'] = task
            records.insert(4, failure)
            self.assert_unproven(stamp(records))

    def test_only_optional_preformation_prts_probe_is_ignored(self):
        probe = {'message': 20000, 'details': {
            'uuid': 'device', 'taskid': 7, 'taskchain': 'Copilot',
            'subtask': 'ProcessTask', 'class': 'asst::ProcessTask',
            'first': ['NotUsePrts'], 'pre_task': '', 'details': {}}}
        records = observations()
        records.insert(2, probe)
        self.assertTrue(prove(stamp(records))['three_star'])
        for index in (0, 1, 3, 4, 5, 6):
            records = observations()
            records.insert(index, copy.deepcopy(probe))
            self.assert_unproven(stamp(records))
        records = observations()
        records[2:2] = [copy.deepcopy(probe), copy.deepcopy(probe)]
        self.assert_unproven(stamp(records))
        for field, value in [('first', ['OtherTask']), ('uuid', 'wrong'),
                             ('taskid', 8), ('pre_task', 'other'), ('why', 'error')]:
            records = observations()
            changed = copy.deepcopy(probe)
            changed['details'][field] = value
            records.insert(2, changed)
            self.assert_unproven(stamp(records))

    def test_completed_plot_skip_error_requires_both_clicks_during_battle(self):
        def event(msg, **fields):
            return {'message': msg, 'details': dict(uuid='device', taskid=7,
                    taskchain='Copilot', **fields)}
        sequence = [event(20001, subtask='BattleProcessTask')]
        for task, previous in [('SkipThePreBattlePlot', ''),
                               ('SkipThePreBattlePlotConfirm', 'SkipThePreBattlePlot')]:
            e = event(20002, subtask='ProcessTask', first=['SkipThePreBattlePlot'], pre_task=previous,
                      details={'task': task, 'action': 'ClickSelf', 'algorithm': 'MatchTemplate',
                               'result': {'template': task + '.png', 'score': .9}})
            e['details']['class'] = 'asst::ProcessTask'
            sequence.append(e)
        e = event(20000, subtask='ProcessTask', first=['SkipThePreBattlePlot'], pre_task='', details={})
        e['details']['class'] = 'asst::ProcessTask'
        sequence.append(e)
        def insert(seq, index=3):
            records = observations()
            records[index:index] = copy.deepcopy(seq)
            return stamp(records)
        self.assertTrue(prove(insert(sequence))['three_star'])
        self.assertTrue(prove(insert(sequence[:-1]))['three_star'])
        for i in range(len(sequence) - 1):
            self.assert_unproven(insert(sequence[:i] + sequence[i + 1:]))
        for i in (0, 1, 2, 4, 5, 6):
            self.assert_unproven(insert(sequence, i))
        self.assert_unproven(insert(sequence + [sequence[-1]]))
        for index, field, value in [(1, 'uuid', 'wrong'), (2, 'taskid', 8),
                                     (3, 'why', 'battle error'), (3, 'first', ['OtherTask'])]:
            changed = copy.deepcopy(sequence)
            changed[index]['details'][field] = value
            self.assert_unproven(insert(changed))
        for field, value in [('template', 'Other.png'), ('score', True), ('score', 0)]:
            changed = copy.deepcopy(sequence)
            changed[2]['details']['details']['result'][field] = value
            self.assert_unproven(insert(changed))
        # A valid skip never substitutes for the battle result or its terminal.
        for index in (3, 4, 5, 6):
            records = observations()
            del records[index]
            records[3:3] = copy.deepcopy(sequence)
            self.assert_unproven(stamp(records))

    def test_malformed_evidence_fails_closed(self):
        for value in (None, [], 'text', 1):
            records = observations()
            records[4]['details'] = value
            self.assert_unproven(records)
            records = observations()
            records[4]['details']['details'] = value
            self.assert_unproven(records)
        for changes in ({'copilot_sha256': 'bad'}, {'execution_sha256': 'bad'},
                        {'support_used': None}, {'copilot_id': True}, {'task_id': True}):
            self.assert_unproven(observations(), **changes)

    def test_native_default_id_skill_swipe_is_auxiliary_only_during_formation(self):
        begin = {'message': 20001, 'details': {'uuid': 'device', 'taskid': 7,
                 'taskchain': 'Copilot', 'subtask': 'BattleFormationTask'}}
        swipe = {'message': 20002, 'details': {'uuid': 'device', 'taskid': 0,
                 'taskchain': 'Copilot', 'subtask': 'ProcessTask', 'class': 'asst::ProcessTask',
                 'first': ['BattleQuickFormationSkill-SwipeToTheDown'], 'pre_task': '',
                 'details': {'task': 'BattleQuickFormationSkill-SwipeToTheDown', 'action': 'Swipe',
                             'algorithm': 'JustReturn', 'result': {}}}}
        def insert(helper=swipe, index=2, forming=True):
            records = observations()
            records[index:index] = ([copy.deepcopy(begin)] if forming else []) + [copy.deepcopy(helper)]
            return stamp(records)
        self.assertTrue(prove(insert())['three_star'])
        self.assert_unproven(insert(forming=False))
        for index in (0, 1, 3, 4, 5, 6):
            self.assert_unproven(insert(index=index))
        for field, value in [('uuid', 'wrong'), ('taskid', 8), ('taskid', True),
                             ('first', ['OtherTask']), ('subtask', 'BattleProcessTask'),
                             ('class', 'OtherClass'), ('why', 'error')]:
            changed = copy.deepcopy(swipe)
            changed['details'][field] = value
            self.assert_unproven(insert(changed))
        for field, value in [('task', 'StageDrops-Stars-3'), ('action', 'ClickSelf'),
                             ('algorithm', 'MatchTemplate')]:
            changed = copy.deepcopy(swipe)
            changed['details']['details'][field] = value
            self.assert_unproven(insert(changed))
        for msg in (20000, 20004, 10002, 3):
            changed = copy.deepcopy(swipe)
            changed['message'] = msg
            self.assert_unproven(insert(changed))
        records = insert()
        del records[4]  # Auxiliary swipe does not replace formation completion.
        self.assert_unproven(stamp(records))


if __name__ == '__main__':
    unittest.main()
