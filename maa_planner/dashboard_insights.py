"""Bounded, read-only explanations of a run's local evidence and task logs.

Audit terminals remain the authority for success. Timestamp-associated logs and
planner decisions are observations, never replacements for missing terminals.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .inventory import InventorySnapshot

LOCAL = ZoneInfo("Asia/Shanghai")
MAX_BYTES = 2 * 1024 * 1024
MAX_LOGS = 64
STAMP = re.compile(r"(?P<stamp>[0-9]{8}-[0-9]{6})(?:-[0-9]+)?-(?P<kind>.+)\.log")
LINE = re.compile(r"\[(?P<at>[0-9-]{10} [0-9:]{8}) (?P<level>INFO|WARN|ERROR)\s*\]\s*(?P<text>.*)")
TASKS = {"StartUp": "启动游戏", "Infrast": "基建换班", "Recruit": "公开招募",
         "Mall": "信用商店", "Depot": "仓库扫描", "Fight": "战斗", "Award": "领取任务奖励"}
ROOMS = {"Power": "发电站", "Office": "办公室", "Control": "控制中枢", "Mfg": "制造站",
         "Trade": "贸易站", "Reception": "会客室", "Dorm": "宿舍", "Training": "训练室"}
ERRORS = {"GameOffline": "游戏断线", "FightMissionFailed": "战斗失败画面",
          "TaskChainError": "任务执行错误", "ConnectFailed": "设备连接失败"}


def _time(value: object) -> datetime | None:
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return at.astimezone(UTC) if at.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _read(root: Path, relative: str) -> bytes:
    """Only read regular files inside the project, with a hard byte bound."""
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("not a project-relative path")
    path = root.resolve() / relative_path
    if path.resolve(strict=True) != path:
        raise ValueError("symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("not a bounded regular file")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("file grew past bound")
    return raw


def _json(root: Path, relative: str) -> dict:
    value = json.loads(_read(root, relative))
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _kind(filename: str) -> tuple[str, str | None, str | None] | None:
    match = STAMP.fullmatch(filename)
    if not match:
        return None
    kind = match["kind"]
    if re.fullmatch(r"annihilation-[0-9]+", kind):
        return "annihilation", "Annihilation", "annihilation"
    if kind in {"depot", "pre-daily-depot"}:
        return "depot", None, None
    if kind in {"daily", "daily-retry"}:
        return "daily", None, None
    if kind in {"award-final", "e2e-award"}:
        return "award", None, None
    battle = re.fullmatch(r"(?:activity-[A-Za-z0-9-]+|fallback|manual)-attempt-[0-9]+-(proxy-preflight|fight)-([A-Za-z0-9-]+)", kind)
    if battle:
        return "farming", battle[2], "preflight" if battle[1] == "proxy-preflight" else "fight"
    return None


def _number(value: object) -> int | float | None:
    return value if type(value) in (int, float) and 0 <= value <= 10**15 and math.isfinite(value) else None


def _message(text: str) -> tuple[str, str] | None:
    if text in ERRORS:
        return ERRORS[text], "warning"
    task = re.fullmatch(r"(StartUp|Infrast|Recruit|Mall|Depot|Fight|Award) (Start|Completed)", text)
    if task:
        return TASKS[task[1]] + ("开始" if task[2] == "Start" else "完成"), "normal"
    room = re.fullmatch(r"EnterFacility (\w+) #([0-9]+)", text)
    if room and room[1] in ROOMS:
        return f"进入{ROOMS[room[1]]} {int(room[2]) + 1}", "normal"
    if text == "Recruit":
        return "确认一项公开招募", "normal"
    if text == "Refresh Tags":
        return "刷新公招标签", "normal"
    if text.startswith("RecruitResult: "):
        return "公招识别：" + text[15:][:300], "normal"
    sanity = re.fullmatch(r"Current sanity: ([0-9]+)/([0-9]+)", text)
    if sanity:
        return f"读取理智 {sanity[1]} / {sanity[2]}", "normal"
    mission = re.fullmatch(r"Mission started \(([0-9]+) times, use ([0-9]+) sanity\)", text)
    if mission:
        return f"启动 {mission[1]} 场连战，费用 {mission[2]} 理智", "normal"
    if text.startswith("Drops: "):
        return "掉落：" + text[7:][:500], "normal"
    return None


def _log(raw: bytes, relative: str, phase: str, stage: str | None, kind: str | None,
         start: datetime, end: datetime, audited: bool) -> dict:
    result = {"path": relative, "phase": phase, "stage": stage, "kind": kind,
              "binding": "audit" if audited else "time-window", "entries": [],
              "tasks": {}, "rooms": [], "recruit_confirmed": 0, "tag_refreshes": 0,
              "reported_battles": 0, "sanity_cost": 0, "drops": [], "warnings": 0,
              "first_sanity": None, "last_sanity": None, "complete": False}
    pending_batch = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        match = LINE.fullmatch(line)
        if not match:
            continue
        at = datetime.strptime(match["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL).astimezone(UTC)
        if not audited and not start <= at <= end:
            continue
        text = match["text"]
        translated = _message(text)
        if translated:
            result["entries"].append({"at": at.isoformat(), "text": translated[0], "tone": translated[1]})
        if match["level"] in {"WARN", "ERROR"}:
            result["warnings"] += 1
            if not translated:
                result["entries"].append({"at": at.isoformat(), "text": "日志报告警告，详细原因未识别", "tone": "warning"})
        if text == "FightMissionFailed":
            pending_batch = None
        task = re.fullmatch(r"(StartUp|Infrast|Recruit|Mall|Depot|Fight|Award) (Start|Completed)", text)
        if task:
            stats = result["tasks"].setdefault(task[1], {"started": 0, "completed": 0})
            stats["started" if task[2] == "Start" else "completed"] += 1
        room = re.fullmatch(r"EnterFacility (\w+) #([0-9]+)", text)
        if room and room[1] in ROOMS:
            label = f"{ROOMS[room[1]]} {int(room[2]) + 1}"
            if label not in result["rooms"]:
                result["rooms"].append(label)
        if text == "Recruit":
            result["recruit_confirmed"] += 1
        if text == "Refresh Tags":
            result["tag_refreshes"] += 1
        sanity = re.fullmatch(r"Current sanity: ([0-9]+)/([0-9]+)", text)
        if sanity:
            result["last_sanity"] = int(sanity[1])
            if result["first_sanity"] is None:
                result["first_sanity"] = int(sanity[1])
        mission = re.fullmatch(r"Mission started \(([0-9]+) times, use ([0-9]+) sanity\)", text)
        if mission:
            pending_batch = (int(mission[1]), int(mission[2]))
        if text.startswith("Drops: "):
            # Count only batches with a subsequent drop report, never starts alone.
            if pending_batch:
                result["reported_battles"] += pending_batch[0]
                result["sanity_cost"] += pending_batch[1]
                pending_batch = None
            for name, count in re.findall(r"([^,]+?)\s*×\s*([0-9]+)", text[7:]):
                result["drops"].append({"name": name.strip()[:120], "count": int(count)})
        if text == "AllTasksCompleted":
            result["complete"] = True
    result["entries"] = result["entries"][-100:]
    return result


def _plan(root: Path, directory: str, pattern: str, start: datetime,
          end: datetime) -> dict | None:
    paths = sorted((root / directory).glob(pattern), reverse=True)
    for path in paths[:100]:
        try:
            data = _json(root, path.relative_to(root).as_posix())
            at = _time(data.get("generated_at"))
            if at and start <= at <= end:
                # A newer unsupported decision must not revive an older plan.
                return data if type(data.get("schema_version")) is int and data["schema_version"] == 1 else None
        except (OSError, ValueError, TypeError):
            continue
    return None


def _farming_plan(data: dict, names: dict) -> dict:
    rows = []
    for row in data.get("candidates", [])[:40]:
        if not isinstance(row, dict):
            continue
        item_id = row.get("item_id")
        rows.append({"stage": row.get("stage_code"), "material": names.get(item_id, item_id),
                     "inventory": _number(row.get("inventory")), "direct": _number(row.get("direct_inventory")),
                     "craftable": _number(row.get("craftable_equivalent")),
                     "target": _number(row.get("target")), "deficit": _number(row.get("deficit")),
                     "efficiency": _number(row.get("overall_efficiency")),
                     "eligible": row.get("eligible") is True, "rejected_by": row.get("rejected_by", [])})
    evidence = data.get("evidence", {})
    return {"decision": data.get("decision"), "reason": data.get("reason"),
            "selected_stage": data.get("selected_stage"), "activity": data.get("activity", {}).get("name"),
            "inventory_at": evidence.get("inventory", {}).get("captured_at"),
            "order": [row.get("stage_code") for row in evidence.get("execution_candidates", [])[:40]
                      if isinstance(row, dict)], "candidates": rows}


def run_insights(root: Path, run: dict) -> dict:
    if run.get("status") == "invalid":
        return {"phases": {}}
    start = _time(run.get("started_at"))
    if not start:
        return {"phases": {}}
    start = start.replace(microsecond=0)
    end = _time(run.get("finished_at")) or datetime.now(UTC)
    # An abandoned unfinished run cannot absorb a newer run's task logs.
    for path in (root / "var/state/supervisor/runs").glob("*"):
        next_start = _time(path.name[:23])
        if next_start and start < next_start < end and path.name != run["run_id"]:
            end = next_start.replace(microsecond=0)
    phases = {name: {"logs": [], "files": []} for name in run.get("expected_phases", [])}
    audited_paths = {}
    for phase in run.get("phases", []):
        bucket = phases[phase["phase"]]
        for evidence in phase.get("evidence_files", []):
            if not isinstance(evidence, dict):
                continue
            relative = evidence.get("path", "")
            if not isinstance(relative, str):
                continue
            permitted = (relative.startswith("var/state/host/") and "/" not in relative[15:]
                         and _kind(Path(relative).name)) or relative in {
                             "var/state/planner/inventory.json", "var/state/planner/latest-sources.json"}
            if not permitted:
                continue
            item = {"path": relative, "state": "unavailable"}
            bucket["files"].append(item)
            try:
                raw = _read(root, relative)
                if len(raw) != evidence.get("size") or hashlib.sha256(raw).hexdigest() != evidence.get("sha256"):
                    item["state"] = "changed"
                    continue
                item["state"] = "verified"
                audited_paths[relative] = raw
                if relative.endswith("inventory.json"):
                    inventory = InventorySnapshot.from_dict(json.loads(raw))
                    bucket["inventory"] = {"captured_at": inventory.captured_at.isoformat(),
                                           "observed_items": len(inventory.items),
                                           "pure_gold": inventory.quantity("3003")}
                elif relative.endswith("latest-sources.json"):
                    data = json.loads(raw)
                    bucket["activities"] = [row.get("name") for row in data.get("activities", [])
                                             if isinstance(row, dict) and _time(row.get("start"))
                                             and _time(row.get("end")) and _time(row["start"]) <= end < _time(row["end"])]
            except (OSError, ValueError, TypeError, AttributeError):
                item["state"] = "unavailable"
    paths = {path.relative_to(root).as_posix() for path in (root / "var/state/host").glob("*.log")
             if _kind(path.name)}
    paths.update(relative for relative in audited_paths if _kind(Path(relative).name))
    selected = []
    for relative in sorted(paths):
        kind = _kind(Path(relative).name)
        if kind[0] not in phases:
            continue
        try:
            at = datetime.strptime(STAMP.fullmatch(Path(relative).name)["stamp"], "%Y%m%d-%H%M%S").replace(tzinfo=LOCAL).astimezone(UTC)
        except ValueError:
            continue
        if relative in audited_paths or start <= at < end:
            selected.append((relative, kind))
    for relative, (phase, stage, kind) in selected[-MAX_LOGS:]:
        # Never reintroduce an audited file whose bytes failed verification.
        listed = next((f for f in phases[phase]["files"] if f["path"] == relative), None)
        if listed and listed["state"] != "verified":
            continue
        try:
            raw = audited_paths.get(relative)
            if raw is None:
                raw = _read(root, relative)
            log = _log(raw, relative, phase, stage, kind, start, end, relative in audited_paths)
            if log["entries"] or log["complete"]:
                phases[phase]["logs"].append(log)
        except (OSError, ValueError, TypeError):
            continue
    try:
        index = _json(root, "var/data/resource/item_index.json")
        names = {key: row["name"] for key, row in index.items() if isinstance(row, dict) and isinstance(row.get("name"), str)}
    except (OSError, ValueError, TypeError):
        names = {}
    for phase, directory, pattern in (("farming", "var/state/planner", "launcher-*.json"),
                                       ("annihilation", "var/state/planner", "annihilation-launcher-*.json")):
        if phase not in phases:
            continue
        terminal = next((p for p in run.get("phases", []) if p["phase"] == phase), None)
        plan_end = _time(terminal.get("recorded_at")) if terminal else end
        data = _plan(root, directory, pattern, start, min(plan_end or end, end))
        if data:
            try:
                if phase == "farming":
                    phases[phase]["plan"] = _farming_plan(data, names)
                else:
                    state = data.get("evidence", {}).get("state") or {}
                    phases[phase]["plan"] = {"decision": data.get("decision"), "reason": data.get("reason"),
                                              "due_at": data.get("due_at"), "week": data.get("week_start_game_day"),
                                              "current": _number(state.get("current")), "total": _number(state.get("total"))}
            except (ValueError, TypeError, AttributeError):
                pass
    return {"phases": phases, "logs_truncated": len(selected) > MAX_LOGS}
