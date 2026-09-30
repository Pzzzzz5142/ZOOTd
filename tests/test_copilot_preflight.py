import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_planner.copilot_core import raid_preflight_complete, worker
from maa_planner.copilot_navigation import navigation_tasks
from maa_planner.copilot_retry import classify_failure
from maa_planner.copilot_run import failure_message


def preflight_events():
    def event(message, **value):
        return {'message': message, 'details': {
            'uuid': 'device', 'taskid': 5, 'taskchain': 'Custom', **value}}

    def observed(task, algorithm, result):
        return event(20002, first=['ZootdRaidPreflight'], subtask='ProcessTask', details={
            'task': task, 'algorithm': algorithm, 'action': 'DoNothing', 'result': result})

    return [event(10001), observed('ZootdRaidPreflight', 'OcrDetect', {'text': 'MN-EX-7'}),
            observed('ZootdRaidConfirmed', 'MatchTemplate', {'template': 'NormalDifficulty.png', 'score': 0.98}),
            event(10002), event(3, finished_tasks=[5])]


def confirmed(records):
    return raid_preflight_complete(records, task_id=5, code='MN-EX-7')


class PreflightEvidenceTests(unittest.TestCase):
    def test_requires_stage_mode_chain_and_final_task_list(self):
        self.assertTrue(confirmed(preflight_events()))
        for index in range(5):
            with self.subTest(missing=index):
                records = preflight_events()
                del records[index]
                self.assertFalse(confirmed(records))
        for index, field, value in [(1, 'task', 'ZootdStageConfirmed'), (1, 'algorithm', 'JustReturn'),
                                    (1, 'action', 'ClickSelf'), (1, 'result', {'text': 'MN-EX-6'}),
                                    (2, 'task', 'RaidConfirm'), (2, 'action', 'ClickSelf'),
                                    (2, 'algorithm', 'JustReturn'), (2, 'result', {}),
                                    (2, 'result', {'template': 'RaidDifficulty.png', 'score': 0.98}),
                                    (2, 'result', {'template': 'NormalDifficulty.png', 'score': float('nan')})]:
            with self.subTest(field=field, value=value):
                records = preflight_events()
                records[index]['details']['details'][field] = value
                self.assertFalse(confirmed(records))

    def test_rejects_wrong_chain_device_task_or_terminal_and_disordered_mode(self):
        for index in range(5):
            for field, value in [('uuid', 'other-device'), ('taskid', 6), ('taskid', True),
                                  ('taskchain', 'Copilot')]:
                with self.subTest(index=index, field=field):
                    records = preflight_events()
                    records[index]['details'][field] = value
                    self.assertFalse(confirmed(records))
        records = preflight_events()
        records[1], records[2] = records[2], records[1]
        self.assertFalse(confirmed(records))
        for change in [('first', ['ZootdNavigate']), ('finished_tasks', [4]), ('finished_tasks', [4, 5])]:
            records = preflight_events()
            records[2 if change[0] == 'first' else 4]['details'][change[0]] = change[1]
            self.assertFalse(confirmed(records))
        for index in (2, 3, 4):
            records = preflight_events()
            records.insert(index, copy.deepcopy(records[2]))
            self.assertFalse(confirmed(records))

    def test_clicked_three_times_then_completed_is_never_authorization(self):
        records = preflight_events()
        switch = copy.deepcopy(records[2])
        switch['details']['details'].update(task='ZootdRaidSwitch', action='ClickSelf',
                                           result={'template': 'RaidDifficulty.png', 'score': 0.90})
        exceeded = copy.deepcopy(switch)
        exceeded.update(message=20003)
        exceeded['details'].update(what='ExceededLimit', details={
            'task': 'ZootdRaidSwitch', 'exec_times': 3, 'max_times': 3})
        records[2:3] = [switch, copy.deepcopy(switch), copy.deepcopy(switch), exceeded]
        self.assertFalse(confirmed(records))
        # Even an injected confirmation cannot turn a failed preflight into permission.
        records.insert(-2, preflight_events()[2])
        self.assertFalse(confirmed(records))

    def test_native_runout_must_recognize_mode_instead_of_finishing(self):
        route = {'code': 'MN-EX-7', 'raid': True, 'has_raid': True, 'kind': 'archive',
                 'activity': '玛莉娅·临光', 'zone_names': ['征战区域']}
        tasks = navigation_tasks(route)
        self.assertEqual(tasks['ChangeToRaidDifficulty']['exceededNext'], ['RaidConfirm'])
        self.assertEqual(tasks['ChangeToRaidDifficulty']['onErrorNext'], [])
        # Only switching and recognition are reachable from the Custom entry.
        visited, remaining = set(), ['ZootdRaidPreflight']
        while remaining:
            name = remaining.pop()
            if name in visited:
                continue
            visited.add(name)
            for field in ('next', 'sub', 'onErrorNext', 'exceededNext'):
                remaining.extend(tasks[name][field])
        self.assertEqual(visited, {'ZootdRaidPreflight', 'ZootdRaidConfirmed', 'ZootdRaidSwitch'})
        self.assertEqual(tasks['ZootdRaidSwitch']['maxTimes'], 3)
        self.assertEqual(tasks['ZootdRaidConfirmed']['baseTask'], 'RaidConfirm')


