"""Offline route heuristics and a checked MAA compiler, not a combat simulator.

Graph and MAA coordinates both use [x, y] from the top left. Coverage uses
four-neighbour shortest paths; game steering, damage and enemy skills are not
simulated. A compiled script is always unproven until the normal runner proves
a fresh three-star result.
"""
from __future__ import annotations

import copy
from collections import deque

from .copilot_matcher import LIMITS, match_member
from .util import canonical_json, sha256_bytes


class BattleSolverError(ValueError):
    pass


NATIVE_INT_MIN = -(2 ** 31)
NATIVE_INT_MAX = 2 ** 31 - 1


def _require(condition, message):
    if not condition:
        raise BattleSolverError(message)


def _integer(value, low=0, high=None):
    maximum = NATIVE_INT_MAX if high is None else min(high, NATIVE_INT_MAX)
    return type(value) is int and max(low, NATIVE_INT_MIN) <= value <= maximum


DIRECTIONS = {'Right': 'Right', 'Down': 'Down', 'Left': 'Left', 'Up': 'Up',
              'None': 'None', '右': 'Right', '下': 'Down', '左': 'Left',
              '上': 'Up', '无': 'None'}
ACTION_TYPES = {'Deploy', 'Skill', 'Retreat', 'SpeedUp', 'BulletTime',
                'SkillUsage', 'SkillDaemon', 'ResetStopwatch', 'Output'}
ACTION_ALIASES = dict(zip(
    ('部署', '技能', '撤退', '二倍速', '子弹时间', '技能用法', '摆完挂机', '重置全局计时器', '打印'),
    ('Deploy', 'Skill', 'Retreat', 'SpeedUp', 'BulletTime', 'SkillUsage',
     'SkillDaemon', 'ResetStopwatch', 'Output')))
ACTION_FIELDS = {'type', 'name', 'location', 'direction', 'kills', 'costs',
                 'cost_changes', 'cooling', 'elapsed_time', 'pre_delay',
                 'post_delay', 'skill_usage', 'skill_times', 'skip_if_not_ready',
                 'timeout', 'doc', 'doc_color'}
OPER_FIELDS = {'name', 'skill', 'skill_usage', 'skill_times', 'requirements'}


def _map(graph):
    _require(isinstance(graph, dict) and graph.get('schema') == 1, 'Unknown battle graph schema.')
    value = graph.get('map', {})
    _require(isinstance(value, dict) and value.get('coordinate_system') == 'top-left',
             'Battle graph must use top-left coordinates.')
    rows, cols = value.get('rows'), value.get('cols')
    _require(_integer(rows, 1, 128) and _integer(cols, 1, 128), 'Invalid map dimensions.')
    tiles = value.get('tiles')
    _require(isinstance(tiles, list) and len(tiles) == rows * cols, 'Incomplete map tiles.')
    indexed = {}
    for tile in tiles:
        _require(isinstance(tile, dict), 'Invalid map tile.')
        x, y = tile.get('x'), tile.get('y')
        _require(_integer(x, 0, cols - 1) and _integer(y, 0, rows - 1), 'Invalid tile coordinate.')
        _require((x, y) not in indexed, 'Duplicate map tile.')
        _require(tile.get('buildable') in ('NONE', 'MELEE', 'RANGED', 'ALL'), 'Unknown tile buildability.')
        indexed[(x, y)] = tile
    return rows, cols, indexed


def _point(value, tiles):
    _require(isinstance(value, (list, tuple)) and len(value) == 2
             and all(type(i) is int for i in value) and tuple(value) in tiles,
             'Invalid or out-of-map coordinate.')
    return tuple(value)


def _shortest(start, end, tiles):
    queue, previous = deque([start]), {start: None}
    while queue:
        cell = queue.popleft()
        if cell == end:
            result = []
            while cell is not None:
                result.append(cell)
                cell = previous[cell]
            return list(reversed(result))
        # Stable neighbour order makes ties repeatable. This is a coverage
        # approximation, not a reproduction of the game's diagonal steering.
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            nxt = (cell[0] + dx, cell[1] + dy)
            if nxt not in previous and nxt in tiles and (
                    nxt == end or tiles[nxt].get('passable_mask') == 'ALL'):
                previous[nxt] = cell
                queue.append(nxt)
    raise BattleSolverError('No four-neighbour path between route checkpoints.')


