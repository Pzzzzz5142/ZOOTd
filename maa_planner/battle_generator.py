"""Deterministic terminal-defence candidate search over reviewed combat profiles.

This is a bounded experimental generator, not an engine simulation. It owns all
roster, skill, position, facing and order choices. Research is non-tactical input;
unsupported enemy states and routes stop generation rather than gaining scores.
"""
from __future__ import annotations

import copy
import itertools

from .battle_solver import (BattleSolverError, _device_tiles, _map, _mechanics,
                            _require, _rotate, _shortest, compile_copilot)
from .combat_model import enemy_behavior, evaluate_plan, route_segments, skill_behavior
from .util import canonical_json, sha256_bytes

GENERATOR_VERSION = 2


def blocker_healing_need(profile, point, paths, spawns):
    """Do not equate nonzero regeneration with surviving modeled pressure.

    Use only the evaluator's reviewed incoming-damage scenarios and routes the
    unit actually intercepts. Other attacks, control and shared healing remain
    unknown; absence of this particular shortfall is not a survival proof.
    """
    enemies = {s['enemy_id'] for s in spawns if tuple(point) in paths[s['route_index']]['terminal']}
    pressure = max((m.get('max_stacks_incoming_physical_dps', 0)
                    for m in profile['row']['matchups'] if m['enemy_id'] in enemies), default=0)
    recovery = profile['row']['self_heal_per_second_skill']
    return {'required': recovery <= 0 or pressure > recovery,
            'self_heal_per_second_skill': recovery, 'modeled_peak_physical_dps': pressure}


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


def _footprint(profile, point, facing, *, skill=False):
    offsets = profile['skill_offsets' if skill else 'normal_offsets']
    return {(point[0] + dx, point[1] + dy) for dx, dy in
            (_rotate(offset, facing) for offset in offsets)}


def _profiles(graph, box, catalog, chars, skills, ranges, equipment, metadata, horizon):
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
                    horizon=horizon, equipment=equipment, equip_metadata=metadata)['operators'][0]
            except BattleSolverError as exc:
                excluded.append({'name': char['name'], 'skill': slot, 'reason': str(exc)})
                continue
            result.append({'oper': oper, 'row': row, 'position_type': char['position'],
                'cost': row['attributes']['cost'][1],
                'normal_offsets': [[x-position[0], y-position[1]] for x, y in row['range_tiles']],
                'skill_offsets': [[x-position[0], y-position[1]] for x, y in row['skill_range_tiles']]})
    return result, excluded


