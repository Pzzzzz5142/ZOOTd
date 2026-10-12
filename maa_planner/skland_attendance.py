"""Explicit Skland reward claims, separate from read-only Box and monitoring."""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .box_cli import load_secret
from .skland import (ZONAI, BoxError, SklandClient, _request_json, array, identity,
                     obj, optional_int, response_data, signed_headers, uid_field)
from .sources import _load_json_bytes
from .util import atomic_write_json

GAMES = ("arknights", "endfield")
PATHS = {"arknights": "/api/v1/game/attendance", "endfield": "/web/v1/game/endfield/attendance"}
SIGN_ROUTES = {(ZONAI, path): "POST" for path in PATHS.values()}
STATUSES = {"pending", "running", "succeeded", "already_signed", "failed", "unbound"}
ERRORS = {
    "authentication": "森空岛登录已失效或尚未登录，请在本机运行 ./bin/zootd box-login。",
    "account_selection": "存在多个官服角色，请为签到配置角色选择参数。",
    "no_official_account": "未找到绑定的国服官服角色。",
    "permission": "森空岛拒绝签到，请检查官方账号权限。",
    "network": "森空岛请求失败，签到结果未确认。",
    "protocol": "森空岛签到协议异常，结果未确认。",
    "storage": "本地签到状态或登录凭据无法读取。",
    "interrupted": "签到执行已中断，结果未确认。",
}


def error_result(exc: Exception) -> dict:
    category = exc.category if isinstance(exc, BoxError) else "storage" if isinstance(exc, OSError) else "protocol"
    category = category if category in ERRORS else "protocol"
    return {"category": category, "message": ERRORS[category]}


def local_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).date().isoformat()


@dataclass(frozen=True)
class Target:
    game: str
    uid: str = field(repr=False)
    server_id: str = field(default="1", repr=False)


def select_target(data: dict, game: str, requested: str | None = None) -> Target:
    if game not in GAMES:
        raise BoxError("protocol", ERRORS["protocol"])
    targets = []
    if requested is not None:
        requested = uid_field(requested)
    for app in array(obj(data).get("list")):
        app = obj(app)
        if app.get("appCode") != game:
            continue
        for binding in array(app.get("bindingList")):
            binding = obj(binding)
            if type(binding.get("isOfficial")) is not bool or type(binding.get("isDelete")) is not bool:
                raise BoxError("protocol", ERRORS["protocol"])
            if not binding["isOfficial"] or binding["isDelete"] or binding.get("channelMasterId") != "1":
                continue
            expected_game_id = 1 if game == "arknights" else 3
            if "gameId" in binding and (type(binding["gameId"]) is not int or binding["gameId"] != expected_game_id):
                raise BoxError("protocol", ERRORS["protocol"])
            if game == "arknights":
                targets.append(Target(game, uid_field(binding.get("uid"))))
            else:
                for role in array(binding.get("roles")):
                    role = obj(role)
                    if type(role.get("isBanned")) is not bool:
                        raise BoxError("protocol", ERRORS["protocol"])
                    server = uid_field(role.get("serverId"))
                    if not role["isBanned"] and server == "1":
                        targets.append(Target(game, uid_field(role.get("roleId")), server))
    if len(set(targets)) != len(targets):
        raise BoxError("protocol", ERRORS["protocol"])
    if requested is not None:
        targets = [target for target in targets if target.uid == requested]
    if not targets:
        raise BoxError("no_official_account", ERRORS["no_official_account"])
    if len(targets) != 1:
        raise BoxError("account_selection", ERRORS["account_selection"])
    return targets[0]


