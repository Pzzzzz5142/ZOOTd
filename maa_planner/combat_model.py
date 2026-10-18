"""Source-bound combat estimates against the actual stage enemies.

Damage packets and supported state transitions are executable models. A duel
is an explicit continuous-contact scenario, never a full-map victory proof.
Unimplemented skills, talents, equipment and environmental interactions remain
visible, rather than silently acquiring a favorable generic interpretation.
"""
from __future__ import annotations

import copy
import heapq
import math

from .battle_solver import BattleSolverError, _require, _map, _shortest, _rotate
from .util import canonical_json, sha256_bytes
from .combat_effects import TargetEffects

MODEL_VERSION = 8


def number(value, label, minimum=0):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= minimum,
             'Invalid combat value: ' + label)
    return float(value)


def blackboard(rows):
    result = {}
    for row in rows or []:
        _require(isinstance(row, dict) and isinstance(row.get('key'), str)
                 and row['key'] not in result, 'Invalid or duplicate combat blackboard key')
        if row.get('valueStr') is not None:
            continue
        result[row['key']] = number(row.get('value'), row['key'], -1e9)
    return result


def damage_packet(attack, damage_type, *, defense=0, resistance=0, elemental_resistance=0,
                  flat_penetration=0, percent_penetration=0, reduction=0, damage_scale=1):
    """One damage instance: penetration before mitigation; minimum before reduction."""
    a = number(attack, 'attack')
    d, r = number(defense, 'defense'), number(resistance, 'resistance')
    flat = number(flat_penetration, 'flat penetration')
    percent = number(percent_penetration, 'percent penetration')
    reduced = number(reduction, 'reduction')
    scale = number(damage_scale, 'damage scale')
    _require(percent <= 1 and reduced <= 1, 'Combat fraction outside 0..1')
    if damage_type == 'physical':
        base = max(.05 * a, a - max(0, d - flat) * (1 - percent))
    elif damage_type == 'arts':
        effective = max(0, r - flat) * (1 - percent)
        base = max(.05 * a, a * max(0, 1 - effective / 100))
    elif damage_type == 'elemental':
        base = max(.05 * a, a * max(0, 1 - number(elemental_resistance, 'elemental resistance') / 100))
    elif damage_type in ('true', 'healing'):
        base = a
    else:
        raise BattleSolverError('Unsupported damage type: ' + str(damage_type))
    return base * scale * (1 - reduced)


def attack_interval(base, speed):
    return number(base, 'attack interval', .000001) / (min(600, max(20, number(speed, 'attack speed'))) / 100)


def interpolate(frames, level):
    _require(isinstance(frames, list) and bool(frames), 'Missing attribute keyframes')
    frames = sorted(frames, key=lambda f: f['level'])
    _require(frames[0]['level'] <= level <= frames[-1]['level'], 'Progression outside attribute keyframes')
    low = max((f for f in frames if f['level'] <= level), key=lambda f: f['level'])
    high = min((f for f in frames if f['level'] >= level), key=lambda f: f['level'])
    ratio = (level - low['level']) / (high['level'] - low['level']) if high['level'] != low['level'] else 0
    return {k: number(v, k, -1e9) + ratio * (number(high['data'][k], k, -1e9) - v)
            for k, v in low['data'].items() if type(v) in (int, float)}


def operator_attributes(char, progress):
    _require(progress.elite is not None and progress.level is not None and progress.potential is not None,
             'Unknown combat progression')
    phases = char['phases']
    _require(0 <= progress.elite < len(phases), 'Unknown combat elite phase')
    attrs = interpolate(phases[progress.elite]['attributesKeyFrames'], progress.level)
    unknown = ['attribute_rounding:linear_estimate']
    mapping = {'ATK': 'atk', 'DEF': 'def', 'MAX_HP': 'maxHp', 'COST': 'cost',
               'ATTACK_SPEED': 'attackSpeed', 'RESPAWN_TIME': 'respawnTime'}
    for potential in char.get('potentialRanks', [])[:progress.potential - 1]:
        if potential.get('type') != 'BUFF':
            unknown.append('potential:custom_effect')
            continue
        for modifier in potential.get('buff', {}).get('attributes', {}).get('attributeModifiers', []):
            key = mapping.get(modifier.get('attributeType'))
            if key and modifier.get('formulaItem') == 'ADDITION':
                attrs[key] += number(modifier['value'], key, -1e9)
            else:
                unknown.append('potential:unknown_modifier')
    maximum = (char.get('favorKeyFrames') or [{'data': {}}])[-1]['data']
    if progress.favor_percent is None:
        unknown.append('trust:account_snapshot_has_no_trust')
        bounds = {k: [v, v + maximum.get(k, 0)] for k, v in attrs.items()}
    else:
        # Source favor levels 0..50 map to displayed trust 0..100%; >100% is capped.
        frames = char.get('favorKeyFrames') or []
        _require(frames and frames[-1]['level'] == 50, 'Unsupported trust attribute scale')
        trust = interpolate(frames, min(100, progress.favor_percent) / 2)
        bounds = {k: [v + trust.get(k, 0)] * 2 for k, v in attrs.items()}
    return bounds, unknown


def unlocked(candidate, progress):
    c = candidate.get('unlockCondition', {})
    phase = {'PHASE_0': 0, 'PHASE_1': 1, 'PHASE_2': 2}.get(c.get('phase'))
    return (phase is not None and (progress.elite > phase or progress.elite == phase
            and progress.level >= c['level']) and progress.potential - 1 >= candidate.get('requiredPotentialRank', 0))


