"""Abort leaking normal Copilot battles using native MAA observations/actions."""
from __future__ import annotations

import hashlib
import math
import time

from .copilot_core import callbacks_are_fresh, formation_auxiliary
from .navigation_vision import resource_file
from .copilot_navigation import zero_result_friend_tasks


class LeakRecognizer:
    def __init__(self, resources):
        import cv2
        self.templates = {}
        for name in ('BattleHpFlag', 'BattleHpFlag2'):
            path = resource_file(resources, f'template/Battle/BattleFlag/{name}.png')
            template = cv2.imread(str(path))
            if template is None:
                raise ValueError('Missing native battle HP template')
            self.templates[name] = template

    def scores(self, image):
        import cv2
        if image is None or image.shape != (720, 1280, 3):
            raise ValueError('Unexpected battle image dimensions')
        roi = image[0:60, 400:1050]
        return {name: float(cv2.minMaxLoc(cv2.matchTemplate(
            roi, template, cv2.TM_CCOEFF_NORMED))[1])
                for name, template in self.templates.items()}


class LeakGuard:
    def __init__(self, resources, run, task_id):
        self.recognizer = LeakRecognizer(resources)
        self.run, self.task_id = run, task_id
        self.uuid = None
        self.loaded = self.formed = self.active = False
        self.baseline = None

    def observe(self, message, value):
        if value.get('taskchain') != 'Copilot' or value.get('taskid') != self.task_id:
            return
        if message == 10001 and isinstance(value.get('uuid'), str) and value['uuid']:
            self.uuid = value['uuid']
        if value.get('uuid') != self.uuid or self.uuid is None:
            return
        if message == 20003 and value.get('what') == 'CopilotListLoadTaskFileSuccess':
            self.loaded = True
        if message == 20002 and value.get('subtask') == 'BattleFormationTask':
            self.formed = self.loaded
        if message == 20001 and value.get('subtask') == 'BattleProcessTask':
            self.active = self.formed
        if (message in (10000, 10002, 10004, 20000, 20004)
                or message == 20002 and value.get('subtask') == 'BattleProcessTask'):
            self.active = False

    def inspect(self, image):
        """A fresh blue baseline precedes the unambiguous red leak indicator."""
        import cv2
        if not self.active:
            return None
        scores = self.recognizer.scores(image)
        if self.baseline is None and scores['BattleHpFlag'] >= .9:
            self.baseline = self._save('baseline', image, scores, cv2)
        if self.baseline is not None and scores['BattleHpFlag2'] >= .9:
            return {'run_id': self.run.name, 'task_id': self.task_id, 'uuid': self.uuid,
                    'baseline': self.baseline, 'leak': self._save('leak', image, scores, cv2)}
        return None

    def _save(self, name, image, scores, cv2):
        directory = self.run / 'leak-guard'
        directory.mkdir(exist_ok=True)
        path = directory / (name + '.png')
        recorded_ns = time.monotonic_ns()
        ok, png = cv2.imencode('.png', image)
        if not ok:
            raise RuntimeError('Could not preserve leak witness')
        raw = png.tobytes()
        with path.open('xb') as output:
            output.write(raw)
        return {'recorded_ns': recorded_ns, 'file': path.name,
                'sha256': hashlib.sha256(raw).hexdigest(), 'scores': scores}


