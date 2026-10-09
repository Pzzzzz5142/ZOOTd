"""Explicit, read-only, revision-bound battle data; never starts the game.

The graph describes source facts, not a simulated battle or a victory proof.
Game route rows start at the bottom; graph/MAA coordinates start at the top.
"""
from __future__ import annotations

import copy
import math
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

from .navigation_catalog import REPOSITORY, load_navigation, load_tables
from .prts import PrtsError, _NoRedirect, decode, require, text
from .util import atomic_write_bytes, atomic_write_json, canonical_json, sha256_bytes

MAX_BYTES = 64 * 1024 * 1024
SCHEMA = 1
LIMITATIONS = [
    'Source facts do not establish victory, operator damage, survival or DP feasibility.',
    'Wave, fragment and action delays are event-relative; absolute spawn times are unknown.',
    'Route checkpoints are constraints, not an exact engine path or arrival-time simulation.',
    'Enemy skills, level runes and tile effects are preserved but are not simulated.',
]


def _number(value, name, *, minimum=None):
    require(type(value) in (int, float) and math.isfinite(value), f'Invalid {name}.')
    require(minimum is None or value >= minimum, f'Invalid {name}.')
    return value


def _list(value, name):
    require(isinstance(value, list), f'Missing or invalid {name}.')
    return value


def _point(value, rows, cols):
    require(isinstance(value, dict), 'Missing route position.')
    row, col = value.get('row'), value.get('col')
    require(type(row) is int and type(col) is int and 0 <= row < rows and 0 <= col < cols,
            'Route position outside map.')
    return [col, rows - 1 - row]


def _defined(data):
    """Decode explicit game values without treating undefined defaults as facts."""
    if isinstance(data, dict):
        if 'm_defined' in data:
            require(type(data['m_defined']) is bool and 'm_value' in data,
                    'Invalid enemy defined-value wrapper.')
            return copy.deepcopy(data['m_value']) if data['m_defined'] else None
        result = {}
        for key, value in data.items():
            if isinstance(value, dict) and value.get('m_defined') is False:
                continue
            result[key] = _defined(value)
        return result
    return copy.deepcopy(data)


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _enemies(level, database):
    require(isinstance(database, dict), 'Invalid enemy database.')
    index = {}
    for entry in _list(database.get('enemies'), 'enemy database enemies'):
        require(isinstance(entry, dict) and text(entry.get('Key')), 'Invalid enemy database key.')
        require(entry['Key'] not in index, 'Duplicate enemy database key.')
        index[entry['Key']] = _list(entry.get('Value'), 'enemy database levels')
    result = {}
    for ref in _list(level.get('enemyDbRefs'), 'enemyDbRefs'):
        require(isinstance(ref, dict) and text(ref.get('id')), 'Invalid enemy reference.')
        key, target = ref['id'], ref.get('level')
        require(key not in result and type(target) is int and target >= 0,
                'Duplicate enemy reference or invalid level.')
        require(type(ref.get('useDb')) is bool, 'Missing enemy useDb flag.')
        data, raw_levels = {}, []
        if ref['useDb']:
            require(key in index, f'Enemy database entry missing: {key}.')
            levels = {}
            for row in index[key]:
                require(isinstance(row, dict) and type(row.get('level')) is int
                        and isinstance(row.get('enemyData'), dict), 'Invalid enemy level data.')
                require(row['level'] not in levels, 'Duplicate enemy database level.')
                levels[row['level']] = row
            require(0 in levels and target in levels, f'Enemy level missing: {key}/{target}.')
            for number in sorted(n for n in levels if n <= target):
                raw_levels.append(copy.deepcopy(levels[number]))
                _merge(data, _defined(levels[number]['enemyData']))
        override = ref.get('overwrittenData')
        require(override is None or isinstance(override, dict), 'Invalid enemy override.')
        require(ref['useDb'] or isinstance(override, dict), 'Local enemy definition missing.')
        if override is not None:
            _merge(data, _defined(override))
        require(text(data.get('name')) and isinstance(data.get('attributes'), dict),
                f'Enemy identity or attributes missing: {key}.')
        for field, value in data['attributes'].items():
            require(type(value) in (int, float, bool), f'Invalid enemy attribute: {key}/{field}.')
            if type(value) is not bool:
                _number(value, f'enemy attribute {key}/{field}')
        result[key] = {
            'id': key, 'level': target, 'name': data['name'],
            'attributes': data['attributes'], 'description': data.get('description'),
            'motion': data.get('motion'), 'apply_way': data.get('applyWay'),
            'not_count_in_total': data.get('notCountInTotal'),
            'life_point_reduce': data.get('lifePointReduce'),
            'skills': data.get('skills'), 'talent_blackboard': data.get('talentBlackboard'),
            'data': data, 'raw': {'reference': copy.deepcopy(ref), 'database_levels': raw_levels},
        }
    # Inline enemies use a different engine representation. Keep them for manual
    # analysis, but do not silently interpret them as enemyDbRefs.
    require(not level.get('enemies'), 'Inline enemies are unsupported; enemyDbRefs required.')
    return result


