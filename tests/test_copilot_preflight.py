import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_planner.copilot_core import raid_preflight_complete, special_panel_complete, worker
from maa_planner.copilot_navigation import navigation_tasks
from maa_planner.navigation_cli import navigation_complete
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


def special_events():
    def event(message, **value):
        return {'message': message, 'details': {
            'uuid': 'device', 'taskid': 5, 'taskchain': 'Custom', **value}}
    records = [event(10001)]
    for task, text in [('ZootdSpecialPanel', 'SPECIAL ACCESS CONTENT'),
                       ('ZootdSpecialStart', '开始行动'),
                       ('ZootdSpecialStageConfirmed', 'MN-EX-7')]:
        records.append(event(20002, first=['ZootdNavigate'], subtask='ProcessTask', details={
            'task': task, 'algorithm': 'OcrDetect', 'action': 'DoNothing', 'result': {'text': text}}))
    return records + [event(10002), event(3, finished_tasks=[5])]


class SpecialPanelEvidenceTests(unittest.TestCase):
    def test_requires_ordered_layout_start_title_and_full_terminal(self):
        records = special_events()
        self.assertTrue(special_panel_complete(records, task_id=5, code='MN-EX-7'))
        self.assertTrue(navigation_complete(records, 'MN-EX-7'))
        for index in range(len(records)):
            changed = copy.deepcopy(records)
            del changed[index]
            self.assertFalse(special_panel_complete(changed, task_id=5, code='MN-EX-7'))
            self.assertFalse(navigation_complete(changed, 'MN-EX-7'))
        for index, field, value in [(1, 'result', {'text': 'OTHER PANEL'}),
                                    (2, 'action', 'ClickSelf'), (2, 'result', {'text': '查看条件'}),
                                    (3, 'result', {'text': 'MN-EX-70'}),
                                    (3, 'algorithm', 'JustReturn')]:
            changed = copy.deepcopy(records)
            changed[index]['details']['details'][field] = value
            self.assertFalse(special_panel_complete(changed, task_id=5, code='MN-EX-7'))
            self.assertFalse(navigation_complete(changed, 'MN-EX-7'))
        for index in range(len(records)):
            for field, value in [('uuid', 'other'), ('taskid', 6), ('taskchain', 'Copilot')]:
                changed = copy.deepcopy(records)
                changed[index]['details'][field] = value
                self.assertFalse(special_panel_complete(changed, task_id=5, code='MN-EX-7'))
                self.assertFalse(navigation_complete(changed, 'MN-EX-7'))
        changed = copy.deepcopy(records)
        changed[1], changed[2] = changed[2], changed[1]
        self.assertFalse(special_panel_complete(changed, task_id=5, code='MN-EX-7'))
        changed = copy.deepcopy(records)
        changed[3]['details']['first'] = ['OldNavigation']
        self.assertFalse(special_panel_complete(changed, task_id=5, code='MN-EX-7'))
        changed = copy.deepcopy(records)
        changed.insert(3, {'message': 20003, 'details': {'what': 'ExceededLimit'}})
        self.assertFalse(special_panel_complete(changed, task_id=5, code='MN-EX-7'))


