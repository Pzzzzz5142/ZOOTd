"""Deterministic terminal-defence candidate search over reviewed combat profiles.

This is a bounded experimental generator, not an engine simulation. It owns all
roster, skill, position, facing and order choices. Research is non-tactical input;
unsupported enemy states and routes stop generation rather than gaining scores.
"""
from __future__ import annotations

import copy
import itertools
import math

from .battle_solver import (BattleSolverError, _device_tiles, _map, _mechanics,
                            _require, _shortest, compile_copilot)
from .combat_model import enemy_behavior, evaluate_plan, route_segments, skill_behavior
from .util import canonical_json, sha256_bytes
from .battle_search import SearchProblem, assess_formation, blocker_healing_need, _footprint
from .battle_strategies import SearchBudget, get_strategy, run_search

GENERATOR_VERSION = 7


def deployment_order(problem, selected):
    """Prioritize the first observed pressure on each unit's defended routes.

    Wave/fragment/action delays are ordered locally, never added into a global
    clock. Initially inactive reviving objects do not invent immediate pressure.
    This is an ordering heuristic, not a DP/arrival-time feasibility proof.
    """
    def first_pressure(item):
        p, cell, facing = item['profile'], item['point'], item['facing']
        times = []
        for s in problem.spawns:
            m, path = problem.mechanics[s['enemy_id']], problem.paths[s['route_index']]
            if m.get('initial_hp_ratio') is not None and m.get('revive_ratio') == 0:
                continue
            if item['role'] == 'blocker':
                covers = m.get('block_cost', 1) > 0 and cell in path['terminal'][-3:]
            else:
                covers = p['row']['behavior'].get('normal_attack', True) and bool(
                    _footprint(p, cell, facing) & set(path['terminal'][-4:]))
            if covers:
                times.append(tuple(s.get(k, math.inf) for k in ('wave', 'fragment', 'pre_delay')))
        return (min(times, default=(math.inf,) * 3), p['cost'], p['oper']['name'])

    dp = [x for x in selected if x['role'] == 'dp']
    healers = sorted((x for x in selected if x['role'] == 'healer'),
                     key=lambda x: (x['profile']['cost'], x['profile']['oper']['name']))
    combat = sorted((x for x in selected if x['role'] in ('blocker', 'damage')), key=first_pressure)
    order, first_medic_added = list(dp), False
    for item in combat:
        order.append(item)
        if item['role'] == 'damage' and healers and not first_medic_added:
            order.append(healers.pop(0))
            first_medic_added = True
    return order + healers


def active_spawns(graph):
    """Resolve only the observed normal/challenge hidden-group enable rune."""
    enabled = set()
    difficulty = graph['stage']['difficulty']
    _require(not graph.get('mechanics', {}).get('branches'), 'Conditional wave branches need an adapter')
    for rune in graph.get('mechanics', {}).get('runes') or []:
        if rune.get('key') != 'level_hidden_group_enable':
            continue
        mask = rune.get('difficultyMask')
        _require(mask in ('NORMAL', 'FOUR_STAR', 'ALL'), 'Unknown hidden-group difficulty mask')
        if mask in (difficulty, 'ALL'):
            keys = [b['valueStr'] for b in rune.get('blackboard', []) if b['key'] == 'key']
            _require(len(keys) == 1 and isinstance(keys[0], str), 'Unknown hidden-group enable rune')
            enabled.add(keys[0])
    result = []
    for spawn in graph['spawns']:
        raw = spawn.get('raw', {})
        _require(raw.get('randomSpawnGroupKey') is None
                 and raw.get('randomSpawnGroupPackKey') is None
                 and raw.get('randomType', 'ALWAYS') == 'ALWAYS',
                 'Random spawn groups are not supported by the generator')
        hidden = raw.get('hiddenGroup')
        if hidden is None or hidden in enabled:
            result.append(spawn)
    return result


def terminal_paths(graph, spawns):
    """Expand each visible segment independently, retaining its final approach."""
    _, _, tiles = _map(graph)
    active = {s['route_index'] for s in spawns}
    result = {}
    for route in graph['routes']:
        if route['index'] not in active:
            continue
        trace = route_segments(route)
        _require(not trace['unsupported'] and route['motion_mode'] == 'WALK',
                 'Generator cannot expand route ' + str(route['index']))
        segments = []
        for segment in trace['segments']:
            cells = [tuple(segment[0])]
            for start, end in zip(segment, segment[1:]):
                cells.extend(_shortest(tuple(start), tuple(end), tiles)[1:])
            segments.append(cells)
        _require(segments and len(segments[-1]) > 1, 'No visible final route approach')
        result[route['index']] = {'cells': set(itertools.chain.from_iterable(segments)),
                                  'terminal': segments[-1][:-1], 'goal': tuple(route['end'])}
    _require(active == set(result), 'Active spawn references a missing route')
    return result


