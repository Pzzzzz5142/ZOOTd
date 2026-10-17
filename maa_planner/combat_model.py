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

MODEL_VERSION = 3


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


def damage_packet(attack, damage_type, *, defense=0, resistance=0,
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
              'unknown': [], 'sp': copy.deepcopy(level['spData']), 'duration': level['duration'],
              'automatic': level.get('skillType') == 'AUTO'}
    if skill_id == 'skchr_f12yin_2':
        result.update(mode='permanent', attack_up=b['atk'], block_add=b['block_cnt'],
                      targets=2, defense_up=b['def'],
                      self_heal_ratio=b['hp_recovery_per_sec_by_max_hp_ratio'])
    elif skill_id == 'skchr_huang_2':
        result.update(mode='permanent', attack_up=b['atk'], defense_up=b['def'], targets=3)
        result['unknown'].append('extended_range:not_applied')
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
    elif skill_id == 'skcom_assist_cost[2]':
        result.update(mode='cost', block_add=-1, targets=0)
    else:
        return None
    _require(result['hits'] >= 1 and result['targets'] >= 0, 'Invalid supported attack multiplicity')
    return result


def enemy_behavior(enemy, graph):
    b = blackboard(enemy.get('talent_blackboard'))
    result = {'kind': 'ordinary', 'unknown': [], 'revive_ratio': 0}
    # Bind an event adapter to data-owned environment and enemy identity.
    envs = [r for r in graph.get('mechanics', {}).get('runes') or []
            if r.get('key') == 'env_system_new' and any(
                x.get('valueStr') == 'env_044_act52side' for x in r.get('blackboard', []))]
    if envs and enemy['id'] == 'enemy_10220_kljbp':
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
    elif envs and enemy['id'] in ('enemy_10213_klbm', 'enemy_10214_klgn'):
        result.update(kind='reviving', revive_ratio=1 + b['reborn_attr.max_hp'],
                      revive_delay=b['reborn.duration'])
        result['unknown'].append('matrix_release_and_energy_field_interaction_not_simulated')
    elif enemy.get('skills') or enemy.get('talent_blackboard'):
        result.update(kind='unsupported')
        result['unknown'].append('unimplemented_enemy_skills_or_talents')
    return result


