import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from maa_planner.battle_cli import main
from maa_planner.battle_data import _source, analyze_battle, fetch_battle, parse_battle, render_ascii
from maa_planner.prts import PrtsError
from maa_planner.util import canonical_json, sha256_bytes


def defined(value, flag=True):
    return {'m_defined': flag, 'm_value': value}


def fixture():
    tiles = [
        {'tileKey': 'tile_start', 'buildableType': 'NONE', 'heightType': 'LOWLAND', 'passableMask': 'ALL'},
        {'tileKey': 'tile_road', 'buildableType': 'MELEE', 'heightType': 'LOWLAND', 'passableMask': 'ALL'},
        {'tileKey': 'tile_end', 'buildableType': 'NONE', 'heightType': 'LOWLAND', 'passableMask': 'ALL'},
        {'tileKey': 'tile_wall', 'buildableType': 'RANGED', 'heightType': 'HIGHLAND', 'passableMask': 'FLY_ONLY'},
    ]
    enemy = {'name': defined('Synthetic enemy'), 'notCountInTotal': defined(False),
             'attributes': {key: defined(value) for key, value in {
                 'maxHp': 1000, 'atk': 100, 'def': 10, 'magicResistance': 0,
                 'moveSpeed': 1, 'baseAttackTime': 2}.items()},
             'skills': [{'prefabKey': 'synthetic_skill'}], 'talentBlackboard': None}
    database = {'enemies': [{'Key': 'enemy_a', 'Value': [
        {'level': 0, 'enemyData': enemy},
        {'level': 1, 'enemyData': {'attributes': {'maxHp': defined(2000), 'atk': defined(0, False)}}},
    ]}]}
    level = {
        'levelId': None, 'options': {'characterLimit': 3, 'initialCost': 10, 'maxCost': 99,
                                    'costIncreaseTime': 1},
        'mapData': {'map': [[0, 1, 2], [3, 1, 1]], 'tiles': tiles},
        'routes': [{'motionMode': 'WALK', 'startPosition': {'row': 1, 'col': 0},
                    'endPosition': {'row': 1, 'col': 2}, 'checkpoints': [
                        {'type': 'MOVE', 'time': 0, 'position': {'row': 1, 'col': 1}},
                        {'type': 'WAIT_FOR_SECONDS', 'time': 5, 'position': {'row': 999, 'col': 999}},
                    ]}],
        'enemies': [], 'enemyDbRefs': [{'useDb': True, 'id': 'enemy_a', 'level': 1,
                                      'overwrittenData': {'attributes': {'def': defined(30)}}}],
        'waves': [{'preDelay': 2, 'postDelay': 3, 'maxTimeWaitingForNextWave': -1,
                   'fragments': [{'preDelay': 5, 'actions': [
                       {'actionType': 'SPAWN', 'key': 'enemy_a', 'routeIndex': 0, 'count': 3,
                        'preDelay': 10, 'interval': 7, 'managedByScheduler': True,
                        'blockFragment': True, 'dontBlockWave': False,
                        'randomType': 'ALWAYS', 'refreshType': 'ALWAYS'},
                       {'actionType': 'STORY', 'key': 'synthetic-story'},
                   ]}]}],
        'runes': [{'key': 'synthetic-environment'}], 'predefines': {'tokenInsts': []},
    }
    identity = {'stage_id': 'synthetic_01', 'battle_id': 'synthetic_01', 'code': 'XX-1',
                'level_id': 'Activities/synthetic/level_synthetic_01', 'difficulty': 'NORMAL'}
    sources = {'level': {'sha256': sha256_bytes(canonical_json(level))},
               'enemy_database': {'sha256': sha256_bytes(canonical_json(database))}}
    return level, database, identity, sources