def parse_battle(level: dict, enemy_database: dict, *, identity: dict, sources: dict) -> dict:
    """Normalize complete level facts, refusing missing references and malformed data."""
    require(isinstance(level, dict) and isinstance(identity, dict), 'Invalid battle input.')
    require(all(text(identity.get(k)) for k in ('stage_id', 'battle_id', 'code', 'level_id')),
            'Incomplete stage identity.')
    require(identity.get('difficulty') == 'NORMAL', 'Only explicit NORMAL battle data is supported.')
    source_level_id = level.get('levelId')
    require(source_level_id is None or (text(source_level_id)
            and source_level_id.casefold() == identity['level_id'].casefold()),
            'Level source disagrees with stage identity.')
    require(isinstance(sources, dict) and all(isinstance(sources.get(k), dict)
            and isinstance(sources[k].get('sha256'), str)
            and re.fullmatch('[0-9a-f]{64}', sources[k]['sha256'])
            for k in ('level', 'enemy_database')), 'Missing raw source SHA-256 evidence.')
    # Also catches non-finite values anywhere, including unmodeled mechanics.
    decode(canonical_json(level))
    decode(canonical_json(enemy_database))
    map_data = level.get('mapData')
    require(isinstance(map_data, dict), 'Missing mapData.')
    grid = _list(map_data.get('map'), 'map grid')
    require(bool(grid) and isinstance(grid[0], list) and bool(grid[0]), 'Empty map grid.')
    rows, cols = len(grid), len(grid[0])
    require(rows <= 128 and cols <= 128, 'Map dimensions too large.')
    definitions = _list(map_data.get('tiles'), 'map tiles')
    tiles, unknown = [], set()
    normal_tiles = {'tile_forbidden', 'tile_floor', 'tile_road', 'tile_wall', 'tile_start', 'tile_end'}
    for y, row in enumerate(grid):
        require(isinstance(row, list) and len(row) == cols, 'Nonrectangular map grid.')
        for x, number in enumerate(row):
            require(type(number) is int and 0 <= number < len(definitions), 'Map tile reference missing.')
            tile = definitions[number]
            require(isinstance(tile, dict) and text(tile.get('tileKey'))
                    and text(tile.get('heightType')) and text(tile.get('passableMask')),
                    'Incomplete map tile definition.')
            buildable = tile.get('buildableType')
            require(buildable in ('NONE', 'MELEE', 'RANGED', 'ALL'), 'Unknown tile buildable type.')
            tiles.append({'x': x, 'y': y, 'game_row': rows - 1 - y, 'game_col': x,
                          'tile_key': tile['tileKey'], 'buildable': buildable,
                          'height': tile['heightType'], 'passable_mask': tile['passableMask'],
                          'raw': copy.deepcopy(tile)})
            if tile['tileKey'] not in normal_tiles:
                unknown.add('tile:' + tile['tileKey'])
            if tile.get('effects') or tile.get('blackboard'):
                unknown.add('tile-effects')
    routes = []
    for i, route in enumerate(_list(level.get('routes'), 'routes')):
        require(isinstance(route, dict) and text(route.get('motionMode')), 'Invalid route.')
        checkpoints = []
        require('checkpoints' in route, 'Missing route checkpoints.')
        for cp in _list([] if route['checkpoints'] is None else route['checkpoints'], 'route checkpoints'):
            require(isinstance(cp, dict) and text(cp.get('type')), 'Invalid checkpoint.')
            # WAIT positions are placeholder coordinates, never path constraints.
            position = (_point(cp.get('position'), rows, cols)
                        if cp['type'] in ('MOVE', 'APPEAR_AT_POS') else None)
            checkpoints.append({'type': cp['type'], 'position': position,
                                'time': _number(cp.get('time'), 'checkpoint time', minimum=0),
                                'raw': copy.deepcopy(cp)})
            if cp['type'] not in ('MOVE', 'WAIT_FOR_SECONDS'):
                unknown.add('checkpoint:' + cp['type'])
        routes.append({'id': i, 'index': i, 'start': _point(route.get('startPosition'), rows, cols),
                       'end': _point(route.get('endPosition'), rows, cols),
                       'motion_mode': route['motionMode'], 'checkpoints': checkpoints,
                       'raw': copy.deepcopy(route)})
        if route['motionMode'] not in ('WALK', 'FLY'):
            unknown.add('route-motion:' + route['motionMode'])
    enemies = _enemies(level, enemy_database)
    waves = _list(level.get('waves'), 'waves')
    spawns, other_actions = [], []
    for wi, wave in enumerate(waves):
        require(isinstance(wave, dict), 'Invalid wave.')
        _number(wave.get('preDelay'), 'wave preDelay', minimum=0)
        _number(wave.get('postDelay'), 'wave postDelay', minimum=0)
        _number(wave.get('maxTimeWaitingForNextWave'), 'wave waiting limit')
        for fi, fragment in enumerate(_list(wave.get('fragments'), 'wave fragments')):
            require(isinstance(fragment, dict), 'Invalid wave fragment.')
            _number(fragment.get('preDelay'), 'fragment preDelay', minimum=0)
            for ai, action in enumerate(_list(fragment.get('actions'), 'fragment actions')):
                require(isinstance(action, dict) and text(action.get('actionType')), 'Invalid action.')
                location = {'wave': wi, 'fragment': fi, 'action': ai}
                if action['actionType'] != 'SPAWN':
                    other_actions.append({**location, 'raw': copy.deepcopy(action)})
                    unknown.add('action:' + action['actionType'])
                    continue
                key, route_index, count = action.get('key'), action.get('routeIndex'), action.get('count')
                require(key in enemies, f'Spawn enemy reference missing: {key}.')
                require(type(route_index) is int and 0 <= route_index < len(routes),
                        'Spawn route reference missing.')
                require(type(count) is int and 0 < count <= 100000, 'Invalid spawn count.')
                require(type(action.get('managedByScheduler')) is bool, 'Missing spawn scheduler flag.')
                pre_delay = _number(action.get('preDelay'), 'action preDelay', minimum=0)
                interval = _number(action.get('interval'), 'spawn interval', minimum=0)
                spawns.append({**location, 'enemy_id': key, 'route_index': route_index,
                               'count': count, 'pre_delay': pre_delay, 'interval': interval,
                               'timing': 'event-relative', 'raw': copy.deepcopy(action)})
                if action.get('randomType') != 'ALWAYS' or action.get('refreshType') != 'ALWAYS':
                    unknown.add('random-or-conditional-spawn')
                if not action['managedByScheduler']:
                    unknown.add('unscheduled-spawn')
    options = level.get('options')
    require(isinstance(options, dict), 'Missing battle options.')
    for field in ('characterLimit', 'initialCost', 'maxCost'):
        require(type(options.get(field)) is int and options[field] >= 0, f'Missing or invalid {field}.')
    _number(options.get('costIncreaseTime'), 'costIncreaseTime', minimum=0)
    mechanics = {k: copy.deepcopy(level.get(k)) for k in (
        'runes', 'optionalRunes', 'globalBuffs', 'branches', 'extraRoutes', 'predefines',
        'hardPredefines', 'tilesDisallowToLocate', 'operaConfig', 'cameraPlugin', 'randomSeed')}
    mechanics['map_extras'] = {k: copy.deepcopy(v) for k, v in map_data.items() if k not in ('map', 'tiles')}
    for key in ('runes', 'optionalRunes', 'globalBuffs', 'branches', 'extraRoutes', 'tilesDisallowToLocate'):
        if mechanics[key]:
            unknown.add('level:' + key)
    for key in ('blockEdges', 'effects', 'layerRects'):
        if map_data.get(key):
            unknown.add('map:' + key)
    if any(enemy['skills'] or enemy['talent_blackboard'] for enemy in enemies.values()):
        unknown.add('enemy-skills-or-talents')
    for enemy in enemies.values():
        missing = sorted(set(('maxHp', 'atk', 'def', 'magicResistance', 'moveSpeed', 'baseAttackTime'))
                         - enemy['attributes'].keys())
        if missing:
            unknown.add('enemy-attributes:' + enemy['id'] + ':' + ','.join(missing))
    mechanics['unknown'] = sorted(unknown)
    return {'schema': SCHEMA, 'stage': copy.deepcopy(identity), 'sources': copy.deepcopy(sources),
            'map': {'rows': rows, 'cols': cols, 'coordinate_system': 'top-left', 'tiles': tiles},
            'routes': routes, 'enemies': enemies, 'options': copy.deepcopy(options),
            'waves': copy.deepcopy(waves), 'spawns': spawns, 'other_actions': other_actions,
            'mechanics': mechanics, 'limitations': list(LIMITATIONS), 'victory_proven': False}


