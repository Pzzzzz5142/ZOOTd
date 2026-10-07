"""Read-only, in-memory Skland monitoring; independent of execution evidence."""
from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from .box_cli import load_secret
from .skland import BoxError, SklandClient, array, obj, optional_int, select_account, uid_field

REFRESH_SECONDS = 20 * 60
MAX_TIMESTAMP = 4102444800
ERROR_MESSAGES = {
    "authentication": "森空岛尚未登录或登录已失效，请在本机终端运行 ./bin/zootd box-login。",
    "account_selection": "存在多个官服角色，请为面板配置 --skland-uid。",
    "no_official_account": "未找到绑定的国服官服角色，请检查森空岛绑定。",
    "account_mismatch": "森空岛返回的角色与请求不一致，本次快照已拒绝。",
    "permission": "森空岛拒绝访问，请检查官方账号的数据权限。",
    "storage": "本地登录凭据不可读取，请检查文件权限或重新登录。",
    "network": "无法连接森空岛，等待下次定时同步。",
    "protocol": "森空岛数据格式异常，本次快照已拒绝。",
}


def number(data: dict, key: str, high: int = MAX_TIMESTAMP) -> int | None:
    return optional_int(data.get(key), 0, high)


def deadline(data: dict, key: str) -> int | None:
    value = optional_int(data.get(key), -1, MAX_TIMESTAMP)
    return value if value is not None and value > 0 else None


def normalize_monitor(data: dict, uid: str) -> dict:
    """Allowlist counts/timestamps only; absent fields stay unknown, never zero."""
    data = obj(data)
    status = obj(data.get("status"))
    if uid_field(status.get("uid")) != uid:
        raise BoxError("account_mismatch", ERROR_MESSAGES["account_mismatch"])
    sanity = None
    if status.get("ap") is not None:
        ap = obj(status["ap"])
        sanity = {"current": number(ap, "current", 100000), "max": number(ap, "max", 10000),
                  "last_added_at": deadline(ap, "lastApAddTime"),
                  "full_at": deadline(ap, "completeRecoveryTime")}
        if sanity["max"] == 0:
            raise BoxError("protocol", ERROR_MESSAGES["protocol"])
    recruit = None
    if data.get("recruit") is not None:
        raw_slots = array(data["recruit"])
        if len(raw_slots) > 4:
            raise BoxError("protocol", ERROR_MESSAGES["protocol"])
        recruit = []
        for index, raw in enumerate(raw_slots, 1):
            raw = obj(raw)
            recruit.append({"slot": index, "state": optional_int(raw.get("state"), -1, 100),
                            "started_at": deadline(raw, "startTs"), "finished_at": deadline(raw, "finishTs")})
    building = obj(data["building"]) if data.get("building") is not None else {}
    drones = None
    if building.get("labor") is not None:
        labor = obj(building["labor"])
        drones = {"current": number(labor, "value", 10000), "max": number(labor, "maxValue", 10000),
                  "updated_at": deadline(labor, "lastUpdateTime"),
                  "remaining_seconds": optional_int(labor.get("remainSecs"), -1, MAX_TIMESTAMP)}
        if drones["remaining_seconds"] == -1:
            drones["remaining_seconds"] = None
        if drones["max"] == 0 or (drones["current"] is not None and drones["max"] is not None
                                 and drones["current"] > drones["max"]):
            raise BoxError("protocol", ERROR_MESSAGES["protocol"])
    tired = len(array(building["tiredChars"])) if building.get("tiredChars") is not None else None
    trading = None
    if building.get("tradings") is not None:
        trading = []
        for index, raw in enumerate(array(building["tradings"]), 1):
            raw = obj(raw)
            trading.append({"station": index,
                            "stored": len(array(raw["stock"])) if raw.get("stock") is not None else None,
                            "limit": number(raw, "stockLimit", 10000)})
    return {"sanity": sanity, "recruit": recruit, "drones": drones,
            "tired_operators": tired, "trading": trading,
            "last_online_at": number(status, "lastOnlineTs")}


def fetch_monitor(root: Path, uid: str | None = None) -> dict:
    # Avoid creating secret directories for an unconfigured dashboard.
    if not (root / "var/secrets").exists():
        raise BoxError("authentication", ERROR_MESSAGES["authentication"])
    client = SklandClient()
    credentials = client.authenticate(**load_secret(root))
    selected = select_account(client.get(credentials, "/api/v1/game/player/binding"), uid)
    data = client.get(credentials, "/api/v1/game/player/info?" + urlencode({"uid": selected}))
    return normalize_monitor(data, selected)


class SklandMonitor:
    """One background poll per server, even with no open pages or many viewers."""

    def __init__(self, root: Path, uid: str | None = None, *, fetcher=fetch_monitor,
                 clock=time.time, monotonic=time.monotonic):
        self.root, self.uid = root, uid
        self.fetcher, self.clock, self.monotonic = fetcher, clock, monotonic
        self.lock = threading.Lock()
        self.poll_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.next_poll = 0.0
        self.value = {"state": "loading", "snapshot": None, "fetched_at": None,
                      "checked_at": None, "next_refresh_at": None, "error": None}

    def poll(self):
        # Fetch outside the state lock: slow upstream never blocks HTTP readers.
        with self.poll_lock:
            if self.monotonic() < self.next_poll:
                return
            checked_at = self.clock()
            try:
                snapshot = self.fetcher(self.root, self.uid)
                update = {"state": "fresh", "snapshot": snapshot,
                          "fetched_at": self.clock(), "error": None}
            except Exception as exc:
                # Never return raw exceptions, response text, credentials or account IDs.
                category = exc.category if isinstance(exc, BoxError) else "storage" if isinstance(exc, OSError) else "protocol"
                category = category if category in ERROR_MESSAGES else "protocol"
                update = {"error": {"category": category, "message": ERROR_MESSAGES[category]}}
            self.next_poll = self.monotonic() + REFRESH_SECONDS
            with self.lock:
                if "snapshot" not in update:
                    update["state"] = "stale" if self.value["snapshot"] is not None else "unavailable"
                self.value.update(update, checked_at=checked_at,
                                  next_refresh_at=self.clock() + REFRESH_SECONDS)

    def read(self) -> dict:
        with self.lock:
            value = copy.deepcopy(self.value)
        now = self.clock()
        if value["state"] == "fresh" and now - value["fetched_at"] >= REFRESH_SECONDS:
            value["state"] = "stale"
        return {**value, "observed_at": now, "refresh_interval_seconds": REFRESH_SECONDS}

    def start(self):
        def run():
            while not self.stop_event.is_set():
                self.poll()
                self.stop_event.wait(max(0, self.next_poll - self.monotonic()))
        self.thread = threading.Thread(target=run, name="skland-monitor", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
