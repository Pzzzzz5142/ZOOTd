import copy
import math
import unittest
from dataclasses import replace

from maa_planner.battle_solver import BattleSolverError
from maa_planner.combat_model import (attack_interval, blackboard, blocking_queue, damage_packet,
                                     duel, enemy_behavior, operator_attributes, route_segments,
                                     skill_behavior, equipment_effects, evaluate_plan, rotation_estimate)
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
    def test_rotation_includes_cooldown_and_repeated_activations_without_editing_enemy(self):
        enemy = target(150)
        result = rotation_estimate(fighter(), behavior(), enemy, {'kind': 'ordinary'}, horizon=11)
        self.assertEqual(result['skill_activations'], [2, 6, 10])
        self.assertEqual(result['damage'], 1600)
        self.assertEqual(result['damage_instances'], 11)
        self.assertEqual(enemy['attributes']['maxHp'], 150)
        self.assertAlmostEqual(result['average_dps'], 1600 / 11, places=3)

    def test_ex_lamp_variants_preserve_inactive_health_and_unblockability(self):
        graph = {'mechanics': {'runes': [{'key': 'env_system_new',
                  'blackboard': [{'key': 'key', 'valueStr': 'env_044_act52side'}]}]}}
        enemy = dict(target(100), id='enemy_10213_klbm_2_2', talent_blackboard=[
            {'key': 'reborn_attr.max_hp', 'value': -.4},
            {'key': 'reborn.duration', 'value': 1},
            {'key': 'fireseedAbility.init_stack_cnt', 'value': 0}])
        state = enemy_behavior(enemy, graph)
        self.assertEqual(state['block_cost'], 0)
        self.assertEqual(state['initial_hp_ratio'], .6)
        result = duel(fighter(), behavior(), enemy, state, horizon=5)
        self.assertEqual(result['damage'], 60)
        self.assertEqual(result['revives'], 0)
        enemy['id'] += '_future'
        self.assertEqual(enemy_behavior(enemy, graph)['kind'], 'unsupported')

    def test_second_life_changes_defences_and_wastes_ammo_during_invulnerability(self):
        profile = behavior()
        profile.update(mode='ammo', ammo=4, attack_up=0, attack_scale=1)
        profile['sp'].update(initSp=1, spCost=1)
        enemy = target(100)
        result = duel(fighter(), profile, enemy, {'kind': 'two_phase_matrix',
            'revive_ratio': 1.5, 'revive_delay': 1, 'revive_invulnerability': 2,
            'revive_defense_add': 50}, horizon=5)
        self.assertEqual(result['revives'], 1)
        self.assertEqual(result['damage'], 200)  # 100 first life, then two 50-point hits
        self.assertIsNone(result['kill_time'])
        self.assertEqual(result['remaining_hp'], 50)
        self.assertEqual(result['skill_activations'], [0, 4])
        self.assertEqual(enemy['attributes']['def'], 0)

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

    def test_new_operator_models_keep_unmodeled_effects_explicit(self):
        def level(skill_type, duration, sp, **blackboard):
            return {'skillType': skill_type, 'duration': duration, 'spData': sp,
                    'blackboard': [{'key': k, 'value': v} for k, v in blackboard.items()]}
        sp = {'spType': 'INCREASE_WITH_TIME', 'initSp': 0, 'spCost': 20, 'maxChargeTime': 1}
        clemnt = skill_behavior('skchr_clemnt_3', level(
            'MANUAL', 0., sp, **{'attack@atk_scale': 2.35, 'attack@max_target': 3., 'trigger_time': 10.}),
            {'profession': 'WARRIOR'})
        self.assertEqual((clemnt['mode'], clemnt['attack_scale'], clemnt['targets'], clemnt['ammo']),
                         ('ammo', 2.35, 3, 10))
        self.assertTrue(any(item.startswith('clemnt:') for item in clemnt['unknown']))
        haruka = skill_behavior('skchr_haruka_2', level(
            'MANUAL', 26., sp, **{'attack@max_target_heal_add': 1.}), {'profession': 'SUPPORT'})
        self.assertEqual((haruka['damage_type'], haruka['damage_type_normal'], haruka['targets']),
                         ('healing', 'arts', 2))
        self.assertAlmostEqual(haruka['attack_scale'], .75)
        sbell = skill_behavior('skchr_sbell2_3', level(
            'MANUAL', 35., sp, **{'atk': 1., 'attack_speed': 30., 'magic_resist_penetrate_fixed': 10.,
                                  'attack@atk_scale_s3': 2.6}), {'profession': 'CASTER'})
        self.assertFalse(sbell['normal_attack'])
        self.assertEqual((sbell['flat_penetration'], sbell['attack_up'], sbell['speed_add']),
                         (10., 1., 30.))
        aglna = skill_behavior('skchr_aglna2_1', level(
            'PASSIVE', 56., {'spType': 8, 'initSp': 0, 'spCost': 0, 'maxChargeTime': 0},
            **{'atk': 1., 'attack@max_target': 2.}), {'profession': 'SPECIAL'})
        self.assertTrue(aglna['passive'] and aglna['automatic'])
        self.assertFalse(aglna['ground_block'])
        self.assertEqual((aglna['mode'], aglna['duration']), ('duration', 56.))

    def test_passive_skill_without_a_limit_stays_active(self):
        profile = behavior()
        profile.update(mode='permanent', passive=True, duration=-1., attack_up=1., attack_scale=1.)
        profile['sp'].update(spType=8)
        result = duel(fighter(), profile, target(), {'kind': 'ordinary'}, horizon=2)
        self.assertEqual(result['damage'], 400)
        self.assertEqual(result['damage_instances'], 2)
        self.assertEqual(result['skill_activations'], [])

    def test_timed_passive_skill_expires_and_falls_back_to_normal_attacks(self):
        profile = behavior()
        profile.update(mode='duration', passive=True, duration=1.5, attack_up=1., attack_scale=1.)
        profile['sp'].update(spType=8)
        result = duel(fighter(), profile, target(), {'kind': 'ordinary'}, horizon=3)
        # Active at t=0 and t=1 for 200 each, then a normal 100 hit at t=2.
        self.assertEqual(result['damage'], 500)
        self.assertEqual(result['damage_instances'], 3)

    def test_no_normal_attack_trait_does_not_invent_basic_hits(self):
        profile = behavior()
        profile.update(normal_attack=False, attack_up=1., attack_scale=1.)
        profile['sp'].update(spCost=10, initSp=0)
        result = duel(fighter(), profile, target(), {'kind': 'ordinary'}, horizon=5)
        self.assertEqual((result['damage'], result['damage_instances']), (0, 0))
        self.assertEqual(result['attack_events'], 5)


if __name__ == '__main__':
    unittest.main()