def equipment_effects(operator_id, spec, identity, progress, equipment, metadata):
    slot = spec.get('requirements', {}).get('module', 0)
    _require(not slot or slot in identity.modules, 'Unknown required equipment slot')
    selected = identity.modules[slot] if slot else progress.default_equip_id
    result = {'selected': selected, 'attributes': {}, 'talents': {}, 'trait': {}, 'unknown': []}
    if selected is None:
        result['unknown'].append('equipment:selection_unknown')
        return result
    _require(selected in metadata.get('charEquip', {}).get(operator_id, []), 'Equipment belongs to another operator')
    if metadata['equipDict'][selected].get('charEquipOrder') == 0:
        return result
    entry = metadata['equipDict'][selected]
    _require(unlocked({'unlockCondition': {'phase': entry['unlockEvolvePhase'],
                                          'level': entry['unlockLevel']}}, progress),
             'Selected equipment is not unlocked at this operator phase/level')
    training = (progress.modules or {}).get(selected)
    _require(training is not None and training.unlocked is True and training.level is not None,
             'Selected equipment progression is unknown')
    _require(selected in equipment, 'Selected equipment combat source missing')
    phase = next((p for p in equipment[selected]['phases'] if p['equipLevel'] == training.level), None)
    _require(phase is not None, 'Selected equipment level not in combat source')
    result['attributes'] = blackboard(phase['attributeBlackboard'])
    for part in phase['parts']:
        if part.get('isToken') or part.get('validInGameTag') or part.get('validInMapTag'):
            result['unknown'].append('equipment:conditional_or_token_part')
            continue
        for group in ('overrideTraitDataBundle', 'addOrOverrideTalentDataBundle'):
            candidates = [c for c in part.get(group, {}).get('candidates') or [] if unlocked(c, progress)]
            if not candidates:
                continue
            picked = max(candidates, key=lambda c: c.get('requiredPotentialRank', 0))
            if picked.get('validModeIndices'):
                result['unknown'].append('equipment:mode_conditional_talent')
                continue
            b = blackboard(picked.get('blackboard'))
            if group == 'overrideTraitDataBundle':
                result['trait'].update(b)
            else:
                index = picked.get('talentIndex')
                if type(index) is int and index >= 0:
                    result['talents'][index] = b
                else:
                    result['unknown'].append('equipment:additional_talent')
    return result


def operator_talents(char, progress, module):
    result = []
    for index, talent in enumerate(char.get('talents') or []):
        candidates = [c for c in talent['candidates'] if unlocked(c, progress)]
        if candidates:
            b = blackboard(max(candidates, key=lambda c: (
                c['unlockCondition']['phase'], c['unlockCondition']['level'],
                c.get('requiredPotentialRank', 0)))['blackboard'])
            result.append(module['talents'].get(index, b))
    return result


def skill_behavior(skill_id, level, char):
    """Small reviewed dispatch: blackboard names alone do not define behavior."""
    b = blackboard(level.get('blackboard'))
    kind = 'arts' if char['profession'] == 'CASTER' else 'healing' if char['profession'] == 'MEDIC' else 'physical'
    result = {'damage_type': kind, 'attack_up': 0, 'attack_scale': 1, 'hits': 1,
              'targets': 1, 'interval_add': 0, 'speed_add': 0, 'block_add': 0,
              'mode': 'duration', 'self_heal_ratio': 0, 'defense_up': 0,
              'flat_penetration': 0, 'normal_attack': True, 'passive': False,
              'ground_block': True,
              'unknown': [], 'sp': copy.deepcopy(level['spData']), 'duration': level['duration'],
              'automatic': level.get('skillType') == 'AUTO'}
    if skill_id == 'skchr_f12yin_2':
        result.update(mode='permanent', attack_up=b['atk'], block_add=b['block_cnt'],
                      targets=2, defense_up=b['def'],
                      self_heal_ratio=b['hp_recovery_per_sec_by_max_hp_ratio'])
    elif skill_id == 'skchr_huang_2':
        result.update(mode='permanent', attack_up=b['atk'], defense_up=b['def'], targets=3)
    elif skill_id == 'skchr_angel_3':
        result.update(attack_scale=b['attack@atk_scale'], hits=int(b['attack@times']),
                      interval_add=b['base_attack_time'])
    elif skill_id == 'skchr_amgoat_2':
        result.update(mode='next_attack', attack_scale=b['atk_scale'] + b['atk_scale_2'],
                      packets_skill=[b['atk_scale'], b['atk_scale_2']],
                      resistance_debuff=b['magic_resistance'], debuff_duration=b['duration'])
        result['unknown'].append('splash:secondary_targets_not_simulated')
    elif skill_id == 'skchr_amgoat_3':
        result.update(attack_up=b['atk'], interval_add=b['base_attack_time'],
                      targets=int(b['attack@max_target']))
    elif skill_id in ('skchr_logos_1', 'skchr_logos_3'):
        result.update(attack_up=b['atk'], targets=int(b.get('attack@max_target', 1)))
        if skill_id == 'skchr_logos_1':
            result.update(mode='permanent')
            result['unknown'].append('logos:execution_aura_not_simulated')
        result['unknown'].append('logos:extra_hits_elemental_damage_and_debuffs_not_simulated')
    elif skill_id == 'skchr_wisdel_3':
        result.update(mode='ammo', attack_up=b['atk'], attack_scale=b['attack@atk_scale_3'],
                      interval_add=b['base_attack_time'], ammo=int(b['attack@trigger_time']))
        result['unknown'].append('wisadel:summons_and_marks_not_simulated')
    elif skill_id == 'skchr_agoat2_1':
        result.update(mode='permanent', attack_up=b['atk'], targets=2)
        result['unknown'].append('elemental_healing_and_regeneration_not_simulated')
    elif skill_id == 'skchr_cgbird_3':
        result.update(attack_up=b['atk'], targets=3)
        result['unknown'].append('sanctuary_resistance_dodge_and_phantoms_not_simulated')
    elif skill_id == 'skchr_shu_1':
        result.update(mode='conditional_heal', damage_type='healing', attack_scale=b['heal_scale'])
        result['unknown'].append('shu:heal_threshold_sowing_and_team_buffs_not_simulated')
    elif skill_id == 'skchr_clemnt_1':
        result.update(mode='next_attack', attack_scale=b['atk_scale'], erosion_ratio=b['ep_damage_ratio'])
    elif skill_id == 'skchr_clemnt_2':
        result.update(attack_scale=b['attack@aoe_atk_scale'], speed_add=b['attack_speed'],
                      targets=int(b['attack@max_target']), control_events=[
                          {'trigger': 'field', 'kind': 'slow', 'duration': 'contact',
                           'ratio': 1 + b['attack@move_speed'], 'condition': 'target_in_vortex'}],
                      vortex={'erosion_per_second': b['attack@water_element'],
                              'physical_scale_per_second': b['attack@physical_atk_scale']})
        result['unknown'].append('clemnt:banishment_pod_vortex_erosion_and_displacement_not_simulated')
    elif skill_id == 'skchr_clemnt_3':
        result.update(mode='ammo', attack_scale=b['attack@atk_scale'],
                      targets=int(b['attack@max_target']), ammo=int(b['trigger_time']),
                      erosion_ratio=b['attack@ep_damage_ratio'], ammo_trigger='erosion',
                      erosion_rocket={'physical_scale': b['s3_atk_scale'],
                                      'elemental_scale': b['ep_damage_scale'], 'ticks': 3,
                                      'interval': 1., 'lifetime': b['projectile_life_time']})
        result['unknown'].append('clemnt:global_erosion_sources_bombardment_overlap_and_secondary_targets_not_simulated')
    elif skill_id in ('skchr_haruka_1', 'skchr_haruka_2', 'skchr_haruka_3'):
        # Trait: normal attacks are arts; an active skill heals for 75% attack.
        result.update(damage_type='healing', damage_type_normal='arts', attack_scale=.75)
        if skill_id == 'skchr_haruka_1':
            result.update(speed_add=b['attack_speed'])
            result['unknown'].append('haruka:bubble_shield_and_retarget_not_simulated')
        elif skill_id == 'skchr_haruka_2':
            result.update(targets=1 + int(b['attack@max_target_heal_add']))
            result['unknown'].append(
                'haruka:heal_conversion_damage_and_second_use_atk_bonus_not_simulated')
        else:
            result.update(attack_up=b['atk'], interval_add=b['base_attack_time'], control_events=[
                {'trigger': 'reactive', 'kind': 'levitate', 'duration': b['levitate_duration'],
                 'condition': 'enemy_attacks_friendly_bubble'}])
            result['unknown'].append('haruka:bubble_targeting_periodic_arts_and_shield_resistance_not_simulated')
    elif skill_id in ('skchr_sbell2_1', 'skchr_sbell2_2', 'skchr_sbell2_3'):
        # Trait: no normal attack; active skills deal area arts damage.
        result.update(normal_attack=False)
        if skill_id == 'skchr_sbell2_1':
            result.update(mode='next_attack', attack_scale=b['atk_scale'],
                          control_events=[{'trigger': 'hit', 'kind': 'cold', 'duration': b['cold']}])
            result['unknown'].append('sbell2:push_and_snow_spread_not_simulated')
        elif skill_id == 'skchr_sbell2_2':
            result.update(mode='permanent', attack_scale=b['attack@atk_scale_s2'])
            result['unknown'].append('sbell2:snow_stacking_cold_freeze_and_tile_damage_not_simulated')
        else:
            result.update(attack_up=b['atk'], speed_add=b['attack_speed'],
                          attack_scale=b['attack@atk_scale_s3'],
                          flat_penetration=b['magic_resist_penetrate_fixed'])
            result['unknown'].append('sbell2:attraction_and_snow_acceleration_not_simulated')
        result['unknown'].append('sbell2:area_target_count_not_simulated')
    elif skill_id == 'skchr_aglna2_1':
        # Passive but time-limited: active from deployment for the skill duration.
        result.update(mode='duration', passive=True, automatic=True, ground_block=False,
                      attack_up=b['atk'], targets=int(b['attack@max_target']))
        result['unknown'].append('aglna2:talent_extra_arts_damage_and_liftoff_not_simulated')
        result['unknown'].append('aglna2:post_duration_landing_state_not_simulated')
    elif skill_id == 'skchr_aglna2_2':
        result.update(damage_type='arts', damage_type_normal='physical', ground_block=False,
                      attack_up=b['atk'], interval_add=b['base_attack_time'],
                      targets=int(b['attack@max_target']), control_events=[
                          {'trigger': 'skill', 'kind': 'levitate', 'duration': b['buff_duration_levitate'],
                           'condition': 'target_in_glide_sweep'}])
        result['unknown'].append('aglna2:glide_sweep_geometry_groundbind_and_slide_not_simulated')
    elif skill_id == 'skchr_aglna2_3':
        result.update(mode='ammo', attack_up=b['atk'], attack_scale=b['attack@atk_scale'],
                      targets=int(b['attack@max_walk_target']), ground_block=False,
                      ammo=int(b['attack@trigger_time']))
        result['control_events'] = [{'trigger': 'skill', 'kind': 'slow', 'duration': 'skill',
                                    'ratio': 1 + b['move_speed'], 'condition': 'flying_target_in_range'}]
        result['unknown'].append(
            'aglna2:air_protection_dash_flying_slow_and_extra_flying_target_not_simulated')
    elif skill_id == 'skcom_assist_cost[2]':
        result.update(mode='cost', block_add=-1, targets=0, cost_gain=b['value'])
    else:
        return None
    _require(result['hits'] >= 1 and result['targets'] >= 0, 'Invalid supported attack multiplicity')
    return result


