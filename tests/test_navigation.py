import copy
import json
import tempfile
import time
from contextlib import ExitStack, contextmanager
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.navigation_samples import sample_route

from maa_planner.copilot_core import map_recognized
from maa_planner.copilot_navigation import navigation_tasks
from maa_planner.navigation_catalog import NavigationCatalog, load_navigation, load_tables, MAX_AGE, activity_labels, title_text
from maa_planner.prts import PrtsError
from maa_planner.navigation_cli import navigate, navigation_complete


def startup_transition_events(task='ReturnButton', *, task_id=2, process_id=0, first=None):
    """Synthetic native lifecycle, with separate enclosing/subtask identities."""
    def event(message, identifier, **value):
        return {'message': message, 'details': {
            'uuid': 'device', 'taskid': identifier, 'taskchain': 'StartUp', **value}}
    process = dict(subtask='ProcessTask', first=first or ['StartUpBegin'])
    return [event(10001, task_id),
            event(20001, process_id, **process, details={
                'task': 'StartUpBegin', 'algorithm': 'JustReturn', 'action': 'DoNothing'}),
            event(20002, process_id, **process, details={
                'task': 'StartUpBegin', 'algorithm': 'JustReturn', 'action': 'DoNothing'}),
            event(20003, process_id, **process, what='ExceededLimit', details={
                'task': task, 'exec_times': 0, 'max_times': 0}),
            event(10002, task_id), event(3, task_id, finished_tasks=[task_id])]


def fixture():
    stages, retros, zones, tiles = {}, {}, {}, {}
    for code, sid, zone, name, retro in (
        ('NL-9', 'act13side_09', 'nl-zone', '大骑士领', True),
        ('MN-EX-7', 'act13d5_ex07', 'mn-ex-zone', ' 钳兽牢笼', True),
        ('DS-1', 'act1dp_s01', 'dp-zone', '轻游', False),
        ('XX-3', 'future_03', 'future-zone', '未来分区', False),
    ):
        row = {'code': code, 'stageId': sid, 'zoneId': zone, 'levelId': 'activities/' + sid,
               'difficulty': 'NORMAL', 'diffGroup': 'NONE', 'apCost': 0 if code == 'DS-1' else 18}
        (retros if retro else stages)[sid] = row
        zones[zone] = {'type': 'SIDESTORY' if retro else 'ACTIVITY', 'zoneNameSecond': name}
        tiles[sid] = {k: row[k] for k in ('stageId', 'levelId', 'code')}
    hard = dict(retros['act13d5_ex07'], stageId='act13d5_ex07#f#', difficulty='FOUR_STAR')
    retros[hard['stageId']] = hard
    tiles[hard['stageId']] = hard
    tables = {
        'stage_table': {'stages': stages},
        'zone_table': {'zones': zones, 'zoneValidInfo': {}},
        'activity_table': {
            'zoneToActivity': {'dp-zone': 'dp', 'future-zone': 'future'},
            'basicInfo': {aid: {'name': name, 'startTime': 100, 'endTime': 200, 'type': 'EXAMPLE'}
                          for aid, name in [('dp', '逐影集趣'), ('future', '全新活动')]},
            'activity': {'EXAMPLE': {'dp': {'stageAdditionDataMap': {'act1dp_s01': {'firstCost': 40}}}}}},
        'retro_table': {'stageList': retros, 'zoneToRetro': {'nl-zone': 'nl', 'mn-ex-zone': 'mn'},
                        'retroActList': {'nl': {'name': '长夜临光', 'startTime': 50},
                                         'mn': {'name': '玛莉娅·临光', 'startTime': 50}}},
    }
    installed = [{'code': 'NL-9', 'stageId': 'act13side_09_perm'}]
    return tables, installed, tiles


def pipeline_catalog(root, *, raid=False):
    """A game table fixture for the existing mocked execution pipeline."""
    rows = json.loads((root / 'var/data/resource/stages.json').read_text())
    if any('apCost' not in row for row in rows):
        raise PrtsError('stage_cost', 'Missing stage cost')
    tables, _, tiles = fixture()
    tables['retro_table']['stageList'] = {
        row['stageId']: dict(row, zoneId='nl-zone', levelId='activities/' + row['stageId'],
                             difficulty='NORMAL', diffGroup='NONE') for row in rows}
    tiles = {row['stageId']: dict(row, levelId='activities/' + row['stageId']) for row in rows}
    return NavigationCatalog(tables, rows, tiles, now=150, evidence={'sha256': 'fixture'}, raid=raid)


