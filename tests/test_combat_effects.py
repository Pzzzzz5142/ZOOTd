"""Source-projected rules and explicit elemental/control event scenarios."""
import copy
import json
import unittest
from pathlib import Path

from maa_planner.combat_effects import TargetEffects, effect_rules
from maa_planner.combat_model import damage_packet, duel, rotation_estimate, skill_behavior
from maa_planner.battle_solver import BattleSolverError
from tests.test_combat_model import behavior, fighter


def rules_fixture():
    return json.loads((Path(__file__).parent / 'fixtures/combat/effects.json').read_bytes())


def rules():
    fixture = rules_fixture()
    return effect_rules(fixture['buff_table'], fixture['ep_breakbuff_table'])


def enemy(**attrs):
    return {'id': 'synthetic', 'motion': 'WALK', 'level_type': 'NORMAL', 'attributes': {
        'maxHp': 1000000, 'def': 0, 'magicResistance': 0, 'epResistance': 0,
        'epDamageResistance': 0, 'stunImmune': False, 'frozenImmune': False,
        'silenceImmune': False, 'levitateImmune': False, 'massLevel': 1, **attrs}}


def clementia():
    level = {'skillType': 'MANUAL', 'duration': 0, 'spData': {
        'spType': 'INCREASE_WITH_TIME', 'initSp': 100, 'spCost': 100, 'maxChargeTime': 1},
        'blackboard': [{'key': k, 'value': v} for k, v in {
            'attack@atk_scale': 2.35, 'attack@max_target': 3, 'trigger_time': 10,
            'attack@ep_damage_ratio': .2, 's3_atk_scale': 3.,
            'ep_damage_scale': 2.5, 'projectile_life_time': 3.5}.items()]}
    return skill_behavior('skchr_clemnt_3', level, {'profession': 'WARRIOR'})


