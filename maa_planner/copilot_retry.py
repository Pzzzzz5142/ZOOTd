"""Bounded candidate retries, with fail-closed structured failure evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from .copilot_core import callbacks_are_fresh


@dataclass(frozen=True)
class RetryLimits:
    max_candidates: int = 1
    max_battles: int = 1
    sanity_budget: int | None = None

    def __post_init__(self):
        for name, maximum in (('max_candidates', 5), ('max_battles', 3)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'{name} must be in 1..{maximum}')
        if self.sanity_budget is not None and (type(self.sanity_budget) is not int
                                               or not 0 <= self.sanity_budget <= 999):
            raise ValueError('sanity_budget must be in 0..999')
        if self.max_battles > 1 and self.sanity_budget is None:
            raise ValueError('Multiple battles require an explicit sanity budget')


@dataclass
class RetryBudget:
    limits: RetryLimits
    stage_cost: int
    candidates: int = 0
    battle_reservations: int = 0
    sanity_reserved: int = 0
    sanity_released: int = 0
    _settled: set[int] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self):
        if type(self.stage_cost) is not int or not 1 <= self.stage_cost <= 999:
            raise ValueError('Unknown or invalid installed stage sanity cost')

    @property
    def sanity_limit(self):
        return self.stage_cost if self.limits.sanity_budget is None else self.limits.sanity_budget

    def take_candidate(self):
        if self.candidates >= self.limits.max_candidates:
            return False
        self.candidates += 1
        return True

    def reserve_battle(self):
        # A worker may start a battle before callbacks are durable. Reserve the
        # entire cost before dispatch; only proven zero-cost outcomes release it.
        if (self.battle_reservations >= self.limits.max_battles
                or self.sanity_reserved + self.stage_cost > self.sanity_limit):
            return False
        self.battle_reservations += 1
        self.sanity_reserved += self.stage_cost
        return True

    def settle(self, reservation: int, *, zero_cost: bool):
        """Settle each dispatch once, without restoring its execution allowance."""
        if (type(reservation) is not int or not 1 <= reservation <= self.battle_reservations
                or reservation in self._settled or type(zero_cost) is not bool):
            raise ValueError('Invalid or already settled battle reservation')
        self._settled.add(reservation)
        released = self.stage_cost if zero_cost else 0
        self.sanity_reserved -= released
        self.sanity_released += released
        return released

    def as_dict(self):
        return {'candidates_considered': self.candidates,
                'battle_reservations': self.battle_reservations,
                'sanity_reserved': self.sanity_reserved, 'sanity_released': self.sanity_released,
                'sanity_limit': self.sanity_limit,
                'stage_cost': self.stage_cost}


def failure(category, *, retryable=False, evidence=None, sanity_outcome='charged_or_unknown'):
    return {'category': category, 'retryable': retryable, 'evidence': evidence or [],
            'sanity_outcome': sanity_outcome}


def classify_failure(events, *, run_id, started_ns, finished_ns, task_id,
                     stage, filename, exit_code, worker_phase=None):
    """Only explicit candidate failures in a fresh, failed Copilot chain retry.

    Informational unmet requirements can be recovered by formation itself; they
    become rejection reasons only with OperatorMissing + TaskChainError and no
    completed formation. Generic BattleProcess errors are never battle results.
    """
    if type(exit_code) is not int or exit_code != 0:
        return failure({'adb': 'adb_failure', 'navigation': 'navigation_failure'}.get(
            worker_phase, 'runtime_failure'))
    if (type(task_id) is not int or task_id < 0 or not callbacks_are_fresh(
            events, run_id=run_id, started_ns=started_ns, finished_ns=finished_ns)):
        return failure('unknown_execution_failure')
    # Infrastructure vetoes always win, even after a candidate-specific event.
    for event in events:
        msg, value = event['message'], event['details']
        if any(value.get(k) is not None and not isinstance(value[k], str)
               for k in ('what', 'taskchain', 'subtask', 'why')):
            return failure('unknown_execution_failure')
        if msg in (0, 1, 10004, 20004) or value.get('what') == 'GameOffline':
            return failure('runtime_failure')
        if value.get('what') in {'Disconnect', 'Reconnecting', 'ScreencapFailed', 'TouchModeNotAvailable'}:
            return failure('adb_failure')
        if msg == 10000 and value.get('details', {}).get('error'):
            return failure('runtime_failure')
        if msg == 10000 and value.get('taskchain') != 'Copilot':
            return failure('navigation_failure')

    active = None
    all_done = False
    loaded = forming = formed = battling = chain_failed = False
    missing = None
    requirements = []
    battle_failed = schema_failed = formation_error = unexpected = False
    mission_failed = cleared = False
    evidence = []
    for event in events:
        msg, value = event['message'], event['details']
        if value.get('taskchain') != 'Copilot':
            continue
        key = (value.get('uuid'), value.get('taskid'))
        if msg == 10001:
            if active is not None or type(key[1]) is not int or key[1] != task_id or not isinstance(key[0], str) or not key[0]:
                return failure('unknown_execution_failure')
            active = key
        if active is None or key != active or type(key[1]) is not int:
            return failure('unknown_execution_failure')
        detail = value.get('details', {})
        subtask, what = value.get('subtask'), value.get('what')
        if msg == 3:
            finished = value.get('finished_tasks')
            if (not chain_failed or all_done or not isinstance(finished, list)
                    or any(type(t) is not int for t in finished) or task_id not in finished):
                return failure('unknown_execution_failure')
            all_done = True
            continue
        if msg == 10002 or chain_failed:
            return failure('unknown_execution_failure')
        if msg == 10000:
            chain_failed = True
        if msg == 20003 and what == 'CopilotListLoadTaskFileSuccess':
            if loaded or detail.get('stage_name') != stage or detail.get('file_name') != filename:
                return failure('unknown_execution_failure')
            loaded = True
        if msg == 20001 and subtask == 'BattleFormationTask':
            if not loaded or forming:
                return failure('unknown_execution_failure')
            forming = True
        if msg == 20002 and subtask == 'BattleFormationTask':
            if not forming or formed:
                return failure('unknown_execution_failure')
            formed = True
        if msg == 20001 and subtask == 'BattleProcessTask':
            if not formed or battling:
                return failure('unknown_execution_failure')
            battling = True
        if msg == 20003 and what == 'BattleFormationOperUnavailable' and subtask == 'BattleFormationTask' and forming and not formed:
            name, kind = detail.get('oper_name'), detail.get('requirement_type')
            if isinstance(name, str) and name and kind in ('elite', 'level', 'skill_level', 'module'):
                requirements.append({'oper_name': name, 'requirement_type': kind})
        if msg == 20003 and what == 'BattleFormationParseFailed' and subtask == 'BattleFormationTask' and forming and not formed:
            schema_failed = True
        if msg == 20000 and subtask == 'BattleFormationTask' and forming and not formed:
            formation_error = True
            if value.get('why') == 'OperatorMissing':
                groups = detail.get('opers')
                if isinstance(groups, dict) and groups and all(isinstance(v, list) and v for v in groups.values()):
                    rows = [r for group in groups.values() for r in group]
                    if all(isinstance(r, dict) and isinstance(r.get('name'), str) and r['name']
                           and r.get('reason') in ('Missing', 'Unavailable') for r in rows):
                        missing = rows
        if msg == 20002 and subtask == 'ProcessTask' and battling:
            task = detail.get('task', '').removeprefix('Copilot@') if isinstance(detail.get('task'), str) else ''
            result = detail.get('result', {})
            if task in {'StageDrops-Stars-2', 'StageDrops-Stars-3', 'StageDrops-Stars-Adverse'}:
                cleared = True
            if (value.get('first') == ['Copilot@WaitUntilEndOfAction']
                    and isinstance(result, dict)
                    and type(result.get('score')) in (float, int)
                    and math.isfinite(result['score']) and 0 < result['score'] <= 1
                    and ((task == 'FightMissionFailed' and detail.get('algorithm') == 'OcrDetect'
                          and detail.get('action') == 'ClickSelf' and result.get('text') == '任务失败')
                         or (task == 'StageDrops-Stars-2' and detail.get('algorithm') == 'MatchTemplate'
                             and result.get('template') == 'StageDrops-Stars-2.png'))):
                battle_failed = True
                mission_failed |= task == 'FightMissionFailed'
                evidence.append({'sequence': event['sequence'], 'task': task})
        if msg == 20000 and subtask == 'ProcessTask' and not battle_failed:
            unexpected = True
        if msg == 20000 and subtask not in {'BattleFormationTask', 'BattleProcessTask', 'ProcessTask'}:
            unexpected = True
    if not loaded or not chain_failed or not all_done or unexpected:
        return failure('unknown_execution_failure')
    if formed and battling and battle_failed:
        # Official CN has refunded failed/abandoned normal operations since
        # 2025-08-02. A two-star CLEAR still costs sanity. Never infer a refund
        # from generic worker/task failure or absence of a successful terminal.
        return failure('battle_failed', retryable=True, evidence=evidence,
                       sanity_outcome='refunded' if mission_failed and not cleared else 'charged_or_unknown')
    if forming and not formed and not battling:
        if schema_failed:
            return failure('copilot_schema_failure', retryable=True, sanity_outcome='not_spent')
        if missing is not None:
            if any(row['reason'] == 'Unavailable' for row in missing):
                # Inconclusive Unchecked rows or untyped requirements never
                # become permission to spend another battle's sanity.
                unavailable = {r['name'] for r in missing if r['reason'] == 'Unavailable'}
                if not unavailable <= {r['oper_name'] for r in requirements}:
                    return failure('formation_other_failure')
                return failure('formation_requirement_unsatisfied', retryable=True, evidence=requirements,
                               sanity_outcome='not_spent')
            return failure('formation_missing_operator', retryable=True, evidence=missing,
                           sanity_outcome='not_spent')
        if formation_error:
            return failure('formation_other_failure')
    if not forming:
        return failure('navigation_failure')
    return failure('unknown_execution_failure')