class NavigationTests(unittest.TestCase):
    def test_promoted_overlays_supply_new_event_maps_in_worker_load_order(self):
        tables, installed, tiles = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'var/data/resource'
            overlay = root / 'var/data/MaaResource/resource'
            cache = root / 'var/data/cache/resource'
            for resource in (base, overlay, cache):
                (resource / 'Arknights-Tile-Pos').mkdir(parents=True)
            (base / 'stages.json').write_text(json.dumps(installed))
            old_tiles = {key: row for key, row in tiles.items() if key != 'future_03'}
            (base / 'Arknights-Tile-Pos/overview.json').write_text(json.dumps(old_tiles))
            (overlay / 'Arknights-Tile-Pos/overview.json').write_text(json.dumps(tiles))
            with patch('maa_planner.navigation_catalog.load_tables', return_value=(tables, {'sha256': 'fixture'})), \
                 patch('maa_planner.navigation_catalog.time.time', return_value=150):
                catalog = load_navigation(root)
                self.assertTrue(catalog.route('XX-3', require_tiles=True)['tile_available'])
                self.assertEqual(catalog.evidence['installed_resources']['Arknights-Tile-Pos/overview.json']['path'],
                                 str(overlay / 'Arknights-Tile-Pos/overview.json'))
                # A later hot-cache overview supersedes the earlier layer.
                (cache / 'Arknights-Tile-Pos/overview.json').write_text(json.dumps(old_tiles))
                with self.assertRaisesRegex(PrtsError, 'battle map'):
                    load_navigation(root).route('XX-3', require_tiles=True)
                (cache / 'Arknights-Tile-Pos/overview.json').write_text('[]')
                with self.assertRaisesRegex(PrtsError, 'tile overview'):
                    load_navigation(root)

    def catalog(self, **kwargs):
        return NavigationCatalog(*fixture(), now=150, **kwargs)

    def test_requested_examples_and_unknown_future_activity_use_same_code(self):
        catalog = self.catalog()
        for code, kind, zone in [('nl 9', 'archive', '大骑士领'),
                                ('mn ex 7', 'archive', '钳兽牢笼'),
                                ('ds1', 'activity', '轻游'), ('xx3', 'activity', '未来分区')]:
            route = catalog.route(code, require_tiles=True)
            self.assertEqual(route['kind'], kind)
            self.assertEqual(route['zone_names'], [zone])
            tasks = navigation_tasks(route)
            self.assertEqual(tasks['ZootdStage']['text'], [route['code']])
            self.assertEqual(tasks['ZootdZone']['text'], [zone])
            if '-EX-' in route['code']:
                self.assertEqual(tasks['ZootdZoneTab']['text'], ['EX'])
                self.assertEqual(tasks['ZootdZoneTab']['specialParams'], [])
                self.assertEqual(tasks['ZootdZoneTab']['roi'], [640, 550, 640, 170])
                self.assertFalse(tasks['ZootdZoneTab']['fullMatch'])
                self.assertNotIn('ZootdZoneTabAscii', tasks)
                for task in ('ZootdZoneTab', 'ZootdZoneTabGlyph'):
                    self.assertEqual(tasks[task]['next'], ['ZootdStage', 'ZootdMapReady'])
                self.assertLess(tasks['ZootdEnter']['next'].index('ZootdZoneTabGlyph'),
                                tasks['ZootdEnter']['next'].index('ZootdZoneTab'))
                self.assertLess(tasks['ZootdEnter']['next'].index('ZootdZoneTabGlyph'),
                                tasks['ZootdEnter']['next'].index('ZootdMapReady'))
                from maa_planner.navigation_vision import normalize
                self.assertEqual(normalize('RHINE-Ex', tasks['ZootdZoneTab']['ocrReplace']), 'RHINE-EX')
                self.assertEqual(normalize('巨x', tasks['ZootdZoneTab']['ocrReplace']), 'EX')
                self.assertEqual(normalize('巨X', tasks['ZootdZoneTab']['ocrReplace']), 'EX')
                self.assertEqual(normalize('其他巨x文字', tasks['ZootdZoneTab']['ocrReplace']), '其他巨x文字')
            self.assertEqual(tasks['ZootdStageConfirmed']['next'],
                             ['NormalConfirm', 'ChangeToNormalDifficulty'] if route['has_raid'] else [])
        self.assertEqual(catalog.resolve('MN-EX-7'), 'act13d5_ex07')
        self.assertEqual(catalog.resolve('act13side_09'), 'act13side_09_perm')
        self.assertEqual(catalog.query_ids['act13side_09_perm'], 'act13side_09')
        with self.assertRaises(PrtsError):
            catalog.resolve('act13d5_ex07#f#')

    def test_challenge_content_alias_does_not_authorize_challenge_navigation(self):
        from maa_planner.prts import PrtsCopilotClient
        from tests.test_prts import row
        catalog = self.catalog()
        self.assertEqual(catalog.resolve_copilot('act13d5_ex07#f#'), 'act13d5_ex07')
        with self.assertRaises(PrtsError):
            catalog.route('act13d5_ex07#f#')
        content = {'stage_name': 'act13d5_ex07#f#', 'difficulty': 3,
                   'doc': {'title': 'Both modes'}, 'opers': [], 'actions': []}
        candidate, _ = PrtsCopilotClient(catalog)._parse(row(content=content), 'act13d5_ex07')
        self.assertEqual(candidate.difficulty, 3)
        with self.assertRaises(PrtsError):
            PrtsCopilotClient(catalog)._parse(row(content=content), 'act13side_09_perm')
        with self.assertRaises(PrtsError):
            catalog.resolve_copilot('act13side_09#f#')  # No game-owned challenge partner.

    def test_raid_identity_is_separate_and_bound_to_normal_search(self):
        normal, raid = self.catalog(), self.catalog(raid=True)
        route = raid.route('mn ex 7', require_tiles=True)
        self.assertTrue(route['raid'])
        self.assertEqual(route['battle_id'], 'act13d5_ex07#f#')
        for alias in ('MN-EX-7', 'act13d5_ex07', 'act13d5_ex07#f#', route['battle_id'].upper()):
            self.assertEqual(raid.resolve(alias), route['stage_id'])
        self.assertEqual(raid.query_ids[route['stage_id']], 'act13d5_ex07')
        self.assertFalse(normal.route('MN-EX-7')['raid'])
        self.assertEqual(navigation_tasks(route)['ZootdStageConfirmed']['next'], [])
        with self.assertRaises(PrtsError):
            raid.resolve('NL-9')  # No challenge record; never invent one.

    def test_raid_requires_exact_partner_and_its_own_tile(self):
        for field, value in [('code', 'MN-EX-8'), ('zoneId', 'nl-zone'),
                             ('levelId', 'foreign-level'), ('diffGroup', 'TOUGH')]:
            tables, installed, tiles = fixture()
            tables['retro_table']['stageList']['act13d5_ex07#f#'][field] = value
            with self.assertRaises(PrtsError):
                NavigationCatalog(tables, installed, tiles, now=150, raid=True)
        tables, installed, tiles = fixture()
        del tiles['act13d5_ex07#f#']
        catalog = NavigationCatalog(tables, installed, tiles, now=150, raid=True)
        self.assertFalse(catalog.route('MN-EX-7')['tile_available'])
        with self.assertRaisesRegex(PrtsError, 'battle map'):
            catalog.route('MN-EX-7', require_tiles=True)

    def test_custom_raid_preflight_explicitly_binds_native_templates(self):
        tasks = navigation_tasks(self.catalog(raid=True).route('MN-EX-7'))
        self.assertEqual(tasks['ZootdRaidConfirmed']['template'],
                         ['NormalDifficulty.png', 'NormalDifficulty-Chapter15.png'])
        self.assertEqual(tasks['ZootdRaidSwitch']['template'],
                         ['RaidDifficulty.png', 'RaidDifficulty-Chapter15.png'])
        self.assertEqual(tasks['ZootdRaidConfirmed']['action'], 'DoNothing')
        self.assertEqual(tasks['ZootdRaidSwitch']['maxTimes'], 3)
        self.assertEqual(tasks['ZootdRaidSwitch']['exceededNext'], [])
        self.assertEqual(tasks['ChangeToRaidDifficulty']['exceededNext'], ['RaidConfirm'])

    def test_current_event_raid_keeps_window_and_challenge_cost(self):
        tables, installed, tiles = fixture()
        normal = tables['stage_table']['stages']['future_03']
        hard = dict(normal, stageId='future_03#f#', difficulty='FOUR_STAR', apCost=25)
        tables['stage_table']['stages'][hard['stageId']] = hard
        tiles[hard['stageId']] = hard
        catalog = NavigationCatalog(tables, installed, tiles, now=150, raid=True)
        self.assertEqual(catalog.route('XX-3')['ap_cost'], 25)
        with self.assertRaisesRegex(PrtsError, 'opening window'):
            NavigationCatalog(tables, installed, tiles, now=200, raid=True).route('XX-3')

    def test_current_zone_window_and_first_clear_cost(self):
        self.assertEqual(self.catalog().route('DS-1')['ap_cost'], 40)
        tables, installed, tiles = fixture()
        tables['zone_table']['zoneValidInfo']['dp-zone'] = {'startTs': 160, 'endTs': 200}
        with self.assertRaisesRegex(PrtsError, 'opening window'):
            NavigationCatalog(tables, installed, tiles, now=150).route('ds1')
        with self.assertRaisesRegex(PrtsError, 'opening window'):
            NavigationCatalog(*fixture(), now=200).route('ds1')

    def test_missing_battle_map_does_not_prevent_zero_battle_navigation(self):
        tables, installed, tiles = fixture()
        del tiles['act1dp_s01']
        catalog = NavigationCatalog(tables, installed, tiles, now=150)
        self.assertFalse(catalog.route('DS-1')['tile_available'])
        with self.assertRaisesRegex(PrtsError, 'battle map'):
            catalog.route('DS-1', require_tiles=True)

    def test_ambiguous_compact_alias_never_picks_first(self):
        tables, installed, tiles = fixture()
        row = dict(tables['stage_table']['stages']['act1dp_s01'], stageId='duplicate', code='DS1')
        tables['stage_table']['stages']['duplicate'] = row
        catalog = NavigationCatalog(tables, installed, tiles, now=150)
        with self.assertRaises(PrtsError):
            catalog.resolve('d s 1')
        self.assertEqual(catalog.resolve('DS-1'), 'act1dp_s01')

    def test_exact_detail_evidence_rejects_other_stage_and_wrong_phase(self):
        event = {'taskchain': 'Custom', 'subtask': 'ProcessTask',
                 'details': {'task': 'ZootdStageConfirmed', 'algorithm': 'OcrDetect',
                             'action': 'DoNothing', 'result': {'text': 'MN-EX-7'}}}
        self.assertTrue(map_recognized(20002, event, 'MN-EX-7'))
        for code in ('NL-9', 'MN-EX-1', 'MN-EX-70'):
            self.assertFalse(map_recognized(20002, event, code))
        self.assertFalse(map_recognized(20001, event, 'MN-EX-7'))
        event['details']['task'] = 'ZootdMapScan'
        self.assertFalse(map_recognized(20002, event, 'MN-EX-7'))

    def test_activity_title_fallback_requires_unique_long_suffix(self):
        self.assertEqual(title_text('玛莉娅·临光·复刻'), title_text('玛莉娅·临光'))
        self.assertEqual(activity_labels('玛莉娅·临光', {'玛莉娅临光', '长夜临光'}),
                         ['玛莉娅临光', '莉娅临光'])
        self.assertEqual(activity_labels('甲莉娅·临光', {'甲莉娅临光', '乙莉娅临光'}),
                         ['甲莉娅临光'])
        self.assertEqual(activity_labels('长夜临光', {'长夜临光'}), ['长夜临光'])

    def test_native_maa_tasks_are_reused_without_battle_actions(self):
        route = self.catalog().route('NL-9')
        tasks = navigation_tasks(route)
        for name, base in [('ZootdStage', 'ClickStageName'),
                           ('ZootdStagePanel', 'ClickedCorrectStageOrSwipe')]:
            self.assertEqual(tasks[name]['baseTask'], base)
        self.assertEqual(tasks['ZootdStagePanel']['action'], 'DoNothing')
        self.assertNotIn('ZootdSpecialPanel', tasks['ZootdStage']['next'])
        self.assertEqual(tasks['StartUp@CloseAnno']['template'], 'CloseAnno.png')
        self.assertEqual(tasks['StartUp@CloseAnno']['next'][-1], 'StartUp@ReturnButtons#next')
        self.assertFalse(tasks['ZootdStage']['isAscii'])
        self.assertEqual(tasks['ZootdStage']['specialParams'], [])
        self.assertEqual(tasks['StartUp@ReturnButtons']['next'][0], 'StartUp@ReturnButton')
        self.assertEqual(tasks['ZootdStartUpTexturedReturn']['template'], 'Return.png')
        self.assertEqual(tasks['ZootdStartUpTexturedReturn']['templThreshold'], 0.7)
        route.update(kind='main', chapter=16, select_normal=True)
        self.assertEqual(navigation_tasks(route)['ZootdEntry']['sub'],
                         ['Episode16', 'ChapterDifficultyNormal'])
        route.update(kind='supplies')
        self.assertEqual(navigation_tasks(route)['ZootdEntry']['baseTask'], 'ResourceStages')

    def test_resource_entry_search_reuses_native_geometry_in_both_directions(self):
        route = self.catalog().route('DS-1')
        route.update(kind='supplies')
        tasks = navigation_tasks(route)
        self.assertEqual(tasks['ZootdEntry']['next'], ['ZootdZone', 'ZootdResourceLeft'])
        self.assertEqual(tasks['ZootdResourceLeft']['baseTask'], 'SwipeToTheLeft')
        self.assertEqual(tasks['ZootdResourceRight']['baseTask'], 'SwipeToTheRight')
        self.assertLessEqual(sum(tasks[name]['maxTimes'] for name in
                                 ('ZootdResourceLeft', 'ZootdResourceRight')), 9)
        self.assertEqual(tasks['ZootdResourceLeft']['exceededNext'],
                         ['ZootdZone', 'ZootdResourceRight'])
        self.assertEqual(tasks['ZootdResourceRight']['exceededNext'], [])

    def test_navigation_graph_has_bounded_fanout_and_no_coordinate_scan(self):
        for code in ('NL-9', 'MN-EX-7', 'DS-1', 'XX-3'):
            tasks = navigation_tasks(self.catalog().route(code))
            self.assertLessEqual(len(tasks), 32)
            for task in tasks.values():
                for edge in ('next', 'exceededNext', 'onErrorNext'):
                    self.assertLessEqual(len(task.get(edge, [])), 6)
                self.assertFalse(task.get('withoutDet'))
            self.assertLessEqual(sum(task.get('maxTimes', 0) for task in tasks.values()
                                     if task.get('action') == 'Swipe'), 60)
            self.assertEqual(tasks['ZootdZone']['maxTimes'], 1)
            self.assertEqual(tasks['ZootdStagePanel']['next'],
                             ['ZootdStageConfirmed', 'ZootdMapReady'])
            self.assertTrue(tasks['ZootdStageConfirmed']['fullMatch'])
            self.assertEqual(tasks['ZootdStageConfirmed']['action'], 'DoNothing')

    def test_special_archive_rook_opens_map_once_and_keeps_exact_target_proof(self):
        route = sample_route('NL-S-1')
        tasks = navigation_tasks(route)
        tab = tasks['ZootdSpecialZoneRook']
        self.assertEqual(tab['template'], 'StageZone-SpecialRook.png')
        self.assertEqual(tab['roi'], [640, 550, 640, 170])
        self.assertEqual(tab['maxTimes'], 1)
        self.assertEqual(tab['next'], ['ZootdStage', 'ZootdMapReady'])
        self.assertEqual(tab['exceededNext'], tab['next'])
        self.assertIn('ZootdSpecialZoneRook', tasks['ZootdEnter']['next'])
        self.assertEqual(tasks['ZootdStageConfirmed']['text'], ['NL-S-1', 'NLS1'])
        self.assertEqual(tasks['ZootdStageConfirmed']['action'], 'DoNothing')
        self.assertNotIn('ZootdSpecialZoneRook', navigation_tasks(dict(route, kind='activity')))
        self.assertNotIn('ZootdSpecialZoneRook', navigation_tasks(dict(route, code='NL-9')))

    def test_navigation_resources_install_both_isolated_glyphs(self):
        from maa_planner.copilot_navigation import install_navigation_resources
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            install_navigation_resources(destination)
            for name in ('StageZone-EX.png', 'StageZone-SpecialRook.png', 'StageDrops-Stars-0.png',
                         'StageResult-FriendCancel.png'):
                self.assertEqual((destination / 'template' / name).read_bytes(),
                                 (Path(__file__).parents[1] / 'maa_planner/resources' / name).read_bytes())

    def test_stranded_result_recovery_clears_defeat_and_zero_and_stops_at_two(self):
        from maa_planner.copilot_navigation import zero_result_recovery_tasks
        tasks = zero_result_recovery_tasks()
        self.assertEqual(tasks['ZootdRecoverZeroResult']['next'], [
            'ZootdRecoverThreeStars', 'ZootdRecoverAdverseStars', 'ZootdRecoverTwoStars',
            'ZootdRecoverFailure', 'ZootdRecoverZeroStars', 'ZootdRecoverNoZero'])
        self.assertEqual(tasks['ZootdRecoverZeroStars']['next'], ['ZootdRecoverFriendPrompt', 'ZootdRecoverClearZero'])
        self.assertEqual(tasks['ZootdRecoverTwoStars']['action'], 'Stop')
        self.assertEqual(tasks['ZootdRecoverTwoStars']['next'], [])
        self.assertEqual(tasks['ZootdRecoverClearZero']['maxTimes'], 3)
        self.assertEqual(tasks['ZootdRecoverClearZero']['baseTask'], 'ClickCorner')
        self.assertEqual(tasks['ZootdRecoverFailure']['baseTask'], 'FightMissionFailed')
        self.assertEqual(tasks['ZootdRecoverFailure']['maxTimes'], 1)
        self.assertEqual(tasks['ZootdRecoverFailure']['next'], ['ZootdRecoverZeroStars', 'ZootdRecoverNoZero'])
        self.assertEqual(tasks['ZootdRecoverFriendPrompt']['action'], 'DoNothing')
        self.assertEqual(tasks['ZootdRecoverFriendPrompt']['next'], ['ZootdRecoverFriendCancel'])
        self.assertEqual(tasks['ZootdRecoverFriendCancel']['template'], 'StageResult-FriendCancel.png')
        self.assertEqual(tasks['ZootdRecoverFriendCancel']['maxTimes'], 1)
        self.assertEqual(tasks['ZootdRecoverFriendCancel']['next'], ['ZootdRecoverClearZero'])
        self.assertTrue(all(t['onErrorNext'] == [] and t['exceededNext'] == [] for t in tasks.values()))

    def test_event_lock_texts_follow_game_prerequisite_graph(self):
        tables, installed, tiles = fixture()
        tables['stage_table']['stages']['act1dp_s01']['unlockCondition'] = [{'stageId': 'previous'}]
        extra = tables['activity_table']['activity']['EXAMPLE']['dp']
        extra['stageUnlockToastMap'] = {
            'act1dp_s01': {'unlockToast': '通关DP-4解锁'},
            'previous': {'unlockToast': '通关DP-1解锁'}}
        route = NavigationCatalog(tables, installed, tiles, now=150).route('DS-1')
        self.assertEqual(route['locked_texts'], ['通关DP-4解锁', '通关DP-1解锁'])
        tasks = navigation_tasks(route)
        self.assertEqual(tasks['ZootdNavigationLocked']['action'], 'DoNothing')
        self.assertEqual(tasks['ZootdNavigationLocked']['next'], [])
        self.assertEqual(tasks['ZootdZone']['next'].count('ZootdNavigationLocked'), 1)

    def test_encrypted_zero_cost_entrance_collects_conditions_without_stage_proof(self):
        route = self.catalog().route('NL-9')
        self.assertNotIn('ZootdEncryptedEntry', navigation_tasks(route))
        route['ap_cost'] = 0
        self.assertNotIn('ZootdEncryptedEntry', navigation_tasks(route))
        route = sample_route('DV-S-2')
        tasks = navigation_tasks(route)
        self.assertIn('ZootdEncryptedEntry', tasks['ZootdEnter']['next'])
        self.assertEqual(tasks['ZootdEncryptedEntry']['maxTimes'], 1)
        self.assertEqual(tasks['ZootdEncryptedConditions']['text'], ['查看条件'])
        self.assertEqual(tasks['ZootdEncryptedRecordPage']['action'], 'DoNothing')
        self.assertEqual(tasks['ZootdEncryptedRecordPage']['next'],
                         ['ZootdEncryptedReconstruct', 'ZootdEncryptedBlocked'])
        self.assertEqual(tasks['ZootdEncryptedReconstruct']['text'], ['事件重构'])
        self.assertEqual(tasks['ZootdEncryptedReconstruct']['maxTimes'], 1)
        self.assertEqual(tasks['ZootdEncryptedBlocked']['next'], [])

    def test_snapshot_ttl_integrity_and_single_revision_download(self):
        tables, _, _ = fixture()
        revision = 'a' * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replies = [{'sha': revision}, *[tables[n] for n in
                       ('stage_table', 'zone_table', 'activity_table', 'retro_table')]]
            with patch('maa_planner.navigation_catalog._request', side_effect=replies) as request:
                result, evidence = load_tables(root, now=1000)
            self.assertEqual(result, tables)
            self.assertTrue(all('/' + revision + '/' in c.args[0] for c in request.call_args_list[1:]))
            with patch('maa_planner.navigation_catalog._request', side_effect=PrtsError('network', 'offline')) as request:
                self.assertEqual(load_tables(root, now=1001)[1], evidence)
                request.assert_not_called()
                with self.assertRaises(PrtsError):
                    load_tables(root, now=1000 + MAX_AGE)
                with self.assertRaises(PrtsError):
                    load_tables(root, now=1001, refresh=True)
                path = root / 'var/cache/copilot-navigation/catalog.json'
                saved = json.loads(path.read_text()); saved['tables']['retro_table'] = {}
                path.write_text(json.dumps(saved))
                with self.assertRaises(PrtsError):
                    load_tables(root, now=1001)


