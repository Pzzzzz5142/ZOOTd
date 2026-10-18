"""Reviewed elemental and control state shared by all combat/search clients.

Buff values come from the graph's pinned source generation. This is a bounded
single-target effect model, not a spatial or enemy-AI simulator.
"""
from __future__ import annotations

import math

from .battle_solver import _require

EFFECTS_VERSION = 1


def numeric(value, name, minimum=0):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= minimum,
             'Invalid effect parameter: ' + name)
    return value


def effect_rules(buffs, breaks):
    """Project reviewed buff semantics; unrelated added definitions are allowed."""
    water = buffs['ep_break_water_enemy']
    _require(water['templateKey'] == 'ep_break_water_enemy' and water['isDurableBuff'] is True,
             'Unknown erosion buff semantics')
    modifiers = water['attributes']['attributeModifiers']
    _require(len(modifiers) == 1 and modifiers[0]['attributeType'] == 'DEF'
             and modifiers[0]['formulaItem'] == 'ADDITION'
             and modifiers[0]['loadFromBlackboard'] is False, 'Unknown erosion defense modifier')
    board = {b['key']: b['value'] for b in water['blackboard']}
    _require(len(board) == len(water['blackboard']) and board.get('max_stack_cnt') == -1,
             'Unknown erosion stack semantics')

    def modifier(key, attribute, formula):
        attrs = buffs[key]['attributes']
        _require(buffs[key]['templateKey'] == 'empty' and not any(attrs.get(k) for k in (
            'abnormalFlags', 'abnormalImmunes', 'abnormalAntis', 'abnormalCombos', 'abnormalComboImmunes')),
            'Unknown control buff semantics: ' + key)
        modifiers = attrs['attributeModifiers'] or []
        matches = [m for m in modifiers
                   if m['attributeType'] == attribute and m['formulaItem'] == formula]
        _require(len(matches) == 1 and matches[0]['loadFromBlackboard'] is False,
                 'Unknown control buff modifier: ' + key)
        _require(not any(m is not matches[0] and (m.get('loadFromBlackboard') or m.get('value') != 0)
                         for m in modifiers), 'Additional control buff modifier: ' + key)
        return numeric(matches[0]['value'], key, -1e6)

    return {'version': EFFECTS_VERSION, 'erosion_damage': numeric(board['ep_water_damage'], 'erosion damage'),
            'erosion_defense_loss': numeric(-modifiers[0]['value'], 'erosion defense loss'),
            'erosion_cooldown': numeric(breaks['WATER']['enemyElementBreakDuration'], 'erosion cooldown', .001),
            'cold_attack_speed': modifier('c2e_cold[debuff]', 'ATTACK_SPEED', 'ADDITION'),
            'frozen_resistance': modifier('c2e_freeze', 'MAGIC_RESISTANCE', 'ADDITION'),
            'sluggish_ratio': 1 + modifier('sluggish', 'MOVE_SPEED', 'FINAL_SCALER')}


