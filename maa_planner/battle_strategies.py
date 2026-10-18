"""Small reviewed strategy table; deterministic search has no I/O or execution.

New algorithms implement solve(SearchProblem, SearchBudget) -> SearchResult.
They consume the same model and return proposals to the common admission path.
"""
from __future__ import annotations

import copy
import heapq
from dataclasses import dataclass
from typing import Protocol

from .battle_search import Formation, SearchProblem, assess_formation, objective, terminal_formation
from .battle_solver import BattleSolverError, _require


@dataclass(frozen=True)
class SearchBudget:
    max_candidates: int = 8
    branch_width: int = 3

    def __post_init__(self):
        _require(type(self.max_candidates) is int and 1 <= self.max_candidates <= 64,
                 'max_candidates must be an integer in 1..64')
        _require(type(self.branch_width) is int and 2 <= self.branch_width <= 8,
                 'branch_width must be an integer in 2..8')


@dataclass(frozen=True)
class SearchResult:
    strategy: str
    formation: Formation
    diagnostics: dict


class Strategy(Protocol):
    id: str
    version: int
    method: str
    description: str

    def solve(self, problem: SearchProblem, budget: SearchBudget) -> SearchResult: ...


class Decisions:
    """Replay ranked choices, recording branches even if a later constraint fails."""
    def __init__(self, ranks=(), branch_width=3):
        self.ranks, self.branch_width = ranks, branch_width
        self.visited = []

    def pick(self, ranked):
        count = min(len(ranked), self.branch_width)
        index = len(self.visited)
        rank = self.ranks[index] if index < len(self.ranks) else 0
        _require(0 <= rank < count, 'Search branch no longer exists')
        self.visited.append((rank, count))
        return ranked[rank]


class GreedyStrategy:
    id, version = 'greedy', 1
    method = 'terminal-defence-greedy-search'
    description = 'Original terminal-defence greedy baseline; one formation attempt.'

    def solve(self, problem, budget):
        formation = terminal_formation(problem, Decisions(branch_width=budget.branch_width))
        assess_formation(problem, formation)
        return SearchResult(self.id, formation, {'attempted': 1, 'feasible': 1,
            'max_candidates': budget.max_candidates, 'branch_width': budget.branch_width,
            'stop_reason': 'greedy_complete'})


class BacktrackingStrategy:
    id, version = 'backtracking', 1
    method = 'terminal-defence-bounded-backtracking'
    description = 'Revisit ranked placement/network choices within a deterministic attempt budget.'

    def solve(self, problem, budget):
        pending, seen = [(0, ())], {()}
        best, best_value, attempted, feasible, failures = None, None, 0, 0, []
        while pending and attempted < budget.max_candidates:
            _, ranks = heapq.heappop(pending)
            decisions = Decisions(ranks, budget.branch_width)
            attempted += 1
            try:
                formation = terminal_formation(problem, decisions)
                metrics, _ = assess_formation(problem, formation)
                feasible += 1
                value = objective(metrics)
                if best_value is None or value > best_value:
                    best, best_value = formation, value
            except BattleSolverError as exc:
                failures.append(str(exc))
            # Prefer fewer deviations from the greedy path. Branches are built
            # from the actual visited prefix, so failed partial plans can recover.
            prefix = []
            for rank, count in decisions.visited:
                if rank + 1 < count:
                    child = tuple(prefix + [rank + 1])
                    if child not in seen:
                        seen.add(child)
                        heapq.heappush(pending, (sum(child), child))
                prefix.append(rank)
        _require(best is not None, 'No feasible formation within backtracking budget: '
                 + (failures[0] if failures else 'no candidates'))
        return SearchResult(self.id, best, {'attempted': attempted, 'feasible': feasible,
            'max_candidates': budget.max_candidates, 'branch_width': budget.branch_width,
            'stop_reason': 'candidate_budget' if pending else 'search_exhausted',
            'failed_attempts': failures})


STRATEGIES: dict[str, Strategy] = {s.id: s for s in (GreedyStrategy(), BacktrackingStrategy())}


def get_strategy(name):
    _require(name in STRATEGIES, 'Unknown solver strategy: ' + str(name))
    return STRATEGIES[name]


def strategy_catalog():
    return [{'id': s.id, 'version': s.version, 'description': s.description}
            for s in STRATEGIES.values()]


def run_search(problem, strategy, budget):
    # Protect shared prepared inputs in sequential runs as well as process runs.
    return get_strategy(strategy).solve(copy.deepcopy(problem), budget)