def abort_tasks(code):
    """Bounded native abandonment; no start, refill or successful-result action."""
    result_guards = ['ZootdAbortStars-3', 'ZootdAbortStars-Adverse', 'ZootdAbortStars-2']
    failure_results = ['ZootdAbortFailureScreen', 'ZootdAbortZeroStars']
    failed = result_guards + failure_results
    cleanup = result_guards + ['ZootdAbortStagePanel', 'ZootdAbortFailureScreen',
               'ZootdAbortZeroStars', 'ZootdAbortLoading', 'ZootdAbortMapStage']
    tasks = {
        'ZootdLeakAbort': {'algorithm': 'JustReturn',
                          'next': result_guards + ['ZootdAbortRed', 'ZootdAbortBlue'] + failure_results},
        'ZootdAbortGear': {'baseTask': 'RoguelikeBattleExitBegin',
                           'template': 'RoguelikeBattleExitBegin.png', 'maxTimes': 1,
                           'postDelay': 200, 'next': ['ZootdAbortAbandon'] + failed},
        'ZootdAbortAbandon': {'baseTask': 'NormalBattleAbandon', 'template': 'NormalBattleAbandon.png',
                              'maxTimes': 1, 'postDelay': 500, 'next': cleanup},
        'ZootdAbortStagePanel': {'baseTask': 'StartButton1', 'action': 'DoNothing',
                                'ocrReplace': [[r'^[+＋]开始行动$', '开始行动']],
                                'postDelay': 0, 'next': ['ZootdAbortStageConfirmed']},
        'ZootdAbortStageConfirmed': {'baseTask': 'ClickedCorrectStage', 'action': 'DoNothing',
                                    'text': [code, code.replace('-', '')], 'next': []},
        'ZootdAbortFailureScreen': {'baseTask': 'FightMissionFailed', 'preDelay': 0,
                                   'maxTimes': 2, 'postDelay': 1000, 'next': cleanup},
        'ZootdAbortZeroStars': {'algorithm': 'MatchTemplate', 'template': 'StageDrops-Stars-0.png',
                               'roi': [50, 270, 250, 100], 'action': 'DoNothing',
                               'maxTimes': 3, 'next': ['ZootdAbortFriendPrompt', 'ZootdAbortReturn']},
        'ZootdAbortReturn': {'baseTask': 'ClickCorner', 'maxTimes': 3, 'postDelay': 1000,
                            'next': cleanup},
        'ZootdAbortLoading': {'baseTask': 'LoadingIcon', 'template': 'LoadingIcon.png',
                             'action': 'DoNothing', 'maxTimes': 30, 'postDelay': 500, 'next': cleanup},
        # Abandonment may return to the map rather than retain the detail
        # panel. Reopen only the exact visible target once, without swiping.
        'ZootdAbortMapStage': {'baseTask': 'ClickStageName', 'text': [code, code.replace('-', '')],
                              'isAscii': True, 'fullMatch': True, 'specialParams': [],
                              'maxTimes': 1, 'postDelay': 700, 'next': ['ZootdAbortStagePanel']},
    }
    for name, template in [('ZootdAbortRed', 'BattleHpFlag2'), ('ZootdAbortBlue', 'BattleHpFlag')]:
        tasks[name] = {'baseTask': template, 'template': template + '.png',
                       'action': 'DoNothing', 'templThreshold': .9,
                       'next': ['ZootdAbortGear'] + failed}
    for stars in ('3', 'Adverse', '2'):
        tasks['ZootdAbortStars-' + stars] = {
            'algorithm': 'MatchTemplate', 'template': f'StageDrops-Stars-{stars}.png',
            'templThreshold': .8, 'roi': [50, 270, 250, 100], 'action': 'Stop', 'next': []}
    tasks.update(zero_result_friend_tasks('ZootdAbort', ['ZootdAbortReturn']))
    for task in tasks.values():
        task.update(sub=[], onErrorNext=[], exceededNext=[])
    return tasks


