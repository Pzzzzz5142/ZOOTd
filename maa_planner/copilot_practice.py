"""Exercise-only resources and fresh, same-candidate admission to live battle.

Practice evidence is confined to one locked experiment. It never grants saved
proxy capability and cannot be imported from old result files.
"""
from __future__ import annotations

import math

REQUIRED_PRACTICES = 3
PRACTICE_TEXT = ('演习',)


def practice_tasks():
    # Selection happens inside Copilot, AFTER its native stage confirmation.
    # Opening formation before MultiCopilot's navigation would lose the panel.
    stop = {'algorithm': 'JustReturn', 'action': 'Stop', 'next': [],
            'sub': [], 'onErrorNext': [], 'exceededNext': []}
    tasks = {name: dict(stop) for name in (
        'UseMedicine', 'MedicineConfirm', 'UseStone', 'StoneConfirm',
        'BattleStartNormal', 'BattleStartRaid', 'BattleStartAdverse',
        'BattleStartSimulation', 'BattleStartSimulationForTwo',
        'BattleStartTrials', 'BattleStartTrialsRaid', 'BattleStartOCR')}
    tasks.update({
        'ZootdPracticeReady': {'algorithm': 'OcrDetect', 'text': list(PRACTICE_TEXT),
            'fullMatch': True, 'roi': [640, 550, 640, 170], 'action': 'DoNothing',
            'next': [], 'sub': [], 'onErrorNext': [], 'exceededNext': []},
        'BattleStartPre': {'algorithm': 'OcrDetect', 'text': list(PRACTICE_TEXT),
            'fullMatch': True, 'roi': [640, 550, 640, 170], 'action': 'ClickSelf',
            'preDelay': 500, 'postDelay': 1000, 'maxTimes': 1,
            'next': [], 'sub': [], 'onErrorNext': [], 'exceededNext': []},
        'BattleStartAll': {'algorithm': 'JustReturn', 'action': 'DoNothing',
            'next': ['BattleStartExercise'], 'sub': [], 'onErrorNext': [], 'exceededNext': []},
        'BattleStartExercise': {'baseTask': 'BattleStart', 'algorithm': 'MatchTemplate',
            'template': 'BattleStartExercise.png', 'templThreshold': .9,
            'action': 'ClickSelf', 'maxTimes': 1,
            'next': ['BattleOfficiallyBegin', 'SkipThePreBattlePlot', 'Stop'],
            'sub': [], 'onErrorNext': [], 'exceededNext': []},
        # CN exercises use the same 行动结束 / stars screen as live battles.
        # Exercise identity is established at entry/start, not inferred from
        # an invented result label. Preserve the native end-marker template.
        'Copilot@EndOfAction': {'baseTask': 'EndOfAction', 'algorithm': 'MatchTemplate',
            'template': 'EndOfAction.png', 'templThreshold': .8,
            'action': 'DoNothing', 'postDelay': 2500,
            'next': ['Copilot@StageDrops-Stars-3', 'Copilot@StageDrops-Stars-2',
                     'Copilot@StageDrops-Stars-0'],
            'sub': [], 'onErrorNext': [], 'exceededNext': []},
    })
    return tasks


def practice_ready(events, *, task_id):
    """One completed Custom chain must observe the exercise button without clicking."""
    device = None
    observed = completed = finished = False
    for event in events:
        msg, v = event['message'], event['details']
        if (msg in (0, 1, 10000, 10004, 20000, 20004)
                or v.get('what') in ('ExceededLimit', 'GameOffline', 'Disconnect', 'Reconnecting')):
            return False
        if (msg == 2 and device is not None and v.get('uuid') == device
                and v.get('what') in ('ScreencapCost', 'EmulatorFPS')
                and set(v) == {'uuid', 'what', 'details'}):
            continue
        if (v.get('taskchain') != 'Custom' or type(v.get('taskid')) is not int
                or v['taskid'] != task_id or finished):
            return False
        if msg == 10001:
            if device is not None or not isinstance(v.get('uuid'), str) or not v['uuid']:
                return False
            device = v['uuid']
        elif device is None or v.get('uuid') != device:
            return False
        d = v.get('details', {})
        if msg == 20002:
            if (observed or completed or v.get('subtask') != 'ProcessTask'
                    or v.get('first') != ['ZootdPracticeReady']
                    or d.get('task') != 'ZootdPracticeReady' or d.get('action') != 'DoNothing'
                    or d.get('algorithm') != 'OcrDetect'
                    or d.get('result', {}).get('text') not in PRACTICE_TEXT):
                return False
            observed = True
        if msg == 10002:
            if not observed or completed:
                return False
            completed = True
        if msg == 3:
            if not completed or v.get('finished_tasks') != [task_id]:
                return False
            finished = True
    return finished