def _route_paths(graph, tiles):
    paths, unsupported, indices = {}, [], set()
    routes = graph.get('routes')
    _require(isinstance(routes, list), 'Missing battle routes.')
    for route in routes:
        index = route.get('index') if isinstance(route, dict) else None
        _require(_integer(index) and index not in indices, 'Invalid or duplicate route index.')
        indices.add(index)
        try:
            _require(route.get('motion_mode') == 'WALK', 'Only WALK paths are expanded.')
            points = [_point(route.get('start'), tiles)]
            checkpoints = route.get('checkpoints', [])
            _require(isinstance(checkpoints, list), 'Invalid route checkpoints.')
            for checkpoint in checkpoints:
                _require(isinstance(checkpoint, dict), 'Invalid route checkpoint.')
                kind = checkpoint.get('type')
                if kind == 'MOVE':
                    points.append(_point(checkpoint.get('position'), tiles))
                else:
                    _require(kind == 'WAIT_FOR_SECONDS', 'Unsupported route checkpoint: ' + str(kind))
            points.append(_point(route.get('end'), tiles))
            path = [points[0]]
            for start, end in zip(points, points[1:]):
                path.extend(_shortest(start, end, tiles)[1:])
            paths[index] = path
        except BattleSolverError as error:
            unsupported.append({'route_index': index, 'reason': str(error)})
    return paths, unsupported


def _rotate(offset, direction):
    x, y = offset
    return {'Right': (x, y), 'Down': (-y, x),
            'Left': (-x, -y), 'Up': (y, -x)}[direction]


def analyze_candidates(graph, *, attack_range=None, placement='MELEE', limit=20):
    """Rank legal positions using an explicit Right-facing range footprint.

    Offsets are [dx, dy], with dy positive down. The default covers the same
    tile and one forward tile, and is labelled generic; caller-supplied ranges
    must come from the selected operator/skill's verified static data.
    """
    _, _, tiles = _map(graph)
    _require(placement in ('MELEE', 'RANGED', 'ALL'), 'Invalid placement type.')
    _require(_integer(limit, 1, 1000), 'Candidate limit must be 1..1000.')
    offsets = [[0, 0], [1, 0]] if attack_range is None else attack_range
    _require(isinstance(offsets, list) and offsets and all(
        isinstance(p, (list, tuple)) and len(p) == 2
        and all(type(i) is int and -30 <= i <= 30 for i in p) for p in offsets),
        'Invalid Right-facing attack range.')
    paths, unsupported = _route_paths(graph, tiles)
    weights = {}
    for spawn in graph.get('spawns', []):
        index, count = spawn.get('route_index'), spawn.get('count')
        _require(_integer(index) and _integer(count, 1), 'Invalid spawn route/count.')
        weights[index] = weights.get(index, 0) + count
    active = {i: p for i, p in paths.items() if weights.get(i, 0)}
    devices = _device_tiles(graph, tiles)
    candidates = []
    for (x, y), tile in sorted(tiles.items()):
        if (x, y) in devices or tile['buildable'] == 'NONE' or not (
                placement == 'ALL' or tile['buildable'] in (placement, 'ALL')):
            continue
        for direction in ('Right', 'Down', 'Left', 'Up'):
            footprint = {(x + dx, y + dy) for dx, dy in (
                _rotate(p, direction) for p in offsets)}
            covered, intercepts, score = [], [], 0.0
            for index, path in active.items():
                # The blue endpoint represents a leak, not useful attack time.
                seen = set(path[:-1])
                fraction = len(footprint & seen) / max(1, len(seen))
                if fraction:
                    covered.append(index)
                    score += weights[index] * fraction
                if (x, y) in seen:
                    intercepts.append(index)
            distance = min((abs(x-p[-1][0]) + abs(y-p[-1][1])
                            for p in active.values()), default=999)
            candidates.append({'location': [x, y], 'direction': direction,
                               'coverage_score': round(score, 6),
                               'covered_routes': covered,
                               'intercepted_routes': intercepts,
                               'goal_distance': distance,
                               'tile_key': tile.get('tile_key')})
    candidates.sort(key=lambda c: (-c['coverage_score'], -len(c['intercepted_routes']),
                                   c['goal_distance'], c['location'][1],
                                   c['location'][0], c['direction']))
    return {'schema_version': 1, 'method': 'route-coverage-heuristic',
            'proof_status': 'unproven', 'combat_simulated': False,
            'range_source': 'generic' if attack_range is None else 'caller-supplied',
            'coordinate_system': 'top-left', 'active_routes': sorted(active),
            'unsupported_routes': unsupported,
            'limitations': ['Four-neighbour coverage approximation; game steering is not simulated.',
                            'Damage, healing, block capacity, DP and enemy abilities are not simulated.'],
            'candidates': candidates[:limit]}