# Human-readable model intent for the dashboard and review. Every modeled skill
# needs an entry here; the coverage test fails when the two drift apart.
SKILL_MODEL_NOTES = {
    'skchr_f12yin_2': '永久：攻击、防御与阻挡提升，多目标攻击并按最大生命比例自回。',
    'skchr_huang_2': '永久：攻击与防御提升，攻击多个目标。',
    'skchr_angel_3': '多段攻击：每段伤害按倍率结算，攻击间隔按技能值调整。',
    'skchr_amgoat_2': '下次攻击：两段伤害，先降低目标法抗再结算第二段；溅射次要目标未建模。',
    'skchr_amgoat_3': '持续：攻击力提升、攻击间隔缩短，攻击多个目标。',
    'skchr_logos_1': '永久：攻击力提升；处决光环、额外元素伤害与减益未建模。',
    'skchr_logos_3': '持续：攻击力提升，攻击多个目标；额外元素伤害与减益未建模。',
    'skchr_wisdel_3': '弹药型：攻击力提升，每发按倍率结算；召唤物与标记爆炸未建模。',
    'skchr_agoat2_1': '永久：攻击力提升、治疗两个目标；元素治疗与再生未建模。',
    'skchr_cgbird_3': '持续：攻击力提升、治疗三个目标；圣域抗性、闪避与幻影未建模。',
    'skchr_shu_1': '条件治疗：治疗量按技能倍率结算；阈值播种与团队增益未建模。',
    'skchr_clemnt_1': '下次攻击：物理伤害倍率提升，按实际伤害积累侵蚀，计算爆发、冷却和永久减防；天赋随机效果未建模。',
    'skchr_clemnt_2': '持续：范围扩大、攻击速度提升，对多名地面敌人造成范围物理伤害；放逐舱、漩涡与侵蚀未建模。',
    'skchr_clemnt_3': '物理攻击积累侵蚀；仅爆发触发轰炸时消耗弹药，分别结算三次延迟物理与元素伤害；全场其他目标与轰炸位置竞争未建模。',
    'skchr_haruka_1': '治疗：治疗量为攻击力的固定比例，攻击速度提升；浮泡庇护与目标转移未建模。',
    'skchr_haruka_2': '治疗：治疗目标数增加；治疗转伤害、浮泡目标数与二次使用加成未建模。',
    'skchr_haruka_3': '治疗：攻击力提升、攻击间隔缩短；攻击浮泡后浮空为独立条件情景，浮空持续伤害与庇护倍率未建模。',
    'skchr_sbell2_1': '瞬发范围法术伤害，可充能；命中后寒冷、重复寒冷触发冻结及冻结减法抗已建模；推动与积雪扩散未建模；通常不攻击。',
    'skchr_sbell2_2': '永久范围法术攻击；积雪层数、寒冷、冻结与地块伤害未建模；通常不攻击。',
    'skchr_sbell2_3': '持续范围法术攻击，攻击力与攻击速度提升、无视部分法抗；诱导与积雪加速未建模；通常不攻击。',
    'skchr_aglna2_1': '限时被动：部署后起飞并计时，攻击力提升、攻击两个目标；时限结束后的落地状态、天赋额外法术伤害与失重未建模。',
    'skchr_aglna2_2': '持续法术多目标攻击；掠过目标的浮空为独立条件情景，计算免疫与重量；缚地和滑翔轨迹未建模。',
    'skchr_aglna2_3': '弹药型：物理多目标攻击；对空庇护、冲刺、飞行减速与额外飞行目标未建模。',
    'skcom_assist_cost[2]': '回费：每次攻击获得费用，阻挡降低。',
}