def practice_proof(events, battle):
    """Add positive exercise entry/start/result observations to fresh star evidence."""
    result = {**battle, 'practice': True, 'saved_proxy': 'not_applicable',
              'ledger_recorded': False, 'status': 'unproven'}
    if battle.get('status') != 'observed' or not battle.get('three_star'):
        return dict(result, reason='practice_three_star_missing')
    phase = 0
    for e in events:
        v, msg = e['details'], e['message']
        if (v.get('taskchain') != 'Copilot' or v.get('uuid') != battle.get('uuid')
                or type(v.get('taskid')) is not int or v['taskid'] != battle.get('task_id')):
            continue
        d = v.get('details', {})
        task = d.get('task', '').removeprefix('Copilot@')
        if task in ('BattleStartNormal', 'BattleStartRaid', 'BattleStartOCR'):
            return dict(result, reason='non_practice_start')
        if msg != 20002:
            continue
        if task == 'BattleStartPre':
            if (phase != 0 or d.get('algorithm') != 'OcrDetect' or d.get('action') != 'ClickSelf'
                    or d.get('result', {}).get('text') not in PRACTICE_TEXT):
                return dict(result, reason='invalid_practice_entry')
            phase = 1
        elif task == 'BattleStartExercise':
            r = d.get('result', {})
            if (phase != 1 or d.get('algorithm') != 'MatchTemplate' or d.get('action') != 'ClickSelf'
                    or r.get('template') != 'BattleStartExercise.png'
                    or type(r.get('score')) not in (int, float) or not math.isfinite(r['score'])
                    or not .9 <= r['score'] <= 1):
                return dict(result, reason='invalid_practice_start')
            phase = 2
        elif task == 'EndOfAction':
            r = d.get('result', {})
            if (phase != 2 or d.get('algorithm') != 'MatchTemplate' or d.get('action') != 'DoNothing'
                    or v.get('first') != ['Copilot@WaitUntilEndOfAction']
                    or r.get('template') != 'EndOfAction.png'
                    or type(r.get('score')) not in (int, float) or not math.isfinite(r['score'])
                    or not .8 <= r['score'] <= 1):
                return dict(result, reason='invalid_practice_result')
            phase = 3
        elif task == 'StageDrops-Stars-3':
            if phase != 3:
                return dict(result, reason='practice_result_before_stars_missing')
            phase = 4
    if phase != 4:
        return dict(result, reason='practice_mode_not_proven')
    return dict(result, status='observed', reason='fresh_practice_three_star')


def admit_live(practices, *, execution_sha256, snapshot_sha256):
    """Require the immediately preceding three successes for the same fixed execution."""
    if len(practices) != REQUIRED_PRACTICES:
        return False
    runs = set()
    for a in practices:
        proof = a.get('battle_proof', {})
        if (a.get('status') != 'success' or a.get('mode') != 'practice'
                or a.get('execution_sha256') != execution_sha256
                or a.get('snapshot_sha256') != snapshot_sha256
                or proof.get('status') != 'observed' or proof.get('three_star') is not True
                or proof.get('practice') is not True or proof.get('ledger_recorded') is not False
                or proof.get('execution_sha256') != execution_sha256
                or proof.get('run_id') != a.get('run_id') or not a.get('run_id')
                or a['run_id'] in runs):
            return False
        runs.add(a['run_id'])
    return True