class PreflightEvidenceTests(unittest.TestCase):
    def test_device_statistics_do_not_replace_or_invalidate_task_evidence(self):
        for what, stats in [('ScreencapCost', {'avg': 161, 'max': 175, 'min': 150}),
                            ('EmulatorFPS', {'fps': 60, 'refresh_period_ns': 16666666})]:
            telemetry = {'message': 2, 'details': {'uuid': 'device', 'what': what, 'details': stats}}
            for index in range(1, 6):
                records = preflight_events()
                records.insert(index, copy.deepcopy(telemetry))
                self.assertTrue(confirmed(records))
                for missing in (1, 2, 3, 4):
                    records = preflight_events()
                    records[missing] = copy.deepcopy(telemetry)
                    self.assertFalse(confirmed(records))
            for index in (0,):
                records = preflight_events()
                records.insert(index, copy.deepcopy(telemetry))
                self.assertFalse(confirmed(records))
            for field, value in [('uuid', 'other-device'), ('taskid', 5), ('taskchain', 'Custom'),
                                  ('what', 'GameOffline'), ('what', 'OtherStatistics'), ('details', [])]:
                for index in (2, 5):
                    records = preflight_events()
                    invalid = copy.deepcopy(telemetry)
                    invalid['details'][field] = value
                    records.insert(index, invalid)
                    self.assertFalse(confirmed(records))
            records = preflight_events()
            invalid = copy.deepcopy(telemetry)
            invalid['message'] = 20000
            records.insert(2, invalid)
            self.assertFalse(confirmed(records))

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
    def __init__(self, records, navigation_records=None, recovery_stars=None):
        self.records = records
        self.navigation_records = navigation_records
        self.recovery_stars = recovery_stars
        self.appended = []
        self.loaded_tasks = []
        self.AsstCreateEx = Function(self.create)
        self.AsstAppendTask = Function(self.append)
        self.AsstStart = Function(self.start)
        self.AsstRunning = Function(lambda *_: 0)
        self.AsstDestroy = Function(lambda *_: None)
        for name in ('AsstSetUserDir', 'AsstLoadResource', 'AsstSetInstanceOption', 'AsstConnect',
                     'AsstStop', 'AsstAsyncScreencap', 'AsstGetImage'):
            setattr(self, name, Function(lambda *_: 1))
        self.AsstLoadResource = Function(self.load)

    def load(self, directory):
        tasks = Path(directory.decode()) / 'resource/tasks/tasks.json'
        if tasks.exists():
            self.loaded_tasks.append((len(self.appended), json.loads(tasks.read_text())))
        return 1

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
        if params.get('task_names') == ['ZootdRecoverZeroResult']:
            self.emit(10001)
            if self.recovery_stars in (0, 2):
                self.emit(20001 if self.recovery_stars == 2 else 20002,
                    subtask='ProcessTask', first=['ZootdRecoverZeroResult'], details={
                    'task': 'ZootdRecoverTwoStars' if self.recovery_stars == 2 else 'ZootdRecoverZeroStars',
                    'algorithm': 'MatchTemplate', 'action': 'Stop' if self.recovery_stars == 2 else 'DoNothing',
                    'result': {'template': f'StageDrops-Stars-{self.recovery_stars}.png', 'score': .99}})
            self.emit(10002)
            self.emit(3, finished_tasks=[len(self.appended)])
            return 1
        if params.get('task_names') == ['ZootdNavigate'] and self.navigation_records is not None:
            for record in self.navigation_records:
                value = copy.deepcopy(record['details'])
                if value.get('taskid') == 5:
                    value['taskid'] = len(self.appended)
                if value.get('finished_tasks') == [5]:
                    value['finished_tasks'] = [len(self.appended)]
                self.callback(record['message'], json.dumps(value).encode(), None)
            return 1
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
    def run_worker(self, records, *, raid=True, navigation_only=False, navigation_records=None,
                   recover_zero_result=False, recovery_stars=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'attempt'
            run.mkdir()
            (run / 'navigation.json').write_text(json.dumps({
                'code': 'MN-EX-7', 'raid': raid, 'navigation_only': navigation_only}))
            (run / 'params.json').write_text(json.dumps({
                'copilot_list': [], 'recover_zero_result': recover_zero_result}))
            lib = FakeCore(records, navigation_records=navigation_records, recovery_stars=recovery_stars)
            with patch('maa_planner.copilot_core.C.CDLL', return_value=lib), \
                    patch('maa_planner.copilot_core.launch_game'), \
                    patch('maa_planner.copilot_core.signal.signal'), \
                    patch('maa_planner.navigation_vision.scan_map', return_value=False), \
                    patch('maa_planner.copilot_core.subprocess.run', return_value=
                          subprocess.CompletedProcess([], 0, stdout=b'')):
                status = worker(root, run, 'device')
            self.loaded_tasks = lib.loaded_tasks
            receipt = json.loads((run / 'worker-result.json').read_text())
            return status, receipt, lib.appended, (run / 'task-id.json').exists()

    def test_pending_two_star_stops_before_startup_or_battle(self):
        status, receipt, appended, task_file = self.run_worker(
            [], raid=False, recover_zero_result=True, recovery_stars=2)
        self.assertEqual(status, 1)
        self.assertEqual(receipt['phase'], 'imperfect_result')
        self.assertEqual(appended, [('Custom', {'task_names': ['ZootdRecoverZeroResult']})])
        self.assertFalse(task_file)

    def test_zero_result_recovery_completes_before_startup_and_normal_navigation(self):
        status, receipt, appended, task_file = self.run_worker(
            [], raid=False, recover_zero_result=True, recovery_stars=0)
        self.assertEqual(status, 0)
        self.assertEqual(appended[0], ('Custom', {'task_names': ['ZootdRecoverZeroResult']}))
        self.assertEqual(appended[1][0], 'StartUp')
        self.assertEqual(appended[-1][0], 'Copilot')
        self.assertTrue(task_file)

    def test_special_panel_adapts_native_checks_only_after_complete_navigation(self):
        for navigation_only in (False, True):
            status, receipt, appended, task_file = self.run_worker(
                [], raid=False, navigation_only=navigation_only, navigation_records=special_events())
            self.assertEqual(status, 0)
            if navigation_only:
                self.assertFalse(task_file)
                self.assertEqual(self.loaded_tasks, [])
            else:
                self.assertTrue(task_file)
                self.assertEqual(appended[-1][0], 'Copilot')
                self.assertEqual(self.loaded_tasks, [(len(appended) - 1, {
                    'StartButton1': {'roi': [775, 560, 415, 60]},
                    'BattleStartPre': {'roi': [775, 560, 415, 60]},
                    'ClickedCorrectStage': {'roi': [770, 145, 250, 90]}})])
        for index in (1, 2, 3):
            changed = special_events()
            del changed[index]
            status, _, appended, task_file = self.run_worker(
                [], raid=False, navigation_records=changed)
            self.assertEqual(status, 1)
            self.assertFalse(task_file)
            self.assertNotIn('Copilot', [kind for kind, _ in appended])
            self.assertEqual(self.loaded_tasks, [])

    def test_encrypted_conditions_stop_before_copilot_even_after_completed_custom(self):
        records = special_events()
        records[1:4] = [copy.deepcopy(records[1])]
        records[1]['details']['details'].update(
            task='ZootdEncryptedRecordPage', result={'text': '加密实验记录03'})
        status, receipt, appended, task_file = self.run_worker(
            [], raid=False, navigation_records=records)
        self.assertEqual(status, 1)
        self.assertEqual(receipt['phase'], 'stage_locked')
        self.assertFalse(task_file)
        self.assertNotIn('Copilot', [kind for kind, _ in appended])
        self.assertFalse(navigation_complete(records, 'MN-EX-7'))

    def test_locked_mode_never_enqueues_copilot_even_with_success_terminal(self):
        records = preflight_events()
        records[2]['details'].update(what='ExceededLimit', details={
            'task': 'ZootdRaidSwitch', 'exec_times': 3, 'max_times': 3})
        records[2]['message'] = 20003
        self.assert_blocked(records)

    def test_reconstruction_click_alone_never_authorizes_copilot(self):
        records = special_events()
        page = copy.deepcopy(records[1])
        page['details']['details'].update(task='ZootdEncryptedRecordPage', result={'text': '加密实验记录04'})
        click = copy.deepcopy(page)
        click['details']['details'].update(task='ZootdEncryptedReconstruct', action='ClickSelf',
                                           result={'text': '事件重构'})
        records[1:1] = [page, click]
        for navigation_only in (False, True):
            status, _, appended, task_file = self.run_worker(
                [], raid=False, navigation_only=navigation_only, navigation_records=records)
            self.assertEqual(status, 0)
            self.assertEqual(task_file, not navigation_only)
        for index in (3, 4, 5):
            changed = copy.deepcopy(records)
            del changed[index]
            status, _, appended, task_file = self.run_worker([], raid=False, navigation_records=changed)
            self.assertEqual(status, 1)
            self.assertFalse(task_file)
            self.assertNotIn('Copilot', [kind for kind, _ in appended])

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
        records = preflight_events()
        with_stats = copy.deepcopy(records)
        with_stats.insert(2, {'message': 2, 'details': {
            'uuid': 'device', 'what': 'ScreencapCost', 'details': {'avg': 161, 'max': 175, 'min': 150}}})
        for events in (records, with_stats):
            status, receipt, appended, task_file = self.run_worker(events)
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
