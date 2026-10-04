import copy
import ctypes as C
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from maa_planner.copilot_core import worker
from maa_planner.copilot_leak_guard import LeakGuard, abort_proof, abort_tasks
from tests.test_copilot_preflight import FakeCore, Function


class LeakGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'attempt-A'
        self.run.mkdir()
        self.resource = self.root / 'var/data/resource'
        directory = self.resource / 'template/Battle/BattleFlag'
        directory.mkdir(parents=True)
        rng = np.random.default_rng(12)
        self.images = {}
        for name in ('BattleHpFlag', 'BattleHpFlag2'):
            template = rng.integers(0, 255, (34, 31, 3), dtype=np.uint8)
            cv2.imwrite(str(directory / (name + '.png')), template)
            image = np.zeros((720, 1280, 3), dtype=np.uint8)
            image[13:47, 681:712] = template
            self.images[name] = image

    def records(self):
        rows = []
        def add(msg, taskid=7, chain='Copilot', **details):
            rows.append({'run_id': self.run.name, 'sequence': len(rows), 'recorded_ns': 100 + len(rows),
                         'message': msg, 'details': dict(uuid='device', taskid=taskid, taskchain=chain, **details)})
        add(10001)
        add(20003, what='CopilotListLoadTaskFileSuccess', details={'stage_name': 'stage', 'file_name': '/execution.json'})
        add(20001, subtask='BattleFormationTask')
        add(20002, subtask='BattleFormationTask')
        add(20001, subtask='BattleProcessTask')
        add(20002, subtask='BattleProcessTask')  # Native task unwinds after stop.
        add(10004)
        add(10001, 8, 'Custom')
        for name, algorithm, action, result in [
                ('ZootdAbortRed', 'MatchTemplate', 'DoNothing', {'template': 'BattleHpFlag2.png'}),
                ('ZootdAbortGear', 'MatchTemplate', 'ClickSelf', {'template': 'RoguelikeBattleExitBegin.png'}),
                ('ZootdAbortAbandon', 'MatchTemplate', 'ClickSelf', {'template': 'NormalBattleAbandon.png'}),
                ('ZootdAbortStagePanel', 'OcrDetect', 'DoNothing', {'text': '开始行动'}),
                ('ZootdAbortStageConfirmed', 'OcrDetect', 'DoNothing', {'text': 'MN-EX-7'})]:
            add(20002, 8, 'Custom', subtask='ProcessTask', first=['ZootdLeakAbort'],
                details={'task': name, 'algorithm': algorithm, 'action': action, 'result': dict(result, score=.99)})
        add(10002, 8, 'Custom')
        add(3, 8, 'Custom', finished_tasks=[8])
        return rows

    def receipt(self):
        directory = self.run / 'leak-guard'
        directory.mkdir(exist_ok=True)
        witness = dict(run_id=self.run.name, task_id=7, uuid='device', abort_task_id=8, requested_ns=105)
        for name, flag in [('baseline', 'BattleHpFlag'), ('leak', 'BattleHpFlag2')]:
            raw = cv2.imencode('.png', self.images[flag])[1].tobytes()
            (directory / (name + '.png')).write_bytes(raw)
            witness[name] = {'recorded_ns': 104 if name == 'baseline' else 105,
                             'file': name + '.png', 'sha256': hashlib.sha256(raw).hexdigest()}
        return dict(run_id=self.run.name, exit_code=0, phase='battle_aborted', leak_abort=witness)

    def prove(self, records, receipt, **changes):
        context = dict(run=self.run, resources=[self.resource], started_ns=100, finished_ns=200,
                       task_id=7, stage='stage', code='MN-EX-7', filename='/execution.json', exit_code=0, raid=False)
        context.update(changes)
        return abort_proof(records, receipt, **context)

    def test_only_bound_started_battle_with_blue_baseline_can_trigger(self):
        guard = LeakGuard([self.resource], self.run, 7)
        self.assertIsNone(guard.inspect(self.images['BattleHpFlag2']))
        for row in self.records()[:5]:
            guard.observe(row['message'], row['details'])
        self.assertIsNone(guard.inspect(self.images['BattleHpFlag2']))
        self.assertIsNone(guard.inspect(self.images['BattleHpFlag']))
        self.assertIsNotNone(guard.inspect(self.images['BattleHpFlag2']))
        guard.observe(10003, dict(uuid='device', taskid=7, taskchain='Copilot', what='ExtraInfo'))
        self.assertTrue(guard.active)
        guard.observe(20002, dict(uuid='device', taskid=7, taskchain='Copilot', subtask='BattleProcessTask'))
        self.assertIsNone(guard.inspect(self.images['BattleHpFlag2']))

    def test_refund_requires_complete_native_abandonment_and_current_image_witness(self):
        rows, receipt = self.records(), self.receipt()
        self.assertEqual(self.prove(rows, receipt)['status'], 'verified')
        self.assertEqual(self.prove(rows, receipt)['sanity_outcome'], 'refunded')
        for changes in ({'raid': True}, {'exit_code': 1}, {'task_id': 9}, {'code': 'DV-S-2'}, {'started_ns': 101}):
            with self.subTest(changes=changes):
                self.assertEqual(self.prove(rows, receipt, **changes)['status'], 'unproven')
        (self.run / 'leak-guard/leak.png').write_bytes((self.run / 'leak-guard/baseline.png').read_bytes())
        self.assertEqual(self.prove(rows, receipt)['status'], 'unproven')

    def test_missing_wrong_old_and_contradictory_callbacks_reject(self):
        rows, receipt = self.records(), self.receipt()
        for index in range(len(rows)):
            if index == 5:  # BattleProcessTask may return false when stopped.
                continue
            changed = copy.deepcopy(rows)
            changed.pop(index)
            for i, row in enumerate(changed):
                row['sequence'] = i
            with self.subTest(missing=index):
                self.assertEqual(self.prove(changed, receipt)['status'], 'unproven')
        for index, field, value in [(6, 'uuid', 'other'), (8, 'taskid', 0), (12, 'first', ['Other'])]:
            changed = copy.deepcopy(rows)
            changed[index]['details'][field] = value
            self.assertEqual(self.prove(changed, receipt)['status'], 'unproven')
        changed = copy.deepcopy(rows)
        changed[6]['message'] = 10003
        self.assertEqual(self.prove(changed, receipt)['status'], 'unproven')
        for message, detail in [(10000, {}), (20003, {'what': 'GameOffline'}),
                                (20002, {'details': {'task': 'StageDrops-Stars-2'}})]:
            changed = copy.deepcopy(rows)
            changed[5].update(message=message, details=dict(uuid='device', taskid=7, taskchain='Copilot', **detail))
            self.assertEqual(self.prove(changed, receipt)['status'], 'unproven')

    def test_abort_graph_never_starts_battle_or_cleans_successful_settlement(self):
        tasks = abort_tasks('MN-EX-7')
        self.assertEqual(tasks['ZootdAbortStagePanel']['action'], 'DoNothing')
        self.assertEqual(tasks['ZootdAbortAbandon']['template'], 'NormalBattleAbandon.png')
        self.assertNotIn('Stars-2', json.dumps(tasks))
        self.assertNotIn('Stars-3', json.dumps(tasks))
        self.assertEqual(tasks['ZootdAbortZeroStars']['maxTimes'], 3)
        self.assertEqual(tasks['ZootdAbortReturn']['maxTimes'], 3)
        self.assertEqual(tasks['ZootdAbortReturn']['postDelay'], 1000)
        self.assertEqual(tasks['ZootdAbortMapStage']['text'], ['MN-EX-7', 'MNEX7'])
        self.assertEqual(tasks['ZootdAbortMapStage']['maxTimes'], 1)
        self.assertEqual(tasks['ZootdAbortMapStage']['next'], ['ZootdAbortStagePanel'])
        self.assertTrue(tasks['ZootdAbortMapStage']['fullMatch'])
        self.assertTrue(all(t['onErrorNext'] == [] and t['exceededNext'] == [] for t in tasks.values()))

    def test_repeated_zero_page_cleanup_is_bounded_and_unknown_inputs_reject(self):
        receipt = self.receipt()
        def extra(task, algorithm, action, result):
            return {'run_id': self.run.name, 'message': 20002, 'details': {'uuid': 'device', 'taskid': 8, 'taskchain': 'Custom',
                'subtask': 'ProcessTask', 'first': ['ZootdLeakAbort'], 'details': {
                'task': task, 'algorithm': algorithm, 'action': action, 'result': result}}}
        zero = extra('ZootdAbortZeroStars', 'MatchTemplate', 'DoNothing',
                     {'template': 'StageDrops-Stars-0.png', 'score': .99})
        click = extra('ZootdAbortReturn', 'JustReturn', 'ClickRect', {})
        for repetitions, expected in [(2, 'verified'), (4, 'unproven')]:
            rows = self.records()
            rows[11:11] = [copy.deepcopy(x) for _ in range(repetitions) for x in (zero, click)]
            for i, row in enumerate(rows):
                row.update(sequence=i, recorded_ns=100+i)
            self.assertEqual(self.prove(rows, receipt)['status'], expected)
        for unexpected in [extra('StartButton1', 'OcrDetect', 'ClickSelf', {'text': '开始行动', 'score': .99}),
                           {'run_id': self.run.name, 'message': 20003, 'details': {'uuid': 'device', 'taskid': 8,
                            'taskchain': 'Custom', 'what': 'ExceededLimit'}}]:
            rows = self.records()
            rows.insert(11, unexpected)
            for i, row in enumerate(rows):
                row.update(sequence=i, recorded_ns=100+i)
            self.assertEqual(self.prove(rows, receipt)['status'], 'unproven')

    def test_exact_map_reopen_requires_detail_and_only_one_click(self):
        receipt = self.receipt()
        original = {'run_id': self.run.name, 'message': 20002, 'details': {
            'uuid': 'device', 'taskid': 8, 'taskchain': 'Custom', 'subtask': 'ProcessTask',
            'first': ['ZootdLeakAbort'], 'details': {'task': 'ZootdAbortMapStage',
            'algorithm': 'OcrDetect', 'action': 'ClickSelf',
            'result': {'text': 'MN-EX-7', 'score': .99}}}}
        for repetitions, text, expected in [(1, 'MN-EX-7', 'verified'),
                                            (2, 'MN-EX-7', 'unproven'),
                                            (1, 'MN-EX-8', 'unproven')]:
            rows = self.records()
            extra = copy.deepcopy(original)
            extra['details']['details']['result']['text'] = text
            rows[11:11] = [copy.deepcopy(extra) for _ in range(repetitions)]
            for i, row in enumerate(rows):
                row.update(sequence=i, recorded_ns=100+i)
            self.assertEqual(self.prove(rows, receipt)['status'], expected)
        rows = self.records()
        rows[11] = copy.deepcopy(original)  # Map code does not replace panel proof.
        self.assertEqual(self.prove(rows, receipt)['status'], 'unproven')

    def test_worker_stops_copilot_before_enqueuing_native_abandonment(self):
        images = self.images
        class GuardCore(FakeCore):
            def __init__(self):
                super().__init__([])
                self.running = False
                self.frame = 0
                self.AsstRunning = Function(lambda *_: int(self.running))
                self.AsstStop = Function(self.stop)
                self.AsstGetImageBgr = Function(self.image)
            def start(self, handle):
                if self.appended[-1][0] == 'Copilot':
                    self.emit(10001)
                    self.emit(20003, what='CopilotListLoadTaskFileSuccess')
                    self.emit(20002, subtask='BattleFormationTask')
                    self.emit(20001, subtask='BattleProcessTask')
                    self.running = True
                    return 1
                self.running = False
                return super().start(handle)
            def image(self, handle, buffer, size):
                flag = 'BattleHpFlag' if self.frame == 0 else 'BattleHpFlag2'
                self.frame += 1
                C.memmove(buffer, images[flag].tobytes(), size)
                return size
            def stop(self, handle):
                if self.running:
                    self.emit(20002, subtask='BattleProcessTask')
                    self.emit(10004)
                self.running = False
                return 1
        (self.run / 'navigation.json').write_text(json.dumps({'code': 'MN-EX-7', 'raid': False}))
        (self.run / 'params.json').write_text(json.dumps({'abort_on_leak': True}))
        core = GuardCore()
        with patch('maa_planner.copilot_core.C.CDLL', return_value=core), \
                patch('maa_planner.copilot_core.launch_game'), \
                patch('maa_planner.copilot_core.signal.signal'), \
                patch('maa_planner.copilot_core.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=b'')):
            self.assertEqual(worker(self.root, self.run, 'device'), 0)
        receipt = json.loads((self.run / 'worker-result.json').read_text())
        self.assertEqual(receipt['phase'], 'battle_aborted')
        self.assertEqual(core.appended[-1], ('Custom', {'task_names': ['ZootdLeakAbort']}))
        events = [json.loads(line) for line in (self.run / 'callbacks.jsonl').read_text().splitlines()]
        stopped = next(i for i, row in enumerate(events) if row['message'] == 10004)
        self.assertEqual(events[stopped + 1]['details']['taskchain'], 'Custom')
        self.assertTrue((self.run / 'leak-guard/leak.png').exists())

    def test_late_two_star_stop_is_never_reported_as_worker_success(self):
        class LateCore(FakeCore):
            def __init__(self):
                super().__init__([])
                self.AsstGetImageBgr = Function(lambda *_: 0)
            def start(self, handle):
                if self.appended[-1][0] == 'Copilot':
                    self.emit(10001)
                    self.emit(20001, subtask='ProcessTask', details={
                        'task': 'StageDrops-Stars-2', 'action': 'Stop', 'algorithm': 'MatchTemplate',
                        'result': {'template': 'StageDrops-Stars-2.png', 'score': .99}})
                    self.emit(10002)
                    self.emit(3, finished_tasks=[len(self.appended)])
                    return 1
                return super().start(handle)
        (self.run / 'navigation.json').write_text(json.dumps({'code': 'MN-EX-7', 'raid': False}))
        (self.run / 'params.json').write_text(json.dumps({'abort_on_leak': True}))
        core = LateCore()
        with patch('maa_planner.copilot_core.C.CDLL', return_value=core), \
                patch('maa_planner.copilot_core.launch_game'), \
                patch('maa_planner.copilot_core.signal.signal'), \
                patch('maa_planner.copilot_core.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=b'')):
            self.assertEqual(worker(self.root, self.run, 'device'), 1)
        self.assertEqual(json.loads((self.run / 'worker-result.json').read_text())['phase'], 'imperfect_result')
        self.assertEqual(core.appended[-1][0], 'Copilot')