class TargetEffects:
    def __init__(self, enemy, rules):
        _require(rules is None or isinstance(rules, dict) and type(rules.get('version')) is int
                 and rules['version'] == EFFECTS_VERSION, 'Unsupported combat effect rules version')
        self.enemy, self.attrs, self.rules = enemy, enemy['attributes'], rules
        self.statuses, self.unknown, self.bursts = [], set(), []
        self.erosion, self.cooldown_until, self.defense_stacks = 0., -1., 0
        self.level_type = enemy.get('level_type', enemy.get('data', {}).get('levelType'))

    def active(self, kind, now):
        return any(s['kind'] == kind and s['start'] <= now < s['end'] for s in self.statuses)

    def apply(self, kind, now, duration, *, ratio=None):
        numeric(now, 'control time')
        numeric(duration, 'control duration')
        _require(kind in ('cold', 'frozen', 'stun', 'silence', 'root', 'levitate', 'slow'),
                 'Unsupported control effect: ' + str(kind))
        immune = {'frozen': 'frozenImmune', 'stun': 'stunImmune',
                  'silence': 'silenceImmune', 'levitate': 'levitateImmune'}.get(kind)
        if immune:
            value = self.attrs.get(immune)
            if type(value) is not bool:
                self.unknown.add('control:missing_' + immune)
                return False
            if value:
                return False
        if kind == 'levitate':
            if self.enemy.get('motion', self.enemy.get('data', {}).get('motion')) != 'WALK':
                return False
            if self.active('levitate', now):
                return False
            weight = self.attrs.get('massLevel')
            if type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0:
                self.unknown.add('control:missing_massLevel')
                return False
            duration *= .5 if weight > 3 else 1
        if kind == 'cold' and self.active('cold', now):
            if self.apply('frozen', now, duration):
                for state in self.statuses:
                    if state['kind'] == 'cold' and state['end'] > now:
                        state['end'] = now
                return True
        if kind == 'cold' and self.active('frozen', now):
            self.unknown.add('control:cold_reapplication_while_frozen_not_simulated')
            return False
        if kind == 'slow':
            numeric(ratio, 'slow ratio')
            _require(ratio <= 1, 'Slow ratio exceeds one')
        self.statuses.append({'kind': kind, 'start': now, 'end': now + duration, 'ratio': ratio})
        return True

    def resistance_add(self, now):
        return self.rules['frozen_resistance'] if self.rules and self.active('frozen', now) else 0

    def feed_erosion(self, amount, now):
        numeric(amount, 'erosion buildup')
        if amount <= 0 or now < self.cooldown_until:
            return False
        if self.rules is None:
            self.unknown.add('erosion:pinned_effect_rules_missing')
            return False
        if self.level_type not in ('NORMAL', 'ELITE', 'BOSS'):
            self.unknown.add('erosion:unknown_enemy_level_type')
            return False
        resistance = self.attrs.get('epResistance')
        if type(resistance) not in (int, float) or not math.isfinite(resistance) or not 0 <= resistance <= 100:
            self.unknown.add('erosion:unknown_damage_buildup_resistance')
            return False
        elemental_resistance = self.attrs.get('epDamageResistance')
        if (type(elemental_resistance) not in (int, float) or not math.isfinite(elemental_resistance)
                or elemental_resistance < 0):
            self.unknown.add('erosion:unknown_elemental_resistance')
            return False
        # Reviewed default MAX_EP: normal/elite 1000, leader 2000. Do not infer
        # an unrecognized level type or silently accept an explicit override.
        threshold = 2000 if self.level_type == 'BOSS' else 1000
        if 'maxEp' in self.attrs:
            threshold = numeric(self.attrs['maxEp'], 'maxEp', .001)
        self.erosion += max(.05 * amount, amount * (1 - resistance / 100))
        if self.erosion < threshold:
            return False
        self.erosion = 0
        self.cooldown_until = now + self.rules['erosion_cooldown']
        self.defense_stacks += 1
        self.bursts.append(now)
        return True

    def reborn(self, now):
        # The reviewed erosion defense buff is durable; control buffs and the
        # non-durable buildup/cooldown state do not transfer to the new life.
        self.erosion, self.cooldown_until = 0., -1.
        for status in self.statuses:
            if status['end'] > now:
                status['end'] = max(status['start'], now)

    def report(self, horizon):
        cuts = sorted({0., horizon, *(max(0., min(horizon, s[k]))
                      for s in self.statuses for k in ('start', 'end'))})
        stopped = attacks = abilities = unblocked = delay = cold = 0.
        for a, b in zip(cuts, cuts[1:]):
            states = {s['kind'] for s in self.statuses if s['start'] <= a < s['end']}
            moving = bool(states & {'stun', 'frozen', 'levitate', 'root'})
            stopped += (b - a) * moving
            attacks += (b - a) * bool(states & {'stun', 'frozen', 'levitate'})
            abilities += (b - a) * bool(states & {'stun', 'frozen', 'levitate', 'silence'})
            cold += (b - a) * ('cold' in states)
            unblocked += (b - a) * ('levitate' in states)
            slows = [s['ratio'] for s in self.statuses
                     if s['kind'] == 'slow' and s['start'] <= a < s['end']]
            delay += (b - a) * (1 if moving else 1 - min(slows, default=1))
        return {'version': EFFECTS_VERSION, 'observation_seconds': horizon, 'erosion_bursts': self.bursts,
                'remaining_erosion': round(self.erosion, 6), 'erosion_defense_stacks': self.defense_stacks,
                'movement_stopped_seconds': round(stopped, 6),
                'attack_disabled_seconds': round(attacks, 6),
                'abilities_suppressed_seconds': round(abilities, 6),
                'cold_seconds': round(cold, 6),
                'cold_attack_speed_modifier': (self.rules['cold_attack_speed'] if self.rules else None)
                                             if cold else 0,
                'unblockable_seconds': round(unblocked, 6),
                'movement_delay_equivalent_seconds': round(delay, 6),
                'statuses': self.statuses, 'unknown': sorted(self.unknown),
                'scope': 'continuous_target_contact; control durations are not a route or survival proof'}