def _source(directory: Path, name: str, url: str, *, refresh=False, opener=None):
    path, receipt = directory / name, directory / (name + '.source.json')
    if not refresh and path.exists() and receipt.exists():
        saved = decode(receipt.read_bytes())
        raw = path.read_bytes()
        require(isinstance(saved, dict) and saved.get('url') == url
                and saved.get('sha256') == sha256_bytes(raw), 'Battle source cache integrity failure.')
        require(len(raw) <= MAX_BYTES, 'Battle source cache too large.')
        return decode(raw), saved
    opener = opener or urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'ZOOTd-battle-data/1'})
    try:
        with opener.open(request, timeout=40) as response:
            raw = response.read(MAX_BYTES + 1)
    except OSError:
        raise PrtsError('battle_data_network', 'Cannot fetch pinned public battle data.') from None
    require(len(raw) <= MAX_BYTES, 'Battle source too large.')
    data = decode(raw)
    evidence = {'url': url, 'sha256': sha256_bytes(raw), 'fetched_at': time.time()}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep exact received bytes; hashes never substitute canonicalized JSON.
    atomic_write_bytes(path, raw, mode=0o600)
    atomic_write_json(receipt, evidence, mode=0o600)
    return data, evidence


def fetch_battle(root: Path, stage: str, *, refresh=False, opener=None) -> dict:
    """Fetch immutable level/enemy sources from the navigation snapshot's revision."""
    tables, navigation = load_tables(root)
    revision = navigation.get('revision')
    require(isinstance(revision, str) and re.fullmatch('[0-9a-f]{40}', revision),
            'Invalid battle source revision.')
    # The navigation loader follows Core's promoted resource/overlay precedence.
    # Refuse a concurrent table refresh rather than mixing two revisions.
    catalog = load_navigation(root)
    require(all(catalog.evidence.get(key) == navigation.get(key) for key in ('revision', 'sha256')),
            'Navigation snapshot changed during battle data fetch; retry explicitly.')
    navigation = catalog.evidence
    route = catalog.route(stage)
    row = next((table.get(route['battle_id']) for table in
                (tables['stage_table']['stages'], tables['retro_table']['stageList'])
                if table.get(route['battle_id'], {}).get('zoneId') == route['zone_id']), None)
    require(isinstance(row, dict) and text(row.get('levelId')), 'Stage level identity missing.')
    level_id = row['levelId']
    require(re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+', level_id), 'Unsafe level source path.')
    require(re.fullmatch(r'[A-Za-z0-9_-]+', route['battle_id']), 'Unsafe battle cache identity.')
    base = f'https://raw.githubusercontent.com/{REPOSITORY}/{revision}/zh_CN/gamedata/levels/'
    directory = root / 'var/cache/battle-data' / revision
    level, level_source = _source(directory / route['battle_id'], 'level.json',
                                 base + level_id.lower() + '.json', refresh=refresh, opener=opener)
    enemies, enemy_source = _source(directory, 'enemy_database.json',
                                   base + 'enemydata/enemy_database.json', refresh=refresh, opener=opener)
    identity = {k: route[k] for k in ('stage_id', 'battle_id', 'code')}
    identity.update(level_id=level_id, difficulty='NORMAL')
    graph = parse_battle(level, enemies, identity=identity,
                         sources={'navigation': navigation, 'level': level_source,
                                  'enemy_database': enemy_source})
    atomic_write_json(directory / route['battle_id'] / 'graph.json', graph, mode=0o600)
    return graph


def analyze_battle(graph: dict) -> dict:
    require(isinstance(graph, dict) and graph.get('schema') == SCHEMA
            and isinstance(graph.get('spawns'), list), 'Invalid battle graph.')
    counts, routes = Counter(), Counter()
    counted = 0
    for spawn in graph['spawns']:
        counts[spawn['enemy_id']] += spawn['count']
        routes[spawn['route_index']] += spawn['count']
        if graph['enemies'][spawn['enemy_id']]['not_count_in_total'] is False:
            counted += spawn['count']
    return {'schema': SCHEMA, 'stage': graph['stage'], 'sources': graph['sources'],
            'dimensions': {'rows': graph['map']['rows'], 'cols': graph['map']['cols']},
            'coordinate_system': 'top-left; MAA location=[x,y]', 'options': graph['options'],
            'wave_count': len(graph['waves']), 'spawn_count': sum(counts.values()),
            'explicit_counted_spawns': counted, 'enemy_counts': dict(sorted(counts.items())),
            'route_spawn_counts': dict(sorted(routes.items())),
            'unknown_mechanics': graph['mechanics']['unknown'],
            'limitations': graph['limitations'], 'victory_proven': False}


def render_ascii(graph: dict) -> str:
    summary = analyze_battle(graph)
    rows, cols = graph['map']['rows'], graph['map']['cols']
    grid = [['?' for _ in range(cols)] for _ in range(rows)]
    for tile in graph['map']['tiles']:
        symbol = {'MELEE': '.', 'RANGED': '^', 'ALL': '+', 'NONE': '#'}[tile['buildable']]
        if tile['tile_key'] == 'tile_start':
            symbol = 'S'
        elif tile['tile_key'] == 'tile_end':
            symbol = 'E'
        elif tile['tile_key'] == 'tile_floor':
            symbol = '_'
        grid[tile['y']][tile['x']] = symbol
    lines = [f"{graph['stage']['code']} ({graph['stage']['stage_id']})",
             'Top-left coordinates: x increases right, y increases down; MAA location=[x,y].',
             '    ' + ' '.join(str(x) for x in range(cols))]
    lines.extend(f'{y:>3} ' + ' '.join(row) for y, row in enumerate(grid))
    lines.append('S spawn, E goal, . melee, ^ ranged, + either, _ unbuildable floor, # unbuildable')
    lines.append(f"Waves: {summary['wave_count']}; spawn instances: {summary['spawn_count']}")
    for key, count in summary['enemy_counts'].items():
        enemy = graph['enemies'][key]
        lines.append(f"  {key}: {enemy['name']} x{count}; attributes={enemy['attributes']}")
    for route in graph['routes']:
        if route['index'] not in summary['route_spawn_counts']:
            continue
        checkpoints = [f"{cp['type']}:{cp['position'] if cp['position'] is not None else cp['time']}"
                       for cp in route['checkpoints']]
        lines.append(f"  route {route['index']}: {route['start']} -> "
                     + ' -> '.join(checkpoints) + f" -> {route['end']}")
    lines.append('Unmodeled mechanics: ' + ', '.join(summary['unknown_mechanics']))
    lines.extend('LIMITATION: ' + item for item in graph['limitations'])
    return '\n'.join(lines)
