"""Shared terminal-formation constraints and strategy-independent assessment.

Search inputs contain reviewed, derived combat profiles, never account metadata
or device handles. Unknown mechanisms remain attached to the prepared problem.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from .battle_solver import _require, _rotate


@dataclass(frozen=True)
class SearchProblem:
    profiles: list
    tiles: dict
    devices: set
    paths: dict
    spawns: list
    mechanics: dict
    capacity: int
    horizon: float
    enemy_attributes: dict = field(default_factory=dict)
    move_multiplier: float | None = None


@dataclass(frozen=True)
class Placement:
    profile_index: int
    point: tuple[int, int]
    facing: str
    role: str


@dataclass(frozen=True)
class Formation:
    placements: tuple[Placement, ...]
    retire_dp: bool = False


def required_normal_fire_routes(problem, blockers):
    """A crossing heavy enemy can consume a defender's entire block capacity.

    Without a shared blocking/kill-time simulation, do not credit that defender
    with also stopping the other stream. Require independent normal fire near
    its goal, as already required for unblockable enemies. This is a coverage
    constraint, not a claim that the fire kills enemies before they arrive.
    """
    saturated = {point for profile, point in blockers if any(
        problem.mechanics[s['enemy_id']].get('block_cost', 1)
        >= profile['row']['block_count_skill']
        and point in problem.paths[s['route_index']]['cells']
        for s in problem.spawns)}
    return {s['route_index'] for s in problem.spawns
            if problem.mechanics[s['enemy_id']].get('block_cost', 1) == 0
            or saturated & set(problem.paths[s['route_index']]['terminal'][-3:])}


def matrix_contact_damage(problem, profile, point, facing, route, enemy_id):
    """Conditional ordinary damage during visible, unblocked matrix passage.

    Each separate range contact loses a full initial attack interval. No skill,
    field absorption, talent RNG, slowdown or blocking time is invented. Target
    competition and exact within-tile movement still need device validation.
    """
    if not profile['row']['behavior'].get('normal_attack', True):
        return 0
    matchup = next((m for m in profile['row']['matchups'] if m['enemy_id'] == enemy_id), {})
    per_attack = matchup.get('normal_attack_damage', 0)
    attrs = problem.enemy_attributes.get(enemy_id, {})
    speed, multiplier = attrs.get('moveSpeed'), problem.move_multiplier
    interval = profile['row']['attack_interval_normal']
    _require(all(type(n) in (int, float) and math.isfinite(n) and n > 0
                 for n in (speed, multiplier, interval)), 'Matrix contact needs known movement and attack intervals')
    footprint = _footprint(profile, point, facing)
    runs, length = [], 0
    for cell in problem.paths[route]['terminal']:
        if cell in footprint:
            length += 1
        elif length:
            runs.append(length)
            length = 0
    if length:
        runs.append(length)
    hits = sum(max(0, math.ceil(length / (speed * multiplier * interval)) - 1) for length in runs)
    return hits * per_attack


def normal_fire_deficits(problem, selected, fire_routes):
    """Independent geometric cover plus a finite-contact floor for matrix leaks."""
    deficits = {}
    for route in sorted(fire_routes):
        cover = any(p['row']['behavior'].get('normal_attack', True)
                    and _footprint(p, cell, direction) & set(problem.paths[route]['terminal'][-2:])
                    for p, cell, direction in selected)
        matrices = {s['enemy_id'] for s in problem.spawns if s['route_index'] == route
                    and problem.mechanics[s['enemy_id']].get('kind') == 'matrix'}
        remaining = {}
        for eid in sorted(matrices):
            hp = problem.enemy_attributes[eid]['maxHp']
            damage = sum(matrix_contact_damage(problem, p, cell, direction, route, eid)
                         for p, cell, direction in selected)
            if damage < hp:
                remaining[eid] = hp - damage
        if not cover or remaining:
            deficits[route] = {'coverage_missing': not cover, 'matrix_damage_missing': remaining}
    return deficits


def assess_formation(problem, formation):
    """Common admission and heuristic metrics, independent of strategy scores."""
    _require(type(formation.retire_dp) is bool
             and 0 < len(formation.placements) <= problem.capacity + int(formation.retire_dp),
             'Invalid formation size')
    selected, used, occupied = [], set(), set(problem.devices)
    for item in formation.placements:
        _require(type(item.profile_index) is int and 0 <= item.profile_index < len(problem.profiles),
                 'Unknown formation profile')
        p = problem.profiles[item.profile_index]
        _require(item.facing in ('Right', 'Down', 'Left', 'Up'), 'Invalid formation facing')
        _require(item.role in ('blocker', 'damage', 'healer', 'dp'), 'Invalid formation role')
        _require(item.point in problem.tiles and item.point not in occupied
                 and problem.tiles[item.point]['buildable'] in (p['position_type'], 'ALL'),
                 'Illegal or overlapping formation placement')
        _require(p['oper']['name'] not in used, 'Duplicate formation operator')
        behavior = p['row']['behavior']
        if item.role == 'blocker':
            _require(p['position_type'] == 'MELEE' and behavior.get('ground_block', True)
                     and p['row']['block_count_normal'] >= 1 and p['row']['block_count_skill'] >= 2
                     and behavior['mode'] != 'cost', 'Unsupported blocker profile')
        elif item.role == 'healer':
            _require(behavior['damage_type'] == 'healing', 'Unsupported healer profile')
        elif item.role == 'damage':
            _require(behavior['damage_type'] != 'healing' and behavior['mode'] != 'cost',
                     'Unsupported damage profile')
        else:
            _require(behavior['mode'] == 'cost', 'Unsupported DP profile')
        selected.append((p, item))
        occupied.add(item.point)
        used.add(p['oper']['name'])
    _require(not formation.retire_dp or (len(formation.placements) == problem.capacity + 1
             and sum(item.role == 'dp' for _, item in selected) == 1),
             'Opening DP retirement requires exactly one extra DP unit')
    heal = set().union(*(_footprint(p, item.point, item.facing)
                         for p, item in selected if item.role == 'healer'))
    requirements = []
    for p, item in selected:
        if item.role == 'blocker':
            need = blocker_healing_need(p, item.point, problem.paths, problem.spawns)
            requirements.append(dict(need, name=p['oper']['name'], location=list(item.point)))
            _require(not need['required'] or item.point in heal, 'Blocker lacks normal healing coverage')
        elif item.role == 'damage':
            _require(item.point in heal, 'Damage placement lacks normal healing coverage')
    fire_routes = required_normal_fire_routes(problem, [
        (p, item.point) for p, item in selected if item.role == 'blocker'])
    for spawn in problem.spawns:
        route = problem.paths[spawn['route_index']]
        if problem.mechanics[spawn['enemy_id']].get('block_cost', 1) > 0:
            _require(any(item.role == 'blocker' and item.point in route['terminal'][-3:]
                         for _, item in selected), 'Blockable final approach lacks a blocker')
        if spawn['route_index'] in fire_routes:
            _require(any(item.role == 'damage' and p['row']['behavior'].get('normal_attack', True)
                         and _footprint(p, item.point, item.facing) & set(route['terminal'][-2:])
                         for p, item in selected), 'Exposed final approach lacks independent normal DPS')
    deficits = normal_fire_deficits(problem, [
        (p, item.point, item.facing) for p, item in selected if item.role == 'damage'], fire_routes)
    _require(not deficits, 'Exposed matrix stream lacks finite-contact normal damage: ' + str(deficits))
    rates, controls = {}, {}
    for r, path in sorted(problem.paths.items()):
        terminal = set(path['terminal'][-4:])
        route_spawns = [s for s in problem.spawns if s['route_index'] == r]
        count = sum(s['count'] for s in route_spawns)
        value = control = 0
        for p, item in selected:
            if item.role not in ('damage', 'blocker'):
                continue
            contact = (len(_footprint(p, item.point, item.facing) & terminal)
                       if p['row']['behavior'].get('normal_attack', True) else 0)
            contact += .35 * len(_footprint(p, item.point, item.facing, skill=True) & terminal)
            value += sum(s['count'] * _damage_rate(p, s['enemy_id'], problem.horizon)
                         for s in route_spawns) * contact / max(1, count)
            by_enemy = {m['enemy_id']: m for m in p['row']['matchups']}
            control += sum(s['count'] * by_enemy.get(s['enemy_id'], {}).get('rotation', {}).get(
                'effects', {}).get('movement_delay_equivalent_seconds', 0)
                for s in route_spawns) * contact / (max(1, count) * problem.horizon)
        rates[str(r)] = round(value, 6)
        controls[str(r)] = round(control, 6)
    return {'objective_version': 2, 'kind': 'terminal_contact_heuristic',
            'required_normal_fire_routes': sorted(fire_routes),
            'route_damage_scores': rates, 'minimum_route_damage_score': min(rates.values(), default=0),
            'total_route_damage_score': round(sum(rates.values()), 6),
            'route_control_contact_scores': controls,
            'total_control_contact_score': round(sum(controls.values()), 6),
            'deployment_cost': sum(p['cost'] for p, _ in selected)}, requirements


def objective(metrics):
    return (metrics['minimum_route_damage_score'], metrics['total_route_damage_score'],
            metrics['total_control_contact_score'],
            -metrics['deployment_cost'])


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


def _footprint(profile, point, facing, *, skill=False):
    offsets = profile['skill_offsets' if skill else 'normal_offsets']
    return {(point[0] + dx, point[1] + dy) for dx, dy in
            (_rotate(offset, facing) for offset in offsets)}


def _damage_rate(profile, eid, horizon):
    m = next((m for m in profile['row']['matchups'] if m['enemy_id'] == eid), None)
    if m is None:
        return 0
    duel = m['duel']
    finite = duel['damage'] / ((duel['kill_time'] if duel['kill_time'] is not None else horizon)
                              + profile['row']['attack_interval_normal'])
    return .5 * finite + .5 * m['rotation']['average_dps']


def terminal_formation(problem, decisions):
    profiles, tiles, devices = problem.profiles, problem.tiles, problem.devices
    paths, spawns, mechanics = problem.paths, problem.spawns, problem.mechanics
    horizon, capacity = problem.horizon, problem.capacity
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

    def best(pool, score, priority=lambda p, cell, direction: 0):
        ranked = [(score(p, cell, direction), p, cell, direction)
                  for p, cell, direction in candidates(pool)]
        ranked = sorted((r for r in ranked if r[0] > 0), key=lambda r: (
            priority(r[1], r[2], r[3]), r[0],
            -r[1]['cost'], r[1]['oper']['name'], -r[2][1], -r[2][0], r[3]), reverse=True)
        # Equivalent facings should not spend the entire alternative budget.
        unique, seen = [], set()
        for value, p, cell, direction in ranked:
            key = (p['oper']['name'], p['oper']['skill'], cell,
                   frozenset(_footprint(p, cell, direction)),
                   frozenset(_footprint(p, cell, direction, skill=True)))
            if key not in seen:
                seen.add(key)
                unique.append((value, p, cell, direction))
                if len(unique) >= decisions.branch_width:
                    break
        _require(bool(unique), 'Required role has no useful legal coverage')
        _, profile, cell, direction = decisions.pick(unique)
        return profile, cell, direction

    def damage_rate(profile, eid):
        return _damage_rate(profile, eid, horizon)

    # Cover all blockable final approaches, rather than placing one defender per
    # spawn/route ID. A single choke can service many routes to the same goal.
    needed = {s['route_index'] for s in spawns if mechanics[s['enemy_id']].get('block_cost', 1) > 0}
    blockers = [p for p in profiles if p['position_type'] == 'MELEE'
                and p['row']['block_count_normal'] >= 1
                and p['row']['block_count_skill'] >= 2
                and p['row']['behavior'].get('ground_block', True)
                and p['row']['behavior']['mode'] != 'cost']
    weights = {r: sum(s['count'] for s in spawns if s['route_index'] == r) for r in paths}
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
    fire_routes = required_normal_fire_routes(problem, [
        (x['profile'], x['point']) for x in selected if x['role'] == 'blocker'])
    dp_pool = [p for p in profiles if p['row']['behavior']['mode'] == 'cost']
    # The opening DP unit can retire after paying for the main formation. Its
    # deployment slot must not permanently displace a required damage unit.
    remaining = capacity - len(selected)
    healing_slots = 2 if remaining >= 4 else 1
    damage_slots = min(3, remaining - healing_slots,
                       len({p['oper']['name'] for p in damage_pool if available(p)}))
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
    damage_cells, fire_cells = {}, {}
    matrix_targets = {r: {s['enemy_id'] for s in spawns if s['route_index'] == r
                         and mechanics[s['enemy_id']].get('kind') == 'matrix'} for r in fire_routes}
    for p, cell, direction in candidates(damage_pool):
        damage_cells[cell] = max(damage_cells.get(cell, 0), fire_score(p, cell, direction, marginal=False))
        if not p['row']['behavior'].get('normal_attack', True):
            continue
        cover = _footprint(p, cell, direction)
        for r in fire_routes:
            if not cover & set(paths[r]['terminal'][-2:]):
                continue
            potential = min((min(1, matrix_contact_damage(problem, p, cell, direction, r, eid)
                                / problem.enemy_attributes[eid]['maxHp'])
                             for eid in matrix_targets[r]), default=1)
            by_route = fire_cells.setdefault(cell, {})
            by_name = by_route.setdefault(r, {})
            name = p['oper']['name']
            by_name[name] = max(by_name.get(name, 0), potential)
    heal_candidates = [(p, cell, direction, _footprint(p, cell, direction))
                       for p, cell, direction in candidates(healer_pool)]
    healing_requirements = [{**blocker_healing_need(x['profile'], x['point'], paths, spawns),
                             'name': x['profile']['oper']['name'], 'location': list(x['point'])}
                            for x in selected if x['role'] == 'blocker']
    must_heal = {tuple(x['location']) for x in healing_requirements if x['required']}
    networks = itertools.combinations(heal_candidates, healing_slots)
    ranked_networks = []
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
        # Do not choose a medic network that strands the only adequate normal
        # fire outside healing. This remains an optimistic placement bound;
        # distinct operators and the final damage gate are checked afterwards.
        potentials = {}
        for r in fire_routes:
            by_name = {}
            for cell in coverage - cells:
                for name, potential in fire_cells.get(cell, {}).get(r, {}).items():
                    by_name[name] = max(by_name.get(name, 0), potential)
            # Distinct damage operators can contribute together. Reusing one
            # exceptional profile at many tiles must not dominate this bound.
            potentials[r] = sum(sorted(by_name.values(), reverse=True)[:damage_slots])
        network_score = (min(potentials.values(), default=1),
                         sum(weights[r] * v for r, v in potentials.items()), value)
        ranked_networks.append((network_score, network))
        ranked_networks.sort(key=lambda item: item[0], reverse=True)
        del ranked_networks[decisions.branch_width:]
    _require(bool(ranked_networks), 'No normal healing coverage network for blockers and ranged damage')
    # Stable ordering preserves the baseline's first maximum on score ties.
    ranked_networks.sort(key=lambda item: item[0], reverse=True)
    _, best_network = decisions.pick(ranked_networks)
    for p, cell, direction, _ in best_network:
        choose(p, cell, direction, 'healer')
    healing = set().union(*(_footprint(x['profile'], x['point'], x['facing'])
                           for x in selected if x['role'] == 'healer'))

    # Complete required normal fire before maximizing marginal damage scores.
    for _ in range(damage_slots):
        deficits = normal_fire_deficits(problem, [
            (x['profile'], x['point'], x['facing']) for x in selected if x['role'] == 'damage'], fire_routes)
        def fire_priority(p, cell, direction):
            value = 0
            for r, need in deficits.items():
                missing = need['matrix_damage_missing']
                if missing:
                    value += weights[r] * min(min(1, matrix_contact_damage(
                        problem, p, cell, direction, r, eid) / amount) for eid, amount in missing.items())
                elif (need['coverage_missing'] and p['row']['behavior'].get('normal_attack', True)
                      and _footprint(p, cell, direction) & set(paths[r]['terminal'][-2:])):
                    value += weights[r]
            return value
        profile, cell, direction = best(damage_pool, lambda p, cell, direction:
            fire_score(p, cell, direction) if cell in healing else 0,
            priority=fire_priority)
        choose(profile, cell, direction, 'damage')
    if dp_pool and len(selected) <= capacity:
        # A DP unit has no facing-sensitive tactical score. Prefer an inward
        # gesture over the old alphabetical Up tie-break at the map's edge.
        xmax, ymax = max(x for x, y in tiles), max(y for x, y in tiles)
        def inward_margin(cell, direction):
            return {'Right': xmax-cell[0], 'Left': cell[0],
                    'Down': ymax-cell[1], 'Up': cell[1]}[direction]
        profile, cell, direction = best(dp_pool, lambda p, cell, direction:
            1000 - sum(weights[r] for r in paths if cell in paths[r]['terminal']) - p['cost']
            + .001 * inward_margin(cell, direction))
        choose(profile, cell, direction, 'dp')

    # A blocker or an expanded skill range cannot satisfy independent fire.
    missing = []
    for r in sorted(fire_routes):
        route = paths[r]
        if not any(_footprint(x['profile'], x['point'], x['facing']) & set(route['terminal'][-2:])
                   for x in selected if x['role'] == 'damage'
                   and x['profile']['row']['behavior'].get('normal_attack', True)):
            missing.append(r)
    _require(not missing, 'Exposed final approaches lack independent normal DPS: ' + str(missing))
    _require(not normal_fire_deficits(problem, [
        (x['profile'], x['point'], x['facing']) for x in selected if x['role'] == 'damage'], fire_routes),
        'Exposed matrix streams lack finite-contact normal damage')

    indices = {id(p): i for i, p in enumerate(profiles)}
    return Formation(tuple(Placement(indices[id(x['profile'])], x['point'], x['facing'], x['role'])
                           for x in selected), retire_dp=len(selected) > capacity)
