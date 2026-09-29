import json
import tempfile
import time
from contextlib import ExitStack, contextmanager
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_planner.copilot_core import map_recognized
from maa_planner.copilot_navigation import navigation_tasks
from maa_planner.navigation_catalog import NavigationCatalog, load_tables, MAX_AGE, activity_labels, title_text
from maa_planner.prts import PrtsError
from maa_planner.navigation_cli import navigate, navigation_complete


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


def pipeline_catalog(root):
    """A game table fixture for the existing mocked execution pipeline."""
    rows = json.loads((root / 'var/data/resource/stages.json').read_text())
    if any('apCost' not in row for row in rows):
        raise PrtsError('stage_cost', 'Missing stage cost')
    tables, _, tiles = fixture()
    tables['retro_table']['stageList'] = {
        row['stageId']: dict(row, zoneId='nl-zone', levelId='activities/' + row['stageId'],
                             difficulty='NORMAL', diffGroup='NONE') for row in rows}
    tiles = {row['stageId']: dict(row, levelId='activities/' + row['stageId']) for row in rows}
    return NavigationCatalog(tables, rows, tiles, now=150, evidence={'sha256': 'fixture'})


class NavigationTests(unittest.TestCase):
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
            self.assertEqual(tasks['ZootdStageConfirmed']['next'], [])
        self.assertEqual(catalog.resolve('MN-EX-7'), 'act13d5_ex07')
        self.assertEqual(catalog.resolve('act13side_09'), 'act13side_09_perm')
        self.assertEqual(catalog.query_ids['act13side_09_perm'], 'act13side_09')
        with self.assertRaises(PrtsError):
            catalog.resolve('act13d5_ex07#f#')

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
                           ('ZootdStagePanel', 'ClickedCorrectStageOrSwipe'),
                           ('ZootdMapReset', 'FullStageNavigation'),
                           ('ZootdMapScan', 'StageNavigationSlowlySwipeLeft')]:
            self.assertEqual(tasks[name]['baseTask'], base)
        self.assertEqual(tasks['ZootdStagePanel']['action'], 'DoNothing')
        self.assertFalse(tasks['ZootdStage']['isAscii'])
        self.assertEqual(tasks['ZootdStage']['specialParams'], [])
        self.assertEqual(tasks['StartUp@ReturnButtons']['next'][0], 'StartUp@ReturnButton')
        self.assertEqual(tasks['ZootdStartUpTexturedReturn']['template'], 'Return.png')
        self.assertEqual(tasks['ZootdStartUpTexturedReturn']['templThreshold'], 0.7)
        self.assertEqual(tasks['ZootdMapScan']['exceededNext'], [])
        route.update(kind='main', chapter=16, select_normal=True)
        self.assertEqual(navigation_tasks(route)['ZootdEntry']['sub'],
                         ['Episode16', 'ChapterDifficultyNormal'])
        route.update(kind='supplies')
        self.assertEqual(navigation_tasks(route)['ZootdEntry']['baseTask'], 'ResourceStages')

    def test_stage_bands_keep_exact_target_and_panel_confirmation(self):
        tasks = navigation_tasks(self.catalog().route('DS-1'))
        for top in (0, 175, 350, 470):
            name = f'ZootdStageBand{top}'
            self.assertEqual(tasks[name]['baseTask'], 'ZootdStage')
            self.assertEqual(tasks[name]['roi'], [0, top, 1280, 250])
            self.assertIn(name, tasks['ZootdZone']['next'])
            self.assertIn(name, tasks['ZootdMapReset']['exceededNext'])
        self.assertEqual(tasks['ZootdStage']['next'][0], 'ZootdStagePanel')

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
    def test_zero_battle_requires_fresh_exact_panel_and_worker_completion(self):
        for mode, expected in [('ok', 'success'), ('wrong_stage', 'failed'),
                               ('old', 'failed'), ('unfinished', 'failed'), ('locked', 'failed'), ('missing_end', 'failed'), ('wrong_chain', 'failed')]:
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
                               {'message': 3, 'details': {'finished_tasks': [4]}}]
                    if mode == 'missing_end':
                        del records[2]
                    for index, record in enumerate(records):
                        record.update(run_id=event['run_id'], sequence=index, recorded_ns=time.monotonic_ns())
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
