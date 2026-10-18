"""Read-only catalog of reviewed operator skill models for the dashboard.

The page answers one question: which Box operators the automatic solver can
actually reason about, and what each reviewed skill model does or does not
simulate. It reads the same static tables as ``battle-solver`` and probes
``skill_behavior`` instead of maintaining a second hand-written list.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .combat_model import MODEL_VERSION, SKILL_MODEL_NOTES, skill_behavior

PROFESSIONS = {'WARRIOR': '近卫', 'SNIPER': '狙击', 'CASTER': '术师', 'MEDIC': '医疗',
               'SUPPORT': '辅助', 'TANK': '重装', 'PIONEER': '先锋', 'SPECIAL': '特种'}
RARITIES = {'TIER_6': ('六星', 6), 'TIER_5': ('五星', 5), 'TIER_4': ('四星', 4),
            'TIER_3': ('三星', 3), 'TIER_2': ('二星', 2), 'TIER_1': ('一星', 1)}
MODE_LABELS = {'permanent': '永久', 'ammo': '弹药', 'next_attack': '下次攻击',
               'conditional_heal': '条件治疗', 'cost': '回费', 'duration': '持续'}
DAMAGE_LABELS = {'physical': '物理', 'arts': '法术', 'healing': '治疗', 'true': '真实'}

# Cached by the exact inputs and their mtimes. Box syncs and game-data
# revisions invalidate it without restarting the dashboard.
_CACHE: dict[tuple, dict] = {}
_LOCK = threading.Lock()


def _value(number):
    """Compact number formatting for chips; avoids 2.3500000000000005."""
    return ('%g' % number) if isinstance(number, float) else str(number)


def _source_directory(root: Path):
    base = root / 'var/cache/battle-data'
    if not base.is_dir():
        return None
    candidates = []
    for directory in base.iterdir():
        static = directory / 'static'
        chars = static / 'character_table.json'
        skills = static / 'skill_table.json'
        if chars.is_file() and skills.is_file():
            candidates.append((chars.stat().st_mtime, directory.name, static))
    if not candidates:
        return None
    _, revision, static = max(candidates)
    return revision, static


def _read_json(path: Path):
    return json.loads(path.read_bytes().decode('utf-8'))


def _box_operators(root: Path):
    path = root / 'var/state/operator-box.json'
    try:
        value = _read_json(path)
    except (OSError, ValueError):
        return None
    operators = value.get('operators') if isinstance(value, dict) else None
    if not isinstance(operators, dict) or type(value.get('schema')) is not int:
        return None
    return {key: row for key, row in operators.items() if isinstance(row, dict)}


def _skill_level(progress, sid, count):
    """Use the account's current training when known, otherwise the last level."""
    if progress is None:
        return count, 'model'
    main = progress.get('main_skill_level')
    training = (progress.get('skills') or {}).get(sid)
    mastery = training.get('mastery') if isinstance(training, dict) else None
    if type(main) is not int or type(mastery) is not int:
        return count, 'model'
    return min(count, max(1, main + mastery)), 'box'


def _summary(behavior):
    chips = [MODE_LABELS.get(behavior['mode'], behavior['mode'])]
    chips.append(DAMAGE_LABELS.get(behavior['damage_type'], behavior['damage_type']))
    if behavior.get('damage_type_normal') not in (None, behavior['damage_type']):
        chips.append('常态 ' + DAMAGE_LABELS.get(behavior['damage_type_normal'],
                                                 behavior['damage_type_normal']))
    if behavior.get('attack_up'):
        chips.append('攻击力 %+.0f%%' % (behavior['attack_up'] * 100))
    if behavior.get('attack_scale', 1) != 1:
        chips.append('倍率 ×' + _value(behavior['attack_scale']))
    if behavior.get('hits', 1) > 1:
        chips.append('%d 段' % behavior['hits'])
    if behavior['mode'] == 'cost':
        chips.append('回费 +' + _value(behavior.get('cost_gain', 0)))
    elif behavior['damage_type'] == 'healing':
        chips.append('治疗目标 %d' % behavior['targets'])
    elif behavior.get('targets', 1) > 1:
        chips.append('目标 %d' % behavior['targets'])
    if behavior.get('interval_add'):
        chips.append('攻击间隔 %+gs' % behavior['interval_add'])
    if behavior.get('speed_add'):
        chips.append('攻击速度 %+g' % behavior['speed_add'])
    if behavior.get('block_add'):
        chips.append('阻挡 %+d' % behavior['block_add'])
    if behavior.get('ammo'):
        chips.append('弹药 %d' % behavior['ammo'])
    if behavior.get('flat_penetration'):
        chips.append('无视法抗 ' + _value(behavior['flat_penetration']))
    if behavior.get('self_heal_ratio'):
        chips.append('自回 %.0f%% 最大生命/秒' % (behavior['self_heal_ratio'] * 100))
    if behavior['mode'] == 'duration' and behavior.get('duration', 0) > 0:
        chips.append('持续 ' + _value(behavior['duration']) + 's')
    if behavior.get('normal_attack') is False:
        chips.append('通常不攻击')
    if behavior.get('ground_block') is False:
        chips.append('不阻挡地面')
    if behavior.get('passive'):
        chips.append('被动')
    elif behavior.get('automatic'):
        chips.append('自动触发')
    return chips


