"""Offline legality and refusal tests; no network, device or real account."""
from copy import deepcopy
from dataclasses import replace
import unittest

from maa_planner.battle_solver import (
    BattleSolverError, analyze_candidates, compile_copilot, validate_copilot,
)
from maa_planner.copilot_matcher import OperatorCatalog, OperatorIdentity
from maa_planner.operator_box import Operator, OperatorBox, SkillProgress


def graph():
    tiles = []
    for y in range(3):
        for x in range(5):
            buildable = 'MELEE' if y == 1 and x in (1, 2, 3) else 'RANGED' if y == 0 else 'NONE'
            tiles.append({'x': x, 'y': y, 'buildable': buildable,
                          'tile_key': 'tile_road' if y == 1 else 'tile_wall',
                          'passable_mask': 'ALL' if y == 1 else 'FLY_ONLY'})
    return {'schema': 1,
            'stage': {'stage_id': 'act52side_01', 'battle_id': 'act52side_01',
                      'code': 'YW-1', 'level_id': 'Activities/act52side/level_act52side_01',
                      'difficulty': 'NORMAL'},
            'map': {'rows': 3, 'cols': 5, 'coordinate_system': 'top-left', 'tiles': tiles},
            'routes': [{'index': 0, 'start': [0, 1], 'end': [4, 1],
                        'motion_mode': 'WALK', 'checkpoints': []}],
            'spawns': [{'route_index': 0, 'count': 10}],
            'mechanics': {'unknown': [], 'predefines': {}},
            'options': {'characterLimit': 2}}


