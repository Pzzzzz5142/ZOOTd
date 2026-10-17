import copy
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from maa_planner.copilot_core import special_panel_complete
from maa_planner.copilot_navigation import navigation_tasks, special_panel_execution_tasks
from maa_planner.navigation_cli import navigation_complete
from maa_planner.navigation_layouts import (
    DV_SPECIAL_ACCESS, NL_SPECIAL_ROOK, STANDARD, active_sidestory_card, navigation_layout, stage_panel_tasks)
from maa_planner.navigation_vision import MapRecognizer
from tests.navigation_samples import SAMPLES, sample_protocol, sample_route
from tests import test_copilot_preflight as preflight


class NavigationLayoutTests(unittest.TestCase):
    def test_active_card_requires_the_observed_event_zone_and_full_normal_identity(self):
        route = {'kind': 'activity', 'activity_id': 'act52side', 'zone_id': 'act52side_zone1',
                 'code': 'YW-6', 'stage_id': 'act52side_06', 'battle_id': 'act52side_06',
                 'raid': False, 'activity': '昨日海', 'zone_names': ['雷亚-伊比利亚']}
        self.assertTrue(active_sidestory_card(route))
        tasks = navigation_tasks(route)
        entry = tasks['ZootdActiveSideStoryCard']
        self.assertTrue(entry['withoutDet'])
        self.assertTrue(entry['fullMatch'])
        self.assertEqual(entry['text'], ['SIDESTORY'])
        self.assertEqual(entry['maxTimes'], 1)
        self.assertNotIn('ZootdStageConfirmed', entry['next'])
        self.assertNotIn('ZootdZone', tasks['ZootdZone']['next'])
        self.assertEqual(tasks['ZootdZone']['maxTimes'], 1)
        self.assertEqual(tasks['ZootdStageConfirmed']['text'], ['YW-6', 'YW6'])
        for key, value in (('kind', 'archive'), ('activity_id', 'future-reissue'),
                           ('zone_id', 'act52side_zone2'), ('code', 'YW-EX-6'),
                           ('stage_id', 'act52side_07'), ('battle_id', 'act52side_06#f#'),
                           ('raid', True), ('raid', None)):
            changed = dict(route, **{key: value})
            self.assertFalse(active_sidestory_card(changed))
            self.assertNotIn('ZootdActiveSideStoryCard', navigation_tasks(changed))

    def test_current_ex_card_accepts_each_exact_mode_and_keeps_target_proof(self):
        for n in range(1, 9):
            for raid in (False, True):
                sid = f'act52side_ex{n:02d}' + ('#f#' if raid else '')
                route = dict(kind='activity', activity_id='act52side', zone_id='act52side_zone2',
                             code=f'YW-EX-{n}', stage_id=sid, battle_id=sid, raid=raid,
                             activity='昨日海', zone_names=['“我们的伊比利亚”'])
                self.assertTrue(active_sidestory_card(route))
                tasks = navigation_tasks(route)
                self.assertIn('ZootdActiveSideStoryCard', tasks)
                self.assertEqual(tasks['ZootdStageConfirmed']['text'][0], route['code'])
                self.assertEqual(tasks['ZootdZone']['text'], ['我们的伊比利亚'])
                self.assertEqual(tasks['ZootdZone']['roi'], [900, 410, 350, 90])
                self.assertEqual(tasks['ZootdZone']['action'], 'ClickRect')
                self.assertEqual(tasks['ZootdZone']['specificRect'], [1145, 410, 50, 60])
                self.assertTrue(tasks['ZootdZone']['fullMatch'])
                self.assertEqual(tasks['ZootdZone']['ocrReplace'], [[r'^[“”"]+|[“”"]+$', '']])
                for field, replacement in (('activity_id', 'future-event'),
                                           ('zone_id', 'act52side_zone1'),
                                           ('stage_id', 'act52side_ex09'),
                                           ('raid', not raid), ('raid', None)):
                    changed = dict(route, **{field: replacement})
                    self.assertFalse(active_sidestory_card(changed))
                    self.assertNotIn('ZootdActiveSideStoryCard', navigation_tasks(changed))

    def test_observed_archive_editions_have_separate_adapters(self):
        for code in ('DV-S-1', 'DV-S-2'):
            route = sample_route(code)
            self.assertEqual(navigation_layout(route), DV_SPECIAL_ACCESS)
            tasks = navigation_tasks(route)
            self.assertIn('ZootdEncryptedEntry', tasks['ZootdEnter']['next'])
            self.assertNotIn('ZootdSpecialZoneRook', tasks)
            self.assertEqual(tasks['ZootdStage']['next'][:-1], stage_panel_tasks(route)[:-1])
            for name in ('ZootdSpecialPanel', 'ZootdSpecialStart', 'ZootdSpecialStageConfirmed'):
                self.assertEqual(tasks[name]['action'], 'DoNothing')
                self.assertTrue(tasks[name]['fullMatch'])
            self.assertEqual(tasks['ZootdSpecialStageConfirmed']['text'], [code, code.replace('-', '')])
            self.assertEqual(special_panel_execution_tasks(route)['ClickedCorrectStage']['roi'], [770, 145, 250, 90])
        for n in range(1, 6):
            for suffix in ('', '#f#'):
                route = sample_route(f'NL-S-{n}' + suffix)
                self.assertEqual(navigation_layout(route), NL_SPECIAL_ROOK)
                tasks = navigation_tasks(route)
                self.assertIn('ZootdSpecialZoneRook', tasks['ZootdEnter']['next'])
                self.assertFalse(any('Encrypted' in name or 'SpecialPanel' in name for name in tasks))
                self.assertEqual(stage_panel_tasks(route), ['ZootdStagePanel', 'ZootdMapReady'])

    def assert_standard(self, route):
        self.assertEqual(navigation_layout(route), STANDARD)
        tasks = navigation_tasks(route)
        self.assertFalse(any('Encrypted' in name or 'SpecialPanel' in name or 'SpecialZoneRook' in name
                             for name in tasks))
        self.assertEqual(tasks['StartUp@CloseAnno']['next'],
                         ['StartUp@MainThemes#next', 'StartUp@CloseAnnos#next',
                          'StartUp@ReturnButtons#next'])
        self.assertEqual(tasks['ZootdStartUpTexturedReturn']['maxTimes'], 6)
        self.assertEqual(stage_panel_tasks(route), ['ZootdStagePanel', 'ZootdMapReady'])
        self.assertEqual(tasks['ZootdStageConfirmed']['text'], [route['code'], route['code'].replace('-', '')])
        with self.assertRaises(ValueError):
            special_panel_execution_tasks(route)

    def test_other_real_s_and_free_stages_keep_generic_navigation(self):
        for code in ('TW-S-1', 'DH-S-1', 'DH-MO-1', 'OF-1', 'DV-EX-1', 'NL-EX-1',
                     'MN-EX-7', 'DP-1', '1-7', 'LS-5', 'AP-5', 'SK-5'):
            with self.subTest(code=code):
                self.assert_standard(sample_route(code))

    def test_partial_or_mismatched_identity_cannot_select_an_adapter(self):
        for code in ('DV-S-2', 'NL-S-3', 'NL-S-3#f#'):
            original = sample_route(code)
            for field in ('kind', 'activity_id', 'zone_id', 'stage_id', 'battle_id', 'code', 'raid'):
                for replacement in (None, 'unknown'):
                    route = dict(original, **{field: replacement})
                    self.assertEqual(navigation_layout(route), STANDARD)
                route = dict(original)
                del route[field]
                if field == 'raid' and not original['raid']:
                    continue  # Old normal-only catalog fixtures omit this optional flag.
                self.assertEqual(navigation_layout(route), STANDARD)
            self.assertEqual(navigation_layout(dict(original, raid=not original['raid'])), STANDARD)
        for code in ('DV-S-2', 'NL-S-3'):
            route = sample_route(code)
            # A future/active edition with the same code and translated title is independent.
            self.assert_standard(dict(route, kind='activity', activity_id='future-reissue'))
            self.assert_standard(dict(route, activity_id='unknown-archive', ap_cost=0))

    def test_foreign_or_unbound_special_panel_evidence_never_adapts_copilot(self):
        records = preflight.special_events()
        route = sample_route('DV-S-2')
        for other in (None, sample_route('MN-EX-7'), dict(route, zone_id='other-section')):
            self.assertFalse(special_panel_complete(records, task_id=5, code='DV-S-2', route=other))
            self.assertFalse(navigation_complete(records, 'DV-S-2', route=other))
        self.assertFalse(special_panel_complete(records, task_id=5, code='DV-S-1', route=route))
        # Replay a foreign title/marker in the worker, including its click fallback.
        records[3]['details']['details']['result']['text'] = 'MN-EX-7'
        harness = preflight.PreflightDispatchTests()
        status, _, appended, task_file = harness.run_worker(
            [], raid=False, navigation_records=records, route=sample_route('MN-EX-7'), map_click=True)
        self.assertEqual(status, 1)
        self.assertFalse(task_file)
        self.assertNotIn('Copilot', [kind for kind, _ in appended])
        self.assertEqual(len(harness.loaded_tasks), 1)
        self.assertEqual(harness.loaded_tasks[0][1]['ZootdVisionClick']['next'],
                         ['ZootdStagePanel', 'ZootdMapReady'])

    def test_pre_event_native_protocols_still_prove_exact_stage(self):
        for code in ('MN-EX-7', 'DP-1', '1-7', 'LS-5', 'AP-5', 'SK-5'):
            with self.subTest(code=code):
                records, route = sample_protocol(code), sample_route(code)
                self.assertTrue(navigation_complete(records, code, route=route))
                self.assertFalse(navigation_complete(records, code + '0', route=route))
                confirmed = next(i for i, row in enumerate(records) if row['message'] == 20002
                                 and row['details'].get('details', {}).get('task') == 'ZootdStageConfirmed')
                for missing in (0, confirmed, len(records)-2, len(records)-1):
                    changed = copy.deepcopy(records)
                    del changed[missing]
                    self.assertFalse(navigation_complete(changed, code, route=route))
                for field, value in [('uuid', 'other'), ('taskid', 6), ('taskchain', 'StartUp'),
                                     ('finished_tasks', [5, 6]), ('finished_tasks', [])]:
                    changed = copy.deepcopy(records)
                    changed[-1]['details'][field] = value
                    self.assertFalse(navigation_complete(changed, code, route=route))
                for message in (10004, 20000, 20004):
                    changed = copy.deepcopy(records)
                    changed.insert(-1, {'message': message, 'details': dict(changed[-2]['details'])})
                    self.assertFalse(navigation_complete(changed, code, route=route))

    def test_foreign_footer_does_not_match_nl_rook(self):
        # Actual MN UI crop, captured before either event's repair session.
        image = cv2.imread(str(SAMPLES / 'mn-footer.png'))
        template = cv2.imread(str(Path(__file__).parents[1] / 'maa_planner/resources/StageZone-SpecialRook.png'))
        for mask in (cv2.inRange(template, np.array([180]*3), np.array([255]*3)),
                     cv2.inRange(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY), 180, 255)):
            score = cv2.minMaxLoc(cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED, mask=mask))[1]
            self.assertTrue(np.isfinite(score))
            self.assertLess(score, .8)


class NavigationImageReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resource = Path(__file__).parents[1] / 'var/data/resource'
        if not (cls.resource / 'PaddleCharOCR/det/inference.onnx').exists():
            raise unittest.SkipTest('Offline OCR replay requires installed MAA PaddleCharOCR resources')
        cls.recognizer = MapRecognizer(cls.resource, 'MN-EX-7')

    def test_real_title_crops_recognize_targets_and_reject_neighbors(self):
        for code in ('MN-EX-7', 'DP-1', '1-7', 'LS-5'):
            image = cv2.imread(str(SAMPLES / (code.lower() + '-title.png')))
            # Keep original pixel scale for the full-frame detector. This is
            # a composite test canvas, not a historical full-screen witness.
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            frame[80:80 + image.shape[0], 900:900 + image.shape[1]] = image
            for target, expected in ((code, True), (code + '0', False), ('DV-S-2', False)):
                with self.subTest(code=code, target=target), tempfile.TemporaryDirectory() as tmp:
                    self.recognizer.code = target
                    self.recognizer.template = None  # Exercise the real detector and recognizer.
                    candidates = list(self.recognizer.proposals(frame, Path(tmp)))
                    self.assertEqual(bool(candidates), expected)
                    if candidates:
                        self.assertEqual(candidates[0]['text'], code)
