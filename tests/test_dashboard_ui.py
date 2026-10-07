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
    from playwright.sync_api import sync_playwright, expect
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
        self.page.locator('#nav-history').click()
        self.page.locator('#rows .view').click()
        self.page.locator('.run-metrics').wait_for()

    def respond(self, route):
        path = route.request.url.split('/api/')[1]
        if path == 'maa-release':
            payload = getattr(self, 'release', dict(state='fresh', version='v6.18.0', fetched_at='2026-09-24T10:22:00Z'))
        elif path == 'skland':
            from maa_planner.skland_monitor import normalize_monitor
            raw = json.loads((Path(__file__).parent / 'fixtures/skland/monitor.json').read_text())
            payload = getattr(self, 'skland', dict(state='fresh', observed_at=2000000000,
                fetched_at=2000000000, next_refresh_at=2000001200, refresh_interval_seconds=1200,
                snapshot=normalize_monitor(raw, '123456789'), error=None))
        elif path == 'status':
            payload = dict(activity={'state': 'idle', 'units': []}, latest_run=self.run,
                           service_failures=[], services={}, runtime={'receipt': {'core': {'active_version': 'v6.17.0'}}},
                           observed_at='2026-09-24T10:22:00Z')
        elif path.startswith('runs?'):
            runs = getattr(self, 'history_runs', [self.run])
            payload = dict(runs=runs, total=len(runs))
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

    def test_navigation_history_and_direct_detail(self):
        self.assertEqual(self.page.locator('#page-title').inner_text(), '运行详情')
        self.assertEqual(self.page.locator('.primary-nav [aria-current="page"]').get_attribute('id'), 'nav-detail')
        self.assertFalse(self.page.locator('#history').is_visible())
        self.assertFalse(self.page.locator('#overview').is_visible())
        self.page.go_back()
        self.page.wait_for_url('**/#history')
        expect(self.page.locator('#history')).to_be_visible()
        self.assertEqual(self.page.locator('#nav-history').get_attribute('aria-current'), 'page')
        self.assertFalse(self.page.locator('#detail').is_visible())
        self.page.go_forward()
        self.page.wait_for_url('**/#run/**')
        self.page.locator('.run-metrics').wait_for()
        self.page.locator('#close').click()
        self.page.locator('#search').fill('abcdef12')
        self.page.get_by_role('button', name='需关注', exact=True).click()
        self.page.locator('#rows .view').click()
        self.page.locator('.run-metrics').wait_for()
        self.page.locator('#close').click()
        self.assertEqual(self.page.locator('#search').input_value(), 'abcdef12')
        self.assertEqual(self.page.locator('[data-filter="attention"]').get_attribute('aria-pressed'), 'true')
        self.page.locator('#nav-overview').click()
        expect(self.page.locator('#overview')).to_be_visible()
        self.assertFalse(self.page.locator('#history').is_visible())
        self.assertEqual(self.page.locator('#nav-overview').get_attribute('aria-current'), 'page')
        self.page.locator('#latest-view').click()
        self.assertTrue(self.page.url.endswith('#overview'))
        expect(self.page.locator('#detail')).to_be_visible()
        self.page.locator('#nav-history').click()
        self.page.locator('#rows .view').click()
        self.page.locator('.run-metrics').wait_for()
        self.page.reload()
        self.page.locator('.run-metrics').wait_for()
        self.assertEqual(self.page.locator('#page-title').inner_text(), '运行详情')
        self.assertFalse(self.page.locator('#history').is_visible())
        for width in (390, 768, 1280):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.assertTrue(self.page.locator('#nav-history').is_visible())
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.locator('#nav-history').click()
        expect(self.page.locator('#history')).to_be_visible()
        self.assertEqual(self.page.locator('.primary-nav [aria-current="page"]').count(), 1)
        self.assertEqual(self.errors, [])

    def test_return_restores_history_position_and_ignores_late_detail(self):
        self.history_runs = [dict(self.run, run_id=f'20260924T100000.000000Z-{i:08x}')
                             for i in range(19)] + [self.run]
        self.page.locator('#close').click()
        expect(self.page.locator('#history')).to_be_visible()
        self.page.evaluate('refresh()')
        button = self.page.locator('#rows .view').last
        button.scroll_into_view_if_needed()
        saved_scroll = self.page.evaluate('window.scrollY')
        self.assertGreater(saved_scroll, 0)
        button.click()
        self.page.locator('.run-metrics').wait_for()
        self.page.locator('#close').click()
        expect(self.page.locator('#history')).to_be_visible()
        self.page.wait_for_function('(y) => Math.abs(window.scrollY - y) < 2', arg=saved_scroll)
        # A request completing after navigation must not reopen the detail page.
        pending = []
        self.page.route('**/api/runs/' + self.run['run_id'], lambda route: pending.append(route))
        button.click()
        self.page.wait_for_url('**/#run/**')
        self.page.locator('#close').click()
        expect(self.page.locator('#history')).to_be_visible()
        self.assertTrue(pending)
        pending[0].fulfill(content_type='application/json', body=json.dumps(self.run))
        expect(self.page.locator('#detail')).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_inline_latest_and_upstream_states(self):
        self.page.locator('#nav-overview').click()
        expect(self.page.locator('#overview')).to_be_visible()
        expect(self.page.locator('.run-metrics')).to_be_visible()
        self.assertEqual(self.page.locator('#detail-title').inner_text(), '最近一次执行')
        self.assertTrue(self.page.locator('#close').is_hidden())
        self.assertIn('有新的稳定版', self.page.locator('#upstream').inner_text())
        self.page.locator('#stage-farming .stage-body summary').last.click()
        self.page.evaluate('refresh()')
        self.assertTrue(self.page.locator('#stage-farming .stage-body details').last.evaluate('(n) => n.open'))
        self.release = dict(state='stale', version='v6.18.0', fetched_at='2026-09-24T10:22:00Z')
        self.page.evaluate('refreshUpstream()')
        self.assertIn('旧结果', self.page.locator('#upstream').inner_text())
        self.assertNotIn('有新的稳定版', self.page.locator('#upstream').inner_text())
        self.release = dict(state='unavailable')
        self.page.evaluate('refreshUpstream()')
        self.assertIn('查询失败', self.page.locator('#upstream').inner_text())
        self.assertTrue(self.page.locator('.run-metrics').is_visible())
        self.release = dict(state='fresh', version='v6.17.0', fetched_at='2026-09-24T10:22:00Z')
        self.page.evaluate('refreshUpstream()')
        self.assertIn('与上游稳定版一致', self.page.locator('#upstream').inner_text())
        self.release['version'] = 'v6.9.0'
        self.page.evaluate('refreshUpstream()')
        self.assertIn('本地版本高于', self.page.locator('#upstream').inner_text())
        self.page.evaluate("state.localVersion = 'v6.19.0-beta.1'; renderUpstream()")
        self.assertIn('未作比较', self.page.locator('#upstream').inner_text())
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
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

    def test_skland_second_ticks_recovery_boundaries_and_confirmation(self):
        self.page.locator('#nav-overview').click()
        self.page.clock.install()
        self.page.reload()
        self.page.evaluate('refreshSkland()')
        self.assertEqual(self.page.locator('#sanity-value').text_content(), '80 / 135')
        offset = float(self.page.locator('#sanity-ring').get_attribute('stroke-dashoffset'))
        self.assertAlmostEqual(offset, (1 - 80 / 135) * 540.354, places=3)
        self.assertEqual(self.page.locator('#sanity-next-time').inner_text(), '00:06:00')
        self.assertEqual(self.page.locator('#sanity-gauge').get_attribute('data-state'), 'recovering')
        self.assertIn('下一点 00:06:00', self.page.locator('#sanity-timer').inner_text())
        self.assertEqual(self.page.locator('.slot-timer').nth(1).inner_text(), '00:01:00')
        requests = []
        self.page.on('request', lambda r: requests.append(r.url))
        self.page.clock.run_for(2000)
        self.assertEqual(self.page.locator('.slot-timer').nth(1).inner_text(), '00:00:59')
        self.assertFalse(any('/api/skland' in url for url in requests))
        self.page.evaluate('state.sklandAnchor -= 359000; renderSkland()')
        self.assertEqual(self.page.locator('#sanity-value').text_content(), '81 / 135')
        self.assertIn('待同步确认', self.page.locator('.recruit-slot').nth(1).inner_text())
        self.assertIn('接口已确认', self.page.locator('.recruit-slot').nth(2).inner_text())
        self.assertIn('招募已结束', self.page.locator('.recruit-slot').nth(2).inner_text())
        self.assertIn('状态未知', self.page.locator('.recruit-slot').nth(3).inner_text())
        self.assertEqual(self.page.locator('#base-status strong').first.inner_text(), '106 / 200')
        self.assertEqual(self.page.evaluate('sanityAt(state.skland.snapshot.sanity, 2000019799)'), 134)
        self.assertEqual(self.page.evaluate('sanityAt(state.skland.snapshot.sanity, 2000019800)'), 135)
        self.assertEqual(self.page.evaluate('sanityAt({...state.skland.snapshot.sanity, current:180}, 2000020000)'), 180)
        self.assertEqual(self.page.evaluate('sanityAt({...state.skland.snapshot.sanity, full_at:null}, 2000020000)'), 80)
        self.page.evaluate('state.skland.snapshot.sanity.current=180; renderSkland()')
        self.assertEqual(self.page.locator('#sanity-value').text_content(), '180 / 135')
        self.assertEqual(float(self.page.locator('#sanity-ring').get_attribute('stroke-dashoffset')), 0)
        self.assertEqual(self.page.locator('#sanity-gauge').get_attribute('data-state'), 'full')
        self.assertEqual(self.errors, [])

    def test_skland_old_snapshot_unknown_and_authentication_error(self):
        self.page.locator('#nav-overview').click()
        self.page.evaluate('refreshSkland()')
        self.page.evaluate('state.sklandAnchor -= 1200000; renderSkland()')
        self.assertIn('旧快照', self.page.locator('#skland-state').inner_text())
        self.assertTrue(self.page.locator('#skland-message').is_visible())
        self.skland = dict(state='unavailable', observed_at=2000000000, fetched_at=None,
                           next_refresh_at=2000001200, refresh_interval_seconds=1200, snapshot=None,
                           error={'category': 'authentication', 'message': './bin/zootd box-login <img src=x onerror=alert(1)>'})
        self.page.evaluate('refreshSkland()')
        self.assertEqual(self.page.locator('#sanity-value').text_content(), '— / —')
        self.assertEqual(self.page.locator('#sanity-gauge').get_attribute('data-state'), 'unknown')
        self.assertEqual(float(self.page.locator('#sanity-ring').get_attribute('stroke-dashoffset')), 540.354)
        self.assertEqual(self.page.locator('#skland img').count(), 0)
        self.assertIn('box-login', self.page.locator('#skland-message').inner_text())
        self.assertEqual(self.page.locator('#recruit-summary').inner_text(), '数据未知')
        self.page.route('**/api/skland', lambda route: route.abort())
        self.page.evaluate('refreshSkland()')
        self.assertIn('面板连接失败', self.page.locator('#skland-message').inner_text())
        self.assertEqual(self.errors, [])

    def test_skland_overview_layout_and_return_to_foreground(self):
        self.page.locator('#nav-overview').click()
        self.page.evaluate('refreshSkland()')
        self.assertIn('2', self.page.locator('#base-status').inner_text())
        for width in (360, 390, 768, 1280, 1500):
            self.page.set_viewport_size({'width': width, 'height': 1000})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
            self.assertTrue(self.page.locator('#sanity-value').is_visible())
            self.assertEqual(self.page.locator('.recruit-slot:visible').count(), 4)
            boxes = self.page.locator('.recruit-slot').evaluate_all('(nodes) => nodes.map(n => {const r=n.getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};})')
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    self.assertTrue(a['right'] <= b['left'] or b['right'] <= a['left']
                                    or a['bottom'] <= b['top'] or b['bottom'] <= a['top'])
        self.page.emulate_media(reduced_motion='reduce')
        self.assertEqual(self.page.locator('.recruit-slot.running .slot-dot').evaluate('(n) => getComputedStyle(n).animationName'), 'none')
        self.assertEqual(self.page.locator('#sanity-ring').evaluate('(n) => getComputedStyle(n).transitionDuration'), '0s')
        self.page.screenshot(path='/tmp/zootd-dashboard-desktop.png', full_page=True)
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.screenshot(path='/tmp/zootd-dashboard-mobile.png', full_page=True)
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        self.page.wait_for_function('() => !state.sklandBusy')
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
