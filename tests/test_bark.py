from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from maa_planner import bark, skland_attendance as sign
from maa_planner.supervisor import RUN_MODES, finish_run, record_phase, start_run
from maa_planner.util import atomic_write_json


INVOCATION = "a" * 32


class BarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {"INVOCATION_ID": INVOCATION, "SERVICE_RESULT": "success", "ZOOTD_BARK_SOURCE": "home"}
        env = patch.dict(os.environ, self.env)
        env.start(); self.addCleanup(env.stop)
        snapshot = patch("maa_planner.supervisor._git_snapshot", return_value={"head": "a" * 40, "dirty": False})
        snapshot.start(); self.addCleanup(snapshot.stop)

    def run_record(self, *, mode="full", complete=True, result="succeeded"):
        run = start_run(self.root, mode)
        if complete:
            for phase in RUN_MODES[mode]:
                record_phase(self.root, run, phase=phase, result=result, outcome="test")
            finish_run(self.root, run, process_status=0, supervisor_enabled=False,
                       supervisor_required=False, command=(), timeout_seconds=1)
        return run

    def test_success_requires_this_invocation_full_chain_and_service_success(self):
        self.run_record()
        for kind in ("morning", "evening"):
            title, body = bark.notification(self.root, kind, self.env)
            self.assertTrue(title.endswith("成功"))
            self.assertIn("通过验收", body)
            self.assertIn("[home]", body)
        for result in ("timeout", "signal", "exit-code", "future", ""):
            title, _ = bark.notification(self.root, "morning", {**self.env, "SERVICE_RESULT": result})
            self.assertTrue(title.endswith("异常"))
        title, _ = bark.notification(self.root, "morning", {**self.env, "INVOCATION_ID": "b" * 32})
        self.assertTrue(title.endswith("异常"))

    def test_old_missing_or_newer_unfinished_failed_and_corrupt_evidence_is_not_success(self):
        self.assertFalse(bark.daily_result(self.root, INVOCATION)[0])
        with patch.dict(os.environ, {"INVOCATION_ID": ""}):
            self.run_record()  # Legacy records have no invocation identity.
        self.assertFalse(bark.daily_result(self.root, INVOCATION)[0])
        self.run_record()
        self.assertTrue(bark.daily_result(self.root, INVOCATION)[0])
        self.run_record(complete=False)
        self.assertFalse(bark.daily_result(self.root, INVOCATION)[0])
        self.run_record(result="failed")
        self.assertFalse(bark.daily_result(self.root, INVOCATION)[0])
        run = self.run_record()
        event = next((self.root / "var/state/supervisor/runs" / run / "events").glob("*run-finished.json"))
        event.write_text(event.read_text().replace('"success"', '"failed"'))
        self.assertFalse(bark.daily_result(self.root, INVOCATION)[0])
        self.run_record(mode="dry-run")
        self.assertFalse(bark.daily_result(self.root, INVOCATION)[0])

    def attendance(self):
        signer = lambda credentials, target: {"status": "succeeded", "rewards": [{"name": "龙门币", "count": 500}], "error": None}
        with patch.object(sign, "load_secret", return_value={}), patch.object(sign, "select_target", return_value=None):
            return sign.run_attendance(self.root, client=Mock(), signer=signer)

    def test_attendance_rewards_partial_failure_repeat_and_no_old_success(self):
        self.assertFalse(bark.attendance_result(self.root, INVOCATION)[0])
        state = self.attendance()
        accepted, body = bark.attendance_result(self.root, INVOCATION)
        self.assertTrue(accepted)
        self.assertIn("龙门币 ×500", body)
        self.assertIn("终末地", body)
        state["games"]["arknights"].update(status="already_signed", rewards=[])
        atomic_write_json(sign.state_path(self.root), state)
        self.assertIn("今日已签到", bark.attendance_result(self.root, INVOCATION)[1])
        state["status"] = "failed"
        state["games"]["endfield"].update(status="failed", rewards=[], error={"category": "network", "message": "SECRET"})
        atomic_write_json(sign.state_path(self.root), state)
        title, body = bark.notification(self.root, "attendance", self.env)
        self.assertTrue(title.endswith("异常"))
        self.assertIn("今日已签到", body)
        self.assertIn("签到结果未确认", body)
        self.assertNotIn("SECRET", body)
        state["service_invocation_id"] = "b" * 32
        atomic_write_json(sign.state_path(self.root), state)
        self.assertFalse(bark.attendance_result(self.root, INVOCATION)[0])
        del state["service_invocation_id"]
        atomic_write_json(sign.state_path(self.root), state)
        self.assertIsNotNone(sign.read_attendance(self.root)["record"])
        self.assertFalse(bark.attendance_result(self.root, INVOCATION)[0])

    def response(self, raw=b'{"code":200}', status=200):
        response = io.BytesIO(raw)
        response.status = status
        opener = Mock()
        opener.open.return_value = response
        return opener

    def test_post_payload_timeout_unicode_size_and_optional_response_fields(self):
        opener = self.response(b'{"code":200,"future":true}')
        with patch.object(bark, "build_opener", return_value=opener), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(bark.send_bark("https://example.invalid/SECRET?group=old", "签到", "龙门币" * 2000))
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(payload["group"], "ZOOTd")
        self.assertLess(len(request.data), 4096)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)
        self.assertIsNone(bark.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_failure_is_safe_and_does_not_print_key_response_or_exception_text(self):
        for raw, status in [(b'{"code":400,"message":"SECRET"}', 200), (b'[]', 200),
                            (b'{"code":true}', 200), (b'not json', 200),
                            (b'x' * 8193, 200), (b'{"code":200}', 500)]:
            output = io.StringIO()
            with patch.object(bark, "build_opener", return_value=self.response(raw, status)), contextlib.redirect_stdout(output):
                self.assertFalse(bark.send_bark("https://example.invalid/SECRET", "t", "b"))
            self.assertNotIn("SECRET", output.getvalue())
        with patch.object(bark, "build_opener", side_effect=OSError("https://example.invalid/SECRET")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(bark.send_bark("https://example.invalid/SECRET", "t", "b"))
            self.assertNotIn("SECRET", output.getvalue())
        for url in ("file:///SECRET", "https:///SECRET", "https://example.invalid", "https://u:SECRET@example.invalid/key"):
            with patch.object(bark, "build_opener") as build, contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(bark.send_bark(url, "t", "b"))
                build.assert_not_called()

    def test_disabled_and_test_command_do_not_read_game_state(self):
        with patch.dict(os.environ, {"ZOOTD_BARK_URL": ""}), patch.object(bark, "notification") as notify, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bark.main(["--project-root", str(self.root), "morning"]), 0)
            notify.assert_not_called()
        with patch.object(bark, "read_run") as read, patch.object(bark, "read_attendance") as attendance:
            title, _ = bark.notification(self.root, "test", {})
            self.assertIn("测试", title)
            read.assert_not_called(); attendance.assert_not_called()
        self.assertRaises(ValueError, bark.notification, self.root, "morning", {})

    def test_unreadable_evidence_still_produces_safe_failure_notification(self):
        with patch.object(bark, "daily_result", side_effect=OSError("SECRET")):
            title, body = bark.notification(self.root, "morning", self.env)
        self.assertTrue(title.endswith("异常"))
        self.assertIn("证据无法读取", body)
        self.assertNotIn("SECRET", body)


if __name__ == "__main__":
    unittest.main()