def _profiles(graph, box, catalog, chars, skills, ranges, equipment, metadata, horizon, effect_rules=None):
    """Reuse the combat evaluator's training, module, talent and packet rules."""
    _, _, tiles = _map(graph)
    devices = _device_tiles(graph, tiles)
    result, excluded = [], []
    for oid, progress in sorted(box.operators.items()):
        char = chars.get(oid)
        if char is None or catalog.resolve(char['name']) is None:
            continue
        if None in (progress.elite, progress.level, progress.main_skill_level):
            continue
        for slot, spec in enumerate(char['skills'], 1):
            sid = spec['skillId']
            training = (progress.skills or {}).get(sid)
            if training is None or training.mastery is None or sid not in skills:
                continue
            unlock = spec['unlockCond']
            elite = {'PHASE_0': 0, 'PHASE_1': 1, 'PHASE_2': 2}.get(unlock['phase'])
            if elite is None or progress.elite < elite or (
                    progress.elite == elite and progress.level < unlock['level']):
                continue
            level = progress.main_skill_level + training.mastery
            if not 1 <= level <= len(skills[sid]['levels']):
                continue
            if skill_behavior(sid, skills[sid]['levels'][level - 1], char) is None:
                continue
            legal = [p for p, t in sorted(tiles.items()) if p not in devices
                     and t['buildable'] in (char['position'], 'ALL')]
            if not legal:
                continue
            position = legal[0]
            requirements = {'elite': progress.elite, 'level': progress.level, 'skill_level': level}
            identity = catalog.resolve(char['name'])
            module_slot = next((n for n, mid in identity.modules.items()
                                if mid == progress.default_equip_id), None)
            selected_metadata = (metadata or {}).get('equipDict', {}).get(progress.default_equip_id, {})
            if progress.default_equip_id and selected_metadata.get('charEquipOrder') != 0 and module_slot is None:
                excluded.append({'name': char['name'], 'skill': slot, 'reason': 'Selected module has no verified native mapping'})
                continue
            if module_slot:
                module = (progress.modules or {}).get(progress.default_equip_id)
                if module is None or module.level is None or module.unlocked is not True:
                    continue
                requirements.update(module=module_slot, module_level=module.level)
            behavior = skill_behavior(sid, skills[sid]['levels'][level - 1], char)
            usage = 0 if behavior.get('passive') else 2 if behavior['mode'] == 'permanent' else 1
            oper = {'name': char['name'], 'skill': slot, 'skill_usage': usage,
                    'requirements': requirements}
            content = {'opers': [oper], 'actions': [dict(type='Deploy', name=char['name'],
                        location=list(position), direction='Right')]}
            try:
                row = evaluate_plan(graph, content, box, catalog, chars, skills, ranges,
                    horizon=horizon, equipment=equipment, equip_metadata=metadata, effect_rules=effect_rules)['operators'][0]
            except BattleSolverError as exc:
                excluded.append({'name': char['name'], 'skill': slot, 'reason': str(exc)})
                continue
            result.append({'oper': oper, 'row': row, 'position_type': char['position'],
                'cost': row['attributes']['cost'][1],
                'normal_offsets': [[x-position[0], y-position[1]] for x, y in row['range_tiles']],
                'skill_offsets': [[x-position[0], y-position[1]] for x, y in row['skill_range_tiles']]})
    return result, excluded


def prepare_problem(graph, box, catalog, chars, skills, ranges, *, research,
                    equipment=None, equip_metadata=None, horizon=120, effect_rules=None):
    _require(graph['stage']['difficulty'] == 'NORMAL', 'Generator currently supports normal stages only')
    _require(isinstance(research, dict) and research.get('schema') == 1
             and research.get('graph_sha256') == sha256_bytes(canonical_json(graph)),
             'Research must bind the exact graph')
    notes = research.get('mechanics')
    _require(isinstance(notes, dict) and all(isinstance(v, str) and v.strip() for v in notes.values()),
             'Each researched mechanic needs a nonempty factual note')
    _mechanics(graph, list(notes))
    _require(not graph.get('mechanics', {}).get('tilesDisallowToLocate'),
             'Additional forbidden-tile constraints need an adapter')
    for rune in graph.get('mechanics', {}).get('runes') or []:
        _require(rune.get('difficultyMask') in ('NORMAL', 'FOUR_STAR', 'ALL'),
                 'Unknown rune difficulty mask')
        if rune.get('difficultyMask') in ('NORMAL', 'ALL'):
            _require(rune.get('key') in ('env_system_new', 'level_hidden_group_enable'),
                     'Unmodeled active level rune: ' + str(rune.get('key')))
            if rune.get('key') == 'env_system_new':
                _require(rune.get('blackboard') == [dict(key='key', value=0.0,
                         valueStr='env_044_act52side')], 'Unmodeled active environment')
    _require(isinstance(research.get('sources'), list) and research['sources'], 'Research sources missing')
    _require(type(horizon) in (int, float) and 1 <= horizon <= 600, 'Invalid generator horizon')
    capacity = graph['options']['characterLimit']
    _require(type(capacity) is int and capacity >= 3, 'Generator needs at least three deployment slots')
    spawns = active_spawns(graph)
    paths = terminal_paths(graph, spawns)
    mechanics = {eid: enemy_behavior(enemy, graph) for eid, enemy in graph['enemies'].items()}
    unsupported = sorted({s['enemy_id'] for s in spawns if mechanics[s['enemy_id']]['kind'] == 'unsupported'})
    _require(not unsupported, 'Generator needs enemy adapters: ' + ', '.join(unsupported))
    profiles, excluded = _profiles(graph, box, catalog, chars, skills, ranges,
                                   equipment, equip_metadata, horizon, effect_rules)
    _, _, tiles = _map(graph)
    devices = _device_tiles(graph, tiles)
    problem = SearchProblem(profiles, tiles, devices, paths, spawns, mechanics, capacity, horizon,
        enemy_attributes={eid: e['attributes'] for eid, e in graph['enemies'].items()},
        move_multiplier=graph['options'].get('moveMultiplier'))
    return problem, excluded