def reward(name, count) -> dict:
    if (not isinstance(name, str) or not name.strip() or len(name) > 128
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise BoxError("protocol", ERRORS["protocol"])
    count = optional_int(count, 1, 100000000)
    if count is None:
        raise BoxError("protocol", ERRORS["protocol"])
    return {"name": name, "count": count}


def normalize_response(game: str, response: dict) -> dict:
    response = obj(response)
    # 10001 also means invalid device/role information. A code alone is no proof.
    if (type(response.get("code")) is int and response["code"] == 10001
            and ("status" not in response or type(response["status"]) is int and response["status"] == 10001)
            and isinstance(response.get("message"), str)
            and re.fullmatch(r"请勿重复签到[!！。]?", response["message"])):
        return {"status": "already_signed", "rewards": [], "error": None}
    data = response_data(response)
    rewards = []
    if game == "arknights":
        for award in array(data.get("awards")):
            award = obj(award)
            rewards.append(reward(obj(award.get("resource")).get("name"), award.get("count")))
    else:
        resources = obj(data.get("resourceInfoMap"))
        ids = []
        for award in array(data.get("awardIds")):
            key = identity(obj(award).get("id"))
            if key in ids:
                raise BoxError("protocol", ERRORS["protocol"])
            ids.append(key)
            resource = obj(resources.get(key))
            rewards.append(reward(resource.get("name"), resource.get("count")))
    if not 1 <= len(rewards) <= 50:
        raise BoxError("protocol", ERRORS["protocol"])
    return {"status": "succeeded", "rewards": rewards, "error": None}


def sign_target(credentials, target: Target, *, transport=None, clock=time.time) -> dict:
    url = ZONAI + PATHS[target.game]
    body = {"uid": target.uid, "gameId": "1"} if target.game == "arknights" else None
    headers = signed_headers(credentials, url, int(clock()) - 1, body)
    if target.game == "endfield":
        headers["sk-game-role"] = f"3_{target.uid}_{target.server_id}"
    if transport is None:
        response = _request_json("POST", url, headers, body, routes=SIGN_ROUTES)
    else:
        response = transport("POST", url, headers, body)
    return normalize_response(target.game, response)


def state_path(root: Path) -> Path:
    return root / "var/state/skland-attendance.json"


def run_attendance(root: Path, *, client=None, signer=sign_target, clock=time.time,
                   uid=None, endfield_role=None, dry_run=False) -> dict:
    client = client or SklandClient()
    value = {"schema": 1, "status": "running", "day": local_day(clock()),
             "started_at": clock(), "finished_at": None,
             "games": {game: {"status": "pending", "checked_at": None, "rewards": [], "error": None}
                       for game in GAMES}}
    def save():
        if not dry_run:
            atomic_write_json(state_path(root), value)
    save()
    try:
        credentials = client.authenticate(**load_secret(root))
        binding = client.get(credentials, "/api/v1/game/player/binding")
        for game, requested in zip(GAMES, (uid, endfield_role)):
            entry = value["games"][game]
            entry.update(status="running", checked_at=clock())
            save()
            try:
                target = select_target(binding, game, requested)
                if dry_run:
                    entry["status"] = "pending"
                else:
                    entry.update(signer(credentials, target))
            except Exception as exc:
                error = error_result(exc)
                entry.update(status="unbound" if error["category"] == "no_official_account" else "failed", error=error)
            save()
    except (Exception, KeyboardInterrupt) as exc:
        error = error_result(exc) if isinstance(exc, Exception) else {"category": "interrupted", "message": ERRORS["interrupted"]}
        for entry in value["games"].values():
            if entry["status"] in {"pending", "running"}:
                entry.update(status="failed", error=error)
    value["status"] = ("ready" if dry_run else "ok") if all(
        entry["status"] in ({"pending"} if dry_run else {"succeeded", "already_signed"})
        for entry in value["games"].values()) else "failed"
    value["finished_at"] = clock()
    save()
    return value


def read_attendance(root: Path, *, clock=time.time) -> dict:
    """Project validated display fields; never expose raw file data or identities."""
    today = local_day(clock())
    empty = {"state": "unavailable", "today": today, "record": None, "error": None}
    path = state_path(root)
    if not path.exists():
        return empty
    try:
        with path.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized state")
        value = obj(_load_json_bytes(raw))
        if type(value.get("schema")) is not int or value["schema"] != 1 or value.get("status") not in {"running", "ok", "failed"}:
            raise ValueError("unsupported state")
        day = value["day"]
        if not isinstance(day, str) or datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
            raise ValueError("invalid date")
        games = {}
        for game in GAMES:
            entry = obj(obj(value.get("games")).get(game))
            if entry.get("status") not in STATUSES:
                raise ValueError("unknown status")
            rewards = [reward(obj(r).get("name"), r.get("count")) for r in array(entry.get("rewards"))]
            if len(rewards) > 50 or entry["status"] == "succeeded" and not rewards:
                raise ValueError("invalid rewards")
            error = entry.get("error")
            if error is not None:
                category = obj(error).get("category")
                if category not in ERRORS:
                    raise ValueError("unknown error")
                error = {"category": category, "message": ERRORS[category]}
            checked = entry.get("checked_at")
            if checked is not None and (type(checked) not in (int, float) or not 0 <= checked <= 4102444800):
                raise ValueError("invalid time")
            games[game] = {"status": entry["status"], "checked_at": checked, "rewards": rewards, "error": error}
        times = {}
        for key in ("started_at", "finished_at"):
            t = value.get(key)
            if t is None and key == "finished_at" and value["status"] == "running":
                times[key] = None
            elif type(t) in (int, float) and 0 <= t <= 4102444800:
                times[key] = t
            else:
                raise ValueError("invalid time")
        if value["status"] == "ok" and any(e["status"] not in {"succeeded", "already_signed"} for e in games.values()):
            raise ValueError("success without results")
        if value["status"] != "running" and any(e["status"] in {"pending", "running"} for e in games.values()):
            raise ValueError("terminal state without terminal results")
        record = {"status": value["status"], "day": day, **times, "games": games}
        return {**empty, "state": "current" if day == today else "previous", "record": record}
    except Exception:
        return {**empty, "error": {"category": "storage", "message": ERRORS["storage"]}}


def stop_signing(*_):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sign both games in Skland; never starts the game")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--uid", help="Choose a bound Official Arknights UID")
    parser.add_argument("--endfield-role", help="Choose a bound Official Endfield role ID")
    parser.add_argument("--dry-run", action="store_true", help="Authenticate and validate bindings without claiming or writing results")
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    previous_handler = signal.signal(signal.SIGTERM, stop_signing)
    try:
        (root / "var/run").mkdir(parents=True, exist_ok=True)
        with (root / "var/run/skland-attendance.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            value = run_attendance(root, uid=args.uid, endfield_role=args.endfield_role, dry_run=args.dry_run)
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value["status"] in {"ok", "ready"} else 1
    except BlockingIOError:
        print("签到已有任务在执行；未发起重复请求。", file=sys.stderr)
        return 1
    except OSError:
        print(ERRORS["storage"], file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