def generate_plan(graph, box, catalog, chars, skills, ranges, *, research,
                  equipment=None, equip_metadata=None, horizon=120):
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
    spawns = active_spawns(graph)
    paths = terminal_paths(graph, spawns)
    mechanics = {eid: enemy_behavior(enemy, graph) for eid, enemy in graph['enemies'].items()}
    unsupported = sorted({s['enemy_id'] for s in spawns if mechanics[s['enemy_id']]['kind'] == 'unsupported'})
    _require(not unsupported, 'Generator needs enemy adapters: ' + ', '.join(unsupported))
    profiles, excluded = _profiles(graph, box, catalog, chars, skills, ranges,
                                   equipment, equip_metadata, horizon)
    _, _, tiles = _map(graph)
    devices = _device_tiles(graph, tiles)
    selected, occupied, used = [], set(devices), set()

    def available(profile):
        return profile['oper']['name'] not in used

    def choose(profile, point, facing, role):
        selected.append({'profile': profile, 'point': point, 'facing': facing, 'role': role})
        occupied.add(point)
        used.add(profile['oper']['name'])

    def candidates(pool):
        for profile in pool:
            if not available(profile):
                continue
            for point, tile in sorted(tiles.items()):
                if point in occupied or tile['buildable'] not in (profile['position_type'], 'ALL'):
                    continue
                for facing in ('Right', 'Down', 'Left', 'Up'):
                    yield profile, point, facing

    def best(pool, score):
        ranked = [(score(p, cell, direction), p, cell, direction)
                  for p, cell, direction in candidates(pool)]
        _require(bool(ranked), 'No legal candidate for a required role')
        value, profile, cell, direction = max(ranked, key=lambda r: (r[0],
            -r[1]['cost'], r[1]['oper']['name'], -r[2][1], -r[2][0], r[3]))
        _require(value > 0, 'Required role has no useful coverage')
        return profile, cell, direction

    def damage_rate(profile, eid):
        m = next((m for m in profile['row']['matchups'] if m['enemy_id'] == eid), None)
        if m is None:
            return 0  # A healing skill is useful for blocking, not damage.
        duel = m['duel']
        # Effective HP is observed damage capped at HP; add an attack interval so
        # an instant first hit cannot acquire infinite throughput.
        finite = duel['damage'] / ((duel['kill_time'] if duel['kill_time'] is not None else horizon)
                                  + profile['row']['attack_interval_normal'])
        return .5 * finite + .5 * m['rotation']['average_dps']

    # Cover all blockable final approaches, rather than placing one defender per
    # spawn/route ID. A single choke can service many routes to the same goal.
    needed = {s['route_index'] for s in spawns if mechanics[s['enemy_id']].get('block_cost', 1) > 0}
    blockers = [p for p in profiles if p['position_type'] == 'MELEE'
                and p['row']['block_count_normal'] >= 1
                and p['row']['block_count_skill'] >= 2
                and p['row']['behavior'].get('ground_block', True)
                and p['row']['behavior']['mode'] != 'cost']
    weights = {r: sum(s['count'] for s in spawns if s['route_index'] == r) for r in paths}
    capacity = graph['options']['characterLimit']
    _require(type(capacity) is int and capacity >= 3, 'Generator needs at least three deployment slots')
    while needed:
        def block_score(p, cell, direction):
            intercepted = {r for r in needed if cell in paths[r]['terminal'][-3:]}
            if not intercepted:
                return 0
            row = p['row']
            survival = (row['attributes']['maxHp'][0] / 2000 + row['attributes']['def'][0] / 500
                        + row['self_heal_per_second_skill'] / 150)
            cover = _footprint(p, cell, direction, skill=True)
            damage = sum(damage_rate(p, s['enemy_id']) * min(3, len(cover & paths[s['route_index']]['cells']))
                         for s in spawns if s['route_index'] in intercepted)
            return sum(weights[r] for r in intercepted) * (10 + survival) + damage / 500 - p['cost'] / 10
        profile, cell, direction = best(blockers, block_score)
        choose(profile, cell, direction, 'blocker')
        needed -= {r for r in needed if cell in paths[r]['terminal'][-3:]}
        _require(len(selected) <= capacity - 2, 'Too many independent approaches for the supported formation')

    healer_pool = [p for p in profiles if p['position_type'] == 'RANGED'
                   and p['row']['behavior']['damage_type'] == 'healing']
    ranged = [p for p in profiles if p['position_type'] == 'RANGED'
              and p['row']['behavior']['damage_type'] not in ('healing',)
              and p['row']['behavior']['mode'] != 'cost']
    # Melee units that cannot block ground enemies still deal damage from melee
    # tiles; keep them out of the blocker role while allowing damage placement.
    melee_damage = [p for p in profiles if p['position_type'] == 'MELEE'
                    and not p['row']['behavior'].get('ground_block', True)
                    and p['row']['behavior']['damage_type'] not in ('healing',)
                    and p['row']['behavior']['mode'] != 'cost']
    damage_pool = ranged + melee_damage
    dp_pool = [p for p in profiles if p['row']['behavior']['mode'] == 'cost']
    remaining = capacity - len(selected) - bool(dp_pool)
    healing_slots = 2 if remaining >= 4 else 1
    damage_slots = min(3, remaining - healing_slots)
    _require(damage_slots >= 1, 'No deployment slot remains for normal damage')

    def fire_score(p, cell, direction, *, marginal=True):
        normal, active = _footprint(p, cell, direction), _footprint(p, cell, direction, skill=True)
        basic = bool(p['row']['behavior'].get('normal_attack', True))
        result = 0
        for spawn in spawns:
            route = paths[spawn['route_index']]
            terminal = set(route['terminal'][-4:])
            existing = sum(bool(_footprint(x['profile'], x['point'], x['facing']) & terminal)
                           for x in selected if x['role'] == 'damage') if marginal else 0
            # Skill downtime cannot replace basic coverage at the blue gate.
            contact = (len(normal & terminal) if basic else 0) + .35 * len(active & terminal)
            result += spawn['count'] * damage_rate(p, spawn['enemy_id']) * contact / (1 + existing)
        return result

    # Select a small healing network before committing ranged placements. The
    # old independent greedy choices could scatter DPS across an unhealable map.
    # Search all one/two-medic networks and score their available damage cells.
    damage_cells = {}
    for p, cell, direction in candidates(damage_pool):
        damage_cells[cell] = max(damage_cells.get(cell, 0), fire_score(p, cell, direction, marginal=False))
    heal_candidates = [(p, cell, direction, _footprint(p, cell, direction))
                       for p, cell, direction in candidates(healer_pool)]
    healing_requirements = [{**blocker_healing_need(x['profile'], x['point'], paths, spawns),
                             'name': x['profile']['oper']['name'], 'location': list(x['point'])}
                            for x in selected if x['role'] == 'blocker']
    must_heal = {tuple(x['location']) for x in healing_requirements if x['required']}
    networks = itertools.combinations(heal_candidates, healing_slots)
    best_network, best_score = None, -1
    for network in networks:
        names = {h[0]['oper']['name'] for h in network}
        cells = {h[1] for h in network}
        if len(names) != healing_slots or len(cells) != healing_slots:
            continue
        coverage = set().union(*(h[3] for h in network))
        if not must_heal <= coverage:
            continue
        useful = sorted((v for cell, v in damage_cells.items() if cell in coverage - cells), reverse=True)
        if len(useful) < damage_slots:
            continue
        value = sum(useful[:damage_slots]) + sum(h[0]['row']['healing_per_second_skill_per_target']
                    * h[0]['row']['healing_target_limit_skill'] for h in network)
        if value > best_score:
            best_network, best_score = network, value
    _require(best_network is not None, 'No normal healing coverage network for blockers and ranged damage')
    for p, cell, direction, _ in best_network:
        choose(p, cell, direction, 'healer')
    healing = set().union(*(_footprint(x['profile'], x['point'], x['facing'])
                           for x in selected if x['role'] == 'healer'))

    # Extra damage uses marginal contact on the least-covered final approaches.
    for _ in range(damage_slots):
        profile, cell, direction = best(damage_pool, lambda p, cell, direction:
                                        fire_score(p, cell, direction) if cell in healing else 0)
        choose(profile, cell, direction, 'damage')
    if dp_pool and len(selected) < capacity:
        profile, cell, direction = best(dp_pool, lambda p, cell, direction:
            1000 - sum(weights[r] for r in paths if cell in paths[r]['terminal']) - p['cost'])
        choose(profile, cell, direction, 'dp')

    # Require normal damage coverage at every final approach of unblockable
    # enemies. A blocker or an expanded skill range cannot satisfy this.
    missing = []
    for s in spawns:
        if mechanics[s['enemy_id']].get('block_cost', 1) != 0:
            continue
        route = paths[s['route_index']]
        if not any(_footprint(x['profile'], x['point'], x['facing']) & set(route['terminal'][-2:])
                   for x in selected if x['role'] == 'damage'
                   and x['profile']['row']['behavior'].get('normal_attack', True)):
            missing.append(s['route_index'])
    _require(not missing, 'Unblockable final approaches lack normal DPS: ' + str(sorted(set(missing))))

    # Interleave frontline, immediate ranged pressure and healing. Permanent
    # warmup and SP initial charge remain in the report; actions never wait for
    # invented absolute wave times. MAA waits for actual DP for each deployment.
    order = {'dp': 0, 'blocker': 1, 'damage': 2, 'healer': 3}
    selected.sort(key=lambda x: (order[x['role']], x['profile']['cost'], x['profile']['oper']['name']))
    front = [x for x in selected if x['role'] == 'blocker']
    other = [x for x in selected if x['role'] == 'damage']
    healers = [x for x in selected if x['role'] == 'healer']
    deployment = [x for x in selected if x['role'] == 'dp'] + front + other[:1] + healers[:1] + other[1:] + healers[1:]
    plan = {'schema_version': 1, 'stage_id': graph['stage']['stage_id'],
            'title': graph['stage']['code'] + ' generated terminal defence',
            'mechanics_acknowledged': sorted(notes),
            'opers': [copy.deepcopy(x['profile']['oper']) for x in deployment],
            'actions': [dict(type='Deploy', name=x['profile']['oper']['name'],
                             location=list(x['point']), direction=x['facing']) for x in deployment]
                       + [{'type': 'SpeedUp'}, {'type': 'SkillDaemon'}]}
    compiled = compile_copilot(graph, plan, box, catalog, character_table=chars)
    combat = evaluate_plan(graph, compiled['copilot'], box, catalog, chars, skills, ranges,
                          horizon=horizon, equipment=equipment, equip_metadata=equip_metadata)
    return {'plan': plan, 'copilot': compiled['copilot'], 'combat': combat,
        'generation': {'schema': 1, 'generator_version': GENERATOR_VERSION,
            'method': 'terminal-defence-greedy-search', 'proof_status': 'unproven',
            'graph_sha256': sha256_bytes(canonical_json(graph)),
            'research_sha256': sha256_bytes(canonical_json(research)),
            'plan_sha256': sha256_bytes(canonical_json(plan)),
            'copilot_sha256': sha256_bytes(canonical_json(compiled['copilot'])),
            'horizon_seconds': horizon,
            'active_spawn_count': sum(s['count'] for s in spawns), 'excluded_profiles': excluded,
            'blocker_healing_requirements': healing_requirements,
            'choices': [{'name': x['profile']['oper']['name'], 'role': x['role'],
                         'cost': x['profile']['cost'], 'location': list(x['point']),
                         'direction': x['facing']} for x in deployment],
            'limitations': ['Greedy candidate generation, not a full-map survival or target-selection simulator.',
                'Fields, summons, conditional control and shared DPS retain the combat report unknowns.',
                'Deployment waits for actual DP; ordering is heuristic, not an arrival-time guarantee.',
                'Three fresh identical practice three-stars are required before live execution.']}}