def materialize_plan(problem, result, graph, box, catalog, chars, skills, ranges, *, research,
                     excluded, equipment=None, equip_metadata=None, effect_rules=None):
    """All strategies pass the same constraints, compiler and combat evaluator."""
    metrics, healing_requirements = assess_formation(problem, result.formation)
    selected = [{'profile': problem.profiles[p.profile_index], 'point': p.point,
                 'facing': p.facing, 'role': p.role} for p in result.formation.placements]
    horizon, spawns, notes = problem.horizon, problem.spawns, research['mechanics']
    deployment = deployment_order(problem, selected)
    actions, deployed, retired = [], 0, False
    for x in deployment:
        if deployed == problem.capacity:
            _require(result.formation.retire_dp and not retired, 'No opening DP slot to release')
            dp = next(y for y in deployment if y['role'] == 'dp')
            actions.append(dict(type='Retreat', name=dp['profile']['oper']['name']))
            deployed -= 1
            retired = True
        actions.append(dict(type='Deploy', name=x['profile']['oper']['name'],
                            location=list(x['point']), direction=x['facing']))
        deployed += 1
    plan = {'schema_version': 1, 'stage_id': graph['stage']['stage_id'],
            'title': graph['stage']['code'] + ' generated terminal defence',
            'mechanics_acknowledged': sorted(notes),
            'opers': [copy.deepcopy(x['profile']['oper']) for x in deployment],
            'actions': actions + [{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}]}
    compiled = compile_copilot(graph, plan, box, catalog, character_table=chars)
    combat = evaluate_plan(graph, compiled['copilot'], box, catalog, chars, skills, ranges,
                          horizon=horizon, equipment=equipment, equip_metadata=equip_metadata, effect_rules=effect_rules)
    coverage = {'graph_unknowns': graph['mechanics'].get('unknown', []),
        'enemy_unknowns': {eid: m['unknown'] for eid, m in problem.mechanics.items() if m['unknown']},
        'operator_unknowns': {row['name']: row.get('unknown', []) for row in combat.get('operators', [])
                              if row.get('unknown')}}
    return {'plan': plan, 'copilot': compiled['copilot'], 'combat': combat,
        'generation': {'schema': 1, 'generator_version': GENERATOR_VERSION,
            'method': get_strategy(result.strategy).method, 'proof_status': 'unproven',
            'strategy': {'id': result.strategy, 'version': get_strategy(result.strategy).version},
            'search': result.diagnostics, 'metrics': metrics, 'coverage': coverage,
            'model_version': combat.get('model_version'),
            'graph_sha256': sha256_bytes(canonical_json(graph)),
            'research_sha256': sha256_bytes(canonical_json(research)),
            'plan_sha256': sha256_bytes(canonical_json(plan)),
            'copilot_sha256': sha256_bytes(canonical_json(compiled['copilot'])),
            'horizon_seconds': horizon,
            'active_spawn_count': sum(s['count'] for s in spawns), 'excluded_profiles': excluded,
            'opening_dp_retired': retired,
            'blocker_healing_requirements': healing_requirements,
            'choices': [{'name': x['profile']['oper']['name'], 'role': x['role'],
                         'cost': x['profile']['cost'], 'location': list(x['point']),
                         'direction': x['facing']} for x in deployment],
            'limitations': ['Bounded terminal formation search, not a full-map survival or target-selection simulator.',
                'Fields, summons, conditional control and shared DPS retain the combat report unknowns.',
                'Deployment waits for actual DP; ordering is heuristic, not an arrival-time guarantee.',
                'Three fresh identical practice three-stars are required before live execution.']}}


def generate_plan(graph, box, catalog, chars, skills, ranges, *, research,
                  equipment=None, equip_metadata=None, horizon=120, strategy='greedy',
                  max_candidates=8, branch_width=3, effect_rules=None):
    """Backward-compatible single-strategy entry point; default remains greedy."""
    budget = SearchBudget(max_candidates, branch_width)
    get_strategy(strategy)
    problem, excluded = prepare_problem(graph, box, catalog, chars, skills, ranges,
        research=research, equipment=equipment, equip_metadata=equip_metadata, horizon=horizon, effect_rules=effect_rules)
    result = run_search(problem, strategy, budget)
    return materialize_plan(problem, result, graph, box, catalog, chars, skills, ranges,
        research=research, excluded=excluded, equipment=equipment, equip_metadata=equip_metadata, effect_rules=effect_rules)