class BattleDataTests(unittest.TestCase):
    def graph(self):
        level, database, identity, sources = fixture()
        return parse_battle(level, database, identity=identity, sources=sources)

    def test_bottom_origin_routes_convert_to_top_origin_without_flipping_grid(self):
        graph = self.graph()
        self.assertEqual(graph['map']['tiles'][0]['tile_key'], 'tile_start')
        self.assertEqual(graph['map']['tiles'][0]['game_row'], 1)
        self.assertEqual(graph['routes'][0]['start'], [0, 0])
        self.assertEqual(graph['routes'][0]['end'], [2, 0])
        self.assertEqual(graph['routes'][0]['checkpoints'][0]['position'], [1, 0])
        self.assertIsNone(graph['routes'][0]['checkpoints'][1]['position'])

    def test_levels_and_overrides_inherit_only_defined_values(self):
        graph = self.graph()
        attributes = graph['enemies']['enemy_a']['attributes']
        self.assertEqual(attributes['maxHp'], 2000)
        self.assertEqual(attributes['atk'], 100)
        self.assertEqual(attributes['def'], 30)
        self.assertEqual(len(graph['enemies']['enemy_a']['raw']['database_levels']), 2)

    def test_local_enemy_requires_own_complete_definition(self):
        level, database, identity, sources = fixture()
        local = copy.deepcopy(database['enemies'][0]['Value'][0]['enemyData'])
        level['enemyDbRefs'] = [{'id': 'enemy_a', 'level': 0, 'useDb': False, 'overwrittenData': local}]
        graph = parse_battle(level, {'enemies': []}, identity=identity, sources=sources)
        self.assertEqual(graph['enemies']['enemy_a']['name'], 'Synthetic enemy')
        level['enemyDbRefs'][0]['overwrittenData'] = None
        with self.assertRaises(PrtsError):
            parse_battle(level, database, identity=identity, sources=sources)

    def test_spawn_timing_remains_event_relative_and_raw_flags_survive(self):
        graph = self.graph()
        spawn = graph['spawns'][0]
        self.assertEqual(spawn['timing'], 'event-relative')
        self.assertEqual((spawn['pre_delay'], spawn['interval']), (10, 7))
        self.assertTrue(spawn['raw']['blockFragment'])
        self.assertNotIn('absolute_time', spawn)
        self.assertEqual(graph['waves'][0]['fragments'][0]['preDelay'], 5)
        self.assertEqual(graph['other_actions'][0]['raw']['actionType'], 'STORY')
        self.assertFalse(graph['victory_proven'])

    def test_unknowns_are_explicit_and_summary_keeps_lower_bound_count(self):
        graph = self.graph()
        self.assertIn('level:runes', graph['mechanics']['unknown'])
        self.assertIn('action:STORY', graph['mechanics']['unknown'])
        self.assertIn('enemy-skills-or-talents', graph['mechanics']['unknown'])
        result = analyze_battle(graph)
        self.assertEqual(result['spawn_count'], 3)
        self.assertEqual(result['explicit_counted_spawns'], 3)
        graph['enemies']['enemy_a']['not_count_in_total'] = None
        self.assertEqual(analyze_battle(graph)['explicit_counted_spawns'], 0)
        self.assertIn('MAA location=[x,y]', render_ascii(graph))

    def test_missing_references_invalid_coordinates_and_invalid_numbers_fail_closed(self):
        for mutate in (
            lambda l, d: l['mapData']['map'][0].__setitem__(0, 99),
            lambda l, d: l['mapData']['map'][0].pop(),
            lambda l, d: l['routes'][0]['startPosition'].__setitem__('row', 2),
            lambda l, d: l['waves'][0]['fragments'][0]['actions'][0].__setitem__('key', 'missing'),
            lambda l, d: l['waves'][0]['fragments'][0]['actions'][0].__setitem__('routeIndex', 8),
            lambda l, d: l['waves'][0]['fragments'][0]['actions'][0].__setitem__('count', True),
            lambda l, d: l['waves'][0]['fragments'][0]['actions'][0].__setitem__('interval', float('nan')),
            lambda l, d: d['enemies'][0]['Value'].pop(),
            lambda l, d: d['enemies'].clear(),
            lambda l, d: l.pop('enemyDbRefs'),
            lambda l, d: l['options'].pop('initialCost'),
        ):
            with self.subTest(mutate=mutate):
                level, database, identity, sources = fixture()
                mutate(level, database)
                with self.assertRaises(PrtsError):
                    parse_battle(level, database, identity=identity, sources=sources)

    def test_identity_difficulty_and_evidence_are_required(self):
        for kind in ('identity', 'difficulty', 'evidence', 'level-disagreement'):
            with self.subTest(kind=kind):
                level, database, identity, sources = fixture()
                if kind == 'identity': identity.pop('level_id')
                if kind == 'difficulty': identity['difficulty'] = 'FOUR_STAR'
                if kind == 'evidence': sources['level']['sha256'] = 'not-a-hash'
                if kind == 'level-disagreement': level['levelId'] = 'other-level'
                with self.assertRaises(PrtsError):
                    parse_battle(level, database, identity=identity, sources=sources)

    def test_null_checkpoints_are_an_explicit_empty_route(self):
        level, database, identity, sources = fixture()
        level['routes'][0]['checkpoints'] = None
        self.assertEqual(parse_battle(level, database, identity=identity, sources=sources)
                         ['routes'][0]['checkpoints'], [])
        level['routes'][0].pop('checkpoints')
        with self.assertRaises(PrtsError):
            parse_battle(level, database, identity=identity, sources=sources)

    def test_source_cache_hashes_exact_bytes_and_refuses_tampering(self):
        raw = b'{ "enemies" : [] }\n'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            opener = unittest.mock.Mock()
            opener.open.return_value = io.BytesIO(raw)
            data, source = _source(directory, 'test.json', 'https://example.test/pinned', opener=opener)
            self.assertEqual(data, {'enemies': []})
            self.assertEqual(source['sha256'], sha256_bytes(raw))
            self.assertNotEqual(source['sha256'], sha256_bytes(canonical_json(data)))
            _source(directory, 'test.json', source['url'], opener=opener)
            self.assertEqual(opener.open.call_count, 1)
            (directory / 'test.json').write_text('{"enemies": [1]}')
            with self.assertRaises(PrtsError):
                _source(directory, 'test.json', source['url'], opener=opener)

    def test_fetch_uses_one_pinned_revision_for_both_sources(self):
        level, database, identity, sources = fixture()
        revision = 'a' * 40
        tables = {'stage_table': {'stages': {'synthetic_01': {
            'levelId': identity['level_id'], 'zoneId': 'synthetic-zone'}}},
            'retro_table': {'stageList': {}}}
        navigation = {'revision': revision, 'sha256': 'b' * 64}
        route = {'stage_id': 'synthetic_01', 'battle_id': 'synthetic_01', 'code': 'XX-1',
                 'zone_id': 'synthetic-zone'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resource = root / 'var/data/resource'
            (resource / 'Arknights-Tile-Pos').mkdir(parents=True)
            (resource / 'stages.json').write_text('[]')
            (resource / 'Arknights-Tile-Pos/overview.json').write_text('{}')
            with patch('maa_planner.battle_data.load_tables', return_value=(tables, navigation)), \
                    patch('maa_planner.battle_data.load_navigation') as load_navigation, \
                    patch('maa_planner.battle_data._source') as source:
                load_navigation.return_value.route.return_value = route
                load_navigation.return_value.evidence = navigation
                source.side_effect = [(level, sources['level']), (database, sources['enemy_database'])]
                graph = fetch_battle(root, 'XX-1')
                urls = [call.args[2] for call in source.call_args_list]
                self.assertTrue(all('/' + revision + '/' in url for url in urls))
                self.assertTrue(urls[0].endswith('/activities/synthetic/level_synthetic_01.json'))
                self.assertTrue(urls[1].endswith('/enemydata/enemy_database.json'))
                self.assertEqual(graph['sources']['navigation']['revision'], revision)
                self.assertTrue((root / 'var/cache/battle-data' / revision /
                                 'synthetic_01/graph.json').exists())

    def test_fetch_refuses_navigation_snapshot_race_before_network_access(self):
        with patch('maa_planner.battle_data.load_tables', return_value=({}, {
                'revision': 'a' * 40, 'sha256': 'b' * 64})), \
                patch('maa_planner.battle_data.load_navigation') as navigation, \
                patch('maa_planner.battle_data._source') as source:
            navigation.return_value.evidence = {'revision': 'c' * 40, 'sha256': 'd' * 64}
            with self.assertRaises(PrtsError):
                fetch_battle(Path('/unused'), 'XX-1')
            source.assert_not_called()

    def test_cli_raw_inputs_are_offline_and_missing_input_is_error(self):
        level, database, identity, sources = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            level_path, enemy_path = root / 'level.json', root / 'enemy.json'
            level_path.write_text(json.dumps(level))
            enemy_path.write_text(json.dumps(database))
            args = ['analyze', '--level', str(level_path), '--enemy-database', str(enemy_path),
                    '--stage-id', identity['stage_id'], '--level-id', identity['level_id'],
                    '--code', identity['code']]
            out = io.StringIO()
            with patch('maa_planner.battle_cli.fetch_battle') as fetch, redirect_stdout(out):
                self.assertEqual(main(args), 0)
                fetch.assert_not_called()
            result = json.loads(out.getvalue())
            self.assertEqual(result['spawn_count'], 3)
            self.assertEqual(result['sources']['level']['sha256'], sha256_bytes(level_path.read_bytes()))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(['analyze', '--graph', str(root / 'absent.json')]), 1)


if __name__ == '__main__':
    unittest.main()