class EffectTests(unittest.TestCase):
    def test_pinned_rules_project_enemy_values_and_reject_changed_semantics(self):
        value = rules()
        self.assertEqual((value['erosion_damage'], value['erosion_defense_loss'], value['erosion_cooldown']),
                         (5000, 120, 8))
        self.assertEqual((value['cold_attack_speed'], value['frozen_resistance']), (-30, -15))
        fixture = rules_fixture()
        fixture['buff_table']['unrelated_future_buff'] = {}
        self.assertEqual(effect_rules(fixture['buff_table'], fixture['ep_breakbuff_table']), value)
        fixture['buff_table']['ep_break_water_enemy']['attributes']['attributeModifiers'][0]['formulaItem'] = 'SCALER'
        with self.assertRaisesRegex(BattleSolverError, 'defense modifier'):
            effect_rules(fixture['buff_table'], fixture['ep_breakbuff_table'])
        fixture = rules_fixture()
        fixture['buff_table']['c2e_freeze']['attributes']['attributeModifiers'][0]['value'] = 100
        with self.assertRaisesRegex(BattleSolverError, 'Additional control buff modifier'):
            effect_rules(fixture['buff_table'], fixture['ep_breakbuff_table'])

    def test_buildup_threshold_cooldown_resistance_and_durable_defense(self):
        state = TargetEffects(enemy(epResistance=50), rules())
        self.assertFalse(state.feed_erosion(1000, 0))
        self.assertTrue(state.feed_erosion(1000, 1))
        self.assertFalse(state.feed_erosion(100000, 8.999))
        self.assertEqual(state.erosion, 0)
        self.assertTrue(state.feed_erosion(2000, 9))
        self.assertEqual(state.defense_stacks, 2)
        state.reborn(10)
        self.assertEqual(state.defense_stacks, 2)
        self.assertEqual(state.erosion, 0)
        boss = enemy()
        boss['level_type'] = 'BOSS'
        state = TargetEffects(boss, rules())
        self.assertFalse(state.feed_erosion(1000, 0))
        self.assertTrue(state.feed_erosion(1000, 1))

    def test_elemental_damage_is_distinct_from_buildup_and_arts(self):
        self.assertEqual(damage_packet(1000, 'elemental', defense=5000, resistance=90,
                                       elemental_resistance=50), 500)
        self.assertEqual(damage_packet(1000, 'elemental', elemental_resistance=100), 50)

    def test_clementia_consumes_ammo_on_burst_and_resolves_three_delayed_ticks(self):
        args = (dict(fighter(), atk=1000), clementia(), enemy(), {'kind': 'ordinary'})
        early = duel(*args, horizon=2.5, effect_rules=rules())
        self.assertEqual(early['effects']['erosion_bursts'], [2])
        self.assertEqual(early['damage'], 12050)
        self.assertEqual(early['remaining_ammo'], 9)
        full = duel(*args, horizon=5.5, effect_rules=rules())
        self.assertEqual(full['damage'], 35600)
        self.assertEqual(full['damage_by_type'], {'physical': 23100, 'elemental': 12500})
        self.assertEqual(full['damage_instances'], 13)
        self.assertEqual(full['bombardments_launched'], 1)

    def test_last_rounds_projectile_continues_after_skill_end(self):
        profile = clementia()
        profile['ammo'] = 1
        result = duel(dict(fighter(), atk=1000), profile, enemy(), {'kind': 'ordinary'},
                      horizon=5.5, effect_rules=rules())
        self.assertEqual(result['remaining_ammo'], 0)
        self.assertEqual(result['damage'], 31550)
        self.assertEqual(result['skill_activations'], [0])

    def test_armor_reduces_buildup_and_basic_attacks_do_not_spend_ammo(self):
        result = duel(dict(fighter(), atk=1000), clementia(), enemy(**{'def': 2300}), {'kind': 'ordinary'},
                      horizon=3, effect_rules=rules())
        self.assertEqual(result['effects']['erosion_bursts'], [])
        self.assertEqual(result['remaining_ammo'], 10)
        self.assertAlmostEqual(result['effects']['remaining_erosion'], 70.5)

    def test_rotation_preserves_enemy_identity_and_effect_state(self):
        target = enemy()
        original = copy.deepcopy(target)
        result = rotation_estimate(dict(fighter(), atk=1000), clementia(), target, {'kind': 'ordinary'},
                                   horizon=5.5, effect_rules=rules())
        self.assertEqual(result['damage'], 35600)
        self.assertEqual(target, original)

    def test_unknown_enemy_fields_or_missing_rules_never_grant_burst_damage(self):
        for target, source in ((dict(enemy(), level_type='FUTURE'), rules()),
                               (enemy(epResistance=None), rules()), (enemy(), None)):
            with self.subTest(target=target['level_type']):
                result = duel(dict(fighter(), atk=1000), clementia(), target, {'kind': 'ordinary'},
                              horizon=3, effect_rules=source)
                self.assertEqual(result['effects']['erosion_bursts'], [])
                self.assertTrue(result['effects']['unknown'])
                self.assertEqual(result['damage'], 7050)

    def test_cold_freeze_immunity_and_damage_order(self):
        profile = behavior()
        profile.update(mode='next_attack', attack_up=0, damage_type='arts',
                       control_events=[{'trigger': 'hit', 'kind': 'cold', 'duration': 3.5}])
        profile['sp'].update(initSp=1, spCost=1)
        result = duel(fighter(), profile, enemy(magicResistance=50), {'kind': 'ordinary'},
                      horizon=2.5, effect_rules=rules())
        # Freeze is applied after the second damage packet; only packet 3 gets -15 RES.
        self.assertEqual(result['damage'], 165)
        self.assertEqual(result['effects']['movement_stopped_seconds'], 1.5)
        immune = duel(fighter(), profile, enemy(magicResistance=50, frozenImmune=True),
                      {'kind': 'ordinary'}, horizon=2.5, effect_rules=rules())
        self.assertEqual(immune['damage'], 150)
        self.assertEqual(immune['effects']['movement_stopped_seconds'], 0)

    def test_levitation_has_weight_air_immunity_and_nonrefresh_rules(self):
        state = TargetEffects(enemy(massLevel=4), rules())
        self.assertTrue(state.apply('levitate', 0, 12))
        self.assertFalse(state.apply('levitate', 1, 12))
        report = state.report(15)
        self.assertEqual(report['attack_disabled_seconds'], 6)
        self.assertEqual(report['movement_stopped_seconds'], 6)
        self.assertEqual(report['unblockable_seconds'], 6)
        self.assertFalse(TargetEffects(enemy(levitateImmune=True), rules()).apply('levitate', 0, 12))
        self.assertFalse(TargetEffects(dict(enemy(), motion='FLY'), rules()).apply('levitate', 0, 12))

    def test_root_silence_slow_and_overlapping_stuns_have_distinct_effects(self):
        state = TargetEffects(enemy(), rules())
        state.apply('root', 0, 2)
        state.apply('silence', 0, 5)
        state.apply('slow', 0, 6, ratio=.5)
        state.apply('stun', 1, 3)
        state.apply('stun', 2, 1)
        result = state.report(6)
        self.assertEqual(result['movement_stopped_seconds'], 4)
        self.assertEqual(result['attack_disabled_seconds'], 3)
        self.assertEqual(result['abilities_suppressed_seconds'], 5)
        self.assertEqual(result['movement_delay_equivalent_seconds'], 5)
        self.assertEqual(result['unblockable_seconds'], 0)

    def test_missing_immunity_and_unobserved_trigger_do_not_credit_control(self):
        state = TargetEffects(enemy(stunImmune=None), rules())
        self.assertFalse(state.apply('stun', 0, 5))
        self.assertTrue(state.report(5)['unknown'])
        profile = behavior()
        profile.update(control_events=[{'trigger': 'skill', 'kind': 'levitate', 'duration': 12,
                                        'condition': 'target_in_glide_sweep'}])
        profile['sp'].update(initSp=2)
        args = (fighter(), profile, enemy(), {'kind': 'ordinary'})
        base = duel(*args, horizon=5, effect_rules=rules())
        assumed = duel(*args, horizon=5, effect_rules=rules(), conditions=('target_in_glide_sweep',))
        self.assertEqual(base['effects']['movement_stopped_seconds'], 0)
        self.assertEqual(assumed['effects']['movement_stopped_seconds'], 5)


if __name__ == '__main__':
    unittest.main()