def _mechanics(graph, acknowledged):
    mechanics = graph.get('mechanics', {})
    _require(isinstance(mechanics, dict), 'Invalid graph mechanics.')
    unknown = mechanics.get('unknown', [])
    _require(isinstance(unknown, list) and all(isinstance(i, str) for i in unknown),
             'Invalid graph mechanics.')
    _require(isinstance(acknowledged, (list, tuple))
             and all(isinstance(i, str) for i in acknowledged), 'Invalid mechanics acknowledgement.')
    _require(len(set(acknowledged)) == len(acknowledged), 'Duplicate mechanics acknowledgement.')
    _require(set(acknowledged) <= set(unknown), 'Acknowledgement does not match graph mechanics.')
    missing = sorted(set(unknown) - set(acknowledged))
    _require(not missing, 'Unsupported mechanics require explicit plan acknowledgement: ' + ', '.join(missing))
    return sorted(set(unknown))


def _device_tiles(graph, tiles):
    result = set()
    predefines = graph.get('mechanics', {}).get('predefines', {})
    for row in predefines.get('tokenInsts', []) if isinstance(predefines, dict) else []:
        position = row.get('position', {})
        if _integer(position.get('col')) and _integer(position.get('row')):
            location = (position['col'], graph['map']['rows'] - 1 - position['row'])
            if location in tiles:
                result.add(location)
    return result


