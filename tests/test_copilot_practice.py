import copy
import unittest

from maa_planner.copilot_practice import admit_live, practice_proof, practice_ready, practice_tasks
from tests.test_copilot_proof import observations, prove, stamp


def practice_observations():
    records = observations()
    def process(task, algorithm, action, result):
        return {'message': 20002, 'details': {
            'uuid': 'device', 'taskid': 7, 'taskchain': 'Copilot', 'subtask': 'ProcessTask',
            'details': {'task': task, 'algorithm': algorithm, 'action': action, 'result': result}}}
    records.insert(2, process('BattleStartPre', 'OcrDetect', 'ClickSelf', {'text': '演习'}))
    records.insert(4, process('BattleStartExercise', 'MatchTemplate', 'ClickSelf',
                              {'template': 'BattleStartExercise.png', 'score': .99}))
    records.insert(6, process('EndOfAction', 'OcrDetect', 'DoNothing', {'text': '演习成功'}))
    return stamp(records)


def readiness():
    def event(msg, **value):
        return {'message': msg, 'details': {'uuid': 'device', 'taskid': 5, 'taskchain': 'Custom', **value}}
    return [event(10001), event(20002, first=['ZootdPracticeReady'], subtask='ProcessTask', details={
        'task': 'ZootdPracticeReady', 'algorithm': 'OcrDetect', 'action': 'DoNothing', 'result': {'text': '演习'}}),
        event(10002), event(3, finished_tasks=[5])]


class PracticeEvidenceTests(unittest.TestCase):
    def test_practice_requires_mode_start_result_and_fresh_stars(self):
        records = practice_observations()
        self.assertEqual(practice_proof(records, prove(records))['status'], 'observed')
        self.assertEqual(practice_proof(observations(), prove(observations()))['status'], 'unproven')
        for missing in (2, 4, 6, 7):
            changed = copy.deepcopy(records)
            del changed[missing]
            changed = stamp(changed)
            self.assertEqual(practice_proof(changed, prove(changed))['status'], 'unproven')
        for index, key, value in ((2, 'action', 'DoNothing'), (2, 'result', {'text': '开始行动'}),
                                   (4, 'result', {'template': 'BattleStartNormal.png', 'score': .99}),
                                   (6, 'result', {'text': '行动结束'})):
            changed = copy.deepcopy(records)
            changed[index]['details']['details'][key] = value
            self.assertEqual(practice_proof(changed, prove(changed))['status'], 'unproven')
        for index in (2, 4, 6):
            for key, value in (('uuid', 'other'), ('taskid', 9)):
                changed = copy.deepcopy(records)
                changed[index]['details'][key] = value
                self.assertEqual(practice_proof(changed, prove(changed))['status'], 'unproven')
        self.assertEqual(practice_proof(records, prove(records, run_id='old'))['status'], 'unproven')

    def test_ready_requires_one_full_same_device_chain(self):
        records = readiness()
        self.assertTrue(practice_ready(records, task_id=5))
        for index in range(len(records)):
            changed = copy.deepcopy(records)
            del changed[index]
            self.assertFalse(practice_ready(changed, task_id=5))
        for key, value in (('uuid', 'other'), ('taskid', True), ('first', ['Other'])):
            changed = copy.deepcopy(records)
            changed[1]['details'][key] = value
            self.assertFalse(practice_ready(changed, task_id=5))
        records[1]['details']['details']['action'] = 'ClickSelf'
        self.assertFalse(practice_ready(records, task_id=5))

    def test_admission_rejects_failure_changes_duplicate_and_nonpractice_proof(self):
        proof = practice_proof(practice_observations(), prove(practice_observations()))
        attempts = []
        for i in range(3):
            attempts.append({'status': 'success', 'mode': 'practice', 'run_id': str(i),
                'execution_sha256': 'b' * 64, 'snapshot_sha256': 'c' * 64,
                'battle_proof': dict(proof, run_id=str(i))})
        def admitted(values):
            return admit_live(values, execution_sha256='b' * 64, snapshot_sha256='c' * 64)
        self.assertTrue(admitted(attempts))
        self.assertFalse(admitted(attempts[:2]))
        self.assertFalse(admitted(attempts + [attempts[0]]))
        for index in range(3):
            for key, value in (('status', 'failed'), ('mode', 'live'),
                               ('execution_sha256', 'd' * 64), ('snapshot_sha256', 'd' * 64)):
                changed = copy.deepcopy(attempts)
                changed[index][key] = value
                self.assertFalse(admitted(changed))
            for key, value in (('three_star', False), ('practice', False), ('ledger_recorded', True),
                               ('run_id', 'old'), ('execution_sha256', 'd' * 64)):
                changed = copy.deepcopy(attempts)
                changed[index]['battle_proof'][key] = value
                self.assertFalse(admitted(changed))
        self.assertFalse(admitted([attempts[0]] * 3))

    def test_exercise_resources_never_fall_back_to_real_start_or_refills(self):
        tasks = practice_tasks()
        self.assertEqual(tasks['BattleStartAll']['next'], ['BattleStartExercise'])
        for name in ('BattleStartNormal', 'BattleStartRaid', 'BattleStartOCR', 'UseMedicine', 'UseStone'):
            self.assertEqual(tasks[name]['action'], 'Stop')
            self.assertEqual(tasks[name]['next'], [])
        self.assertEqual(tasks['BattleStartPre']['text'], ['演习'])
        self.assertEqual(tasks['BattleStartExercise']['template'], 'BattleStartExercise.png')


if __name__ == '__main__':
    unittest.main()