class Function:
    """ctypes-like callable allowing argtypes/restype on a fake MaaCore API."""
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class FakeCore:
    def __init__(self, records):
        self.records = records
        self.appended = []
        self.AsstCreateEx = Function(self.create)
        self.AsstAppendTask = Function(self.append)
        self.AsstStart = Function(self.start)
        self.AsstRunning = Function(lambda *_: 0)
        self.AsstDestroy = Function(lambda *_: None)
        for name in ('AsstSetUserDir', 'AsstLoadResource', 'AsstSetInstanceOption', 'AsstConnect',
                     'AsstStop', 'AsstAsyncScreencap', 'AsstGetImage'):
            setattr(self, name, Function(lambda *_: 1))

    def create(self, callback, _):
        self.callback = callback
        return 1

    def append(self, _, kind, params):
        self.appended.append((kind.decode(), json.loads(params)))
        return len(self.appended)

    def emit(self, msg, **value):
        kind, _ = self.appended[-1]
        self.callback(msg, json.dumps({'uuid': 'device', 'taskid': len(self.appended),
                                      'taskchain': kind, **value}).encode(), None)

    def start(self, _):
        kind, params = self.appended[-1]
        if params.get('task_names') == ['ZootdRaidPreflight']:
            for record in self.records:
                value = copy.deepcopy(record['details'])
                # Preserve intentionally wrong UUIDs/task IDs in rejection cases.
                if value.get('taskid') == 5:
                    value['taskid'] = len(self.appended)
                self.callback(record['message'], json.dumps(value).encode(), None)
            return 1
        self.emit(10001)
        if params.get('task_names') == ['Home', 'Home@ReturnButtons']:
            self.emit(20001, subtask='ProcessTask', details={
                'task': 'Home', 'action': 'Stop', 'algorithm': 'MatchTemplate',
                'result': {'template': 'SwitchTheme@ToggleSettingsMenu.png'}})
        if params.get('task_names') == ['ZootdNavigate']:
            self.emit(20002, subtask='ProcessTask', details={
                'task': 'ZootdStageConfirmed', 'action': 'DoNothing', 'algorithm': 'OcrDetect',
                'result': {'text': 'MN-EX-7'}})
        self.emit(10002)
        self.emit(3, finished_tasks=[len(self.appended)])
        return 1


class PreflightDispatchTests(unittest.TestCase):
    def run_worker(self, records, *, raid=True, navigation_only=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'attempt'
            run.mkdir()
            (run / 'navigation.json').write_text(json.dumps({
                'code': 'MN-EX-7', 'raid': raid, 'navigation_only': navigation_only}))
            (run / 'params.json').write_text('{"copilot_list":[]}')
            lib = FakeCore(records)
            with patch('maa_planner.copilot_core.C.CDLL', return_value=lib), \
                    patch('maa_planner.copilot_core.launch_game'), \
                    patch('maa_planner.copilot_core.signal.signal'), \
                    patch('maa_planner.copilot_core.subprocess.run', return_value=
                          subprocess.CompletedProcess([], 0, stdout=b'')):
                status = worker(root, run, 'device')
            receipt = json.loads((run / 'worker-result.json').read_text())
            return status, receipt, lib.appended, (run / 'task-id.json').exists()

    def test_locked_mode_never_enqueues_copilot_even_with_success_terminal(self):
        records = preflight_events()
        records[2]['details'].update(what='ExceededLimit', details={
            'task': 'ZootdRaidSwitch', 'exec_times': 3, 'max_times': 3})
        records[2]['message'] = 20003
        self.assert_blocked(records)

    def assert_blocked(self, records):
        status, receipt, appended, task_file = self.run_worker(records)
        self.assertEqual(status, 1)
        self.assertEqual(receipt['phase'], 'raid_preflight')
        self.assertFalse(task_file)
        self.assertNotIn('Copilot', [kind for kind, _ in appended])

    def test_missing_wrong_late_or_cross_device_confirmation_blocks_dispatch(self):
        for index in range(5):
            with self.subTest(missing=index):
                records = preflight_events()
                del records[index]
                self.assert_blocked(records)
        for index, field, value in [(1, 'uuid', 'other'), (2, 'uuid', 'other'),
                                    (2, 'taskid', 8), (2, 'first', ['OldPreflight'])]:
            with self.subTest(field=field):
                records = preflight_events()
                records[index]['details'][field] = value
                self.assert_blocked(records)
        records = preflight_events()
        records[2], records[3] = records[3], records[2]
        self.assert_blocked(records)

    def test_confirmed_mode_enqueues_exactly_one_copilot_after_preflight(self):
        status, receipt, appended, task_file = self.run_worker(preflight_events())
        self.assertEqual(status, 0)
        self.assertEqual(receipt['phase'], 'execution')
        self.assertTrue(task_file)
        self.assertEqual([kind for kind, _ in appended].count('Copilot'), 1)
        self.assertEqual(appended[-2], ('Custom', {'task_names': ['ZootdRaidPreflight']}))

    def test_normal_and_navigation_only_do_not_require_challenge_mode(self):
        for options in ({'raid': False}, {'navigation_only': True}):
            with self.subTest(options=options):
                status, _, appended, _ = self.run_worker([], **options)
                self.assertEqual(status, 0)
                self.assertNotIn(('Custom', {'task_names': ['ZootdRaidPreflight']}), appended)

    def test_preflight_failure_stops_retries_and_has_actionable_message(self):
        outcome = classify_failure([], run_id='attempt', started_ns=1, finished_ns=2,
                                   task_id=None, stage='stage', filename='file', exit_code=1,
                                   worker_phase='raid_preflight', raid=True)
        self.assertEqual(outcome['category'], 'raid_unconfirmed')
        self.assertFalse(outcome['retryable'])
        self.assertIn('启动作业前停止', failure_message({'failure': outcome}))
        self.assertIn('三星通关普通', failure_message({'failure': outcome}))
