"""Game-owned stage/zone identities; disposable, revision-bound data under var/."""
from __future__ import annotations

import re
import time
import urllib.request
from pathlib import Path

from .prts import StageCatalog, PrtsError, decode, require, text, _NoRedirect
from .util import atomic_write_json, canonical_json, sha256_bytes

TABLES = ('stage_table', 'zone_table', 'activity_table', 'retro_table')
REPOSITORY = 'Kengxxiao/ArknightsGameData'
MAX_AGE = 6 * 3600


def compact_code(value: str) -> str:
    return re.sub(r'[\s\-]+', '', value).casefold()


def _request(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'ZOOTd-navigation/1'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=40) as response:
            raw = response.read(64 * 1024 * 1024 + 1)
    except OSError:
        raise PrtsError('navigation_data', 'Cannot refresh game navigation data.') from None
    require(len(raw) <= 64 * 1024 * 1024, 'Game table too large.')
    return decode(raw)


def load_tables(root: Path, *, refresh=False, now=None):
    now = time.time() if now is None else now
    cache = root / 'var/cache/copilot-navigation/catalog.json'
    if cache.exists() and not refresh:
        try:
            saved = decode(cache.read_bytes())
            if (saved['schema'] == 1 and 0 <= now - saved['fetched_at'] < MAX_AGE
                    and saved['sha256'] == sha256_bytes(canonical_json(saved['tables']))):
                return saved['tables'], {k: saved[k] for k in ('revision', 'fetched_at', 'sha256')}
        except (KeyError, TypeError, OSError, PrtsError):
            pass
    revision = _request(f'https://api.github.com/repos/{REPOSITORY}/commits/master').get('sha')
    require(isinstance(revision, str) and re.fullmatch('[0-9a-f]{40}', revision),
            'Invalid game data revision.')
    tables = {name: _request(f'https://raw.githubusercontent.com/{REPOSITORY}/{revision}/'
                            f'zh_CN/gamedata/excel/{name}.json') for name in TABLES}
    for name, data in tables.items():
        require(isinstance(data, dict), f'Invalid {name}.')
    evidence = {'revision': revision, 'fetched_at': now,
                'sha256': sha256_bytes(canonical_json(tables))}
    atomic_write_json(cache, {'schema': 1, **evidence, 'tables': tables}, mode=0o600)
    return tables, evidence


def title_text(value):
    value = re.sub(r'[\s·•・.\-]+', '', value)
    return re.sub(r'^复刻[:：]?|[:：]?复刻$', '', value)


def activity_labels(name, all_names):
    normalized = title_text(name)
    labels = [normalized]
    # Stylized title OCR sometimes loses the leading glyph. Accept a suffix
    # only when it still identifies a unique activity in the whole snapshot;
    # the target detail panel is verified independently before any battle.
    suffix = normalized[1:]
    if len(suffix) >= 4 and not any(suffix in other and other != normalized for other in all_names):
        labels.append(suffix)
    return labels