def abort_proof(events, receipt, *, run, resources, started_ns, finished_ns,
                task_id, stage, code, filename, exit_code, raid):
    """An intentional stop is a failure, never a successful Copilot terminal."""
    rejected = {'status': 'unproven', 'reason': 'incomplete_leak_abort'}
    witness = receipt.get('leak_abort', {})
    if (raid or exit_code != 0 or receipt.get('phase') != 'battle_aborted'
            or receipt.get('run_id') != run.name or receipt.get('exit_code') != exit_code
            or type(task_id) is not int or not callbacks_are_fresh(
                events, run_id=run.name, started_ns=started_ns, finished_ns=finished_ns)
            or witness.get('run_id') != run.name or witness.get('task_id') != task_id
            or type(witness.get('abort_task_id')) is not int or witness['abort_task_id'] == task_id):
        return rejected
    uuid = witness.get('uuid')
    if not isinstance(uuid, str) or not uuid:
        return rejected
    try:
        import cv2
        import numpy as np
        recognizer = LeakRecognizer(resources)
        previous = started_ns
        for name, template in [('baseline', 'BattleHpFlag'), ('leak', 'BattleHpFlag2')]:
            observation = witness[name]
            when = observation['recorded_ns']
            if type(when) is not int or not previous <= when <= finished_ns or observation['file'] != name + '.png':
                return rejected
            raw = (run / 'leak-guard' / observation['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != observation['sha256']:
                return rejected
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if recognizer.scores(image)[template] < .9:
                return rejected
            previous = when
        requested = witness['requested_ns']
        if type(requested) is not int or not previous <= requested <= finished_ns:
            return rejected
    except (KeyError, TypeError, ValueError, OSError):
        return rejected

    loaded = forming = formed = battling = stopped = custom = completed = done = False
    chain_started = False
    observed = []
    battle_start = None
    abort_id = witness['abort_task_id']
    expected = [('hp', 'MatchTemplate', 'DoNothing'),
                ('ZootdAbortGear', 'MatchTemplate', 'ClickSelf'),
                ('ZootdAbortAbandon', 'MatchTemplate', 'ClickSelf'),
                ('ZootdAbortStagePanel', 'OcrDetect', 'DoNothing'),
                ('ZootdAbortStageConfirmed', 'OcrDetect', 'DoNothing')]
    cleanup_signatures = {
        'ZootdLeakAbort': ('JustReturn', 'DoNothing', 1),
        'ZootdAbortFailureScreen': ('OcrDetect', 'ClickSelf', 2),
        'ZootdAbortZeroStars': ('MatchTemplate', 'DoNothing', 3),
        'ZootdAbortReturn': ('JustReturn', 'ClickRect', 3),
        'ZootdAbortLoading': ('MatchTemplate', 'DoNothing', 30),
        'ZootdAbortMapStage': ('OcrDetect', 'ClickSelf', 1),
        'ZootdAbortFriendPrompt': ('OcrDetect', 'DoNothing', 1),
        'ZootdAbortFriendCancel': ('MatchTemplate', 'ClickSelf', 1),
    }
    signatures = {name: (algorithm, action) for name, algorithm, action in expected[1:]}
    signatures.update({name: ('MatchTemplate', 'DoNothing') for name in ('ZootdAbortRed', 'ZootdAbortBlue')})
    primary_tasks = set(signatures)
    signatures.update({name: item[:2] for name, item in cleanup_signatures.items()})
    cleanup_counts = {}
    defeat = False
    defeat_evidence = []
    friend_prompt = False
    for event in events:
        msg, value = event['message'], event['details']
        detail = value.get('details', {})
        if battling and any(token in str(detail.get('task', ''))
                            for token in ('Stars-2', 'Stars-3', 'Stars-Adverse')):
            return rejected
        if (msg in (0, 1, 10000, 20004)
                or value.get('what') in ('GameOffline', 'Disconnect', 'Reconnecting', 'ScreencapFailed')):
            return rejected
        if msg == 10004 and not (value.get('taskchain') == 'Copilot'
                and value.get('taskid') == task_id and value.get('uuid') == uuid
                and event['recorded_ns'] >= requested):
            return rejected
        if value.get('taskchain') == 'Copilot':
            if (not forming and msg == 20000 and value.get('subtask') == 'ProcessTask'
                    and value.get('first') == ['NotUsePrts'] and detail == {}):
                continue
            if forming and not formed and formation_auxiliary(msg, value, device_uuid=uuid):
                continue
            if value.get('uuid') != uuid or value.get('taskid') != task_id or stopped or msg == 20000:
                return rejected
            if msg == 10001:
                if chain_started:
                    return rejected
                chain_started = True
            elif not chain_started:
                return rejected
            if msg == 20003 and value.get('what') == 'CopilotListLoadTaskFileSuccess':
                if loaded or detail.get('stage_name') != stage or detail.get('file_name') != filename:
                    return rejected
                loaded = True
            if msg == 20001 and value.get('subtask') == 'BattleFormationTask':
                if not loaded or forming:
                    return rejected
                forming = True
            if msg == 20002 and value.get('subtask') == 'BattleFormationTask':
                if not forming or formed:
                    return rejected
                formed = True
            if msg == 20001 and value.get('subtask') == 'BattleProcessTask':
                if not formed or battling:
                    return rejected
                battling, battle_start = True, event['recorded_ns']
            if (msg in (10002, 3) or msg == 20002 and value.get('subtask') == 'BattleProcessTask'
                    and event['recorded_ns'] < requested
                    or 'Stars-' in str(detail.get('task', ''))):
                return rejected
            if msg == 10004:
                if not battling or not battle_start <= witness['baseline']['recorded_ns'] <= requested <= event['recorded_ns']:
                    return rejected
                stopped = True
        elif value.get('taskchain') == 'Custom' and value.get('taskid') == abort_id:
            if (not stopped or value.get('uuid') != uuid or done
                    or completed and msg != 3 or msg == 20000 or value.get('what') == 'ExceededLimit'):
                return rejected
            if msg == 10001:
                if custom:
                    return rejected
                custom = True
            elif not custom:
                return rejected
            if msg in (20001, 20002):
                task = detail.get('task')
                if (value.get('first') != ['ZootdLeakAbort'] or value.get('subtask') != 'ProcessTask'
                        or task not in signatures
                        or (detail.get('algorithm'), detail.get('action')) != signatures[task]):
                    return rejected
                if task in primary_tasks:
                    if len(observed) >= len(expected):
                        return rejected
                    name = expected[len(observed)][0]
                    if task not in ('ZootdAbortRed', 'ZootdAbortBlue') if name == 'hp' else task != name:
                        return rejected
                if task.startswith('ZootdAbortFriend') and (cleanup_counts.get('ZootdAbortZeroStars', 0) < 1
                        or task == 'ZootdAbortFriendCancel' and not friend_prompt):
                    return rejected
                defeat_signal = task in ('ZootdAbortFailureScreen', 'ZootdAbortZeroStars') and not defeat and len(observed) < 3
                if task in cleanup_signatures:
                    phase = 0 if task == 'ZootdLeakAbort' else len(expected) - 2
                    if len(observed) != phase and not defeat_signal:
                        return rejected
                if task in cleanup_signatures and msg == 20002:
                    cleanup_counts[task] = cleanup_counts.get(task, 0) + 1
                    if cleanup_counts[task] > cleanup_signatures[task][2]:
                        return rejected
                    result = detail.get('result', {})
                    if task in ('ZootdLeakAbort', 'ZootdAbortReturn'):
                        if result != {}:
                            return rejected
                    else:
                        score = result.get('score')
                        if type(score) not in (int, float) or not math.isfinite(score) or not 0 < score <= 1:
                            return rejected
                        if task == 'ZootdAbortFailureScreen' and (result.get('text') != '任务失败' or score < .8):
                            return rejected
                        if task == 'ZootdAbortMapStage' and result.get('text') not in (code, code.replace('-', '')):
                            return rejected
                        if task == 'ZootdAbortFriendPrompt':
                            if result.get('text') != '是否添加为好友' or score < .8:
                                return rejected
                            friend_prompt = True
                        if task == 'ZootdAbortFriendCancel' and (result.get('template') != 'StageResult-FriendCancel.png' or score < .9):
                            return rejected
                        templates = {'ZootdAbortZeroStars': 'StageDrops-Stars-0.png',
                                     'ZootdAbortLoading': 'LoadingIcon.png'}
                        if task in templates and (result.get('template') != templates[task] or score < .8):
                            return rejected
                    if defeat_signal:
                        # The game may finish a real defeat before the native
                        # stop unwinds. Require fresh defeat plus exact return,
                        # without inventing a gear/abandon click that did not run.
                        expected = expected[:len(observed)] + expected[3:]
                        defeat = True
                        defeat_evidence.append({'sequence': event['sequence'], 'task': task})
            if msg == 20002 and detail.get('task') in primary_tasks:
                if len(observed) >= len(expected):
                    return rejected
                name, algorithm, action = expected[len(observed)]
                actual = detail.get('task')
                result = detail.get('result', {})
                score = result.get('score')
                if (value.get('first') != ['ZootdLeakAbort'] or value.get('subtask') != 'ProcessTask'
                        or (actual not in ('ZootdAbortRed', 'ZootdAbortBlue') if name == 'hp' else actual != name)
                        or detail.get('algorithm') != algorithm or detail.get('action') != action
                        or type(score) not in (int, float) or not math.isfinite(score) or not 0 < score <= 1):
                    return rejected
                templates = {'ZootdAbortRed': 'BattleHpFlag2.png', 'ZootdAbortBlue': 'BattleHpFlag.png',
                             'ZootdAbortGear': 'RoguelikeBattleExitBegin.png',
                             'ZootdAbortAbandon': 'NormalBattleAbandon.png'}
                if actual in templates and result.get('template') != templates[actual]:
                    return rejected
                if name == 'hp' and score < .9:
                    return rejected
                if actual == 'ZootdAbortStagePanel' and result.get('text') not in ('开始行动', '开始作战', '开始推演'):
                    return rejected
                if actual == 'ZootdAbortStageConfirmed' and result.get('text') not in (code, code.replace('-', '')):
                    return rejected
                observed.append({'sequence': event['sequence'], 'task': actual})
            if msg == 10002:
                if len(observed) != len(expected):
                    return rejected
                completed = True
            if msg == 3:
                if not completed or value.get('finished_tasks') != [abort_id]:
                    return rejected
                done = True
        elif battling and value.get('taskchain'):
            return rejected
    return ({'status': 'verified', 'reason': 'leak_raced_to_defeat' if defeat else 'leak_abandoned',
             'evidence': sorted(observed + defeat_evidence, key=lambda e: e['sequence']),
             'sanity_outcome': 'refunded'} if done else rejected)
