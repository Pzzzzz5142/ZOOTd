"""Bounded map recognition using installed MAA templates, models and OCR rules."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MAX_OCR_PASSES = 2
MAX_SCANS = 7
MAX_SWIPES = MAX_SCANS - 1
UNCHANGED_LIMIT = 2


def preprocess(image, *, dark=False):
    import cv2
    # MultiCopilotTaskPlugin::find_stage, v6.18.0. The second polarity is
    # required for dark labels on light cards; neither path uses coordinates.
    source = cv2.bitwise_not(image) if dark else image
    mask = cv2.inRange(cv2.cvtColor(source, cv2.COLOR_BGR2HSV), (0, 0, 160), (180, 30, 255))
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 8)))
    return cv2.bitwise_and(source, source, mask=mask)


def normalize(text, replacements):
    for pattern, replacement in replacements:
        replacement = re.sub(r'\$\{(\d+)\}|\$(\d+)',
                             lambda m: '\\g<' + (m[1] or m[2]) + '>', replacement)
        text = re.sub(pattern, replacement, text)
    return text.strip()


def template_path(resource: Path, code: str):
    # Same convention as StageNavigationHelper::get_stage_template_path.
    if not re.fullmatch(r'[A-Z]{2,3}-[A-Z0-9-]+', code):
        return None
    path = resource / 'template/StageNavigation/SideStory' / code.split('-')[0] / (code + '.png')
    return path if path.is_file() else None


def resource_file(resources, relative):
    """Match MaaCore's load order: the last installed overlay wins."""
    for resource in reversed(resources):
        path = resource / relative
        if path.is_file():
            return path
    raise FileNotFoundError(relative)


class MapRecognizer:
    def __init__(self, resource: Path, code: str):
        from rapidocr_onnxruntime.ch_ppocr_v3_det import TextDetector
        from rapidocr_onnxruntime.ch_ppocr_v3_rec import TextRecognizer
        self.code = code
        self.ocr_passes = 0
        resources = [resource] if isinstance(resource, Path) else resource
        self.template = next((path for root in reversed(resources)
                              if (path := template_path(root, code))), None)
        task = {}
        for root in resources:
            path = root / 'tasks/tasks.json'
            if path.is_file():
                task.update(json.loads(path.read_text()).get('ClickStageName', {}))
        self.replacements = task['ocrReplace']
        # No bundled RapidOCR models or network downloads are used.
        common = {'use_cuda': False, 'intra_op_num_threads': 2, 'inter_op_num_threads': 2}
        self.detector = TextDetector({**common, 'model_path': str(resource_file(resources, 'PaddleCharOCR/det/inference.onnx'))})
        self.recognizer = TextRecognizer({**common, 'model_path': str(resource_file(resources, 'PaddleCharOCR/rec/inference.onnx')),
                                         'keys_path': str(resource_file(resources, 'PaddleCharOCR/rec/keys.txt')),
                                         'rec_img_shape': [3, 48, 320], 'rec_batch_num': 6})

    def proposals(self, image, evidence_dir, refresh=None):
        import cv2
        from rapidocr_onnxruntime import RapidOCR
        if self.template:
            template = cv2.imread(str(self.template))
            if template is not None and all(a <= b for a, b in zip(template.shape[:2], image.shape[:2])):
                _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED))
                if score >= .8:
                    yield {'branch': 'upstream_template', 'score': score,
                           'rect': [*point, template.shape[1], template.shape[0]], 'text': self.code}
        for dark in (False, True)[:MAX_OCR_PASSES]:
            branch = 'hsv_dark' if dark else 'upstream_hsv_white'
            if refresh is not None:
                image = refresh()
            cv2.imwrite(str(evidence_dir / (branch + '-source.png')), image)
            processed = preprocess(image, dark=dark)
            cv2.imwrite(str(evidence_dir / (branch + '.png')), processed)
            self.ocr_passes += 1
            boxes, _ = self.detector(processed)
            if boxes is None or not len(boxes):
                continue
            crops = RapidOCR.get_crop_img_list(None, processed, boxes)
            results, _ = self.recognizer(crops)
            observations = []
            candidates = []
            for box, (text, score) in zip(boxes, results):
                text = normalize(text, self.replacements)
                observations.append({'text': text, 'score': float(score)})
                if score < .5 or len(text) <= 1 or text != self.code:
                    continue
                x, y, w, h = cv2.boundingRect(box)
                x, y = max(0, x), max(0, y)
                w, h = min(w, image.shape[1] - x), min(h, image.shape[0] - y)
                if w <= 0 or h <= 0:
                    continue
                candidates.append({'branch': branch, 'score': float(score),
                                   'rect': [x, y, w, h], 'text': text})
            (evidence_dir / (branch + '-ocr.json')).write_text(
                json.dumps(observations, ensure_ascii=False, indent=2))
            # One click proposal per polarity, not an unbounded list of detections.
            if candidates:
                yield max(candidates, key=lambda item: item['score'])


def scan_map(resource, code, directory, *, capture, click_and_confirm, swipe):
    """Coordinates never prove success: only the caller's fresh Custom panel proof does."""
    import cv2
    import numpy as np
    directory.mkdir(parents=True, exist_ok=True)
    recognizer = MapRecognizer(resource, code)
    audit = {'status': 'failed', 'reason': 'stage_not_found_on_map', 'scans': [], 'swipes': 0}
    previous = None
    unchanged = 0
    started = time.monotonic()
    try:
        for index in range(MAX_SCANS):
            image = capture()
            frame = directory / f'{index:02d}'
            frame.mkdir()
            cv2.imwrite(str(frame / 'screen.png'), image)
            # Coarse change detection ignores small particles and cursor noise.
            small = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (64, 36))
            delta = None if previous is None else float(np.mean(np.abs(small.astype(float) - previous)))
            unchanged = unchanged + 1 if delta is not None and delta < 3 else 0
            previous = small.astype(float)
            record = {'index': index, 'image_change': delta, 'proposals': []}
            audit['scans'].append(record)
            for proposal in recognizer.proposals(image, frame, refresh=capture):
                record['proposals'].append(proposal)
                if click_and_confirm(proposal['rect']):
                    audit.update(status='success', reason='detail_panel_confirmed', branch=proposal['branch'])
                    return True
            if unchanged >= UNCHANGED_LIMIT:
                audit['reason'] = 'stage_not_found_on_map_unchanged'
                return False
            if index < MAX_SWIPES:
                swipe(index)
                audit['swipes'] += 1
        return False
    finally:
        audit['ocr_passes'] = recognizer.ocr_passes
        audit['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (directory / 'result.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2))
