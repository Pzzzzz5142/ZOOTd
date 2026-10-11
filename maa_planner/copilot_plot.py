"""Native plot helpers are auxiliary, scoped to one bound battle lifecycle."""
from __future__ import annotations

import math


SKIP = 'SkipThePreBattlePlot'
CONFIRM = SKIP + 'Confirm'
SETTLEMENT = 'Copilot@WaitUntilEndOfAction'


class PlotLifecycle:
    """Recognize a finite task grammar, not a fixed number of story scenes.

    Callers supply the fresh execution identity and battle phase. Standalone
    BattleHelper calls end at Confirm, or at an empty native error after a
    matched Skip. The settlement process loops through Wait; its errors are
    never optional. Neither lifecycle establishes battle success or a refund.
    """

    def __init__(self):
        self.previous = {SKIP: None, SETTLEMENT: None}
        self.pending = None
        self.error_previous = None

    @property
    def complete(self):
        return self.pending is None and all(v is None for v in self.previous.values())

    def observe(self, message, value, *, during_battle, after_battle):
        """Return unrelated/accepted/ignored_error/invalid for one callback."""
        detail = value.get('details', {})
        raw_task = detail.get('task', '')
        task = raw_task.removeprefix('Copilot@') if isinstance(raw_task, str) else ''
        first = value.get('first')
        standalone = first == [SKIP]
        if task not in (SKIP, CONFIRM) and not standalone:
            return 'unrelated'
        root = SKIP if standalone else SETTLEMENT
        if (not (during_battle if standalone else after_battle)
                or first != [root] or value.get('subtask') != 'ProcessTask'
                or value.get('class') != 'asst::ProcessTask'
                or value.get('why') or value.get('what')):
            return 'invalid'
        previous = self.previous[root]
        if message == 20000:
            # BattleHelper deliberately ignores this ProcessTask return value.
            # Confirm normally ends that helper; an error must belong to a
            # currently open Skip attempt, not a prior successful invocation.
            if (not standalone or previous != SKIP or self.pending is not None
                    or detail != {} or value.get('pre_task') != self.error_previous):
                return 'invalid'
            self.previous[root] = None
            self.error_previous = None
            return 'ignored_error'
        if message not in (20001, 20002) or task not in (SKIP, CONFIRM):
            return 'invalid'
        prefix = '' if standalone else 'Copilot@'
        expected_previous = (prefix + SKIP if previous == SKIP
                             else '' if standalone else SETTLEMENT)
        match = detail.get('result', {})
        if (value.get('pre_task') != expected_previous
                or (task == CONFIRM and previous != SKIP)
                or detail.get('action') != 'ClickSelf' or detail.get('algorithm') != 'MatchTemplate'
                or not isinstance(match, dict) or match.get('template') != task + '.png'
                or type(match.get('score')) not in (int, float)
                or not math.isfinite(match['score']) or not 0 < match['score'] <= 1):
            return 'invalid'
        # Start and completion must describe the same native click. Ignore
        # unrelated added fields rather than binding to today's full payload.
        click = (root, task, expected_previous, match['template'], match['score'])
        if message == 20001:
            if self.pending is not None:
                return 'invalid'
            self.pending = click
        else:
            if self.pending != click:
                return 'invalid'
            self.pending = None
            self.previous[root] = SKIP if task == SKIP else None
            if standalone:
                self.error_previous = expected_previous if task == SKIP else None
        return 'accepted'
