from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from maa_planner.dashboard import read_run
from maa_planner.dashboard_insights import MAX_BYTES
from maa_planner.supervisor import describe_evidence_files, record_phase, start_run


class RunInsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = datetime(2026, 10, 13, 10, 0, 1, tzinfo=UTC)
        self.git = patch('maa_planner.supervisor._git_snapshot', return_value={'head': 'a' * 40, 'dirty': False})
        self.git.start()
        self.addCleanup(self.git.stop)
        self.run = start_run(self.root, 'full', now=self.at)
        clock = patch('maa_planner.dashboard_insights.datetime', wraps=datetime)
        clock.start().now.return_value = self.at + timedelta(hours=2)
        self.addCleanup(clock.stop)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return name

    def terminal(self, phase, minute, evidence=(), result='succeeded'):
        record_phase(self.root, self.run, phase=phase, result=result, outcome='test',
                     evidence_files=describe_evidence_files(self.root, evidence),
                     now=self.at + timedelta(minutes=minute))

    def snapshot(self):
        return read_run(self.root, self.run, explain=True)

    def test_daily_subtasks_and_full_farming_route(self):
        daily = self.write('var/state/host/20261013-180005-123-daily.log',
            '[2026-10-13 18:00:05 INFO ] Infrast Start\n'
            '[2026-10-13 18:00:10 INFO ] EnterFacility Mfg #0\n'
            '[2026-10-13 18:01:10 INFO ] Infrast Completed\n'
            '[2026-10-13 18:01:11 INFO ] Infrast Start\n'
            '[2026-10-13 18:01:20 INFO ] EnterFacility Dorm #0\n'
            '[2026-10-13 18:02:00 INFO ] Infrast Completed\n'
            '[2026-10-13 18:02:10 INFO ] Recruit\n'
            '[2026-10-13 18:02:11 INFO ] Refresh Tags\n'
            '[2026-10-13 18:02:20 INFO ] Recruit Completed\n'
            '[2026-10-13 18:02:30 INFO ] Mall Completed\n'
            '[2026-10-13 18:02:31 INFO ] AllTasksCompleted\n')
        self.terminal('daily', 3, [daily])
        self.write('var/state/host/20261013-180400-activity-YW-8-attempt-1-fight-YW-8.log',
            '[2026-10-13 18:04:01 INFO ] Current sanity: 97/210\n'
            '[2026-10-13 18:04:10 INFO ] Mission started (4 times, use 84 sanity)\n'
            '[2026-10-13 18:10:10 INFO ] Drops: 液化高能气体 × 4, 龙门币 × 1008\n'
            '[2026-10-13 18:10:15 INFO ] Current sanity: 15/210\n'
            '[2026-10-13 18:10:20 INFO ] AllTasksCompleted\n')
        tail = self.write('var/state/host/20261013-181100-123-fallback-attempt-1-fight-1-7.log',
            '[2026-10-13 18:11:01 INFO ] Current sanity: 15/210\n'
            '[2026-10-13 18:11:10 INFO ] Mission started (2 times, use 12 sanity)\n'
            '[2026-10-13 18:13:00 INFO ] Drops: 固源岩 × 2\n'
            '[2026-10-13 18:13:10 INFO ] Current sanity: 4/210\n'
            '[2026-10-13 18:13:20 INFO ] AllTasksCompleted\n')
        self.terminal('farming', 14, [tail])
        self.write('var/state/host/20261013-175900-999-fallback-attempt-1-fight-1-7.log',
                   '[2026-10-13 17:59:00 INFO ] Drops: 固源岩 × 9999\n')
        self.write('var/state/planner/launcher-20261013-180400.json', json.dumps({
            'schema_version': 1, 'generated_at': '2026-10-13T10:04:00Z', 'decision': 'FIGHT',
            'selected_stage': 'YW-8', 'reason': 'ELIGIBLE_STAGE_SELECTED',
            'activity': {'name': '昨日海'}, 'candidates': [dict(stage_code='YW-8', item_id='31103',
                inventory=15, target=200, deficit=185, eligible=True, extra_future_field='ignored')],
            'evidence': {'execution_candidates': [{'stage_code': 'YW-8'}],
                         'inventory': {'captured_at': '2026-10-12T22:19:00Z'}}}))
        self.write('var/data/resource/item_index.json', json.dumps({'31103': {'name': '液化高能气体'}}))
        r = self.snapshot()
        phases = r['insights']['phases']
        log = phases['daily']['logs'][0]
        self.assertEqual(log['tasks']['Infrast']['completed'], 2)
        self.assertEqual(log['rooms'], ['制造站 1', '宿舍 1'])
        self.assertEqual(log['recruit_confirmed'], 1)
        self.assertEqual(log['tag_refreshes'], 1)
        fights = phases['farming']['logs']
        self.assertEqual([f['stage'] for f in fights], ['YW-8', '1-7'])
        self.assertEqual([f['reported_battles'] for f in fights], [4, 2])
        self.assertEqual([f['sanity_cost'] for f in fights], [84, 12])
        self.assertEqual(fights[-1]['last_sanity'], 4)
        self.assertEqual([f['binding'] for f in fights], ['time-window', 'audit'])
        self.assertEqual(phases['farming']['plan']['candidates'][0]['material'], '液化高能气体')
        self.assertNotIn('extra_future_field', phases['farming']['plan']['candidates'][0])
        # The supplemental observations have not filled in missing phase terminals.
        self.assertEqual(r['status'], 'unfinished')
        self.assertEqual(len(r['phases']), 2)

    def test_changed_audited_file_is_never_reintroduced_by_time_scan(self):
        log = self.write('var/state/host/20261013-180005-123-daily.log',
                         '[2026-10-13 18:00:05 INFO ] Mall Completed\n')
        self.terminal('daily', 1, [log], result='failed')
        (self.root / log).write_text('[2026-10-13 18:00:05 INFO ] Recruit Completed\n')
        r = self.snapshot()
        info = r['insights']['phases']['daily']
        self.assertEqual(info['files'][0]['state'], 'changed')
        self.assertEqual(info['logs'], [])
        self.assertEqual(r['phases'][0]['result'], 'failed')
        (self.root / log).unlink()
        self.assertEqual(self.snapshot()['insights']['phases']['daily']['files'][0]['state'], 'unavailable')

    def test_unsafe_paths_large_files_and_raw_warning_content_are_not_returned(self):
        secret = self.write('var/secrets/hidden.json', 'private-token')
        link = self.root / 'var/state/host/20261013-180005-123-daily.log'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.root / secret)
        self.write('var/state/host/20261013-180010-123-daily.log', 'x' * (MAX_BYTES + 1))
        self.write('var/state/host/20261013-180015-123-daily-retry.log',
            '[2026-10-13 18:00:15 WARN ] private-token\n'
            '[2026-10-13 18:00:16 WARN ] GameOffline\n')
        self.write('var/state/host/20269999-180005-123-daily.log', 'invalid filename date')
        self.terminal('daily', 1, [secret])
        r = self.snapshot()
        payload = json.dumps(r['insights'])
        self.assertNotIn('private-token', payload)
        self.assertNotIn('hidden.json', payload)
        self.assertIn('游戏断线', json.dumps(r['insights'], ensure_ascii=False))
        self.assertEqual(len(r['insights']['phases']['daily']['logs']), 1)

    def test_unfinished_history_does_not_absorb_next_run_or_future_plan(self):
        start_run(self.root, 'full', now=self.at + timedelta(minutes=10))
        self.write('var/state/host/20261013-181500-123-daily.log',
                   '[2026-10-13 18:15:00 INFO ] Mall Completed\n')
        self.write('var/state/planner/launcher-20261013-181500.json', json.dumps({
            'schema_version': 1, 'generated_at': '2026-10-13T10:15:00Z', 'candidates': []}))
        info = self.snapshot()['insights']['phases']
        self.assertEqual(info['daily']['logs'], [])
        self.assertNotIn('plan', info['farming'])

    def test_started_batch_without_drop_report_is_not_counted_as_completed(self):
        self.write('var/state/host/20261013-180005-activity-YW-8-attempt-1-fight-YW-8.log',
            '[2026-10-13 18:00:05 INFO ] Mission started (6 times, use 126 sanity)\n'
            '[2026-10-13 18:00:06 WARN ] FightMissionFailed\n'
            '[2026-10-13 18:00:07 INFO ] Drops: 龙门币 × 144\n')
        log = self.snapshot()['insights']['phases']['farming']['logs'][0]
        self.assertEqual(log['reported_battles'], 0)
        self.assertEqual(log['sanity_cost'], 0)
        self.assertFalse(log['complete'])

    def test_reused_inventory_is_bound_to_original_bytes_and_fields_are_minimized(self):
        name = self.write('var/state/planner/inventory.json', json.dumps({
            'schema_version': 1, 'captured_at': '2026-10-12T22:00:00Z', 'complete': True,
            'source': 'maa-depot-log', 'account': 'private-account', 'items': {'3003': 1042, '31103': 15}}))
        self.terminal('depot', 1, [name])
        info = self.snapshot()['insights']['phases']['depot']
        self.assertEqual(info['inventory']['pure_gold'], 1042)
        self.assertEqual(info['inventory']['observed_items'], 2)
        self.assertNotIn('private-account', json.dumps(info))
        (self.root / name).write_text('{}')
        self.assertNotIn('inventory', self.snapshot()['insights']['phases']['depot'])

    def test_broken_chain_does_not_receive_explanations(self):
        self.terminal('daily', 1)
        event = next((self.root / 'var/state/supervisor/runs' / self.run / 'events').glob('*phase-finished.json'))
        event.write_text('{}')
        r = self.snapshot()
        self.assertEqual(r['status'], 'invalid')
        self.assertNotIn('insights', r)

    def test_future_schema_does_not_revive_older_plan_and_manual_log_remains_visible(self):
        self.write('var/state/planner/launcher-20261013-180100.json', json.dumps({
            'schema_version': 1, 'generated_at': '2026-10-13T10:01:00Z',
            'selected_stage': 'YW-8', 'candidates': []}))
        self.write('var/state/planner/launcher-20261013-180200.json', json.dumps({
            'schema_version': 2, 'generated_at': '2026-10-13T10:02:00Z',
            'selected_stage': 'new-meaning'}))
        self.write('var/state/host/20261013-180300-123-manual-attempt-1-fight-1-7.log',
                   '[2026-10-13 18:03:00 INFO ] Fight Start\n')
        info = self.snapshot()['insights']['phases']['farming']
        self.assertNotIn('plan', info)
        self.assertEqual(info['logs'][0]['stage'], '1-7')


if __name__ == '__main__':
    unittest.main()