def enemy_behavior(enemy, graph):
    b = blackboard(enemy.get('talent_blackboard'))
    result = {'kind': 'ordinary', 'unknown': [], 'revive_ratio': 0}
    # Bind an event adapter to data-owned environment and enemy identity.
    envs = [r for r in graph.get('mechanics', {}).get('runes') or []
            if r.get('key') == 'env_system_new' and any(
                x.get('valueStr') == 'env_044_act52side' for x in r.get('blackboard', []))]
    if envs and enemy['id'] in ('enemy_10220_kljbp', 'enemy_10220_kljbp_2'):
        result.update(kind='basin', initial_stacks=int(b['fireseedAbility.init_stack_cnt']),
            max_stacks=int(b['fireseedAbility.max_stack_cnt']),
            hits_per_stack=int(b['kljbp_t.atk_cnt']), reduction_per_stack=b['fireseedAbility.damage_resistance'],
            attack_per_stack=b['fireseedAbility.atk'], stun_duration=b['over_fireseed.duration'])
        _require(result['hits_per_stack'] > 0 and 0 <= result['initial_stacks'] <= result['max_stacks']
                 and 0 <= result['max_stacks'] * result['reduction_per_stack'] <= 1,
                 'Unsupported basin mechanic parameters')
        result['unknown'].append('ambient_matrix_absorption_and_energy_field_not_simulated')
    elif envs and enemy['id'] == 'enemy_10212_klhz':
        result.update(kind='matrix')
        result['unknown'].append('absorption_into_friendly_units_and_SP_gain_not_simulated')
    elif envs and enemy['id'] == 'enemy_10218_klxl_2':
        result['unknown'].append('censer:matrix_consumption_channel_damage_and_field_sharing_not_simulated')
    elif envs and enemy['id'] == 'enemy_10217_klljs_2':
        result['unknown'].append('apprentice:periodic_matrix_summons_not_simulated')
    elif envs and enemy['id'] == 'enemy_10219_klhjdd_2':
        result.update(kind='stored_matrix', initial_stacks=int(b['fireseedAbility.init_stack_cnt']),
                      max_stacks=int(b['fireseedAbility.max_stack_cnt']),
                      reduction_per_stack=b['fireseedAbility.damage_resistance'])
        result['unknown'].append('armor:external_matrix_absorption_overload_splash_and_attack_speed_not_simulated')
    elif envs and enemy['id'] in ('enemy_1594_presaint', 'enemy_1594_presaint_now'):
        result.update(kind='two_phase_matrix',
            initial_stacks=int(b['fireseedAbility.init_stack_cnt']),
            reduction_per_stack=b['fireseedAbility.damage_resistance'],
            revive_ratio=1 + b['Reborn.max_hp'] / enemy['attributes']['maxHp'],
            revive_delay=b['Reborn.duration'],
            revive_defense_add=b['Reborn.def'],
            revive_resistance_add=b['Reborn.magic_resistance'],
            revive_invulnerability=b['Reborn.invi_duration'],
            revive_stacks=int(b['fireseedAbility.init_stack_cnt_2']))
        _require(0 <= result['initial_stacks'] * result['reduction_per_stack'] < 1
                 and 0 <= result['revive_stacks'] * result['reduction_per_stack'] < 1,
                 'Unsupported boss matrix reduction')
        result['unknown'].append('presaint:external_matrix_absorption_overload_burn_aoe_stun_and_enemy_attacks_not_simulated')
    elif envs and enemy['id'] in (
            'enemy_10213_klbm', 'enemy_10213_klbm_2', 'enemy_10213_klbm_2_2',
            'enemy_10214_klgn', 'enemy_10214_klgn_2',
            'enemy_10215_klhjd_2', 'enemy_10215_klhjd_2_a',
            'enemy_10216_kltwhp_2', 'enemy_10216_kltwhp_2_2'):
        result.update(kind='reviving', revive_ratio=1 + b['reborn_attr.max_hp'],
                      revive_delay=b['reborn.duration'])
        inactive = b.get('fireseedAbility.init_stack_cnt') == 0
        if inactive:
            result.update(initial_hp_ratio=1 + b['reborn_attr.max_hp'], revive_ratio=0)
        if enemy['id'] in ('enemy_10213_klbm', 'enemy_10213_klbm_2', 'enemy_10213_klbm_2_2'):
            result['block_cost'] = 0
        if enemy['id'] in ('enemy_10215_klhjd_2', 'enemy_10215_klhjd_2_a'):
            result['block_cost'] = 2
        if enemy['id'] in ('enemy_10216_kltwhp_2', 'enemy_10216_kltwhp_2_2'):
            result['unknown'].append('vase:channel_stun_and_beam_suppression_not_simulated')
        result['unknown'].append('matrix_release_and_energy_field_interaction_not_simulated')
    elif enemy.get('skills') or enemy.get('talent_blackboard'):
        result.update(kind='unsupported')
        result['unknown'].append('unimplemented_enemy_skills_or_talents')
    return result