def validate_copilot(graph, content, box=None, catalog=None, *, character_table=None,
                     mechanics_acknowledged=()):
    """Validate a fixed roster and action sequence without executing it.

    Supplying Box/catalog checks ownership and training. Supplying the game
    character table checks each operator's legal placement class. Missing
    inputs are reported as not checked, never interpreted as satisfied.
    """
    _, _, tiles = _map(graph)
    stage = graph.get('stage', {})
    _require(stage.get('difficulty') == 'NORMAL', 'Only normal stages are supported.')
    _require(isinstance(content, dict), 'Copilot must be an object.')
    _require(content.get('stage_name') in (stage.get('stage_id'), stage.get('battle_id'),
                                         stage.get('level_id'), stage.get('code'))
             and bool(content.get('stage_name')), 'Copilot stage differs from graph.')
    _require(type(content.get('difficulty')) is int and content['difficulty'] == 1,
             'Copilot must explicitly specify normal difficulty=1.')
    _require(isinstance(content.get('minimum_required'), str)
             and bool(content['minimum_required']), 'Missing MAA minimum version.')
    _require(isinstance(content.get('doc'), dict)
             and isinstance(content['doc'].get('title'), str)
             and bool(content['doc']['title'].strip()), 'Missing Copilot title.')
    _require(not content.get('groups'), 'Solver validation currently requires a fixed roster.')
    unknown = _mechanics(graph, mechanics_acknowledged)
    opers = content.get('opers')
    _require(isinstance(opers, list) and opers, 'Missing fixed operator roster.')
    _require((box is None) == (catalog is None), 'Box and operator catalog must be supplied together.')
    names, identities, phase_checks = {}, set(), []
    for spec in opers:
        _require(isinstance(spec, dict) and not set(spec) - OPER_FIELDS, 'Unknown operator field.')
        name = spec.get('name')
        _require(isinstance(name, str) and bool(name.strip()) and name not in names,
                 'Invalid or duplicate operator name.')
        _require(_integer(spec.get('skill'), 0, 3), 'Invalid operator skill index.')
        for key, default, maximum in (('skill_usage', 0, 2), ('skill_times', 1, 100)):
            _require(_integer(spec.get(key, default), 0 if key == 'skill_usage' else 1, maximum),
                     'Invalid operator ' + key + '.')
        requirements = spec.get('requirements', {})
        _require(isinstance(requirements, dict) and all(
            key in LIMITS and _integer(value, 0, LIMITS[key])
            for key, value in requirements.items()), 'Invalid operator requirements.')
        native_elite = max(0, spec['skill'] - 1)
        if requirements.get('skill_level', 0) > 7 or requirements.get('module', 0) > 0:
            native_elite = max(2, native_elite)
        elif requirements.get('skill_level', 0) > 4:
            native_elite = max(1, native_elite)
        _require('elite' not in requirements or requirements['elite'] >= native_elite,
                 'Native MAA rejects an encoded elite requirement below the selected skill/module requirement.')
        _require(spec['skill'] > 0 or not spec.get('skill_usage', 0),
                 'An operator with skill=0 cannot use automatic skills.')
        identity = None
        if catalog is not None:
            identity = catalog.resolve(name)
            _require(identity is not None and identity.id not in identities,
                     'Unknown or duplicate canonical operator identity.')
            identities.add(identity.id)
            _require(spec['skill'] == 0 if identity.has_no_skills else spec['skill'] > 0,
                     'Skill selection must match the verified operator skills.')
            member = match_member(spec, box, catalog)
            _require(member.status == 'yes', 'Box does not verify operator ' + name + ': '
                     + ', '.join(member.unsatisfied + member.unknown))
            operator = box.operators.get(identity.id)
            _require(operator is not None and operator.elite is not None and operator.level is not None,
                     'Box training is unknown for operator ' + name + '.')
            _require(identity.has_no_skills or operator.main_skill_level is not None,
                     'Box skill training is unknown for operator ' + name + '.')
        position = None
        if character_table is not None:
            _require(isinstance(character_table, dict), 'Invalid character table.')
            rows = ([character_table.get(identity.id)] if identity is not None else
                    [r for r in character_table.values() if isinstance(r, dict) and r.get('name') == name])
            _require(len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get('name') == name,
                     'Character table cannot verify unique operator ' + name + '.')
            position = rows[0].get('position')
            _require(position in ('MELEE', 'RANGED', 'ALL'), 'Unknown operator placement class.')
            phases = rows[0].get('phases')
            phase_checks.append(phases is not None)
            if phases is not None:
                _require(isinstance(phases, list) and 1 <= len(phases) <= 3 and all(
                    isinstance(phase, dict) and _integer(phase.get('maxLevel'), 1, 90)
                    for phase in phases), 'Invalid operator phase level limits.')
                _require(native_elite < len(phases), 'Selected skill/module needs an unavailable operator phase.')
                requested_elite = requirements.get('elite')
                _require(requested_elite is None or requested_elite < len(phases),
                         'Encoded elite requirement exceeds operator phase limit.')
                maximum_level = (phases[requested_elite]['maxLevel'] if requested_elite is not None
                                 else max(phase['maxLevel'] for phase in phases))
                _require(requirements.get('level', 0) <= maximum_level,
                         'Encoded level requirement exceeds the operator phase level limit.')
                if identity is not None:
                    _require(_integer(operator.elite, 0, len(phases) - 1)
                             and _integer(operator.level, 1, phases[operator.elite]['maxLevel']),
                             'Box training exceeds authoritative operator phase level limits.')
        names[name] = {'spec': spec, 'position': position}
    actions = content.get('actions')
    _require(isinstance(actions, list) and actions and len(actions) <= 500, 'Invalid Copilot actions.')
    deployed, occupied, stopwatch = {}, {}, False
    ignored_timeouts = []
    device_tiles = _device_tiles(graph, tiles)
    max_deployed = 0
    capacity = graph.get('options', {}).get('characterLimit')
    for index, action in enumerate(actions):
        prefix = 'Action ' + str(index) + ': '
        _require(isinstance(action, dict) and not set(action) - ACTION_FIELDS, prefix + 'Unknown action field.')
        kind = action.get('type', 'Deploy')
        _require(isinstance(kind, str), prefix + 'Invalid action type.')
        kind = ACTION_ALIASES.get(kind, kind)
        _require(kind in ACTION_TYPES, prefix + 'Unsupported action type.')
        for key in ('kills', 'costs', 'elapsed_time', 'pre_delay', 'post_delay'):
            _require(_integer(action.get(key, 0)), prefix + 'Invalid ' + key + '.')
        _require(_integer(action.get('cost_changes', 0), NATIVE_INT_MIN), prefix + 'Invalid cost_changes.')
        _require(_integer(action.get('cooling', -1), -1), prefix + 'Invalid cooling.')
        _require(not action.get('elapsed_time', 0) or stopwatch,
                 prefix + 'elapsed_time requires an earlier ResetStopwatch.')
        if 'skip_if_not_ready' in action:
            _require(kind == 'Skill' and type(action['skip_if_not_ready']) is bool,
                     prefix + 'Invalid skip_if_not_ready.')
            _require('timeout' not in action, prefix + 'Native MAA ignores actions combining timeout and skip_if_not_ready.')
        if 'timeout' in action:
            _require(_integer(action['timeout'], -1), prefix + 'Invalid action timeout.')
            # v6.18.0 parses timeout for every action, but BattleProcessTask
            # only passes it to click_skill/use_skill. Deploy's DP/CD wait does
            # not consult it. Preserve the valid native field without claiming
            # that it bounds deployment or other condition waits.
            if kind != 'Skill':
                ignored_timeouts.append({'action_index': index, 'type': kind,
                                         'timeout': action['timeout']})
        for key in ('doc', 'doc_color'):
            _require(key not in action or isinstance(action[key], str), prefix + 'Invalid ' + key + '.')
        name = action.get('name')
        if name is not None:
            _require(isinstance(name, str) and name in names, prefix + 'Unknown operator name.')
        location = _point(action['location'], tiles) if 'location' in action else None
        if kind == 'Deploy':
            _require(name in names and location is not None, prefix + 'Deploy needs name and location.')
            _require(name not in deployed and location not in occupied and location not in device_tiles,
                     prefix + 'Deployment overlaps an occupied tile/operator.')
            _require(isinstance(action.get('direction'), str) and action['direction'] in DIRECTIONS,
                     prefix + 'Invalid deployment direction.')
            tile_type, position = tiles[location]['buildable'], names[name]['position']
            _require(tile_type != 'NONE' and (position is None or position == 'ALL'
                     or tile_type in (position, 'ALL')), prefix + 'Operator cannot deploy on this tile.')
            deployed[name], occupied[location] = location, name
            max_deployed = max(max_deployed, len(deployed))
            _require(not _integer(capacity, 1) or len(deployed) <= capacity,
                     prefix + 'Map deployment limit exceeded.')
        elif kind in ('Skill', 'Retreat'):
            _require(name is not None or location is not None, prefix + kind + ' needs a target.')
            if name is not None:
                _require(name in deployed, prefix + 'Target operator has not been deployed.')
                _require(location is None or deployed[name] == location, prefix + 'Target location differs from operator.')
            elif location in occupied:
                name = occupied[location]
            else:
                _require(kind == 'Skill' and location in device_tiles,
                         prefix + 'Location target is not a known operator/device.')
            if kind == 'Retreat':
                occupied.pop(deployed.pop(name))
            elif name is not None:
                _require(names[name]['spec']['skill'] > 0, prefix + 'Operator has no selected skill.')
        elif kind == 'SkillUsage':
            _require(name in names and _integer(action.get('skill_usage'), 0, 2),
                     prefix + 'Invalid skill usage target/value.')
            _require(_integer(action.get('skill_times', 1), 1, 100), prefix + 'Invalid skill_times.')
        elif kind == 'BulletTime':
            _require(name is not None or location is not None, prefix + 'BulletTime needs a target.')
            _require(location is None or location in occupied or location in device_tiles,
                     prefix + 'Unknown BulletTime location.')
        elif kind == 'ResetStopwatch':
            stopwatch = True
        elif kind == 'SkillDaemon':
            _require(index == len(actions) - 1, prefix + 'SkillDaemon must be the final action.')
        if kind in ('SpeedUp', 'SkillDaemon', 'ResetStopwatch', 'Output'):
            _require(name is None and location is None, prefix + 'This action does not take an operator target.')
        if 'direction' in action:
            _require(kind == 'Deploy', prefix + 'direction is only valid for Deploy.')
        if 'skill_usage' in action or 'skill_times' in action:
            _require(kind == 'SkillUsage', prefix + 'Skill usage fields require SkillUsage.')
    _require(max_deployed > 0, 'Copilot never deploys an operator.')
    return {'schema_version': 1, 'method': 'checked-copilot-compiler',
            'proof_status': 'unproven', 'combat_simulated': False,
            'stage_id': stage['stage_id'],
            'graph_sha256': sha256_bytes(canonical_json(graph)),
            'copilot_sha256': sha256_bytes(canonical_json(content)),
            'box_validation': 'verified' if box is not None else 'not_checked',
            'graph_source_validation': 'local_input_not_authenticated',
            'placement_type_validation': 'verified' if character_table is not None else 'not_checked',
            'phase_level_validation': 'verified' if phase_checks and all(phase_checks) else 'not_checked',
            'mechanics_acknowledged': unknown,
            'non_skill_timeouts_ignored': ignored_timeouts,
            'operators': list(names), 'action_count': len(actions), 'max_deployed': max_deployed,
            'limitations': ['Static legality and Box compatibility do not prove battle success.',
                            'Damage, healing, DP, cooldowns, enemy abilities and event mechanics are not simulated.',
                            'Native v6.18.0 timeout only bounds Skill activation, not Deploy DP/CD or action condition waits.']}


