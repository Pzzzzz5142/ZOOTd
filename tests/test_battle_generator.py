"""Synthetic tactical search tests; no Box, network, device or private plans."""
import copy
import unittest
from unittest.mock import patch

from maa_planner.battle_generator import active_spawns, blocker_healing_need, generate_plan, terminal_paths
from maa_planner.battle_solver import BattleSolverError
from maa_planner.copilot_matcher import OperatorCatalog, OperatorIdentity
from maa_planner.operator_box import Operator, OperatorBox, SkillProgress
from maa_planner.util import canonical_json, sha256_bytes


def problem():
    tiles = [{'x': x, 'y': y, 'buildable': 'MELEE' if y == 1 else 'RANGED',
              'tile_key': 'tile_road' if y == 1 else 'tile_wall',
              'passable_mask': 'ALL' if y == 1 else 'FLY_ONLY'}
             for y in range(3) for x in range(6)]
    return {'schema': 1, 'stage': {'stage_id': 'synthetic', 'code': 'T-EX-1',
             'battle_id': 'synthetic', 'difficulty': 'NORMAL'},
            'map': {'rows': 3, 'cols': 6, 'coordinate_system': 'top-left', 'tiles': tiles},
            'routes': [{'index': 0, 'motion_mode': 'WALK', 'start': [0, 1],
                        'end': [5, 1], 'checkpoints': []}],
            'enemies': {'enemy': {'id': 'enemy', 'name': 'Synthetic enemy',
                         'attributes': {'maxHp': 1000, 'def': 0, 'magicResistance': 0}}},
            'spawns': [{'route_index': 0, 'count': 5, 'enemy_id': 'enemy', 'raw': {}}],
            'options': {'characterLimit': 3}, 'mechanics': {'unknown': [], 'predefines': {}}}