def duel(attrs, behavior, enemy, mechanics, *, horizon=60, skill_enabled=True, start_stacks=None,
         effect_rules=None, conditions=()):
    """Continuous-contact attack and delayed-effect events; no spatial admission."""
    horizon = number(horizon, 'horizon', .001)
    _require(horizon <= 600, 'Combat horizon exceeds 600 seconds')
    e = enemy['attributes']
    max_hp = number(e['maxHp'], 'enemy HP', .001)
    hp = max_hp * mechanics.get('initial_hp_ratio', 1)
    passive = behavior.get('passive', False)
    sp_data = behavior['sp']
    sp, need = number(sp_data['initSp'], 'initial SP'), number(sp_data['spCost'], 'SP cost')
    sp_kind = sp_data['spType']
    if not passive:
        _require(sp_kind in ('INCREASE_WITH_TIME', 'INCREASE_WHEN_ATTACK',
                             'INCREASE_WHEN_TAKEN_DAMAGE'), 'Unknown SP recovery type')
    sp_rate = number(attrs.get('spRecoveryPerSec', 1), 'SP recovery')
    active_until = (max(0., behavior['duration']) if behavior.get('duration', -1) > 0
                    else math.inf) if passive and skill_enabled else -1.
    now = last = next_attack = next_ready = 0.
    ammo, attacks, hits, total, revives, overloads, launches = 0, 0, 0, 0., 0, 0, 0
    stacks = mechanics.get('initial_stacks', 0) if start_stacks is None else start_stacks
    stack_hits, stunned_until, debuffed_until, invulnerable_until = 0, -1., -1., -1.
    defense, base_resistance = e['def'], e['magicResistance']
    first_activation = kill_time = None
    activations, pending = [], []
    breakdown = {}
    effects = TargetEffects(enemy, effect_rules)

    def packet(attack, kind, *, penetration=False):
        nonlocal hp, total, hits, stacks, stack_hits, stunned_until, overloads
        nonlocal revives, next_ready, invulnerable_until, defense, base_resistance, debuffed_until
        if now < next_ready:
            return 'hidden', 0
        reduction = stacks * mechanics.get('reduction_per_stack', 0) if now >= stunned_until else 0
        resistance = max(0, base_resistance + effects.resistance_add(now)) * (
            1 + behavior.get('resistance_debuff', 0) if now < debuffed_until else 1)
        def_loss = effects.defense_stacks * (effect_rules['erosion_defense_loss'] if effect_rules else 0)
        value = damage_packet(attack, kind, defense=max(0, defense - def_loss), resistance=resistance,
            elemental_resistance=e.get('epDamageResistance', 0), reduction=reduction,
            flat_penetration=behavior.get('flat_penetration', 0) if penetration else 0,
            percent_penetration=behavior.get('percent_penetration', 0) if penetration else 0)
        if now < invulnerable_until:
            value = 0
        actual = min(hp, value)
        total += actual
        breakdown[kind] = breakdown.get(kind, 0) + actual
        hp -= value
        hits += 1
        if mechanics['kind'] == 'basin' and now >= stunned_until:
            stack_hits += 1
            if stack_hits >= mechanics['hits_per_stack']:
                stack_hits = 0
                stacks += 1
                if stacks > mechanics['max_stacks']:
                    stacks = 0
                    stunned_until = now + mechanics['stun_duration']
                    overloads += 1
        if hp <= 0 and mechanics.get('revive_ratio', 0) and not revives:
            hp = max_hp * mechanics['revive_ratio']
            revives += 1
            next_ready = now + mechanics['revive_delay']
            invulnerable_until = next_ready + mechanics.get('revive_invulnerability', 0)
            defense = max(0, defense + mechanics.get('revive_defense_add', 0))
            base_resistance = max(0, base_resistance + mechanics.get('revive_resistance_add', 0))
            stacks = mechanics.get('revive_stacks', stacks)
            debuffed_until = -1.
            effects.reborn(now)
            return 'reborn', actual
        return ('dead' if hp <= 0 else 'alive'), actual

    def apply_controls(trigger):
        for spec in behavior.get('control_events', []):
            if spec['trigger'] != trigger:
                continue
            if effect_rules is None:
                effects.unknown.add('control:pinned_effect_rules_missing')
                continue
            condition = spec.get('condition')
            if condition == 'flying_target_in_range':
                accepted = enemy.get('motion', enemy.get('data', {}).get('motion')) == 'FLY'
            else:
                accepted = condition is None or condition in conditions
            if not accepted:
                if condition != 'flying_target_in_range':
                    effects.unknown.add('control:condition_not_assumed:' + str(condition))
                continue
            duration = min(horizon - now, active_until - now) if spec['duration'] == 'skill' else spec['duration']
            if effects.apply(spec['kind'], now, duration, ratio=spec.get('ratio')) and spec['duration'] == 'skill':
                effects.statuses[-1]['bound_to_skill'] = True

    while attacks < 100000:
        next_attack = max(next_attack, next_ready)
        # Delayed damage precedes an ordinary attack at the same timestamp.
        delayed = bool(pending and pending[0][0] <= next_attack)
        now = pending[0][0] if delayed else next_attack
        if now >= horizon:
            break
        if delayed:
            _, _, kind, value = heapq.heappop(pending)
            outcome, _ = packet(value, kind)
            if outcome == 'dead':
                kill_time = now
                break
            continue
        active = active_until > now
        if sp_kind == 'INCREASE_WITH_TIME':
            free_time = max(0., now - max(last, active_until)) if active_until > last else now - last
            sp = min(need * sp_data.get('maxChargeTime', 1), sp + free_time * sp_rate)
        if skill_enabled and not active and sp >= need and need > 0:
            sp -= need
            first_activation = now if first_activation is None else first_activation
            activations.append(now)
            mode = behavior['mode']
            if mode in ('permanent', 'ammo'):
                active_until = math.inf
                ammo = behavior.get('ammo', 0)
            elif mode in ('next_attack', 'conditional_heal'):
                active_until = now + .00001
            else:
                active_until = now + max(0, behavior['duration'])
            active = True
            apply_controls('skill')
        last = now
        attacks += 1
        if behavior['mode'] == 'cost' and active or (not active and behavior.get('normal_attack', True) is False):
            next_attack = now + attack_interval(attrs['baseAttackTime'], attrs['attackSpeed'])
            continue
        kind = behavior['damage_type'] if active else behavior.get('damage_type_normal', behavior['damage_type'])
        attack = attrs['atk'] * (1 + (behavior['attack_up'] if active else 0))
        multiplier = behavior['attack_scale'] if active else 1
        packets = behavior.get('packets_skill' if active else 'packets_normal')
        packets = packets if packets is not None else [multiplier] * (behavior['hits'] if active else 1)
        if active and behavior.get('resistance_debuff'):
            debuffed_until = now + behavior['debuff_duration']
        for scale in packets:
            outcome, actual = packet(attack * scale, kind, penetration=True)
            if outcome != 'alive':
                break
            if active and effects.feed_erosion(actual * behavior.get('erosion_ratio', 0), now):
                outcome, _ = packet(effect_rules['erosion_damage'], 'elemental')
                if outcome != 'alive':
                    break
                rocket = behavior.get('erosion_rocket')
                if rocket and active_until > now and ammo > 0 and enemy.get('motion', enemy.get('data', {}).get('motion')) == 'WALK':
                    ammo -= 1
                    launches += 1
                    # Fixed-target/tile contact scenario. Each physical and
                    # elemental packet resolves separately with live defenses.
                    for tick in range(1, rocket['ticks'] + 1):
                        for damage_kind, raw in (('physical', attrs['atk'] * rocket['physical_scale']),
                                                  ('elemental', attrs['atk'] * rocket['elemental_scale'])):
                            due = now + tick * rocket['interval']
                            if due <= now + rocket['lifetime']:
                                heapq.heappush(pending, (due, launches * 10 + tick * 2 + (damage_kind == 'elemental'), damage_kind, raw))
                    if ammo <= 0:
                        active_until = now
        if hp <= 0:
            kill_time = now
            break
        if active and outcome == 'alive' and now >= invulnerable_until:
            apply_controls('hit')
        if sp_kind == 'INCREASE_WHEN_ATTACK' and not active:
            sp += 1
        if active and behavior['mode'] == 'ammo' and behavior.get('ammo_trigger') != 'erosion':
            ammo -= 1
            if ammo <= 0:
                active_until = now
                for state in effects.statuses:
                    if state.get('bound_to_skill') and state['end'] > now:
                        state['end'] = now
        elif active and behavior['mode'] in ('next_attack', 'conditional_heal'):
            active_until = now
        interval = attack_interval(attrs['baseAttackTime'] + (behavior['interval_add'] if active else 0),
                                   attrs['attackSpeed'] + (behavior['speed_add'] if active else 0))
        next_attack = max(now + interval, next_ready)
    return {'damage': round(total, 3), 'kill_time': kill_time, 'damage_instances': hits,
            'damage_by_type': {k: round(v, 3) for k, v in breakdown.items()},
            'attack_events': attacks, 'skill_activations': activations,
            'average_dps_over_horizon': round(total / horizon, 3),
            'first_skill_activation': first_activation, 'revives': revives, 'overloads': overloads,
            'remaining_hp': round(max(0, hp), 3), 'final_stacks': stacks,
            'remaining_ammo': ammo, 'bombardments_launched': launches,
            'effects': effects.report(kill_time if kill_time is not None else horizon),
            'scenario': 'continuous_single_target_contact_from_deployment',
            'timing': 'attack_cadence_estimate_without_animation_or_SP_tick_quantization'}


