from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from maa_planner import box_cli
from maa_planner.skland import (
    AS, ZONAI, MAX_BODY, BoxError, Credentials, NoRedirect, SklandBoxProvider,
    SklandClient, normalize_player, request_json, response_data, select_account, signed_headers,
)

FIXTURES = Path(__file__).parent / "fixtures" / "skland"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class SklandTests(unittest.TestCase):
    def assert_category(self, category, callback):
        with self.assertRaises(BoxError) as caught:
            callback()
        self.assertEqual(caught.exception.category, category)
        return str(caught.exception)

    def test_normalizes_progression_without_private_response_fields(self):
        result = normalize_player(fixture("player")["data"], "123456789", "2026-09-23T00:00:00Z")
        oper = result.operators["char_fixture_six"]
        self.assertEqual((oper.elite, oper.level, oper.potential, oper.main_skill_level), (2, 70, 1, 7))
        self.assertEqual(oper.skills["skchr_fixture_3"].mastery, 3)
        self.assertEqual(oper.skills["skcom_atk_up[1]"].mastery, 0)
        self.assertTrue(oper.modules["uniequip_fixture_x"].unlocked)
        self.assertFalse(oper.modules["uniequip_fixture_y"].unlocked)
        self.assertEqual(oper.modules["uniequip_fixture_x"].level, 3)
        serialized = json.dumps(result.to_dict())
        for forbidden in ("SYNTHETIC-PRIVATE", "building", "skinId", "gainTime", "potentialRank"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(result.account_id, "Official:123456789")

    def test_unknown_is_not_zero_or_empty(self):
        oper = normalize_player(fixture("player")["data"], "123456789", "now").operators["char_fixture_unknown"]
        for value in (oper.elite, oper.level, oper.potential, oper.main_skill_level, oper.skills, oper.modules):
            self.assertIsNone(value)

    def test_malformed_player_rejected_not_empty(self):
        for bad in (None, {}, {"status": {"uid": "123456789"}}, {"status": {"uid": "123456789"}, "chars": {}}):
            with self.subTest(bad=bad):
                self.assert_category("protocol", lambda: normalize_player(bad, "123456789", "now"))
        self.assert_category("account_mismatch", lambda: normalize_player(fixture("player")["data"], "999", "now"))

    def test_invalid_progression_and_duplicate_id_fail_closed(self):
        for field, value in (("level", True), ("level", 0), ("potentialRank", 6), ("evolvePhase", "2"), ("mainSkillLvl", 8), ("equip", {}), ("skills", "bad")):
            data = fixture("player")["data"]
            data["chars"][0][field] = value
            self.assert_category("protocol", lambda: normalize_player(data, "123456789", "now"))
        for field in ("skills", "equip"):
            data = fixture("player")["data"]
            data["chars"][0][field].append(copy.deepcopy(data["chars"][0][field][0]))
            self.assert_category("protocol", lambda: normalize_player(data, "123456789", "now"))
        data = fixture("player")["data"]
        data["chars"].append(copy.deepcopy(data["chars"][0]))
        self.assert_category("protocol", lambda: normalize_player(data, "123456789", "now"))

    def test_account_selection_never_guesses_or_accepts_bilibili(self):
        data = fixture("binding")["data"]
        self.assertEqual(select_account(data), "123456789")
        self.assert_category("no_official_account", lambda: select_account(data, "987654321"))
        other = copy.deepcopy(data["list"][1]["bindingList"][0]); other["uid"] = "555"
        data["list"][1]["bindingList"].append(other)
        self.assert_category("account_selection", lambda: select_account(data))
        self.assertEqual(select_account(data, "555"), "555")
        other["isDelete"] = True
        self.assert_category("no_official_account", lambda: select_account(data, "555"))
        self.assert_category("protocol", lambda: select_account({}))

    def test_response_error_classes_do_not_echo_remote_message(self):
        for code, category in ((10000, "permission"), (10002, "authentication"), (999, "protocol")):
            msg = self.assert_category(category, lambda: response_data({"code": code, "message": "SECRET"}))
            self.assertNotIn("SECRET", msg)
        for value in ({}, {"code": False}, {"code": "0"}, {"code": 0, "status": 1}, {"code": 0, "data": []}):
            self.assert_category("protocol", lambda: response_data(value))
        self.assertEqual(response_data({"status": 0}, empty=True), {})

    def test_sms_auth_refresh_and_read_only_provider_chain(self):
        responses = [
            {"status": 0}, {"status": 0, "data": {"token": "fake-login"}},
            {"status": 0, "data": {"code": "fake-grant"}},
            {"code": 0, "data": {"cred": "fake-cred"}},
            {"code": 0, "data": {"token": "fake-signing"}},
            fixture("binding"), fixture("player"),
        ]
        transport = Mock(side_effect=responses)
        client = SklandClient(transport, clock=lambda: 1700000001)
        client.send_code("19900000000")
        token = client.login("19900000000", "123456")
        credentials = client.authenticate(token=token)
        result = SklandBoxProvider(client, credentials).fetch_box()
        self.assertEqual(len(result.operators), 2)
        calls = transport.call_args_list
        self.assertEqual(calls[0].args[3], {"phone": "19900000000", "type": 2})
        self.assertEqual(calls[2].args[3]["token"], "fake-login")
        self.assertEqual(calls[4].args, ("GET", ZONAI + "/api/v1/auth/refresh", {"cred": "fake-cred"}))
        self.assertEqual(calls[-1].args[1], ZONAI + "/api/v1/game/player/info?uid=123456789")
        self.assertEqual(calls[-1].args[2]["timestamp"], "1700000000")
        self.assertNotIn("fake", repr(credentials))

    def test_signature_known_vector_query_is_included(self):
        headers = signed_headers(Credentials("fake-cred", "fake-key"), ZONAI + "/api/v1/game/player/info?uid=123", 1700000000)
        self.assertEqual(headers["sign"], "d2d74cba6361b9cf1403bc1201308ae1")

    def test_cred_import_refreshes_without_login_token(self):
        transport = Mock(return_value={"code": 0, "data": {"token": "fake-new"}})
        result = SklandClient(transport).authenticate(cred="fake-cred")
        self.assertEqual(result.signing_token, "fake-new")
        self.assertEqual(transport.call_count, 1)

    def test_route_and_redirect_guard(self):
        for url in ("https://evil.example/api/v1/auth/refresh", ZONAI + "/api/v1/game/attendance", "http://zonai.skland.com/api/v1/auth/refresh"):
            self.assert_category("protocol", lambda: request_json("GET", url, {}))
        self.assert_category("protocol", lambda: NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_http_failure_bodies_and_exception_details_are_hidden(self):
        for error, category in ((urllib.error.HTTPError(ZONAI, 401, "SECRET", {}, None), "authentication"),
                                (urllib.error.HTTPError(ZONAI, 403, "SECRET", {}, None), "permission"),
                                (urllib.error.URLError("SECRET"), "network")):
            with patch("urllib.request.build_opener") as build:
                build.return_value.open.side_effect = error
                message = self.assert_category(category, lambda: request_json("GET", ZONAI + "/api/v1/auth/refresh", {}))
                self.assertNotIn("SECRET", message)

    def test_strict_json_and_response_size(self):
        for body in (b'{"code":0,"code":1}', b'{"code":NaN}', b'SECRET invalid json', b'x' * (MAX_BODY + 1)):
            with patch("urllib.request.build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = body
                self.assert_category("protocol", lambda: request_json("GET", ZONAI + "/api/v1/auth/refresh", {}))


class BoxCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_private_secret_roundtrip_and_permissions(self):
        box_cli.save_secret(self.root, "token", "FAKE-PRIVATE-TOKEN")
        path = self.root / "var/secrets/skland.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(box_cli.load_secret(self.root), {"token": "FAKE-PRIVATE-TOKEN"})
        path.chmod(0o644)
        with self.assertRaises(BoxError):
            box_cli.load_secret(self.root)

    def test_symlink_secret_rejected(self):
        path = box_cli.secret_path(self.root)
        outside = self.root / "outside"; outside.write_text("keep")
        path.symlink_to(outside)
        with self.assertRaises(BoxError):
            box_cli.save_secret(self.root, "token", "fake")
        self.assertEqual(outside.read_text(), "keep")

    def test_visible_phone_and_code_input_but_hidden_token_import(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="19900000000") as visible:
            self.assertEqual(box_cli.visible_input("phone:"), "19900000000")
            visible.assert_called_once_with("phone:")
        with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value="fake-token") as hidden:
            self.assertEqual(box_cli.hidden_input("token:"), "fake-token")
            hidden.assert_called_once_with("token:")

    def test_noninteractive_login_sends_no_sms(self):
        client = Mock()
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(BoxError):
            box_cli.login(self.root, client, "sms")
        client.send_code.assert_not_called()

    def test_sms_login_stores_only_token_and_prints_no_secrets(self):
        client = Mock(); client.login.return_value = "FAKE-PRIVATE-TOKEN"
        output = io.StringIO()
        with patch.object(box_cli, "visible_input", side_effect=["19900000000", "123456"]), contextlib.redirect_stdout(output):
            box_cli.login(self.root, client, "sms")
        client.send_code.assert_called_once_with("19900000000")
        client.login.assert_called_once_with("19900000000", "123456")
        self.assertEqual(box_cli.load_secret(self.root), {"token": "FAKE-PRIVATE-TOKEN"})
        for secret in ("FAKE-PRIVATE-TOKEN", "19900000000", "123456"):
            self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("19900000000", box_cli.secret_path(self.root).read_text())

    def test_failed_relogin_preserves_previous_credentials(self):
        box_cli.save_secret(self.root, "token", "old-fake")
        client = Mock(); client.authenticate.side_effect = BoxError("authentication", "failed")
        with patch.object(box_cli, "hidden_input", return_value="new-fake"), self.assertRaises(BoxError):
            box_cli.login(self.root, client, "token")
        self.assertEqual(box_cli.load_secret(self.root), {"token": "old-fake"})

    def test_sync_writes_normalized_snapshot_and_failure_preserves_it(self):
        box_cli.save_secret(self.root, "token", "fake-token")
        client = Mock()
        client.get.side_effect = [fixture("binding")["data"], fixture("player")["data"]]
        summary = box_cli.sync(self.root, client, None)
        path = Path(summary["path"]); old = path.read_bytes()
        self.assertEqual(summary["operator_count"], 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("123456789", json.dumps(summary))
        self.assertNotIn("SYNTHETIC-PRIVATE", old.decode())
        client.get.side_effect = BoxError("network", "failed")
        with self.assertRaises(BoxError):
            box_cli.sync(self.root, client, None)
        self.assertEqual(path.read_bytes(), old)

    def test_missing_credentials_cli_is_sanitized_failure(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(box_cli.main(["--project-root", str(self.root), "box-sync"]), 1)
        self.assertEqual(json.loads(output.getvalue())["category"], "authentication")


if __name__ == "__main__":
    unittest.main()
