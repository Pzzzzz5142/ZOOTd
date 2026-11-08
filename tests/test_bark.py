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
from maa_planner.supervisor import (
    RUN_MODES, finish_run, load_run_events, record_phase, record_recovery_report,
    record_recovery_started, start_run,
)
from maa_planner.util import atomic_write_json


INVOCATION = "a" * 32
FAILED_RUN = "20261101T040000.000000Z-12345678"
SUCCESSFUL_RUN = "20261101T050000.000000Z-12345678"


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

    def test_source_can_be_hidden_without_replacing_it_with_hostname(self):
        for kind in ("test", "morning", "evening", "attendance"):
            for configured, expected in [(None, "[zenbox] "), (" home ", "[home] "),
                                         ("", ""), ("  ", "")]:
                with self.subTest(kind=kind, source=configured):
                    env = {key: value for key, value in self.env.items() if key != "ZOOTD_BARK_SOURCE"}
                    if configured is not None:
                        env["ZOOTD_BARK_SOURCE"] = configured
                    with patch.object(bark.socket, "gethostname", return_value="zenbox"), \
                         patch.object(bark, "daily_result", return_value=(True, "整轮验收通过")), \
                         patch.object(bark, "attendance_result", return_value=(True, "今日已签到")):
                        _, body = bark.notification(self.root, kind, env)
                    if expected:
                        self.assertTrue(body.startswith(expected), body)
                    else:
                        self.assertFalse(body.startswith(("[", " ")), body)
                        self.assertNotIn("zenbox", body)
                    self.assertIn("北京时间", body)

    def test_recovery_notifications_use_safe_controller_status_and_same_source_policy(self):
        cases = [("started", "进场"), ("recovered", "退出（成功）"),
                 ("scope-blocked", "退出（需人工处理）"), ("failed", "退出（失败）"),
                 ("interrupted", "退出（已中断）"), ("future", "退出（结果未确认）")]
        with patch.dict(os.environ, {"ZOOTD_BARK_URL": "https://example.invalid/SECRET", "ZOOTD_BARK_SOURCE": ""}), \
             patch.object(bark, "send_bark", return_value=True) as send:
            for status, expected in cases:
                with self.subTest(status=status):
                    self.assertTrue(bark.notify_recovery(
                        FAILED_RUN, "morning", status,
                        successful_run_id=SUCCESSFUL_RUN, scope_blocker="manual-login"))
                    _, title, body = send.call_args.args
                    self.assertIn(expected, title)
                    self.assertIn(FAILED_RUN, body)
                    self.assertFalse(body.startswith(("[", " ")))
                    self.assertIn("北京时间", body)
                    if status == "started":
                        self.assertIn("早晨托管出现故障", body)
                    if status == "scope-blocked":
                        self.assertIn("需要手动登录", body)
                    self.assertEqual(SUCCESSFUL_RUN in body, status == "recovered")
            self.assertTrue(bark.notify_recovery(FAILED_RUN, "manual", "scope-blocked", scope_blocker="SECRET"))
            self.assertNotIn("SECRET", send.call_args.args[2])
            send.reset_mock()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertFalse(bark.notify_recovery("SECRET", "manual", "started"))
                self.assertFalse(bark.notify_recovery(FAILED_RUN, "manual", "recovered"))
            send.assert_not_called()
            self.assertNotIn("SECRET", output.getvalue())

    def test_recovery_notification_failure_or_disabled_delivery_is_nonfatal(self):
        with patch.dict(os.environ, {"ZOOTD_BARK_URL": ""}), patch.object(bark, "send_bark") as send:
            self.assertFalse(bark.notify_recovery(FAILED_RUN, "manual", "started"))
            send.assert_not_called()
        with patch.dict(os.environ, {"ZOOTD_BARK_URL": "https://example.invalid/SECRET"}), \
             patch.object(bark, "send_bark", side_effect=OSError("SECRET")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(bark.notify_recovery(FAILED_RUN, "manual", "failed"))
            self.assertNotIn("SECRET", output.getvalue())

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

    def recovery_record(self, *, status="recovered", child=True, finish=True,
                        attempt_matches=True, proof_matches=True, report=True,
                        inherit_invocation=False, summary=None, actions=None,
                        audit_version=1, report_version=2, repair=None):
        parent = self.run_record(result="failed")
        attempt = {"attempt_id": "a" * 32, "slot": "morning"}
        record_recovery_started(self.root, parent, {"recovery_attempt": attempt})
        child_id = None
        proof = {}
        if child:
            with patch.dict(os.environ, {
                "INVOCATION_ID": INVOCATION if inherit_invocation else "",
                "MAA_RECOVERY_ACTIVE": "true", "MAA_RECOVERY_PARENT_RUN_ID": parent,
                "MAA_RECOVERY_ATTEMPT_ID": ("a" if attempt_matches else "b") * 32,
                "MAA_RECOVERY_SLOT": "morning",
            }):
                child_id = self.run_record(result="succeeded" if status == "recovered" else "failed")
            events = load_run_events(self.root, child_id)
            proof = {"successful_run_id": child_id,
                     "event_sha256": events[-1]["event_sha256"] if proof_matches else "0" * 64}
        if finish:
            agent = {"report": {
                "schema_version": report_version,
                "summary": summary or "客户端无响应；Android ANR 与启动日志一致。",
                "actions_taken": actions or ["重建 Waydroid 会话并重启客户端，保留游戏数据。", "重新执行完整日常。"],
                "scope_blocker": "manual-login" if status == "scope-blocked" else "none",
            }} if report else {"error": "adapter timed out"}
            record_recovery_report(self.root, parent, {
                "schema_version": audit_version, "status": status, "agent": agent,
                "controller_verification": {
                    "status": {"recovered": "success", "scope-blocked": "scope-accepted",
                               "failed": "rejected"}[status],
                    "run": proof,
                    "code_repair": repair or {"status": "not-needed"},
                },
            })
        return parent, child_id

    def test_service_settlement_posts_agent_diagnosis_and_fix_with_linked_child_proof(self):
        for inherited in (False, True):
            with self.subTest(inherit_invocation=inherited):
                parent, child = self.recovery_record(inherit_invocation=inherited)
                accepted, body = bark.daily_result(self.root, INVOCATION)
                self.assertTrue(accepted)
                self.assertIn("Android ANR", body)
                self.assertIn("重建 Waydroid 会话", body)
                self.assertIn("9 阶段及清理通过独立验收", body)
                self.assertIn(child, body)
                self.assertIn(parent + "/events/", body)
                opener = self.response()
                with patch.dict(os.environ, {"ZOOTD_BARK_URL": "https://example.invalid/key", "SERVICE_RESULT": "exit-code"}), \
                     patch.object(bark, "build_opener", return_value=opener), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(bark.main(["--project-root", str(self.root), "morning"]), 0)
                payload = json.loads(opener.open.call_args.args[0].data)
                self.assertIn("异常", payload["title"])  # Original service failure remains visible.
                self.assertIn(body, payload["body"])
                self.assertNotIn("缺少与本次执行匹配", payload["body"])

    def test_failed_blocked_pending_or_missing_reports_never_claim_recovery(self):
        for status in ("failed", "scope-blocked"):
            for child in (False, True):
                with self.subTest(status=status, child=child):
                    self.recovery_record(status=status, child=child)
                    accepted, body = bark.daily_result(self.root, INVOCATION)
                    self.assertFalse(accepted)
                    self.assertIn("Android ANR", body)
                    self.assertIn("重建 Waydroid 会话", body)
                    self.assertIn("需要手动登录" if status == "scope-blocked" else "恢复未通过验收", body)
        self.recovery_record(finish=False)
        accepted, body = bark.daily_result(self.root, INVOCATION)
        self.assertFalse(accepted)
        self.assertIn("尚无终态报告", body)
        self.recovery_record(status="failed", report=False)
        accepted, body = bark.daily_result(self.root, INVOCATION)
        self.assertFalse(accepted)
        self.assertIn("未提交有效诊断报告", body)
        self.assertIn("adapter timed out", body)

    def test_recovery_settlement_rejects_wrong_invocation_attempt_proof_and_newer_run(self):
        self.recovery_record()
        accepted, body = bark.daily_result(self.root, "b" * 32)
        self.assertFalse(accepted)
        self.assertNotIn("Android ANR", body)
        self.recovery_record(attempt_matches=False)
        title, body = bark.notification(self.root, "morning", self.env)
        self.assertTrue(title.endswith("异常"))
        self.assertIn("证据无法读取", body)
        self.recovery_record(proof_matches=False)
        accepted, body = bark.daily_result(self.root, INVOCATION)
        self.assertFalse(accepted)
        self.assertIn("证据不符", body)
        self.assertNotIn("通过独立验收", body)
        self.run_record(complete=False)
        accepted, body = bark.daily_result(self.root, INVOCATION)
        self.assertFalse(accepted)
        self.assertNotIn("Android ANR", body)
        _, child = self.recovery_record()
        event = next((self.root / "var/state/supervisor/runs" / child / "events").glob("*run-finished.json"))
        event.write_text(event.read_text().replace('"success"', '"failed"'))
        self.assertFalse(bark.daily_result(self.root, INVOCATION)[0])

    def test_future_recovery_formats_do_not_become_success(self):
        for versions in ({"audit_version": 2}, {"report_version": 3}):
            with self.subTest(versions=versions):
                self.recovery_record(**versions)
                accepted, body = bark.daily_result(self.root, INVOCATION)
                self.assertFalse(accepted)
                self.assertIn("版本不受支持", body)

    def test_code_repair_keeps_verified_pr_and_application_status(self):
        for verified in (True, False):
            with self.subTest(verified=verified):
                self.recovery_record(repair={
                    "status": "pr-opened", "commit": "a" * 40,
                    "pull_request_url": "https://github.com/example/repo/pull/7",
                    "pull_request_verification": {"status": "verified" if verified else "unverified"},
                    "applied_to_runtime": False,
                })
                accepted, body = bark.daily_result(self.root, INVOCATION)
                self.assertTrue(accepted)
                self.assertIn("本轮未应用", body)
                if verified:
                    self.assertIn("https://github.com/example/repo/pull/7", body)
                else:
                    self.assertNotIn("https://github.com", body)
                    self.assertIn("PR 未发布或未核验", body)

    def test_long_reports_keep_fix_verdict_audit_and_timestamp_within_post_budget(self):
        self.recovery_record(summary="诊断信息" * 1800,
                             actions=[f"修复步骤{i}：" + "操作结果" * 100 for i in range(8)])
        _, body = bark.notification(self.root, "morning", self.env)
        self.assertLessEqual(len(body.encode()), 3000)
        self.assertIn("诊断（agent）：", body)
        self.assertIn("修复步骤0", body)
        self.assertIn("修复步骤5", body)
        self.assertIn("另 2 项见审计", body)
        self.assertIn("通过独立验收", body)
        self.assertIn("recovery-finished.json", body)
        self.assertIn("北京时间", body)

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