def route_segments(route):
    """Keep teleport discontinuities; never create attack coverage through the gap."""
    segments, points, visible, wait = [], [route['start']], True, 0.
    unknown = []
    for cp in route['checkpoints']:
        kind = cp['type']
        if kind == 'MOVE' and visible:
            points.append(cp['position'])
        elif kind == 'DISAPPEAR' and visible:
            segments.append(points)
            points, visible = [], False
        elif kind == 'APPEAR_AT_POS' and not visible:
            points, visible = [cp['position']], True
        elif kind == 'WAIT_FOR_SECONDS':
            wait += cp['time']
        else:
            unknown.append(kind)
    if visible:
        points.append(route['end'])
        segments.append(points)
    return {'segments': segments, 'wait_seconds': wait, 'unsupported': unknown}


def rotation_estimate(attrs, behavior, enemy, mechanics, *, horizon=120, skill_enabled=True, effect_rules=None):
    """Sustained single-target scenario with the enemy's defenses and state rules.

    Artificial durability avoids stopping at the first kill. This does not alter
    the source enemy or claim that a moving enemy stays in range for the horizon.
    Revival/inactive HP transitions are intentionally reserved for the real-HP
    duel; the rotation scenario measures attack/SP cadence against one life.
    """
    target = copy.deepcopy(enemy)
    target['attributes']['maxHp'] = 1e15
    state = dict(mechanics, revive_ratio=0, initial_hp_ratio=1)
    result = duel(attrs, behavior, target, state, horizon=horizon, skill_enabled=skill_enabled,
                  effect_rules=effect_rules)
    return {'scenario': 'sustained_contact_with_enemy_defences_and_artificial_durability',
            'horizon_seconds': horizon, 'damage': result['damage'],
            'average_dps': result['average_dps_over_horizon'],
            'attack_events': result['attack_events'], 'damage_instances': result['damage_instances'],
            'skill_activations': result['skill_activations'], 'overloads': result['overloads'],
            'damage_by_type': result['damage_by_type'], 'effects': result['effects'],
            'bombardments_launched': result['bombardments_launched'], 'remaining_ammo': result['remaining_ammo']}


def blocking_queue(capacity, arrivals):
    """A stated arrival/service scenario, including multi-slot and unblockable enemies."""
    _require(type(capacity) is int and capacity >= 0, 'Invalid blocking capacity')
    releases, used, peak, leaks = [], 0, 0, []
    for index, arrival in enumerate(sorted(arrivals, key=lambda a: a['time'])):
        at = number(arrival['time'], 'arrival time')
        cost = arrival['block_cost']
        _require(type(cost) is int and cost >= 0, 'Invalid enemy block cost')
        service = number(arrival['service_seconds'], 'kill service time')
        while releases and releases[0][0] <= at:
            _, released = heapq.heappop(releases)
            used -= released
        if cost == 0 or used + cost > capacity:
            leaks.append({'index': index, 'time': at, 'block_cost': cost})
        else:
            used += cost
            peak = max(peak, used)
            heapq.heappush(releases, (at + service, cost))
    return {'peak_occupied_slots': peak, 'overflow': leaks,
            'assumption': 'independent_service_times_without_shared_attack_or_survival'}


