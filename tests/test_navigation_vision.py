import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from maa_planner.navigation_vision import (MAX_OCR_PASSES, MAX_SCANS, MAX_SWIPES,
                                          MapRecognizer, normalize, preprocess, scan_map, template_path)


class VisionTests(unittest.TestCase):
    def test_preprocessing_supports_both_label_polarities_without_coordinates(self):
        image = np.full((90, 180, 3), (180, 170, 240), dtype=np.uint8)
        image[20:25, 30:60] = 255
        image[65:70, 110:150] = 20
        white, dark = preprocess(image), preprocess(image, dark=True)
        self.assertEqual(tuple(white[22, 45]), (255, 255, 255))
        self.assertFalse(white[67, 130].any())
        self.assertEqual(tuple(dark[67, 130]), (235, 235, 235))
        self.assertFalse(dark[22, 45].any())

    def test_upstream_replacements_and_template_convention(self):
        self.assertEqual(normalize(' DP—I ', [['I', '1'], ['—', '-']]), 'DP-1')
        self.assertEqual(normalize('DP1', [[r'(DP)(1)', '$1-${2}']]), 'DP-1')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'template/StageNavigation/SideStory/DP/DP-1.png'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fixture')
            self.assertEqual(template_path(root, 'DP-1'), path)
            self.assertIsNone(template_path(root, '../DP-1'))
            self.assertIsNone(template_path(root, 'DP-2'))

    def scan(self, capture, confirm, proposals):
        with tempfile.TemporaryDirectory() as tmp, patch(
                'maa_planner.navigation_vision.MapRecognizer') as recognizer:
            recognizer.return_value.proposals.side_effect = proposals
            swipes = []
            result = scan_map(Path(tmp), 'DP-1', Path(tmp) / 'audit', capture=capture,
                              click_and_confirm=confirm, swipe=swipes.append)
            return result, json.loads((Path(tmp) / 'audit/result.json').read_text()), swipes

    def test_stationary_map_stops_without_exhausting_swipe_budget(self):
        result, audit, swipes = self.scan(lambda: np.zeros((720, 1280, 3), np.uint8),
                                           lambda rect: False, lambda *a, **kw: [])
        self.assertFalse(result)
        self.assertEqual(audit['reason'], 'stage_not_found_on_map_unchanged')
        self.assertEqual(len(swipes), 2)
        self.assertEqual(len(audit['scans']), 3)

    def test_moving_map_has_hard_budgets_and_rejects_unconfirmed_clicks(self):
        frames = iter(np.full((720, 1280, 3), i * 20, np.uint8) for i in range(MAX_SCANS))
        proposal = {'rect': [100, 100, 70, 20], 'branch': 'hsv_dark', 'text': 'DP-1', 'score': .9}
        result, audit, swipes = self.scan(lambda: next(frames), lambda rect: False,
                                           lambda *a, **kw: [proposal])
        self.assertFalse(result)
        self.assertEqual(audit['reason'], 'stage_not_found_on_map')
        self.assertEqual(len(swipes), MAX_SWIPES)
        self.assertLessEqual(MAX_SWIPES, 6)
        self.assertLessEqual(MAX_SCANS * MAX_OCR_PASSES, 14)

    def test_only_detail_confirmation_accepts_proposal(self):
        result, audit, swipes = self.scan(lambda: np.zeros((720, 1280, 3), np.uint8),
            lambda rect: rect == [100, 100, 70, 20],
            lambda *a, **kw: [{'rect': [100, 100, 70, 20], 'branch': 'hsv_dark'}])
        self.assertTrue(result)
        self.assertEqual(audit['reason'], 'detail_panel_confirmed')
        self.assertEqual(swipes, [])

    def test_full_image_ocr_filters_and_refreshes_after_rejected_click(self):
        recognizer = MapRecognizer.__new__(MapRecognizer)
        recognizer.code, recognizer.template, recognizer.replacements = 'DP-1', None, []
        box = np.array([[10, 10], [90, 10], [90, 40], [10, 40]], np.float32)
        recognizer.detector = lambda image: (np.array([box] * 4), 0)
        recognizer.recognizer = lambda crops: ([('DP-1', .49), ('1', .99),
                                               ('DP-10', .99), ('DP-1', .8)], 0)
        calls = []
        def capture():
            calls.append(True)
            return np.zeros((720, 1280, 3), np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            proposals = recognizer.proposals(capture(), Path(tmp), refresh=capture)
            first = next(proposals)
            self.assertEqual(first['text'], 'DP-1')
            self.assertEqual(first['score'], .8)
            count = len(calls)
            self.assertEqual(next(proposals)['branch'], 'hsv_dark')
            self.assertEqual(len(calls), count + 1)
            self.assertEqual(list(proposals), [])
            self.assertEqual(len(calls), 3)
