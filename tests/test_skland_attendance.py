from __future__ import annotations

import contextlib
import copy
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from maa_planner.box_cli import save_secret
from maa_planner.skland import BoxError, Credentials, ZONAI, request_json, signed_headers
from maa_planner import skland_attendance as sign


def bindings():
    return {"list": [
        {"appCode": "arknights", "bindingList": [dict(uid="123", isOfficial=True,
            isDelete=False, channelMasterId="1", gameId=1)]},
        {"appCode": "endfield", "bindingList": [dict(uid="456", isOfficial=True,
            isDelete=False, channelMasterId="1", gameId=3,
            roles=[dict(roleId="789", serverId="1", isBanned=False)])]},
        {"appCode": "future-game", "bindingList": "unrelated future schema"},
    ]}


def receipt(game):
    if game == "arknights":
        return {"code": 0, "data": {"awards": [{"resource": {"name": "龙门币", "secret": "SECRET"}, "count": 200}], "extra": "SECRET"}}
    return {"code": 0, "data": {"ts": "2000000000", "awardIds": [{"id": "item1", "type": 1}],
        "resourceInfoMap": {"item1": {"id": "item1", "name": "折金票", "count": 1000, "icon": "SECRET"}}, "extra": "SECRET"}}


class AttendanceProtocolTests(unittest.TestCase):
    def test_role_selection_uses_endfield_role_not_parent_uid(self):
        self.assertEqual(sign.select_target(bindings(), "arknights"), sign.Target("arknights", "123"))
        self.assertEqual(sign.select_target(bindings(), "endfield"), sign.Target("endfield", "789", "1"))
        self.assertNotIn("789", repr(sign.select_target(bindings(), "endfield")))
        self.assertRaises(BoxError, sign.select_target, bindings(), "endfield", "456")

    def test_deleted_nonofficial_foreign_banned_and_ambiguous_roles_never_signed(self):
        for key, value in [("isDelete", True), ("isOfficial", False), ("channelMasterId", "2")]:
            data = bindings(); data["list"][1]["bindingList"][0][key] = value
            self.assertRaises(BoxError, sign.select_target, data, "endfield")
        for key, value in [("isBanned", True), ("serverId", "2")]:
            data = bindings(); data["list"][1]["bindingList"][0]["roles"][0][key] = value
            self.assertRaises(BoxError, sign.select_target, data, "endfield")
        data = bindings(); role = dict(roleId="999", serverId="1", isBanned=False)
        data["list"][1]["bindingList"][0]["roles"].append(role)
        self.assertRaises(BoxError, sign.select_target, data, "endfield")
        self.assertEqual(sign.select_target(data, "endfield", "999").uid, "999")
        data["list"][1]["bindingList"][0]["roles"].append(copy.deepcopy(role))
        self.assertRaises(BoxError, sign.select_target, data, "endfield", "999")

    def test_unknown_binding_semantics_fail_closed(self):
        for key, value in [("gameId", 4), ("gameId", True), ("isDelete", None), ("isOfficial", "true")]:
            data = bindings(); data["list"][1]["bindingList"][0][key] = value
            self.assertRaises(BoxError, sign.select_target, data, "endfield")
        data = bindings(); del data["list"][1]["bindingList"][0]["roles"][0]["isBanned"]
        self.assertRaises(BoxError, sign.select_target, data, "endfield")

    def test_posts_exact_signed_body_and_dedicated_empty_endfield_request(self):
        credentials = Credentials("fake-cred", "fake-key")
        for game in sign.GAMES:
            with self.subTest(game=game), patch("urllib.request.build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(receipt(game)).encode()
                result = sign.sign_target(credentials, sign.Target(game, "789"), clock=lambda: 1700000001)
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.full_url, ZONAI + sign.PATHS[game])
                headers = dict((k.lower(), v) for k, v in request.header_items())
                if game == "arknights":
                    self.assertEqual(request.data, b'{"uid":"789","gameId":"1"}')
                    expected = signed_headers(credentials, request.full_url, 1700000000, {"uid": "789", "gameId": "1"})
                    self.assertNotIn("sk-game-role", headers)
                else:
                    self.assertIsNone(request.data)
                    self.assertEqual(headers["sk-game-role"], "3_789_1")
                    expected = signed_headers(credentials, request.full_url, 1700000000)
                self.assertEqual(headers["sign"], expected["sign"])
                self.assertEqual(headers["sign"], {"arknights": "3af3d4197506ace30316eaddb73c0cff",
                    "endfield": "dcb84bbb95e715beee857feecdef0968"}[game])
                self.assertEqual(result["status"], "succeeded")
                self.assertNotIn("SECRET", json.dumps(result))

    def test_read_only_transport_still_rejects_reward_claims(self):
        with patch("urllib.request.build_opener") as build:
            for path in sign.PATHS.values():
                self.assertRaises(BoxError, request_json, "POST", ZONAI + path, {})
            for url in (ZONAI + "/api/v1/score/checkin", ZONAI + sign.PATHS["endfield"] + "?roleId=123", "https://evil.example" + sign.PATHS["endfield"]):
                self.assertRaises(BoxError, sign._request_json, "POST", url, {}, None, routes=sign.SIGN_ROUTES)
            build.assert_not_called()

    def test_repeated_code_requires_verified_message_not_just_10001(self):
        for game in sign.GAMES:
            for message in ("请勿重复签到", "请勿重复签到！"):
                self.assertEqual(sign.normalize_response(game, {"code": 10001, "message": message})["status"], "already_signed")
            for bad in ({"code": 10001}, {"code": 10001, "message": "设备信息无效"},
                        {"code": 10001, "message": "SECRET 请勿重复签到"},
                        {"code": 10001, "status": 0, "message": "请勿重复签到"},
                        {"code": 0, "data": {}}, {"code": False, "data": receipt(game)["data"]}):
                self.assertRaises(BoxError, sign.normalize_response, game, bad)

    def test_malformed_or_unknown_rewards_do_not_become_success(self):
        for name, count in [("", 1), ("x\nSECRET", 1), ("x", True), ("x", 0), ("x", "1")]:
            data = receipt("arknights"); data["data"]["awards"][0] = {"resource": {"name": name}, "count": count}
            self.assertRaises(BoxError, sign.normalize_response, "arknights", data)
        data = receipt("endfield"); data["data"]["resourceInfoMap"] = {}
        self.assertRaises(BoxError, sign.normalize_response, "endfield", data)
        data = receipt("endfield"); data["data"]["awardIds"] *= 2
        self.assertRaises(BoxError, sign.normalize_response, "endfield", data)


class AttendanceStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save_secret(self.root, "token", "SECRET-TOKEN")
        self.client = Mock(); self.client.get.return_value = bindings()
        self.client.authenticate.return_value = Credentials("SECRET-CRED", "SECRET-KEY")
        self.now = 2000000000

    def run_sign(self, **kwargs):
        kwargs.setdefault("signer", lambda cred, target: sign.normalize_response(target.game, receipt(target.game)))
        return sign.run_attendance(self.root, client=self.client, clock=lambda: self.now, **kwargs)

    def test_live_success_each_game_persisted_privately_and_older_day_is_not_today(self):
        result = self.run_sign()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sign.state_path(self.root).stat().st_mode & 0o777, 0o600)
        current = sign.read_attendance(self.root, clock=lambda: self.now)
        self.assertEqual(current["state"], "current")
        self.assertNotIn("SECRET", json.dumps(current))
        self.assertNotIn('"123"', json.dumps(current))
        self.assertEqual(sign.read_attendance(self.root, clock=lambda: self.now + 86400)["state"], "previous")

    def test_partial_failure_preserves_other_game_without_reusing_old_success(self):
        self.run_sign()
        calls = []
        def signer(cred, target):
            calls.append(target.game)
            if target.game == "arknights": raise BoxError("network", "SECRET")
            return sign.normalize_response(target.game, receipt(target.game))
        result = self.run_sign(signer=signer)
        self.assertEqual(calls, list(sign.GAMES))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["games"]["arknights"]["rewards"], [])
        self.assertEqual(result["games"]["endfield"]["status"], "succeeded")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_authentication_failure_and_unbound_game_visible(self):
        self.client.authenticate.side_effect = BoxError("authentication", "SECRET")
        signer = Mock()
        result = self.run_sign(signer=signer)
        signer.assert_not_called()
        self.assertTrue(all(e["status"] == "failed" for e in result["games"].values()))
        self.client.authenticate.side_effect = None
        self.client.get.return_value["list"][1]["bindingList"] = []
        result = self.run_sign()
        self.assertEqual(result["games"]["endfield"]["status"], "unbound")
        self.assertEqual(result["games"]["arknights"]["status"], "succeeded")

    def test_interruption_records_unconfirmed_results(self):
        result = self.run_sign(signer=Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(e["error"]["category"] == "interrupted" for e in result["games"].values()))

    def test_dry_run_authenticates_but_does_not_claim_or_replace_state(self):
        self.run_sign(); previous = sign.state_path(self.root).read_bytes()
        signer = Mock(); result = self.run_sign(dry_run=True, signer=signer)
        self.assertEqual(result["status"], "ready")
        signer.assert_not_called()
        self.assertEqual(sign.state_path(self.root).read_bytes(), previous)

    def test_reader_projects_fields_and_rejects_corrupt_future_or_forged_state(self):
        result = self.run_sign(); result["private"] = "SECRET"
        result["games"]["arknights"]["private"] = "SECRET"
        sign.atomic_write_json(sign.state_path(self.root), result)
        self.assertNotIn("SECRET", json.dumps(sign.read_attendance(self.root)))
        for mutation in (lambda d: d.update(schema=2), lambda d: d.update(status="future"),
                         lambda d: d["games"]["arknights"].update(status="failed"),
                         lambda d: d["games"]["arknights"].update(rewards=[]),
                         lambda d: d["games"]["arknights"].update(checked_at=True)):
            bad = copy.deepcopy(result); mutation(bad); sign.atomic_write_json(sign.state_path(self.root), bad)
            self.assertEqual(sign.read_attendance(self.root)["error"]["category"], "storage")
        sign.state_path(self.root).write_text('{"schema":1,"schema":2}')
        self.assertIsNone(sign.read_attendance(self.root)["record"])

    def test_cli_lock_prevents_simultaneous_reward_requests(self):
        directory = self.root / "var/run"; directory.mkdir()
        with (directory / "skland-attendance.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sign, "run_attendance") as run, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sign.main(["--project-root", str(self.root)]), 1)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
