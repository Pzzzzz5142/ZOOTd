"""Fresh Copilot battle evidence; never equates completion with saved proxy.

The callback recorder is the trust boundary. Run identity and monotonic sequence
are assigned there, not read from MaaCore's reusable device UUID/task IDs.
"""
from __future__ import annotations

import math

from .copilot_core import callbacks_are_fresh, terminal_result
from .util import canonical_json, sha256_bytes


def battle_proof(events: list[dict], *, run_id: str, task_id: int, stage: str,
                 filename: str, copilot_id: int, copilot_sha256: str,
                 execution_sha256: str, started_ns: int, finished_ns: int,
                 exit_code: int, support_used: bool) -> dict:
    """Reduce recorder-owned evidence, failing closed on old/malformed records.

    This proves a battle-screen observation, not account ownership, first clear,
    activity scope, or a saved auto-deploy. Those facts need independent evidence.
    """
    result = {'schema': 1, 'status': 'unproven', 'three_star': False,
              'saved_proxy': 'unknown', 'account_binding': 'unknown',
              'activity_binding': 'unknown', 'ledger_recorded': False,
              'support_used': support_used}

    def reject(reason):
        return dict(result, reason=reason)

    if (not isinstance(run_id, str) or not run_id or type(task_id) is not int
            or task_id < 0 or type(copilot_id) is not int or copilot_id <= 0
            or type(support_used) is not bool
            or any(not isinstance(v, str) or not v for v in (stage, filename))
            or any(not isinstance(v, str) or len(v) != 64
                   or any(c not in '0123456789abcdef' for c in v)
                   for v in (copilot_sha256, execution_sha256))
            or type(started_ns) is not int or type(finished_ns) is not int
            or not 0 < started_ns <= finished_ns):
        return reject('invalid_context')
    if type(exit_code) is not int or exit_code != 0:
        return reject('worker_failed')
    if not isinstance(events, list) or not events:
        return reject('missing_callbacks')
    if not callbacks_are_fresh(events, run_id=run_id, started_ns=started_ns, finished_ns=finished_ns):
        return reject('stale_or_malformed_callbacks')
    try:
        terminal = terminal_result(events, task_id=task_id, stage=stage, filename=filename)
    except (TypeError, ValueError, KeyError):
        return reject('invalid_terminal')
    if terminal['status'] != 'success':
        return reject('incomplete_terminal')

    active = None
    phase = 0
    stars = None
    all_done = False
    for event in events:
        msg, value = event['message'], event['details']
        if msg in (20000, 20004) or value.get('what') == 'GameOffline':
            return reject('callback_failure')
        if msg == 10001 and value.get('taskchain') == 'Copilot':
            active = (value.get('uuid'), value.get('taskid'))
            if not isinstance(active[0], str) or not active[0]:
                return reject('invalid_device')
        if msg == 3 and task_id in value.get('finished_tasks', []):
            if (phase != 4 or all_done or value.get('uuid') != active[0]
                    or value.get('taskchain') != 'Copilot'
                    or type(value.get('taskid')) is not int or value['taskid'] != task_id
                    or not isinstance(value.get('finished_tasks'), list)
                    or any(type(t) is not int for t in value['finished_tasks'])):
                return reject('invalid_all_tasks_completion')
            all_done = True
            continue
        if value.get('taskchain') != 'Copilot':
            continue
        if (active is None or active != (value.get('uuid'), value.get('taskid'))
                or type(value.get('taskid')) is not int):
            return reject('wrong_execution_identity')
        if phase == 4:
            return reject('callback_after_chain_completion')
        detail = value.get('details', {})
        if msg == 20003 and value.get('what') == 'CopilotListLoadTaskFileSuccess':
            if phase != 0 or detail.get('stage_name') != stage or detail.get('file_name') != filename:
                return reject('unexpected_load')
            phase = 1
        if msg == 20002 and value.get('subtask') == 'BattleFormationTask':
            if phase != 1:
                return reject('unexpected_formation')
            phase = 2
        if msg == 20002 and value.get('subtask') == 'BattleProcessTask':
            if phase != 2:
                return reject('unexpected_battle')
            phase = 3
        task = detail.get('task', '')
        if not isinstance(task, str):
            return reject('invalid_task')
        task = task.removeprefix('Copilot@')
        if msg == 20002 and ('FightMissionFailed' in task or task in {
                'StageDrops-Stars-2', 'StageDrops-Stars-Adverse', 'EndOfAction-Sandbox'}):
            return reject('unsupported_battle_result')
        if msg == 20002 and task == 'StageDrops-Stars-3':
            match = detail.get('result', {})
            if (phase != 3 or stars is not None or value.get('subtask') != 'ProcessTask'
                    or value.get('first') != ['Copilot@WaitUntilEndOfAction']
                    or detail.get('action') != 'DoNothing'
                    or detail.get('algorithm') != 'MatchTemplate'
                    or not isinstance(match, dict)
                    or match.get('template') != 'StageDrops-Stars-3.png'
                    or type(match.get('score')) not in (int, float)
                    or not math.isfinite(match['score']) or not 0 < match['score'] <= 1):
                return reject('invalid_three_star_observation')
            stars = event['sequence']
        if msg == 10002:
            if phase != 3 or stars is None:
                return reject('missing_three_star_observation')
            phase = 4
    if phase != 4 or stars is None or not all_done:
        return reject('missing_three_star_observation')
    return dict(result, status='observed', three_star=True,
                reason='account_activity_and_saved_proxy_not_proven',
                run_id=run_id, stage=stage, task_id=task_id, uuid=active[0],
                copilot_id=copilot_id, copilot_sha256=copilot_sha256,
                execution_sha256=execution_sha256, star_sequence=stars,
                started_ns=started_ns, finished_ns=finished_ns,
                callbacks_canonical_sha256=sha256_bytes(canonical_json(events)))
