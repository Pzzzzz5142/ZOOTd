"""Static game identities, independently sourced from player progression."""
from __future__ import annotations

import urllib.request

from .copilot_matcher import OperatorCatalog, OperatorIdentity
from .prts import decode, require, _NoRedirect
from .util import sha256_bytes

BASE = 'https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/master/zh_CN/gamedata/excel/'


def build_catalog(chars: dict, equips: dict, battle: dict) -> OperatorCatalog:
    identities = []
    for key, row in chars.items():
        if not key.startswith('char_') or key not in battle['chars'] or battle['chars'][key]['name'] != row['name']:
            continue
        skills = {i: s['skillId'] for i, s in enumerate(row['skills'], 1)}
        modules = {}
        for module in equips['charEquip'].get(key, []):
            entry = equips['equipDict'][module]
            index = entry['charEquipOrder']
            # MAA's module numbers are the explicit game display order, not
            # the position of the module in charEquip or the Box dictionary.
            if type(index) is int and index > 0:
                require(index not in modules, 'Ambiguous module display order.')
                modules[index] = module
        identities.append(OperatorIdentity(key, row['name'], skills, modules))
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
