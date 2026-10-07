from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from maa_planner.dashboard import Handler
from maa_planner.skland import BoxError
from maa_planner.skland_monitor import REFRESH_SECONDS, SklandMonitor, fetch_monitor, normalize_monitor

FIXTURE = Path(__file__).parent / "fixtures/skland/monitor.json"


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(FIXTURE.read_text())
        self.now = 2000000000.0
        self.elapsed = 0.0
        self.fetch = Mock(return_value=normalize_monitor(self.raw, "123456789"))
        self.monitor = SklandMonitor(Path("/unused"), fetcher=self.fetch,
                                     clock=lambda: self.now, monotonic=lambda: self.elapsed)

    def test_allowlisted_counts_and_unknown_fields(self):
        result = self.fetch.return_value
        self.assertEqual(result["sanity"]["current"], 80)
        self.assertEqual(result["recruit"][1]["finished_at"], 2000000060)
        self.assertEqual(result["tired_operators"], 2)
        self.assertEqual(result["trading"], [{"station": 1, "stored": 2, "limit": 2},
                                             {"station": 2, "stored": 0, "limit": 3}])
        for private in ("123456789", "SYNTHETIC", "private", "cred", "charId"):
            self.assertNotIn(private, json.dumps(result))
        unknown = normalize_monitor({"status": {"uid": "123456789"}}, "123456789")
        self.assertTrue(all(v is None for v in unknown.values()))
        self.assertEqual(normalize_monitor({"status": {"uid": "123456789"}, "recruit": []},
                                          "123456789")["recruit"], [])

    def test_wrong_identity_types_and_bounds_reject_snapshot(self):
        for path, value in [(('status', 'uid'), '987654321'), (('status', 'ap', 'current'), True),
                            (('status', 'ap', 'max'), 0), (('recruit',), {}),
                            (('recruit',), [{}] * 5), (('building', 'tiredChars'), {}),
                            (('building', 'labor', 'value'), 201),
                            (('status', 'lastOnlineTs'), -1)]:
            with self.subTest(path=path):
                raw = copy.deepcopy(self.raw)
                target = raw
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(BoxError):
                    normalize_monitor(raw, '123456789')

    def test_inactive_recovery_deadlines_are_unknown_and_overflow_is_preserved(self):
        self.raw['status']['ap'].update(current=180, completeRecoveryTime=-1)
        self.raw['recruit'][0].update(startTs=-1, finishTs=-1)
        self.raw['building']['labor']['remainSecs'] = -1
        result = normalize_monitor(self.raw, '123456789')
        self.assertEqual(result['sanity']['current'], 180)
        self.assertIsNone(result['sanity']['full_at'])
        self.assertIsNone(result['recruit'][0]['finished_at'])
        self.assertIsNone(result['drones']['remaining_seconds'])

    def test_no_viewers_required_and_http_reads_never_fetch(self):
        ready = threading.Event()
        self.fetch.side_effect = lambda *args: (ready.set(), self.fetch.return_value)[1]
        self.monitor.start()
        self.addCleanup(self.monitor.close)
        self.assertTrue(ready.wait(2))
        self.monitor.close()
        self.monitor.thread.join(2)
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.monitor.read(), range(8)))
        self.assertTrue(all(r["state"] == "fresh" for r in values))
        self.assertEqual(self.fetch.call_count, 1)

    def test_cadence_stale_failure_and_recovery(self):
        self.monitor.poll()
        fresh = self.monitor.read()
        self.assertEqual(fresh["next_refresh_at"], self.now + 1200)
        self.now += 1199
        self.elapsed += 1199
        self.monitor.poll()
        self.assertEqual(self.fetch.call_count, 1)
        self.now += 1
        self.elapsed += 1
        self.assertEqual(self.monitor.read()["state"], "stale")
        self.fetch.side_effect = BoxError("network", "SYNTHETIC-SECRET response")
        self.monitor.poll()
        failed = self.monitor.read()
        self.assertEqual(failed["state"], "stale")
        self.assertEqual(failed["fetched_at"], fresh["fetched_at"])
        self.assertEqual(failed["snapshot"], fresh["snapshot"])
        self.assertNotIn("SYNTHETIC-SECRET", json.dumps(failed))
        self.monitor.poll()
        self.assertEqual(self.fetch.call_count, 2)
        self.elapsed += REFRESH_SECONDS
        self.now += REFRESH_SECONDS
        self.fetch.side_effect = None
        self.monitor.poll()
        self.assertEqual(self.monitor.read()["state"], "fresh")
        self.assertIsNone(self.monitor.read()["error"])
        # Readers cannot mutate cached nested state.
        self.monitor.read()["snapshot"]["sanity"]["current"] = 1
        self.assertEqual(self.monitor.read()["snapshot"]["sanity"]["current"], 80)

    def test_concurrent_polls_share_one_attempt_and_errors_are_sanitized(self):
        self.fetch.side_effect = RuntimeError("SYNTHETIC-SECRET")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.monitor.poll(), range(4)))
        self.assertEqual(self.fetch.call_count, 1)
        r = self.monitor.read()
        self.assertEqual(r["state"], "unavailable")
        self.assertIsNone(r["snapshot"])
        self.assertNotIn("SYNTHETIC-SECRET", json.dumps(r))

    def test_missing_credentials_do_not_create_files(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(BoxError) as raised, patch('maa_planner.skland_monitor.SklandClient') as client:
                fetch_monitor(Path(d))
            self.assertEqual(raised.exception.category, "authentication")
            client.assert_not_called()
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_fetch_uses_selected_official_account_and_existing_secret(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'var/secrets').mkdir(parents=True)
            binding = {"list": [{"appCode": "arknights", "bindingList": [
                {"uid": "123456789", "isOfficial": True, "isDelete": False, "channelMasterId": "1"}]}]}
            with patch('maa_planner.skland_monitor.load_secret', return_value={'token': 'synthetic'}), \
                    patch('maa_planner.skland_monitor.SklandClient') as client:
                client.return_value.get.side_effect = [binding, self.raw]
                self.assertEqual(fetch_monitor(root, '123456789'), self.fetch.return_value)
                client.return_value.authenticate.assert_called_once_with(token='synthetic')
                paths = [c.args[1] for c in client.return_value.get.call_args_list]
                self.assertEqual(paths, ['/api/v1/game/player/binding', '/api/v1/game/player/info?uid=123456789'])

    def test_http_endpoint_reads_cache_without_polling_and_remains_read_only(self):
        self.monitor.poll()
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, root=Path('/unused')))
        server.skland_monitor = self.monitor
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(connection.close)
        for _ in range(3):
            connection.request('GET', '/api/skland')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertEqual(json.loads(response.read())['state'], 'fresh')
        self.assertEqual(self.fetch.call_count, 1)
        connection.request('POST', '/api/skland')
        response = connection.getresponse()
        self.assertEqual(response.status, 501)
        response.read()


if __name__ == '__main__':
    unittest.main()
