"""Run independent offline strategies against one prepared model snapshot."""
from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor

from .battle_generator import GENERATOR_VERSION, materialize_plan, prepare_problem
from .battle_search import objective
from .battle_solver import BattleSolverError, _require
from .battle_strategies import SearchBudget, get_strategy, run_search
from .combat_model import MODEL_VERSION
from .util import canonical_json, sha256_bytes


def _attempt(problem, strategy, budget):
    started = time.monotonic()
    try:
        result = run_search(problem, strategy, budget)
        return {'status': 'candidate', 'result': result,
                'search_seconds': time.monotonic() - started}
    except Exception as exc:
        return {'status': 'failed', 'category': ('infeasible' if isinstance(exc, BattleSolverError)
                                                else 'strategy_error'),
                'message': str(exc), 'search_seconds': time.monotonic() - started}


def compare_plans(graph, box, catalog, chars, skills, ranges, *, research,
                  strategies=('greedy', 'backtracking'), workers=1,
                  max_candidates=8, branch_width=3, equipment=None, equip_metadata=None, horizon=120, effect_rules=None):
    _require(isinstance(strategies, (list, tuple)) and strategies
             and len(set(strategies)) == len(strategies), 'Strategies must be nonempty and distinct')
    for name in strategies:
        get_strategy(name)
    _require(type(workers) is int and 1 <= workers <= 8, 'workers must be an integer in 1..8')
    budget = SearchBudget(max_candidates, branch_width)
    started = time.monotonic()
    problem, excluded = prepare_problem(graph, box, catalog, chars, skills, ranges,
        research=research, equipment=equipment, equip_metadata=equip_metadata, horizon=horizon, effect_rules=effect_rules)
    preparation_seconds = time.monotonic() - started
    count = min(workers, len(strategies))
    if count == 1:
        outcomes = [_attempt(problem, name, budget) for name in strategies]
    else:
        # Explicit spawn avoids inherited device handles, locks and interpreter
        # state. Only the compact derived search problem is sent to workers.
        with ProcessPoolExecutor(max_workers=count, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = [pool.submit(_attempt, problem, name, budget) for name in strategies]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    outcomes.append({'status': 'failed', 'category': 'worker_error',
                                     'message': str(exc), 'search_seconds': None})
    candidates, rows, fingerprints = {}, [], {}
    for name, outcome in zip(strategies, outcomes):
        strategy = get_strategy(name)
        row = {'strategy': name, 'version': strategy.version,
               **{k: v for k, v in outcome.items() if k != 'result'}}
        if outcome['status'] == 'candidate':
            evaluation_started = time.monotonic()
            try:
                generated = materialize_plan(problem, outcome['result'], graph, box, catalog,
                    chars, skills, ranges, research=research, excluded=excluded,
                    equipment=equipment, equip_metadata=equip_metadata, effect_rules=effect_rules)
                candidates[name] = generated
                evidence = generated['generation']
                fingerprint = evidence['copilot_sha256']
                row.update(metrics=evidence['metrics'], search=evidence['search'],
                           coverage=evidence['coverage'], copilot_sha256=fingerprint,
                           duplicate_of=fingerprints.get(fingerprint))
                fingerprints.setdefault(fingerprint, name)
            except Exception as exc:
                row.update(status='failed', category='validation_error', message=str(exc))
            row['evaluation_seconds'] = time.monotonic() - evaluation_started
        rows.append(row)
    ranking = sorted((row for row in rows if row['status'] == 'candidate'),
                     key=lambda r: (objective(r['metrics']), r['strategy']), reverse=True)
    return {'candidates': candidates, 'comparison': {
        'schema': 1, 'status': 'candidates' if candidates else 'failed', 'proof_status': 'unproven',
        'generator_version': GENERATOR_VERSION, 'model_version': MODEL_VERSION,
        'graph_sha256': sha256_bytes(canonical_json(graph)),
        'research_sha256': sha256_bytes(canonical_json(research)), 'horizon_seconds': horizon,
        'budget': {'max_candidates': max_candidates, 'branch_width': branch_width},
        'workers': count, 'preparation_seconds': preparation_seconds,
        'wall_seconds': time.monotonic() - started, 'results': rows,
        'ranking': [r['strategy'] for r in ranking],
        'limitations': ['Ranking uses the common terminal-contact heuristic, not clear probability.',
                        'Search time excludes shared preparation and parent-process combat evaluation.',
                        'Unmodeled mechanics remain unknown in every strategy; candidates require fresh practice proof.']}}