class BattleSolverTests(unittest.TestCase):
    def setUp(self):
        self.graph = graph()
        self.catalog = OperatorCatalog([
            OperatorIdentity('guard', 'Guard', {1: 'guard_s1', 2: 'guard_s2'}),
            OperatorIdentity('medic', 'Medic', {1: 'medic_s1'}),
        ])
        self.box = OperatorBox('fixture', '2026-10-09', 'private-fixture', {
            'guard': Operator('guard', 2, 80, 1, 7, {'guard_s1': SkillProgress(3)}, {}),
            'medic': Operator('medic', 1, 50, 1, 7, {'medic_s1': SkillProgress(0)}, {}),
        })
        self.chars = {'guard': {'name': 'Guard', 'position': 'MELEE'},
                      'medic': {'name': 'Medic', 'position': 'RANGED'}}
        self.plan = {'schema_version': 1, 'stage_id': 'act52side_01',
                     'placements': [{'name': 'Guard', 'skill': 1, 'location': [2, 1],
                                     'direction': 'Left', 'requirements': {'elite': 2, 'skill_level': 10}},
                                    {'name': 'Medic', 'skill': 1, 'location': [2, 0],
                                     'direction': 'Down'}]}

    def compile(self, plan=None):
        return compile_copilot(self.graph, plan or self.plan, self.box,
                               self.catalog, character_table=self.chars)

    def validate(self, content, **kwargs):
        return validate_copilot(self.graph, content, self.box, self.catalog,
                                character_table=self.chars, **kwargs)

    def test_compile_is_compatible_but_never_a_victory_proof(self):
        result = self.compile()
        self.assertEqual(result['copilot']['stage_name'], 'act52side_01')
        self.assertEqual(result['copilot']['difficulty'], 1)
        self.assertEqual(result['analysis']['proof_status'], 'unproven')
        self.assertFalse(result['analysis']['combat_simulated'])
        self.assertEqual(result['analysis']['box_validation'], 'verified')
        self.assertEqual(result['analysis']['placement_type_validation'], 'verified')
        self.assertNotIn('private-fixture', str(result))
        self.assertEqual(result, self.compile())

    def test_missing_validation_inputs_are_never_reported_verified(self):
        report = validate_copilot(self.graph, self.compile()['copilot'])
        self.assertEqual(report['box_validation'], 'not_checked')
        self.assertEqual(report['placement_type_validation'], 'not_checked')

    def test_out_of_map_and_wrong_tile_class_are_rejected(self):
        for location in ([5, 1], [2, 0], [0, 1], [True, 1]):
            with self.subTest(location=location):
                plan = deepcopy(self.plan)
                plan['placements'][0]['location'] = location
                with self.assertRaises(BattleSolverError):
                    self.compile(plan)

    def test_occupancy_retires_only_after_a_retreat(self):
        content = self.compile()['copilot']
        content['actions'].append(dict(content['actions'][0]))
        with self.assertRaises(BattleSolverError):
            self.validate(content)
        content['actions'].insert(2, {'type': 'Retreat', 'name': 'Guard'})
        self.validate(content)

    def test_skill_unlock_missing_identity_and_unknown_training_rejected(self):
        for mutation in ('locked_skill', 'unknown_identity', 'unknown_training', 'insufficient_training'):
            with self.subTest(mutation=mutation):
                plan = deepcopy(self.plan)
                box = self.box
                if mutation == 'locked_skill':
                    plan['placements'][1]['skill'] = 2
                elif mutation == 'unknown_identity':
                    plan['placements'][1]['name'] = 'Unknown'
                elif mutation == 'unknown_training':
                    box = replace(box, operators={**box.operators, 'medic': replace(box.operators['medic'], level=None)})
                else:
                    plan['placements'][0]['requirements']['level'] = 90
                with self.assertRaises(BattleSolverError):
                    compile_copilot(self.graph, plan, box, self.catalog, character_table=self.chars)

    def test_elapsed_time_compiler_adds_reset_and_validator_requires_it(self):
        plan = deepcopy(self.plan)
        plan['placements'][1]['time_elapsed'] = 10000
        content = self.compile(plan)['copilot']
        self.assertEqual(content['actions'][0], {'type': 'ResetStopwatch'})
        self.assertEqual(content['actions'][2]['elapsed_time'], 10000)
        self.assertNotIn('time_elapsed', str(content))
        content['actions'].pop(0)
        with self.assertRaises(BattleSolverError):
            self.validate(content)

    def test_native_actions_use_elapsed_time_and_reject_the_non_native_spelling(self):
        plan = deepcopy(self.plan)
        plan['actions'] = [{'type': 'Skill', 'name': 'Guard', 'elapsed_time': 15000, 'timeout': 0}]
        content = self.compile(plan)['copilot']
        self.assertEqual(content['actions'][0], {'type': 'ResetStopwatch'})
        self.assertEqual(content['actions'][-1]['elapsed_time'], 15000)
        self.assertNotIn('time_elapsed', str(content))
        content['actions'][-1]['time_elapsed'] = content['actions'][-1].pop('elapsed_time')
        with self.assertRaises(BattleSolverError):
            self.validate(content)
        plan['placements'][0].update(elapsed_time=1000, time_elapsed=2000)
        with self.assertRaises(BattleSolverError):
            self.compile(plan)

    def test_native_timeout_and_deprecated_skip_combination_is_rejected(self):
        plan = deepcopy(self.plan)
        plan['actions'] = [{'type': 'Skill', 'name': 'Guard', 'timeout': 0, 'skip_if_not_ready': True}]
        with self.assertRaises(BattleSolverError):
            self.compile(plan)

    def test_native_deploy_timeout_is_accepted_but_never_claimed_as_bounded(self):
        for timeout in (-1, 0, 2500):
            with self.subTest(timeout=timeout):
                plan = deepcopy(self.plan)
                plan['placements'][0]['timeout'] = timeout
                result = self.compile(plan)
                self.assertEqual(result['copilot']['actions'][0]['timeout'], timeout)
                self.assertEqual(result['analysis']['non_skill_timeouts_ignored'],
                                 [{'action_index': 0, 'type': 'Deploy', 'timeout': timeout}])
                self.assertEqual(result['analysis']['proof_status'], 'unproven')
                self.assertFalse(result['analysis']['combat_simulated'])
        for invalid in (-2, True, 1.5, '2500'):
            plan = deepcopy(self.plan)
            plan['placements'][0]['timeout'] = invalid
            with self.assertRaises(BattleSolverError):
                self.compile(plan)

    def test_deprecated_skip_is_only_a_skill_compatibility_field(self):
        plan = deepcopy(self.plan)
        plan['actions'] = [{'type': 'Skill', 'name': 'Guard', 'skip_if_not_ready': True}]
        self.compile(plan)
        plan['placements'][0]['skip_if_not_ready'] = True
        with self.assertRaises(BattleSolverError):
            self.compile(plan)

    def test_native_signed_int_boundaries_are_enforced_without_timing_conversion(self):
        maximum, minimum = 2 ** 31 - 1, -(2 ** 31)
        for field in ('kills', 'costs', 'cooling', 'pre_delay', 'post_delay', 'elapsed_time', 'timeout'):
            with self.subTest(field=field):
                plan = deepcopy(self.plan)
                plan['placements'][0][field] = maximum
                content = self.compile(plan)['copilot']
                deploy = next(action for action in content['actions'] if action['type'] == 'Deploy')
                self.assertEqual(deploy[field], maximum)
                for value in (maximum + 1, 2 ** 63):
                    plan['placements'][0][field] = value
                    with self.assertRaises(BattleSolverError):
                        self.compile(plan)
        for value in (minimum, maximum):
            plan = deepcopy(self.plan)
            plan['placements'][0]['cost_changes'] = value
            self.assertEqual(self.compile(plan)['copilot']['actions'][0]['cost_changes'], value)
        for value in (minimum - 1, maximum + 1):
            plan['placements'][0]['cost_changes'] = value
            with self.assertRaises(BattleSolverError):
                self.compile(plan)

    def test_phase_level_limits_reject_impossible_encoded_or_box_training(self):
        self.chars['guard']['phases'] = [{'maxLevel': 50}, {'maxLevel': 80}, {'maxLevel': 90}]
        self.chars['medic']['phases'] = [{'maxLevel': 50}, {'maxLevel': 70}, {'maxLevel': 80}]
        self.assertEqual(self.compile()['analysis']['phase_level_validation'], 'verified')
        plan = deepcopy(self.plan)
        plan['placements'][0]['requirements'] = {'elite': 1, 'level': 85}
        # The actual E2 LV80 Box would not meet LV85 either; make it E2 LV90
        # so refusal specifically protects the impossible encoded E1 LV85.
        self.box = replace(self.box, operators={**self.box.operators,
                                               'guard': replace(self.box.operators['guard'], level=90)})
        with self.assertRaisesRegex(BattleSolverError, 'Encoded level'):
            self.compile(plan)
        self.box = replace(self.box, operators={**self.box.operators,
                                               'medic': replace(self.box.operators['medic'], level=75)})
        with self.assertRaisesRegex(BattleSolverError, 'Box training exceeds'):
            self.compile()

    def test_native_module_requirement_integer_overflow_is_rejected(self):
        content = self.compile()['copilot']
        content['opers'][0]['requirements']['module'] = 2 ** 31
        with self.assertRaises(BattleSolverError):
            validate_copilot(self.graph, content)

    def test_native_parser_rejects_inconsistent_encoded_elite(self):
        plan = deepcopy(self.plan)
        # The local Box is E2 and can use this mastery. Native MAA still refuses
        # an explicitly encoded E0 minimum together with an M3 skill requirement.
        plan['placements'][0]['requirements']['elite'] = 0
        with self.assertRaises(BattleSolverError):
            self.compile(plan)

    def test_unsupported_mechanics_need_exact_acknowledgements(self):
        self.graph['mechanics']['unknown'] = ['tile:tile_alchemy_wall', 'enemy-skills']
        with self.assertRaises(BattleSolverError):
            self.compile()
        plan = deepcopy(self.plan)
        plan['mechanics_acknowledged'] = list(self.graph['mechanics']['unknown'])
        result = self.compile(plan)
        self.assertEqual(result['analysis']['mechanics_acknowledged'], sorted(plan['mechanics_acknowledged']))
        self.assertEqual(result['analysis']['proof_status'], 'unproven')
        plan['mechanics_acknowledged'].append('invented')
        with self.assertRaises(BattleSolverError):
            self.compile(plan)

    def test_stage_difficulty_and_unknown_actions_are_rejected(self):
        for change in ({'stage_name': 'YW-2'}, {'difficulty': 3}, {'groups': [{'name': 'choice'}]}):
            content = self.compile()['copilot']
            content.update(change)
            with self.assertRaises(BattleSolverError):
                self.validate(content)
        for action in ({'type': 'MoveCamera'}, {'type': []}, {'type': 'SpeedUp', 'surprise': 1},
                       {'type': 'Skill', 'name': ['Guard']}, {'type': 'Skill', 'name': 'Unknown'},
                       {'type': 'SpeedUp', 'costs': True}, {'type': 'SpeedUp', 'name': 'Guard'}):
            content = self.compile()['copilot']
            content['actions'].append(action)
            with self.assertRaises(BattleSolverError):
                self.validate(content)

    def test_skill_daemon_prevents_unreachable_actions(self):
        content = self.compile()['copilot']
        content['actions'].append({'type': 'SkillDaemon'})
        self.validate(content)
        content['actions'].append({'type': 'Retreat', 'name': 'Guard'})
        with self.assertRaises(BattleSolverError):
            self.validate(content)

    def test_location_skill_only_targets_known_operators_or_devices(self):
        content = self.compile()['copilot']
        content['actions'].append({'type': 'Skill', 'location': [3, 2]})
        with self.assertRaises(BattleSolverError):
            self.validate(content)
        self.graph['mechanics']['predefines'] = {'tokenInsts': [{'position': {'col': 3, 'row': 0}}]}
        self.validate(content)

    def test_deployment_limit_and_fixed_device_occupancy(self):
        self.graph['options']['characterLimit'] = 1
        with self.assertRaises(BattleSolverError):
            self.compile()
        self.graph['options']['characterLimit'] = 2
        self.graph['mechanics']['predefines'] = {'tokenInsts': [{'position': {'col': 2, 'row': 1}}]}
        with self.assertRaises(BattleSolverError):
            self.compile()

    def test_rankings_use_only_legal_tiles_and_rotate_explicit_ranges(self):
        result = analyze_candidates(self.graph, placement='RANGED', attack_range=[[0, 0], [1, 0]])
        down = next(c for c in result['candidates'] if c['location'] == [2, 0] and c['direction'] == 'Down')
        up = next(c for c in result['candidates'] if c['location'] == [2, 0] and c['direction'] == 'Up')
        self.assertGreater(down['coverage_score'], up['coverage_score'])
        self.assertTrue(all(c['location'][1] == 0 for c in result['candidates']))
        self.assertEqual(result, analyze_candidates(self.graph, placement='RANGED', attack_range=[[0, 0], [1, 0]]))
        self.assertEqual(result['proof_status'], 'unproven')

    def test_unmodeled_routes_are_reported_without_inventing_paths(self):
        self.graph['routes'][0]['checkpoints'] = [{'type': 'APPEAR_AT_POS', 'position': [2, 1]}]
        result = analyze_candidates(self.graph)
        self.assertEqual(result['active_routes'], [])
        self.assertEqual(result['unsupported_routes'][0]['route_index'], 0)
        self.assertTrue(all(c['coverage_score'] == 0 for c in result['candidates']))

    def test_ranking_excludes_blue_endpoint_and_occupied_device_tiles(self):
        result = analyze_candidates(self.graph, limit=100)
        left = next(c for c in result['candidates'] if c['location'] == [3, 1] and c['direction'] == 'Left')
        right = next(c for c in result['candidates'] if c['location'] == [3, 1] and c['direction'] == 'Right')
        self.assertGreater(left['coverage_score'], right['coverage_score'])
        self.graph['mechanics']['predefines'] = {'tokenInsts': [{'position': {'col': 3, 'row': 1}}]}
        result = analyze_candidates(self.graph, limit=100)
        self.assertTrue(all(c['location'] != [3, 1] for c in result['candidates']))


if __name__ == '__main__':
    unittest.main()