def build_catalog(root):
    """Return the dashboard payload, or an explicit unavailable state."""
    source = _source_directory(root)
    if source is None:
        return {'schema': 1, 'state': 'unavailable',
                'message': '未找到缓存的关卡静态数据，请先运行 battle-solver fetch。'}
    revision, static = source
    chars_path = static / 'character_table.json'
    skills_path = static / 'skill_table.json'
    try:
        box_stat = ((root / 'var/state/operator-box.json').stat().st_mtime,
                    (root / 'var/state/operator-box.json').stat().st_size)
    except OSError:
        box_stat = None
    key = (str(root), revision, chars_path.stat().st_mtime, chars_path.stat().st_size,
           skills_path.stat().st_mtime, skills_path.stat().st_size, box_stat)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        catalog = _build(chars_path, skills_path, revision, _box_operators(root))
        _CACHE.clear()
        _CACHE[key] = catalog
        return catalog


def _build(chars_path, skills_path, revision, box):
    chars = _read_json(chars_path)
    skills = _read_json(skills_path)
    operators, modeled_skills = [], set()
    for cid, char in sorted(chars.items()):
        if not isinstance(char, dict) or not char.get('name'):
            continue
        progress = box.get(cid) if box else None
        entries = []
        for slot, spec in enumerate(char.get('skills') or [], 1):
            sid = spec.get('skillId')
            table = skills.get(sid) if isinstance(sid, str) else None
            if not table or not table.get('levels'):
                continue
            level, level_source = _skill_level(progress, sid, len(table['levels']))
            behavior = skill_behavior(sid, table['levels'][level - 1], char)
            if behavior is None:
                continue
            name = next((l.get('name') for l in reversed(table['levels']) if l.get('name')), sid)
            entries.append({
                'slot': slot, 'skill_id': sid, 'name': name, 'level': level,
                'level_source': level_source,
                'mode': behavior['mode'], 'damage_type': behavior['damage_type'],
                'automatic': behavior.get('automatic', False),
                'summary': _summary(behavior),
                'unknown': behavior.get('unknown', []),
                'note': SKILL_MODEL_NOTES.get(sid),
            })
            modeled_skills.add(sid)
        if not entries:
            continue
        rarity, rank = RARITIES.get(char.get('rarity'), (char.get('rarity') or '未知', 0))
        operators.append({
            'id': cid, 'name': char['name'], 'rarity': rarity, 'rarity_rank': rank,
            'profession': PROFESSIONS.get(char.get('profession'), char.get('profession') or '未知'),
            'in_box': None if box is None else cid in box,
            'skills': entries,
        })
    operators.sort(key=lambda row: (-row['rarity_rank'], row['name']))
    covered = None if box is None else sum(1 for row in operators if row['in_box'])
    unmodeled = None
    if box is not None:
        known = {row['id'] for row in operators}
        unmodeled = sum(1 for cid in box if cid not in known)
    return {'schema': 1, 'state': 'ready', 'revision': revision,
            'model_version': MODEL_VERSION,
            'counts': {'modeled_operators': len(operators), 'modeled_skills': len(modeled_skills),
                       'box_operators': None if box is None else len(box),
                       'modeled_in_box': covered, 'unmodeled_in_box': unmodeled},
            'operators': operators, 'box_available': box is not None}