class NavigationCliTests(unittest.TestCase):
    @staticmethod
    def target_events():
        def event(message, **value):
            return {'message': message, 'details': {
                'uuid': 'device', 'taskid': 4, 'taskchain': 'Custom', **value}}
        return [event(10001), event(20002, subtask='ProcessTask', first=['ZootdNavigate'], details={
            'task': 'ZootdStageConfirmed', 'algorithm': 'OcrDetect', 'action': 'DoNothing',
            'result': {'text': 'NL-9'}}), event(10002), event(3, finished_tasks=[4])]

    def test_disabled_startup_nodes_are_not_navigation_failure_or_target_evidence(self):
        for task in ('ReturnButton', 'StartButton1'):
            for interface_id, process_id in ((2, 0), (72, 31), (0, 9)):
                with self.subTest(task=task, ids=(interface_id, process_id)):
                    prefix = startup_transition_events(task, task_id=interface_id, process_id=process_id,
                        first=['StartUpBegin', 'FutureSettingsEntry'])
                    target = self.target_events()
                    self.assertTrue(navigation_complete(prefix + target, 'NL-9'))
                    self.assertFalse(navigation_complete(prefix, 'NL-9'))
                    for index in range(len(target)):
                        self.assertFalse(navigation_complete(prefix + target[:index] + target[index + 1:], 'NL-9'))

    def test_startup_exception_requires_observed_identity_scope_and_successful_terminal(self):
        prefix, target = startup_transition_events(), self.target_events()
        for index, field, value in [
                (3, 'taskid', 99), (3, 'taskid', True), (3, 'taskid', -1),
                (3, 'uuid', 'other-device'), (3, 'taskchain', 'Custom'),
                (3, 'subtask', 'UnknownTask'), (4, 'taskid', 99),
                (5, 'uuid', 'other-device'), (5, 'taskchain', 'Custom'),
                (5, 'finished_tasks', [99]), (5, 'finished_tasks', [True, 2])]:
            changed = copy.deepcopy(prefix)
            changed[index]['details'][field] = value
            with self.subTest(index=index, field=field, value=value):
                self.assertFalse(navigation_complete(changed + target, 'NL-9'))
        for indices in ((0,), (1, 2), (4,), (5,)):
            changed = [event for index, event in enumerate(prefix) if index not in indices]
            self.assertFalse(navigation_complete(changed + target, 'NL-9'))
        notification = prefix[3]
        for records in ([notification] + prefix[:3] + prefix[4:] + target,
                        prefix[:3] + prefix[4:5] + [notification] + prefix[5:] + target,
                        prefix[:3] + prefix[4:] + [notification] + target,
                        prefix[:3] + prefix[4:] + target[:2] + [notification] + target[2:]):
            self.assertFalse(navigation_complete(records, 'NL-9'))
        target[0]['details']['uuid'] = 'other-device'
        self.assertFalse(navigation_complete(prefix + target, 'NL-9'))
        for interface_id, invalid_id in ((0, False), (1, True)):
            for index in (4, 5):
                changed = startup_transition_events(task_id=interface_id, process_id=31)
                changed[index]['details']['taskid'] = invalid_id
                self.assertFalse(navigation_complete(changed + self.target_events(), 'NL-9'))

    def test_startup_exception_never_allows_errors_or_unknown_limit_semantics(self):
        prefix, target = startup_transition_events(), self.target_events()
        for field, value in [('task', 'FutureDisabledNode'), ('exec_times', 1),
                             ('max_times', 1), ('exec_times', True), ('max_times', False),
                             ('exec_times', '0'), ('action', 'Stop')]:
            changed = copy.deepcopy(prefix)
            changed[3]['details']['details'][field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(navigation_complete(changed + target, 'NL-9'))
        for message in (0, 1, 10000, 10004, 20000, 20004):
            changed = copy.deepcopy(prefix)
            changed[3]['message'] = message
            self.assertFalse(navigation_complete(changed + target, 'NL-9'))
        for what in ('GameOffline', 'Disconnect', 'Reconnecting'):
            changed = copy.deepcopy(prefix)
            changed[3]['details']['what'] = what
            self.assertFalse(navigation_complete(changed + target, 'NL-9'))
        # Even the recognized zero-limit semantics remain fatal in navigation.
        custom = copy.deepcopy(prefix[3])
        custom['details'].update(taskchain='Custom', taskid=4, first=['ZootdNavigate'])
        self.assertFalse(navigation_complete(target[:2] + [custom] + target[2:], 'NL-9'))

    def test_zero_battle_requires_fresh_exact_panel_and_worker_completion(self):
        for mode, expected in [('ok', 'success'), ('wrong_stage', 'failed'),
                               ('old', 'failed'), ('unfinished', 'failed'), ('locked', 'failed'),
                               ('missing_end', 'failed'), ('wrong_chain', 'failed'),
                               ('startup_transition', 'success'), ('startup_nonzero', 'failed'),
                               ('startup_old', 'failed')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                catalog = NavigationCatalog(*fixture(), now=150, evidence={'sha256': 'test'})
                def mock(name, **kwargs):
                    return stack.enter_context(patch('maa_planner.navigation_cli.' + name, **kwargs))
                mock('load_navigation', return_value=catalog)
                mock('command', side_effect=['', 'head'])
                mock('validate_runtime_receipt', return_value='sealed')
                @contextmanager
                def fake_device(*args):
                    yield 'device'
                mock('device', side_effect=fake_device)
                def execute(root, run, address):
                    tasks = json.loads((run / 'navigation/resource/tasks/tasks.json').read_text())
                    self.assertTrue((run / 'navigation/resource/template/StageZone-EX.png')
                                    .read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
                    self.assertEqual(tasks['ExpiringMedicineConfirm']['algorithm'], 'JustReturn')
                    self.assertEqual(tasks['ExpiringMedicineConfirm']['action'], 'Stop')
                    for name in ('StartButton1', 'StartButton2', 'MedicineConfirm', 'StoneConfirm'):
                        self.assertEqual(tasks[name]['action'], 'Stop')
                    self.assertFalse((run / 'params.json').exists())
                    event = {'run_id': 'old' if mode == 'old' else run.name, 'sequence': 0,
                             'recorded_ns': time.monotonic_ns(), 'message': 20002,
                             'details': {'taskchain': 'Custom', 'subtask': 'ProcessTask',
                                         'details': {'task': 'ZootdStageConfirmed', 'action': 'DoNothing',
                                                     'algorithm': 'OcrDetect',
                                                     'result': {'text': 'NL-8' if mode == 'wrong_stage' else 'NL-9'}}}}
                    event['details'].update(uuid='device', taskid=4 if mode != 'wrong_chain' else 9,
                                            first=['ZootdNavigate'])
                    records = [{'message': 10001, 'details': {'taskchain': 'Custom', 'uuid': 'device', 'taskid': 4}},
                               event,
                               {'message': 10002, 'details': {'taskchain': 'Custom', 'uuid': 'device', 'taskid': 4}},
                               {'message': 3, 'details': {'finished_tasks': [4], 'taskchain': 'Custom', 'uuid': 'device', 'taskid': 4}}]
                    if mode == 'missing_end':
                        del records[2]
                    if mode.startswith('startup_'):
                        prefix = startup_transition_events()
                        if mode == 'startup_nonzero':
                            prefix[3]['details']['details']['max_times'] = 1
                        records = prefix + records
                    for index, record in enumerate(records):
                        record.update(run_id=event['run_id'], sequence=index, recorded_ns=time.monotonic_ns())
                    if mode == 'startup_old':
                        records[3]['run_id'] = 'old-run'
                    (run / 'callbacks.jsonl').write_text('\n'.join(json.dumps(r) for r in records))
                    phase = 'stage_locked' if mode == 'locked' else 'navigation' if mode == 'unfinished' else 'navigation_complete'
                    (run / 'worker-result.json').write_text(json.dumps({'phase': phase}))
                    return 1 if mode == 'locked' else 0
                mock('execute', side_effect=execute)
                result = navigate(root, 'nl 9')
                self.assertEqual(result['status'], expected)
                self.assertFalse(result['consumes_sanity'])
                if mode == 'locked':
                    self.assertEqual(result['category'], 'stage_locked')

    def test_plan_does_not_start_device_or_validate_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('maa_planner.navigation_cli.load_navigation',
                      return_value=NavigationCatalog(*fixture(), now=150)), \
                patch('maa_planner.navigation_cli.device') as device, \
                patch('maa_planner.navigation_cli.validate_runtime_receipt') as runtime:
            self.assertEqual(navigate(Path(tmp), 'DS-1', plan_only=True)['status'], 'planned')
            device.assert_not_called()
            runtime.assert_not_called()


if __name__ == '__main__':
    unittest.main()