class NavigationCatalog(StageCatalog):
    """Normal playable stages, including EX and current events omitted by MAA stages.json."""

    def __init__(self, tables, installed, tiles, *, now=None, evidence=None):
        now = time.time() if now is None else now
        self.evidence = evidence or {}
        self.routes = {}
        game, zones, activities, retro = (tables[t] for t in TABLES)
        all_names = {title_text(a['name']) for group in (activities['basicInfo'], retro['retroActList'])
                     for a in group.values() if text(a.get('name'))}
        require(isinstance(tiles, dict), 'Invalid installed tile overview.')
        tile_index = {}
        for tile in tiles.values():
            if isinstance(tile, dict):
                key = (tile.get('code'), tile.get('stageId'), str(tile.get('levelId', '')).casefold())
                tile_index.setdefault(key, []).append(tile)
        installed_ids = {}
        for row in installed:
            installed_ids.setdefault(row.get('code'), set()).add(row.get('stageId'))
        rows = []
        bindings = []
        # Retros have their own zone IDs. They supersede expired original event
        # records, but never a currently open event with the same stage ID.
        stages = dict(retro['stageList'])
        for sid, stage in game['stages'].items():
            aid = activities['zoneToActivity'].get(stage['zoneId'])
            act = activities['basicInfo'].get(aid, {})
            if sid not in stages or act.get('startTime', 0) <= now < act.get('endTime', 0):
                stages[sid] = stage
        for sid, stage in stages.items():
            if (stage.get('difficulty') != 'NORMAL' or not text(stage.get('levelId'))
                    or not text(stage.get('code'))
                    or stage.get('diffGroup') not in ('NONE', 'NORMAL', None)):
                continue
            zone_id = stage['zoneId']
            zone = zones['zones'].get(zone_id, {})
            rid = retro['zoneToRetro'].get(zone_id)
            aid = activities['zoneToActivity'].get(zone_id)
            kind = zone.get('type')
            window = None
            if rid:
                act = retro['retroActList'][rid]
                route_kind = 'archive'
                available = now >= act['startTime']
                activity = act['name']
                instance = rid
            elif aid:
                act = activities['basicInfo'][aid]
                route_kind = 'activity'
                window = zones.get('zoneValidInfo', {}).get(zone_id, {})
                start = max(act['startTime'], window.get('startTs', act['startTime']))
                end = min(act['endTime'], window.get('endTs', act['endTime']))
                window = {'start': start, 'end': end}
                available = start <= now < end
                activity = act['name']
                instance = aid
            elif kind in ('MAINLINE', 'WEEKLY'):
                route_kind = 'main' if kind == 'MAINLINE' else 'supplies'
                available = True
                activity = ''
                instance = zone_id
            else:
                continue
            code = stage['code']
            chapter_match = re.search(r'(?:main_|EPISODE\s*)(\d+)',
                                      str(zone.get('zoneNameThird', '')) + ' ' + zone_id)
            if kind in ('MAINLINE', 'MAINLINE_ACTIVITY', 'MAINLINE_RETRO') or (rid and act.get('type') == 'MAINLINE'):
                route_kind = 'main'
            # Use existing farm identity only with an exact ID/code binding.
            ids = installed_ids.get(code, set()) & {sid, sid + '_perm'}
            canonical = sid + '_perm' if rid and sid + '_perm' in ids else sid
            cost = stage.get('apCost')
            require(type(cost) is int and 0 <= cost <= 999, 'Invalid game stage cost.')
            if aid:
                extra = activities.get('activity', {}).get(act.get('type'), {}).get(aid, {})
                first = extra.get('stageAdditionDataMap', {}).get(sid, {}).get('firstCost', cost)
                require(type(first) is int and 0 <= first <= 999, 'Invalid first-clear stage cost.')
                cost = max(cost, first)
            tile_matches = tile_index.get((code, sid, stage['levelId'].casefold()), [])
            names = list(dict.fromkeys(zone[f].strip() for f in
                         ('zoneNameSecond', 'zoneNameFirst', 'zoneNameTitleCurrent', 'zoneNameTitleUnCurrent')
                         if text(zone.get(f))))
            unlock_texts = []
            if aid:
                extra = activities.get('activity', {}).get(act.get('type'), {}).get(aid, {})
                to_visit, seen = [sid], set()
                while to_visit:
                    prerequisite = to_visit.pop()
                    if prerequisite in seen:
                        continue
                    seen.add(prerequisite)
                    toast = extra.get('stageUnlockToastMap', {}).get(prerequisite, {}).get('unlockToast')
                    if text(toast):
                        unlock_texts.append(toast)
                    previous = game['stages'].get(prerequisite, {})
                    to_visit.extend(c['stageId'] for c in previous.get('unlockCondition', [])
                                    if isinstance(c, dict) and text(c.get('stageId')))
            self.routes[canonical] = {
                'kind': route_kind, 'stage_id': canonical, 'battle_id': sid, 'code': code,
                'activity': activity, 'activity_labels': activity_labels(activity, all_names),
                'activity_id': instance, 'zone_id': zone_id,
                'zone_names': names, 'available': available, 'window': window,
                'ap_cost': cost, 'tile_available': len(tile_matches) == 1,
                'locked_texts': list(dict.fromkeys(unlock_texts)),
                'chapter': int(chapter_match[1]) if chapter_match else None,
                'select_normal': stage.get('diffGroup') == 'NORMAL',
            }
            rows.append({'stageId': canonical, 'code': code, 'levelId': stage['levelId']})
            bindings.append((canonical, sid))
        super().__init__(rows)
        for canonical, sid in bindings:
            self.aliases.setdefault(sid.casefold(), set()).add(canonical)
            self.query_ids[canonical] = sid
        self.short_codes = {}
        for stage, route in self.routes.items():
            self.short_codes.setdefault(compact_code(route['code']), set()).add(stage)

    def resolve(self, value):
        if text(value):
            matches = self.aliases.get(value.strip().casefold())
            if matches is None:
                matches = self.short_codes.get(compact_code(value), set())
            if len(matches) == 1:
                return next(iter(matches))
        raise PrtsError('stage_identity', 'Unknown or ambiguous normal stage; specify its stageId.')

    def route(self, stage, *, require_tiles=False):
        result = dict(self.routes[self.resolve(stage)])
        if not result['available']:
            raise PrtsError('stage_unavailable', 'Stage or event zone is outside its opening window.')
        if require_tiles and not result['tile_available']:
            raise PrtsError('stage_map_missing', 'Installed MAA battle map is missing; update the runtime before execution.')
        return result


def load_navigation(root: Path, *, refresh=False):
    tables, evidence = load_tables(root, refresh=refresh)
    resource = root / 'var/data/resource'
    installed = decode((resource / 'stages.json').read_bytes())
    tiles = decode((resource / 'Arknights-Tile-Pos/overview.json').read_bytes())
    return NavigationCatalog(tables, installed, tiles, evidence=evidence)
