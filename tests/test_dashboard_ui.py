"""Optional browser checks with synthetic API responses; never reads live history.

Run with a Python environment containing Playwright and its Chromium browser:
    python -m unittest discover -s tests -p test_dashboard_ui.py -v
"""
from __future__ import annotations

import copy
import json
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from maa_planner.dashboard import Handler

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, 'optional Playwright browser dependency not installed')
class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
            Handler, root=Path(__file__).resolve().parents[1]))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.browser_runtime = sync_playwright().start()
        cls.browser = cls.browser_runtime.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.browser_runtime.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        names = ['runtime-readiness', 'device-readiness', 'depot', 'daily',
                 'source-refresh', 'annihilation', 'farming', 'award', 'cleanup']
        self.run = {
            'run_id': '20260924T100000.000000Z-abcdef12', 'status': 'failed',
            'mode': 'full', 'started_at': '2026-09-24T10:00:00Z',
            'updated_at': '2026-09-24T10:20:00Z', 'finished_at': '2026-09-24T10:20:00Z',
            'expected_phases': names, 'repository': {'head': 'a' * 40},
            'phases': [dict(phase=name, result=result, outcome=outcome,
                            recorded_at=f'2026-09-24T10:0{i+1}:00Z',
                            details={'stage': '<img src=x onerror=alert(1)>', 'farm_mode': 'auto'},
                            evidence_files=[{'path': 'var/log/synthetic.log'}])
                       for i, (name, result, outcome) in enumerate([
                           ('runtime-readiness', 'succeeded', 'receipt-accepted'),
                           ('device-readiness', 'succeeded', 'device-ready'),
                           ('source-refresh', 'policy-resolved', 'refresh-failed-cache-only'),
                           ('annihilation', 'not-applicable', 'farming-disabled'),
                           ('farming', 'degraded', 'inventory-unavailable')])],
            'recovery': [{'event_type': 'recovery-started', 'recorded_at': '2026-09-24T10:21:00Z'}],
        }
        self.page = self.browser.new_page(viewport={'width': 1500, 'height': 1000})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.route('**/api/**', self.respond)
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}')
        self.page.locator('#rows .view').click()
        self.page.locator('.run-metrics').wait_for()

    def respond(self, route):
        path = route.request.url.split('/api/')[1]
        if path == 'status':
            payload = dict(activity={'state': 'idle', 'units': []}, latest_run=self.run,
                           service_failures=[], services={}, runtime={'receipt': None},
                           observed_at='2026-09-24T10:22:00Z')
        elif path.startswith('runs?'):
            payload = dict(runs=[self.run], total=1)
        else:
            payload = self.run
        route.fulfill(content_type='application/json', body=json.dumps(payload))

    def test_counts_details_filter_and_refresh(self):
        self.assertIn('已记录 5 / 9', self.page.locator('#rows').inner_text())
        self.assertEqual(self.page.locator('.run-metrics strong').all_text_contents(),
                         ['5 / 9', '2', '1', '1', '1', '4'])
        self.assertTrue(self.page.locator('#stage-farming').evaluate('(n) => n.open'))
        self.assertEqual(self.page.locator('#stage-farming img').count(), 0)
        self.assertIn('<img src=x onerror=alert(1)>', self.page.locator('#stage-farming').inner_text())
        self.page.locator('#stage-farming .stage-body summary').last.click()
        self.page.locator('.stage-filter select').select_option('attention')
        self.assertEqual(self.page.locator('.stage-card:visible').count(), 5)
        self.page.evaluate('refresh()')
        self.assertTrue(self.page.locator('#stage-farming .stage-body details').last.evaluate('(n) => n.open'))
        # A new snapshot preserves disclosure and filter state too.
        self.run['updated_at'] = '2026-09-24T10:23:00Z'
        self.page.evaluate('refresh()')
        self.assertEqual(self.page.locator('.stage-filter select').input_value(), 'attention')
        self.assertTrue(self.page.locator('#stage-farming .stage-body details').last.evaluate('(n) => n.open'))
        self.page.get_by_role('button', name='设备准备，通过，展开详情').click()
        self.assertTrue(self.page.locator('#stage-device-readiness').evaluate('(n) => n.open'))
        self.assertEqual(self.page.locator('.stage-filter select').input_value(), 'all')
        self.page.keyboard.press('Enter')
        self.assertFalse(self.page.locator('#stage-device-readiness').evaluate('(n) => n.open'))
        times = self.page.locator('.timeline time').evaluate_all('(nodes) => nodes.map(n => n.dateTime)')
        self.assertEqual(times, sorted(times))
        self.assertIn('恢复已开始', self.page.locator('.timeline').inner_text())
        self.assertEqual(self.errors, [])

    def test_unfinished_invalid_success_and_mobile(self):
        self.run.update(status='unfinished', finished_at=None)
        self.page.evaluate('refresh()')
        self.page.get_by_role('button', name='仓库扫描，尚无记录，展开详情').click()
        self.assertIn('无法据此判断是否已开始执行', self.page.locator('#stage-depot').inner_text())
        self.assertNotIn('运行中', self.page.locator('#detail').inner_text())
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.assertTrue(self.page.locator('#close').is_visible())
        complete = copy.deepcopy(self.run)
        complete.update(status='success', finished_at='2026-09-24T10:20:00Z')
        complete['phases'] = [dict(complete['phases'][0], phase=name, result='succeeded')
                              for name in complete['expected_phases']]
        self.run = complete
        self.page.evaluate('refresh()')
        self.assertEqual(self.page.locator('.run-metrics strong').first.inner_text(), '9 / 9')
        self.page.locator('.stage-filter select').select_option('attention')
        self.assertEqual(self.page.locator('.stage-card:visible').count(), 0)
        self.run = dict(run_id=self.run['run_id'], status='invalid', phases=[], error='证据无法核验')
        self.page.evaluate('refresh()')
        self.assertIn('证据无法核验', self.page.locator('#detail').inner_text())
        self.assertEqual(self.page.locator('.run-metrics').count(), 0)
        self.assertIn('无法核验', self.page.locator('#rows').inner_text())
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
