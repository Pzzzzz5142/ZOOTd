"""Static game identities, independently sourced from player progression."""
from __future__ import annotations

import urllib.request

from .copilot_matcher import OperatorCatalog, OperatorIdentity
from .prts import decode, require, _NoRedirect
from .util import sha256_bytes

BASE = 'https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/master/zh_CN/gamedata/excel/'
# Native Copilot ABI / BattleQuickFormationModule templates. Game display order
# is a different namespace: e.g. Logos D has charEquipOrder=3, native module=4.
MAA_MODULE_TYPES = {'X': 1, 'Y': 2, 'A': 3, 'D': 4, 'B': 5}


def build_catalog(chars: dict, equips: dict, battle: dict) -> OperatorCatalog:
    identities = []
    for key, row in chars.items():
        if not key.startswith('char_') or key not in battle['chars'] or battle['chars'][key]['name'] != row['name']:
            continue
        skills = {i: s['skillId'] for i, s in enumerate(row['skills'], 1)}
        modules = {}
        for module in equips['charEquip'].get(key, []):
            entry = equips['equipDict'][module]
            index = MAA_MODULE_TYPES.get(entry.get('typeName2'))
            # Unknown future types remain unmapped; only that requirement loses
            # support, rather than rejecting unrelated operators in the table.
            if index is not None:
                require(index not in modules, 'Ambiguous native module type.')
                modules[index] = module
        identities.append(OperatorIdentity(key, row['name'], skills, modules, has_no_skills=not row['skills']))
    return OperatorCatalog(identities)


def fetch_catalog(battle: dict) -> tuple[OperatorCatalog, dict]:
    data, evidence = [], {}
    opener = urllib.request.build_opener(_NoRedirect())
    for name in ('character_table.json', 'uniequip_table.json'):
        url = BASE + name
        with opener.open(url, timeout=30) as response:
            raw = response.read(64 * 1024 * 1024 + 1)
        require(len(raw) <= 64 * 1024 * 1024, 'Static table too large.')
        data.append(decode(raw))
        evidence[name] = {'url': url, 'sha256': sha256_bytes(raw)}
    return build_catalog(*data, battle), evidence
