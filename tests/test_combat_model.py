import copy
import math
import unittest
from dataclasses import replace

from maa_planner.battle_solver import BattleSolverError
from maa_planner.combat_model import (attack_interval, blackboard, blocking_queue, damage_packet,
                                     duel, enemy_behavior, operator_attributes, route_segments,
                                     skill_behavior, equipment_effects, evaluate_plan)
from maa_planner.operator_box import Operator, ModuleProgress
from maa_planner.copilot_matcher import OperatorIdentity


def fighter(**changes):
    return dict(atk=100., baseAttackTime=1., attackSpeed=100., spRecoveryPerSec=1., **changes)


def behavior(**changes):
    return dict(damage_type='physical', attack_up=1., attack_scale=1., hits=1, targets=1,
                interval_add=0., speed_add=0., mode='duration', duration=2.,
                sp={'spType': 'INCREASE_WITH_TIME', 'initSp': 0, 'spCost': 2, 'maxChargeTime': 1}, **changes)


def target(hp=1000000):
    return {'attributes': {'maxHp': hp, 'def': 0, 'magicResistance': 0}}


class DamageModelTests(unittest.TestCase):
    def test_challenge_cannot_be_evaluated_with_unmodified_normal_enemy_stats(self):
        with self.assertRaisesRegex(BattleSolverError, 'difficulty runes are not simulated'):
            evaluate_plan({'stage': {'difficulty': 'FOUR_STAR'}}, {}, None, None, {}, {}, {})

    def test_mitigation_applies_per_damage_instance(self):
        self.assertEqual(damage_packet(1000, 'physical', defense=300), 700)
        self.assertEqual(5 * damage_packet(200, 'physical', defense=300), 50)
        self.assertEqual(damage_packet(1000, 'arts', resistance=50), 500)
        self.assertEqual(damage_packet(1000, 'true', defense=300, resistance=100), 1000)

    def test_penetration_minimum_and_post_mitigation_reduction_order(self):
        self.assertEqual(damage_packet(1000, 'physical', defense=1000,
            flat_penetration=200, percent_penetration=.5), 600)
        self.assertEqual(damage_packet(1000, 'arts', resistance=100,
            flat_penetration=20, percent_penetration=.5), 600)
        self.assertAlmostEqual(damage_packet(1000, 'physical', defense=10000, reduction=.8), 10)
        self.assertEqual(damage_packet(1000, 'true', reduction=.5), 500)

    def test_unknown_types_nonfinite_and_invalid_fractions_are_rejected(self):
        for changes in ({'attack': float('nan')}, {'attack': True}, {'reduction': 1.1},
                        {'percent_penetration': -1}, {'damage_type': 'unknown'}):
            args = dict(attack=100, damage_type='physical')
            args.update(changes)
            with self.assertRaises(BattleSolverError):
                damage_packet(**args)
        with self.assertRaises(BattleSolverError):
            blackboard([{'key': 'atk', 'value': 1}, {'key': 'atk', 'value': 2}])

    def test_attack_speed_caps_and_interval_modification(self):
        self.assertEqual(attack_interval(2, 200), 1)
        self.assertEqual(attack_interval(1, 1000), 1 / 6)
        self.assertEqual(attack_interval(1, 0), 5)

    def test_sp_does_not_recharge_during_skill(self):
        result = duel(fighter(), behavior(), target(), {'kind': 'ordinary'}, horizon=7)
        self.assertEqual(result['first_skill_activation'], 2)
        self.assertEqual(result['damage'], 1000)
        self.assertIsNone(result['kill_time'])

    def test_revive_health_is_added_without_overkill_carryover(self):
        profile = behavior()
        profile['sp']['spCost'] = 100
        result = duel(dict(fighter(), atk=200), profile, target(100),
            {'kind': 'reviving', 'revive_ratio': .6, 'revive_delay': 1}, horizon=5)
        self.assertEqual(result['damage'], 160)
        self.assertEqual(result['revives'], 1)
        self.assertEqual(result['kill_time'], 1)

    def test_basins_change_reduction_between_hits_and_overload(self):
        profile = behavior()
        profile.update(mode='permanent', attack_up=0, hits=5)
        profile['sp'].update(initSp=1, spCost=1)
        result = duel(fighter(), profile, target(), {'kind': 'basin', 'initial_stacks': 1,
            'max_stacks': 2, 'hits_per_stack': 2, 'reduction_per_stack': .1, 'stun_duration': 15}, horizon=.1)
        self.assertEqual(result['damage'], 440)
        self.assertEqual(result['damage_instances'], 5)
        self.assertEqual(result['overloads'], 1)
        self.assertEqual(result['final_stacks'], 0)

    def test_attack_recovery_requires_attacks_and_damage_recovery_is_not_invented(self):
        profile = behavior()
        profile['sp']['spType'] = 'INCREASE_WHEN_TAKEN_DAMAGE'
        self.assertIsNone(duel(fighter(), profile, target(), {'kind': 'ordinary'}, horizon=10)['first_skill_activation'])
        profile['sp']['spType'] = 'INCREASE_WHEN_ATTACK'
        self.assertEqual(duel(fighter(), profile, target(), {'kind': 'ordinary'}, horizon=5)['first_skill_activation'], 2)

    def test_attributes_use_actual_level_and_potential_keep_trust_unknown(self):
        char = {'phases': [{'attributesKeyFrames': [
            {'level': 1, 'data': {'atk': 100}}, {'level': 11, 'data': {'atk': 200}}]}],
            'favorKeyFrames': [{'level': 0, 'data': {'atk': 0}}, {'level': 50, 'data': {'atk': 20}}],
            'potentialRanks': [{'type': 'BUFF', 'buff': {'attributes': {'attributeModifiers': [
                {'attributeType': 'ATK', 'formulaItem': 'ADDITION', 'value': 10}]}}}]}
        attrs, unknown = operator_attributes(char, Operator('unit', 0, 6, 2, 7, None, None))
        self.assertEqual(attrs['atk'], [160, 180])
        self.assertTrue(any('trust' in x for x in unknown))
        known, unknown = operator_attributes(char, Operator('unit', 0, 6, 2, 7, None, None, 50))
        self.assertEqual(known['atk'], [170, 170])
        capped, _ = operator_attributes(char, Operator('unit', 0, 6, 2, 7, None, None, 200))
        self.assertEqual(capped['atk'], [180, 180])
        self.assertFalse(any('trust' in x for x in unknown))

    def test_equipment_uses_selected_level_and_potential_not_unlocked_list_order(self):
        identity = OperatorIdentity('unit', 'A', {}, {1: 'module'})
        progress = Operator('unit', 2, 70, 1, 7, None, {'module': ModuleProgress(3, True)}, 100, 'module')
        metadata = {'charEquip': {'unit': ['base', 'module']}, 'equipDict': {
            'base': {'charEquipOrder': 0}, 'module': {'charEquipOrder': 1,
                'unlockEvolvePhase': 'PHASE_2', 'unlockLevel': 60}}}
        def candidate(value, rank=0):
            return {'requiredPotentialRank': rank, 'unlockCondition': {'phase': 'PHASE_2', 'level': 60},
                    'talentIndex': 0, 'blackboard': [{'key': 'atk_scale', 'value': value}]}
        effects = {'module': {'phases': [
            {'equipLevel': 1, 'attributeBlackboard': [{'key': 'atk', 'value': 10}], 'parts': []},
            {'equipLevel': 3, 'attributeBlackboard': [{'key': 'atk', 'value': 30}], 'parts': [{
                'addOrOverrideTalentDataBundle': {'candidates': [candidate(1.2), candidate(1.5, 4)]}}]}]}}
        result = equipment_effects('unit', {}, identity, progress, effects, metadata)
        self.assertEqual(result['attributes']['atk'], 30)
        self.assertEqual(result['talents'][0]['atk_scale'], 1.2)
        stronger = equipment_effects('unit', {}, identity, replace(progress, potential=5), effects, metadata)
        self.assertEqual(stronger['talents'][0]['atk_scale'], 1.5)
        base = equipment_effects('unit', {}, identity, replace(progress, default_equip_id='base'), effects, metadata)
        self.assertEqual(base['attributes'], {})
        for changed in (replace(progress, elite=1), replace(progress, default_equip_id='foreign'),
                        replace(progress, modules={})):
            with self.assertRaises(BattleSolverError):
                equipment_effects('unit', {}, identity, changed, effects, metadata)
        unknown = equipment_effects('unit', {}, identity, replace(progress, default_equip_id=None), effects, metadata)
        self.assertIn('equipment:selection_unknown', unknown['unknown'])

    def test_distinct_attack_packets_each_apply_defense(self):
        profile = behavior()
        profile.update(mode='permanent', attack_up=0, packets_skill=[2, .5, .5])
        profile['sp'].update(initSp=1, spCost=1)
        enemy = target()
        enemy['attributes']['def'] = 60
        result = duel(fighter(), profile, enemy, {'kind': 'ordinary'}, horizon=.1)
        self.assertEqual(result['damage'], 145)
        self.assertEqual(result['damage_instances'], 3)

    def test_unknown_skill_remains_unsupported_and_native_auto_skill_is_explicit(self):
        self.assertIsNone(skill_behavior('future_skill', {'spData': {}, 'duration': 0}, {'profession': 'CASTER'}))
        profile = skill_behavior('skchr_amgoat_2', {'skillType': 'AUTO', 'duration': -1,
            'spData': {'initSp': 0, 'spCost': 5}, 'blackboard': [
                {'key': k, 'value': v} for k, v in {'atk_scale': 1.85, 'atk_scale_2': 1.85,
                    'magic_resistance': -.25, 'duration': 6}.items()]}, {'profession': 'CASTER'})
        self.assertTrue(profile['automatic'])
        self.assertEqual(profile['attack_scale'], 3.7)

    def test_ignition_reduces_resistance_before_two_primary_target_packets(self):
        profile = skill_behavior('skchr_amgoat_2', {'skillType': 'AUTO', 'duration': -1,
            'spData': {'spType': 'INCREASE_WITH_TIME', 'initSp': 5, 'spCost': 5}, 'blackboard': [
                {'key': k, 'value': v} for k, v in {'atk_scale': 1.85, 'atk_scale_2': 1.85,
                    'magic_resistance': -.25, 'duration': 6}.items()]}, {'profession': 'CASTER'})
        enemy = target()
        enemy['attributes']['magicResistance'] = 50
        result = duel(dict(fighter(), atk=1000), profile, enemy, {'kind': 'ordinary'}, horizon=.1)
        # 2 * 1850 * (1 - 50 * .75 / 100), including the first activation.
        self.assertEqual(result['damage'], 2312.5)
        self.assertEqual(result['damage_instances'], 2)
        result = duel(dict(fighter(), atk=1000), profile, enemy, {'kind': 'basin',
            'initial_stacks': 1, 'max_stacks': 8, 'hits_per_stack': 1,
            'reduction_per_stack': .1, 'stun_duration': 15}, horizon=.1)
        self.assertEqual(result['damage'], 1965.625)
        self.assertEqual(result['final_stacks'], 3)
        enemy['attributes']['maxHp'] = 500
        result = duel(dict(fighter(), atk=1000), profile, enemy, {'kind': 'reviving',
            'revive_ratio': .6, 'revive_delay': 1}, horizon=.1)
        self.assertEqual(result['damage'], 500)
        self.assertEqual(result['remaining_hp'], 300)
        self.assertEqual(result['damage_instances'], 1)

    def test_enemy_mechanics_cannot_be_enabled_by_an_acknowledgement_or_code_prefix(self):
        enemy = {'id': 'enemy_10220_kljbp', 'skills': None, 'talent_blackboard': [
            {'key': k, 'value': v} for k, v in {'fireseedAbility.init_stack_cnt': 1,
            'fireseedAbility.max_stack_cnt': 8, 'kljbp_t.atk_cnt': 5,
            'fireseedAbility.damage_resistance': .1, 'fireseedAbility.atk': 100,
            'over_fireseed.duration': 15}.items()]}
        self.assertEqual(enemy_behavior(enemy, {'mechanics': {}})['kind'], 'unsupported')
        graph = {'mechanics': {'runes': [{'key': 'env_system_new', 'blackboard': [
            {'key': 'key', 'valueStr': 'env_044_act52side'}]}]}}
        self.assertEqual(enemy_behavior(enemy, graph)['kind'], 'basin')
        changed = copy.deepcopy(enemy)
        changed['talent_blackboard'][2]['value'] = 0
        with self.assertRaises(BattleSolverError):
            enemy_behavior(changed, graph)

    def test_teleport_gaps_are_not_filled_with_fictitious_walks(self):
        route = {'start': [0, 0], 'end': [9, 9], 'checkpoints': [
            {'type': 'MOVE', 'position': [1, 0]}, {'type': 'DISAPPEAR'},
            {'type': 'WAIT_FOR_SECONDS', 'time': 10}, {'type': 'APPEAR_AT_POS', 'position': [8, 9]}]}
        result = route_segments(route)
        self.assertEqual(result['segments'], [[[0, 0], [1, 0]], [[8, 9], [9, 9]]])
        self.assertEqual(result['wait_seconds'], 10)

    def test_blocking_requires_capacity_and_releases_slots_after_kill(self):
        arrivals = [{'time': t, 'block_cost': 1, 'service_seconds': 10} for t in (0, 1, 2)]
        self.assertEqual(len(blocking_queue(2, arrivals)['overflow']), 1)
        arrivals[0]['service_seconds'] = 2
        self.assertEqual(blocking_queue(2, arrivals)['overflow'], [])
        self.assertEqual(len(blocking_queue(0, arrivals)['overflow']), 3)
        self.assertEqual(len(blocking_queue(2, [{'time': 0, 'block_cost': 3, 'service_seconds': 1}])['overflow']), 1)


if __name__ == '__main__':
    unittest.main()
