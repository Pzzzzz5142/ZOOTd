from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from maa_planner.dashboard import Handler, history, overview, read_run, service_status
from maa_planner.supervisor import start_run, record_phase, finish_run
from maa_planner.util import canonical_json, sha256_bytes


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = patch('maa_planner.supervisor._git_snapshot', return_value={'head': 'a'*40, 'dirty': False})
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def run_record(self, complete=True, result='succeeded'):
        run = start_run(self.root, 'dry-run')
        if complete:
            for phase in ('runtime-readiness', 'cleanup'):
                record_phase(self.root, run, phase=phase, result=result, outcome='test')
            finish_run(self.root, run, process_status=0, supervisor_enabled=False,
                       supervisor_required=False, command=(), timeout_seconds=1)
        return run

    def test_success_requires_complete_evidence(self):
        run = self.run_record()
        self.assertEqual(read_run(self.root, run)['status'], 'success')
        self.assertEqual(len(read_run(self.root, run)['phases']), 2)
        self.assertEqual(read_run(self.root, self.run_record(result='degraded'))['status'], 'failed')

    def test_forged_success_terminal_without_phases_is_invalid(self):
        run = self.run_record(False)
        finish_run(self.root, run, process_status=0, supervisor_enabled=False,
                   supervisor_required=False, command=(), timeout_seconds=1)
        path = next((self.root / 'var/state/supervisor/runs' / run / 'events').glob('*run-finished.json'))
        event = json.loads(path.read_text())
        event['payload']['status'] = 'success'
        del event['event_sha256']
        event['event_sha256'] = sha256_bytes(canonical_json(event))
        path.write_text(json.dumps(event))
        self.assertEqual(read_run(self.root, run)['status'], 'invalid')

    def test_old_unfinished_run_is_not_reported_running(self):
        self.assertEqual(read_run(self.root, self.run_record(False))['status'], 'unfinished')

    def test_tampering_is_not_success(self):
        run = self.run_record()
        path = next((self.root / 'var/state/supervisor/runs' / run / 'events').glob('*phase-finished.json'))
        value = json.loads(path.read_text())
        value['payload']['outcome'] = 'changed'
        path.write_text(json.dumps(value))
        self.assertEqual(read_run(self.root, run)['status'], 'invalid')

    def test_broken_run_does_not_hide_other_history_and_index_is_ignored(self):
        good = self.run_record()
        bad = self.run_record()
        path = next((self.root / 'var/state/supervisor/runs' / bad / 'events').glob('*.json'))
        path.write_text('{')
        (self.root / 'var/state/supervisor/latest-run.json').write_text('{}')
        runs = history(self.root)['runs']
        self.assertEqual(len(runs), 2)
        self.assertEqual({r['run_id']:r['status'] for r in runs}, {good:'success', bad:'invalid'})
        self.assertEqual(len(history(self.root, 1, 1)['runs']), 1)

    def test_empty_and_unavailable(self):
        self.assertEqual(history(self.root)['runs'], [])
        with patch('maa_planner.dashboard.subprocess.run', side_effect=OSError):
            self.assertEqual(service_status(), {'available':False})

    def test_old_service_failure_does_not_override_idle_or_latest_success(self):
        self.run_record(result='failed')
        latest = self.run_record()
        idle = {'available': True, 'ActiveState': 'inactive'}
        failed = {'available': True, 'ActiveState': 'failed', 'Result': 'exit-code',
                  'ExecMainStatus': '1', 'ExecMainExitTimestamp': 'yesterday'}
        with patch('maa_planner.dashboard.service_status', side_effect=[idle, failed]):
            result = overview(self.root)
        self.assertEqual(result['activity']['state'], 'idle')
        self.assertEqual(result['latest_run']['run_id'], latest)
        self.assertEqual(result['latest_run']['status'], 'success')
        self.assertEqual(result['service_failures'][0]['unit'], 'zootd-prereset.service')
        self.assertEqual(result['service_failures'][0]['ExecMainStatus'], '1')

    def test_running_and_partial_service_availability(self):
        active = {'available': True, 'ActiveState': 'activating', 'MainPID': '123'}
        with patch('maa_planner.dashboard.service_status', side_effect=[active, {'available': False}]):
            result = overview(self.root)
        self.assertEqual(result['activity']['state'], 'running')
        self.assertIsNone(result['latest_run'])
        with patch('maa_planner.dashboard.service_status', side_effect=[
                {'available': True, 'ActiveState': 'inactive'}, {'available': False}]):
            self.assertEqual(overview(self.root)['activity']['state'], 'unknown')

    def test_latest_unfinished_or_invalid_run_is_not_skipped(self):
        self.run_record()
        latest = self.run_record(False)
        with patch('maa_planner.dashboard.service_status', return_value={'available': False}):
            self.assertEqual(overview(self.root)['latest_run']['status'], 'unfinished')
            path = next((self.root / 'var/state/supervisor/runs' / latest / 'events').glob('*.json'))
            path.write_text('{')
            result = overview(self.root)
        self.assertEqual(result['latest_run']['run_id'], latest)
        self.assertEqual(result['latest_run']['status'], 'invalid')

    def test_http_routes_and_read_only_boundary(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, root=self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(path, method='GET', headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            self.addCleanup(connection.close)
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        self.assertEqual(request('/api/runs')[0], 200)
        self.assertEqual(request('/api/runs?limit=101')[0], 400)
        self.assertEqual(request('/api/runs?offset=-1')[0], 400)
        self.assertEqual(request('/api/runs?limit=no')[0], 400)
        self.assertEqual(request('/api/runs', 'POST')[0], 501)
        self.assertEqual(request('/../../config/host.env')[0], 404)
        self.assertEqual(request('/api/runs', headers={'Host':'10.0.0.139:8765'})[0], 200)
        self.assertEqual(request('/api/runs', headers={'Host':'zenbox:8765'})[0], 200)
        run = self.run_record()
        status, body = request('/api/runs/'+run)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'success')


if __name__ == '__main__':
    unittest.main()