def compile_copilot(graph, plan, box, catalog, *, character_table):
    """Compile a manually or skill-authored plan into a fixed MAA script."""
    _require(isinstance(plan, dict) and plan.get('schema_version') == 1, 'Unknown plan schema.')
    _require(plan.get('stage_id') == graph.get('stage', {}).get('stage_id'), 'Plan stage differs from graph.')
    _require(box is not None and catalog is not None and character_table is not None,
             'Compilation requires a local Box and authoritative character identities/placement types.')
    placements = plan.get('placements')
    if placements is not None:
        _require(isinstance(placements, list) and placements and 'opers' not in plan,
                 'Use either placements or explicit opers/actions.')
        opers, actions = [], []
        for row in placements:
            _require(isinstance(row, dict), 'Invalid placement plan.')
            allowed = OPER_FIELDS | (ACTION_FIELDS - {'type', 'skill_usage', 'skill_times'}) | {'time_elapsed'}
            _require(not set(row) - allowed, 'Unknown placement field.')
            row = copy.deepcopy(row)
            if 'time_elapsed' in row:
                _require('elapsed_time' not in row, 'Use only one placement timing field.')
                # Only the placement shorthand accepts this alias. The native
                # v6.18.0 parser reads elapsed_time; never emit time_elapsed.
                row['elapsed_time'] = row.pop('time_elapsed')
            opers.append({k: copy.deepcopy(v) for k, v in row.items() if k in OPER_FIELDS})
            actions.append(dict(type='Deploy', **{k: copy.deepcopy(v) for k, v in row.items()
                                                 if k in ACTION_FIELDS and k not in ('skill_usage', 'skill_times')}))
        extra = plan.get('actions', [])
        _require(isinstance(extra, list), 'Invalid additional actions.')
        actions.extend(copy.deepcopy(extra))
    else:
        opers, actions = copy.deepcopy(plan.get('opers')), copy.deepcopy(plan.get('actions'))
    _require(isinstance(actions, list), 'Missing plan actions.')
    _require(all(isinstance(a, dict) for a in actions), 'Invalid plan action.')
    if any(a.get('elapsed_time', 0) for a in actions):
        if not actions or ACTION_ALIASES.get(actions[0].get('type'), actions[0].get('type')) != 'ResetStopwatch':
            actions.insert(0, {'type': 'ResetStopwatch'})
    content = {'stage_name': plan['stage_id'], 'minimum_required': 'v6.18.0',
               'difficulty': 1, 'opers': opers, 'actions': actions,
               'doc': {'title': plan.get('title', graph['stage'].get('code', plan['stage_id'])
                                        + ' experimental heuristic plan'),
                       'details': 'ZOOTd offline compiler. Heuristic plan; no combat simulation or clear proof. '
                                  'Verify through the normal Copilot runner and fresh three-star evidence.'}}
    analysis = validate_copilot(graph, content, box, catalog, character_table=character_table,
                                mechanics_acknowledged=plan.get('mechanics_acknowledged', []))
    return {'copilot': content, 'analysis': analysis}