def duel(attrs, behavior, enemy, mechanics, *, horizon=60, skill_enabled=True, start_stacks=None):
    """Event-driven single-target continuous-contact scenario starting at deployment."""
    horizon = number(horizon, 'horizon', .001)
    _require(horizon <= 600, 'Combat horizon exceeds 600 seconds')
    e = enemy['attributes']
    hp = max_hp = number(e['maxHp'], 'enemy HP', .001)
    sp_data = behavior['sp']
    sp, need = number(sp_data['initSp'], 'initial SP'), number(sp_data['spCost'], 'SP cost')
    sp_kind = sp_data['spType']
    _require(sp_kind in ('INCREASE_WITH_TIME', 'INCREASE_WHEN_ATTACK', 'INCREASE_WHEN_TAKEN_DAMAGE'),
             'Unknown SP recovery type')
    sp_rate = number(attrs.get('spRecoveryPerSec', 1), 'SP recovery')
    now, last, active_until, next_ready = 0., 0., -1., 0.
    ammo, attacks, hits, total, revives, overloads = 0, 0, 0, 0., 0, 0
    stacks = mechanics.get('initial_stacks', 0) if start_stacks is None else start_stacks
    stack_hits, stunned_until, debuffed_until = 0, -1., -1.
    first_activation = kill_time = None
    while now < horizon and attacks < 100000:
        active = active_until > now
        if sp_kind == 'INCREASE_WITH_TIME':
            # Recharge only the part of the interval outside SP lockout.
            free_time = max(0., now - max(last, active_until)) if active_until > last else now - last
            sp = min(need * sp_data.get('maxChargeTime', 1), sp + free_time * sp_rate)
        if skill_enabled and not active and sp >= need and need > 0:
            sp -= need
            first_activation = now if first_activation is None else first_activation
            mode = behavior['mode']
            if mode in ('permanent', 'ammo'):
                active_until = math.inf
                ammo = behavior.get('ammo', 0)
            elif mode in ('next_attack', 'conditional_heal'):
                active_until = now + .00001
            else:
                active_until = now + max(0, behavior['duration'])
            active = True
        last = now
        attacks += 1
        if behavior['mode'] == 'cost' and active:
            interval = attack_interval(attrs['baseAttackTime'], attrs['attackSpeed'])
            now += interval
            continue
        attack = attrs['atk'] * (1 + (behavior['attack_up'] if active else 0))
        multiplier = behavior['attack_scale'] if active else 1
        packets = behavior.get('packets_skill' if active else 'packets_normal')
        packets = packets if packets is not None else [multiplier] * (behavior['hits'] if active else 1)
        # Ignition reduces the primary target's RES before its splash and
        # additional primary-target packet. Each packet advances enemy state.
        if active and behavior.get('resistance_debuff'):
            debuffed_until = now + behavior['debuff_duration']
        for packet in packets:
            if hp <= 0:
                break
            reduction = stacks * mechanics.get('reduction_per_stack', 0) if now >= stunned_until else 0
            resistance = e['magicResistance'] * (1 + behavior.get('resistance_debuff', 0)
                if now < debuffed_until else 1)
            damage = damage_packet(attack * packet, behavior['damage_type'],
                defense=e['def'], resistance=resistance, reduction=reduction,
                flat_penetration=behavior.get('flat_penetration', 0),
                percent_penetration=behavior.get('percent_penetration', 0))
            # Sum damage against HP, excluding overkill, so multi-hit ordering matters.
            total += min(hp, damage)
            hp -= damage
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
                break
        if hp <= 0:
            kill_time = now
            break
        if sp_kind == 'INCREASE_WHEN_ATTACK' and not active:
            sp += 1
        if active and behavior['mode'] == 'ammo':
            ammo -= 1
            if ammo <= 0:
                active_until = now
        elif active and behavior['mode'] in ('next_attack', 'conditional_heal'):
            active_until = now
        interval = attack_interval(attrs['baseAttackTime'] + (behavior['interval_add'] if active else 0),
                                   attrs['attackSpeed'] + (behavior['speed_add'] if active else 0))
        now = max(now + interval, next_ready)
    return {'damage': round(total, 3), 'kill_time': kill_time, 'damage_instances': hits,
            'first_skill_activation': first_activation, 'revives': revives, 'overloads': overloads,
            'remaining_hp': round(max(0, hp), 3), 'final_stacks': stacks,
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
                  equipment=None, equip_metadata=None):
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
                                            if behavior['damage_type'] == 'arts' else 0)
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
        if behavior['damage_type'] != 'healing':
            for enemy in graph['enemies'].values():
                mechanics = enemy_behavior(enemy, graph)
                matchup = {'enemy_id': enemy['id'], 'enemy': enemy['name'], 'mechanics': mechanics}
                if mechanics['kind'] == 'unsupported':
                    matchup['status'] = 'unsupported_enemy_mechanics'
                else:
                    matchup['status'] = 'conditional_estimate'
                    matchup['duel'] = duel(base, behavior, enemy, mechanics, horizon=horizon,
                        skill_enabled=behavior['automatic'] or spec.get('skill_usage', 0) != 0 or any(
                            a.get('type') == 'Skill' and a.get('name') == spec['name'] for a in content['actions']))
                    if mechanics['kind'] == 'basin':
                        # A second explicit scenario exposes sensitivity to external matrix feeding.
                        matchup['max_stacks_duel'] = duel(base, behavior, enemy, mechanics, horizon=horizon,
                            skill_enabled=behavior['automatic'] or spec.get('skill_usage', 0) != 0,
                            start_stacks=mechanics['max_stacks'])
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
                    'block_cost': 1, 'service_seconds': service} for i in range(spawn['count']))
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