def profile(name, role, offsets, *, ground_block=True):
    healing = role == 'healer'
    return {'oper': {'name': name, 'skill': 1, 'skill_usage': 1},
            'cost': 10, 'position_type': 'MELEE' if role == 'blocker' else 'RANGED',
            'normal_offsets': offsets, 'skill_offsets': offsets,
            'row': {'behavior': {'damage_type': 'healing' if healing else 'physical',
                                 'mode': 'duration', 'ground_block': ground_block},
                    'attributes': {'maxHp': [2000, 2000], 'def': [400, 400]},
                    'block_count_normal': 2 if role == 'blocker' else 0,
                    'block_count_skill': 2 if role == 'blocker' else 0,
                    'self_heal_per_second_skill': 0, 'attack_interval_normal': 1,
                    'healing_per_second_skill_per_target': 200, 'healing_target_limit_skill': 2,
                    'matchups': [] if healing else [{'enemy_id': 'enemy',
                        'duel': {'damage': 1000, 'kill_time': 5}, 'rotation': {'average_dps': 200}}]}}


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.graph = problem()
        names = ('Guard', 'Caster', 'Medic', 'Special')
        self.catalog = OperatorCatalog([OperatorIdentity(n, n, {1: n + '_s1'}) for n in names])
        self.box = OperatorBox('fixture', '2026-10-18', 'private-fixture', {
            n: Operator(n, 2, 70, 1, 7, {n + '_s1': SkillProgress(0)}, {})
            for n in names})
        self.chars = {n: {'name': n, 'position': 'MELEE' if n in ('Guard', 'Special') else 'RANGED'}
                      for n in names}
        self.profiles = [profile('Guard', 'blocker', [[0, 0], [1, 0]]),
                         profile('Caster', 'damage', [[0, 0], [1, -1], [1, 0], [1, 1], [2, 0]]),
                         profile('Medic', 'healer', [[x, y] for x in range(3) for y in range(-1, 2)])]

    def generate(self, **changes):
        research = {'schema': 1, 'graph_sha256': sha256_bytes(canonical_json(self.graph)),
                    'mechanics': {}, 'sources': ['synthetic fixture']}
        with patch('maa_planner.battle_generator._profiles', return_value=(self.profiles, [])), \
                patch('maa_planner.battle_generator.evaluate_plan', return_value={'proof_status': 'unproven'}):
            return generate_plan(self.graph, self.box, self.catalog, self.chars, {}, {},
                                 research=changes.pop('research', research), **changes)

    def test_generates_deterministic_legal_distinct_roles_and_never_proves_clear(self):
        a, b = self.generate(), self.generate()
        self.assertEqual(a, b)
        self.assertEqual(a['generation']['proof_status'], 'unproven')
        self.assertEqual({r['role'] for r in a['generation']['choices']}, {'blocker', 'damage', 'healer'})
        self.assertEqual(len({tuple(r['location']) for r in a['generation']['choices']}), 3)
        self.assertNotIn('private-fixture', str(a))
        self.assertFalse(any('elapsed_time' in a for a in a['copilot']['actions']))

    def test_healer_must_cover_others_with_normal_range(self):
        self.profiles[-1]['normal_offsets'] = [[0, 0]]
        with self.assertRaisesRegex(BattleSolverError, 'coverage'):
            self.generate()

    def test_new_enemy_mechanic_or_changed_research_stops_generation(self):
        with self.assertRaisesRegex(BattleSolverError, 'exact graph'):
            self.generate(research={'schema': 1, 'graph_sha256': 'old'})
        self.graph['enemies']['enemy']['skills'] = [{'new_skill': True}]
        with self.assertRaisesRegex(BattleSolverError, 'enemy adapters'):
            self.generate()

    def test_normal_hidden_groups_follow_exact_difficulty_not_all_spawns(self):
        self.graph['mechanics']['runes'] = [
            {'key': 'level_hidden_group_enable', 'difficultyMask': 'FOUR_STAR',
             'blackboard': [{'key': 'key', 'valueStr': 'raid'}]},
            {'key': 'level_hidden_group_enable', 'difficultyMask': 'NORMAL',
             'blackboard': [{'key': 'key', 'valueStr': 'normal'}]}]
        self.graph['spawns'] = [dict(self.graph['spawns'][0], raw={'hiddenGroup': g})
                                for g in (None, 'normal', 'raid')]
        self.assertEqual([s['raw']['hiddenGroup'] for s in active_spawns(self.graph)], [None, 'normal'])

    def test_unknown_rune_random_group_and_forbidden_tiles_are_not_acknowledged_away(self):
        for key, value in [('runes', [{'key': 'enemy_attribute_mul', 'difficultyMask': 'NORMAL'}]),
                           ('tilesDisallowToLocate', [[1, 1]]), ('branches', ['unknown'])]:
            with self.subTest(key=key):
                g = copy.deepcopy(self.graph)
                self.graph['mechanics'][key] = value
                with self.assertRaises(BattleSolverError):
                    self.generate()
                self.graph = g
        self.graph['spawns'][0]['raw']['randomType'] = 'UNKNOWN_FUTURE_RANDOM'
        with self.assertRaisesRegex(BattleSolverError, 'Random spawn'):
            active_spawns(self.graph)

    def test_teleport_gap_is_never_walked_for_coverage(self):
        route = self.graph['routes'][0]
        route.update(start=[0, 1], checkpoints=[{'type': 'DISAPPEAR'},
                     {'type': 'APPEAR_AT_POS', 'position': [3, 1]}])
        path = terminal_paths(self.graph, self.graph['spawns'])[0]
        self.assertNotIn((2, 1), path['cells'])
        self.assertEqual(path['terminal'], [(3, 1), (4, 1)])

    def test_regeneration_shortfall_requires_healing_only_on_intercepted_enemy_routes(self):
        guard = self.profiles[0]
        guard['row']['self_heal_per_second_skill'] = 192
        guard['row']['matchups'][0]['max_stacks_incoming_physical_dps'] = 430
        paths = terminal_paths(self.graph, self.graph['spawns'])
        need = blocker_healing_need(guard, (3, 1), paths, self.graph['spawns'])
        self.assertTrue(need['required'])
        self.assertEqual(need['modeled_peak_physical_dps'], 430)
        # An enemy on a different lane must not mandate healing this blocker.
        self.assertFalse(blocker_healing_need(guard, (3, 0), paths, self.graph['spawns'])['required'])
        guard['row']['self_heal_per_second_skill'] = 500
        self.assertFalse(blocker_healing_need(guard, (3, 1), paths, self.graph['spawns'])['required'])

    def test_melee_damage_without_ground_block_is_not_a_blocker(self):
        self.graph['options']['characterLimit'] = 4
        special = profile('Special', 'damage', [[0, 0], [1, 0], [2, 0], [1, -1], [1, 1]],
                          ground_block=False)
        special['position_type'] = 'MELEE'
        special['row']['block_count_normal'] = 2
        special['row']['block_count_skill'] = 2
        special['row']['matchups'][0]['duel'] = {'damage': 100000, 'kill_time': 1}
        special['row']['matchups'][0]['rotation']['average_dps'] = 100000
        self.profiles.append(special)
        result = self.generate()
        choices = {row['name']: row['role'] for row in result['generation']['choices']}
        self.assertEqual(choices.get('Special'), 'damage')
        self.assertNotEqual(choices.get('Special'), 'blocker')

    def test_dp_facing_prefers_inward_room_when_tactical_scores_tie(self):
        self.graph['options']['characterLimit'] = 4
        dp = profile('Special', 'blocker', [[0, 0], [1, 0]])
        dp['row']['behavior']['mode'] = 'cost'
        self.profiles.append(dp)
        result = self.generate()
        choice = next(c for c in result['generation']['choices'] if c['role'] == 'dp')
        self.assertEqual(choice['location'], [5, 1])
        self.assertEqual(choice['direction'], 'Left')


if __name__ == '__main__':
    unittest.main()