def evaluate_plan(graph, content, box, catalog, chars, skills, ranges, *, horizon=60,
                  equipment=None, equip_metadata=None, effect_rules=None):
    _require(graph.get('stage', {}).get('difficulty') == 'NORMAL',
             'Challenge combat evaluation is unsupported: difficulty runes are not simulated.')
    _, _, tiles = _map(graph)
    rows = []
    paths = {}
    for route in graph['routes']:
        trace = route_segments(route)
        cells = set()
        if not trace['unsupported'] and route['motion_mode'] == 'WALK':
            try:
                for segment in trace['segments']:
                    cells.update(tuple(p) for p in segment)
                    for first, second in zip(segment, segment[1:]):
                        cells.update(_shortest(tuple(first), tuple(second), tiles))
                cells.discard(tuple(route['end']))
                paths[route['index']] = cells
            except BattleSolverError:
                pass
    placements = {a['name']: a for a in content['actions'] if a.get('type') == 'Deploy'}
    for spec in content['opers']:
        identity = catalog.resolve(spec['name'])
        _require(identity is not None and identity.id in box.operators, 'Missing combat operator identity')
        progress, char = box.operators[identity.id], chars[identity.id]
        bounds, unknown = operator_attributes(char, progress)
        sid = identity.skills.get(spec['skill'])
        mastery = (progress.skills or {}).get(sid)
        _require(progress.main_skill_level is not None and mastery is not None and mastery.mastery is not None,
                 'Unknown combat skill training')
        level_number = progress.main_skill_level + mastery.mastery
        _require(sid in skills and 1 <= level_number <= len(skills[sid]['levels']), 'Skill source missing level')
        behavior = skill_behavior(sid, skills[sid]['levels'][level_number - 1], char)
        module = {'attributes': {}, 'talents': {}, 'trait': {}, 'unknown': []}
        if equipment is not None and equip_metadata is not None:
            module = equipment_effects(identity.id, spec, identity, progress, equipment, equip_metadata)
            mapping = {'atk': 'atk', 'def': 'def', 'max_hp': 'maxHp', 'attack_speed': 'attackSpeed'}
            for key, value in module['attributes'].items():
                if key in mapping:
                    bounds[mapping[key]] = [v + value for v in bounds[mapping[key]]]
                else:
                    unknown.append('equipment:attribute:' + key)
            unknown.extend(module['unknown'])
        elif spec.get('requirements', {}).get('module', 0) or progress.default_equip_id:
            unknown.append('equipment:combat_source_not_supplied')
        talents = operator_talents(char, progress, module)
        if behavior is not None:
            trait = module['trait']
            behavior['flat_penetration'] = (trait.get('magic_resist_penetrate_fixed', 0)
                                            if behavior['damage_type'] == 'arts' else 0) + behavior[
                                                'flat_penetration']
            if trait.get('attack_speed'):
                bounds['attackSpeed'] = [v + trait['attack_speed'] for v in bounds['attackSpeed']]
            if sid == 'skchr_wisdel_3' and talents:
                t = talents[0]
                count = 2 if trait.get('attack@enable_third_attack') == 1 else 1
                aftershock = trait.get('attack@append_atk_scale', .5)
                behavior['packets_normal'] = [t['attack@main_atk_scale']] + [aftershock] * count
                behavior['packets_skill'] = [behavior['attack_scale'] * s for s in behavior['packets_normal']]
                behavior['unknown'].append('wisadel:aftershock_delays_and_mark_explosions_not_simulated')
            elif sid in ('skchr_amgoat_2', 'skchr_amgoat_3') and talents:
                # This self-applicable caster talent shares the attack additive modifier zone.
                bonus = talents[0].get('atk', 0)
                behavior['attack_up'] += bonus
                for key in ('atk',):
                    bounds[key] = [v * (1 + bonus) for v in bounds[key]]
                # The active modifier must not apply the talent twice.
                behavior['attack_up'] = (1 + behavior['attack_up']) / (1 + bonus) - 1
        place = placements.get(spec['name'])
        _require(place is not None, 'Combat evaluation needs a fixed deployment')
        tile = tiles[tuple(place['location'])]
        if char.get('talents'):
            unknown.append('talents:effects_and_rng_not_simulated')
        entry = {'name': spec['name'], 'skill_id': sid, 'skill_level': level_number,
                 'attributes': bounds, 'location': place['location'], 'tile': tile['tile_key'],
                 'unknown': unknown, 'matchups': []}
        entry['equipment'] = module
        if behavior is None:
            entry.update(status='unsupported_skill')
            rows.append(entry)
            continue
        entry.update(status='conditional_estimate', behavior=behavior)
        unknown.extend(behavior['unknown'])
        base = {k: v[0] for k, v in bounds.items()}
        if tile['tile_key'] == 'tile_infection':
            effects = blackboard(tile['raw'].get('blackboard'))
            base['atk'] *= 1 + effects['atk']
            base['attackSpeed'] += effects['attack_speed']
            unknown.append('infection:self_damage_duration_and_healing_need_not_simulated')
        elif tile['tile_key'] not in ('tile_floor', 'tile_wall', 'tile_road'):
            unknown.append('placement_tile_effect:' + tile['tile_key'])
        range_id = char['phases'][progress.elite]['rangeId']
        _require(range_id in ranges, 'Missing authoritative normal attack range')
        # Game range offsets use right/up axes; normalize dy into graph's down axis.
        footprint = {tuple((place['location'][0] + dx, place['location'][1] + dy))
            for cell in ranges[range_id]['grids'] for dx, dy in [
                _rotate([cell['col'], -cell['row']], place['direction'])]}
        entry['range_tiles'] = [list(p) for p in sorted(footprint)]
        skill_range_id = skills[sid]['levels'][level_number - 1].get('rangeId') or range_id
        _require(skill_range_id in ranges, 'Missing authoritative skill attack range')
        skill_footprint = {tuple((place['location'][0] + dx, place['location'][1] + dy))
            for cell in ranges[skill_range_id]['grids'] for dx, dy in [
                _rotate([cell['col'], -cell['row']], place['direction'])]}
        entry['skill_range_tiles'] = [list(p) for p in sorted(skill_footprint)]
        entry['covered_routes_skill'] = sorted(i for i, path in paths.items() if path & skill_footprint)
        entry['covered_routes'] = sorted(i for i, path in paths.items() if path & footprint)
        entry['intercepted_routes'] = sorted(i for i, path in paths.items() if tuple(place['location']) in path)
        entry['block_count_normal'] = base['blockCnt']
        entry['block_count_skill'] = max(0, base['blockCnt'] + behavior['block_add'])
        entry['self_heal_per_second_skill'] = base['maxHp'] * behavior['self_heal_ratio']
        entry['attack_interval_normal'] = attack_interval(base['baseAttackTime'], base['attackSpeed'])
        entry['attack_interval_skill'] = attack_interval(base['baseAttackTime'] + behavior['interval_add'],
                                                         base['attackSpeed'] + behavior['speed_add'])
        if behavior['damage_type'] == 'healing':
            entry['healing_per_second_skill_per_target'] = (base['atk'] * (1 + behavior['attack_up'])
                * behavior['attack_scale'] / entry['attack_interval_skill'])
            entry['healing_target_limit_skill'] = behavior['targets']
        entry['first_charge_seconds'] = ((behavior['sp']['spCost'] - behavior['sp']['initSp']) /
            base['spRecoveryPerSec'] if behavior['sp']['spType'] == 'INCREASE_WITH_TIME'
            and base['spRecoveryPerSec'] > 0 else None)
        entry['conditional_control_scenarios'] = []
        for spec in behavior.get('control_events', []):
            if not spec.get('condition') or spec['condition'] == 'flying_target_in_range':
                continue
            for enemy in graph['enemies'].values():
                state = TargetEffects(enemy, effect_rules)
                if effect_rules is None:
                    state.unknown.add('control:pinned_effect_rules_missing')
                else:
                    duration = horizon if isinstance(spec['duration'], str) else spec['duration']
                    state.apply(spec['kind'], 0, duration, ratio=spec.get('ratio'))
                scenario = {'enemy_id': enemy['id'], 'condition': spec['condition'],
                    'assumption': 'one qualifying trigger at scenario start; contact duration is hypothetical',
                    'credited_to_primary_estimate': False, 'effects': state.report(horizon)}
                if spec['condition'] == 'target_in_vortex':
                    vortex = behavior['vortex']
                    scenario['while_in_vortex'] = {
                        'erosion_per_second_before_resistance': vortex['erosion_per_second'],
                        'physical_damage_per_tick_before_enemy_mechanics': damage_packet(base['atk'] * vortex[
                            'physical_scale_per_second'], 'physical', defense=enemy['attributes']['def'])}
                entry['conditional_control_scenarios'].append(scenario)
        if behavior['damage_type'] != 'healing':
            for enemy in graph['enemies'].values():
                mechanics = enemy_behavior(enemy, graph)
                matchup = {'enemy_id': enemy['id'], 'enemy': enemy['name'], 'mechanics': mechanics}
                if mechanics['kind'] == 'unsupported':
                    matchup['status'] = 'unsupported_enemy_mechanics'
                else:
                    matchup['status'] = 'conditional_estimate'
                    skill_enabled = behavior['automatic'] or spec.get('skill_usage', 0) != 0 or any(
                        a.get('type') == 'Skill' and a.get('name') == spec['name'] for a in content['actions'])
                    matchup['duel'] = duel(base, behavior, enemy, mechanics, horizon=horizon,
                        skill_enabled=skill_enabled, effect_rules=effect_rules)
                    matchup['rotation'] = rotation_estimate(base, behavior, enemy, mechanics,
                        horizon=horizon, skill_enabled=skill_enabled, effect_rules=effect_rules)
                    if mechanics['kind'] == 'matrix':
                        # One ordinary attack, without a passive deployment buff
                        # or active skill. Search combines this with finite path
                        # contact; it must not credit an early buff forever.
                        interval = entry['attack_interval_normal']
                        normal = duel(base, behavior, enemy, mechanics, horizon=interval,
                                      skill_enabled=False, effect_rules=effect_rules)
                        matchup['normal_attack_damage'] = normal['damage']
                    unknown.extend(enemy['id'] + ':' + item for item in
                                   matchup['duel']['effects']['unknown'])
                    if mechanics['kind'] == 'basin':
                        # A second explicit scenario exposes sensitivity to external matrix feeding.
                        matchup['max_stacks_duel'] = duel(base, behavior, enemy, mechanics, horizon=horizon,
                            skill_enabled=behavior['automatic'] or spec.get('skill_usage', 0) != 0,
                            start_stacks=mechanics['max_stacks'], effect_rules=effect_rules)
                        enemy_atk = enemy['attributes']['atk'] + mechanics['max_stacks'] * mechanics['attack_per_stack']
                        matchup['max_stacks_incoming_physical_dps'] = damage_packet(enemy_atk, 'physical',
                            defense=base['def'] * (1 + behavior['defense_up'])) / attack_interval(
                                enemy['attributes']['baseAttackTime'], enemy['attributes']['attackSpeed'])
                entry['matchups'].append(matchup)
            by_enemy = {m['enemy_id']: m for m in entry['matchups']}
            pressure = {}
            for spawn in graph['spawns']:
                if spawn['route_index'] not in entry['intercepted_routes']:
                    continue
                key = f"{spawn['wave']}:{spawn['fragment']}"
                item = pressure.setdefault(key, {'arrivals': [], 'unknown': []})
                service = by_enemy[spawn['enemy_id']].get('duel', {}).get('kill_time')
                if service is None:
                    item['unknown'].append(spawn['enemy_id'] + ':no_kill_within_horizon')
                    continue
                _require(spawn['count'] <= 5000, 'Blocking scenario spawn count too large')
                item['arrivals'].extend({'time': spawn['pre_delay'] + i * spawn['interval'],
                    'block_cost': by_enemy[spawn['enemy_id']]['mechanics'].get('block_cost', 1),
                    'service_seconds': service} for i in range(spawn['count']))
            entry['blocking_scenarios'] = {key: {
                'normal_capacity': blocking_queue(int(entry['block_count_normal']), item['arrivals']),
                'skill_capacity': blocking_queue(int(entry['block_count_skill']), item['arrivals']),
                'unknown': item['unknown'],
                'assumptions': ['Independent fragment-relative arrivals; no fabricated global spawn timeline.',
                                'Ordinary one-slot enemies; actual travel, stun, immunity and shared DPS are not included.']}
                for key, item in pressure.items()}
        rows.append(entry)
    return {'schema': 1, 'model_version': MODEL_VERSION, 'proof_status': 'unproven',
            'graph_sha256': sha256_bytes(canonical_json(graph)),
            'copilot_sha256': sha256_bytes(canonical_json(content)), 'horizon_seconds': horizon,
            'operators': rows, 'routes': {str(r['index']): route_segments(r) for r in graph['routes']},
            'map_unknown': graph['mechanics']['unknown'],
            'limitations': ['No full-map scheduler, target selection, blocking queue or survival simulation.',
                'Numbers describe the stated single-target scenario, not guaranteed live damage or victory.',
                'Unmodeled talents, modules, summons, fields and RNG remain explicit; do not use estimates as admission.',
                'Live admission requires three fresh identical practice three-stars independently of this report.']}
