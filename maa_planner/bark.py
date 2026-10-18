"""Optional, bounded Bark delivery after systemd stops a scheduled service."""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .dashboard import RUN_ID, read_run
from .skland_attendance import GAMES, read_attendance

LABELS = {"morning": "早晨托管", "evening": "晚间托管", "attendance": "森空岛签到"}
GAME_NAMES = {"arknights": "明日方舟", "endfield": "终末地"}
RESULTS = {"success": "正常结束", "timeout": "执行超时", "signal": "被信号中止",
           "exit-code": "执行失败", "core-dump": "进程崩溃", "resources": "资源不足",
           "start-limit-hit": "启动次数受限", "oom-kill": "内存不足被终止"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def send_bark(url: str, title: str, body: str) -> bool:
    """Never print device URLs, response bodies, or exception messages."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
                or not parsed.path.strip("/")):
            raise ValueError("invalid Bark URL")
        if len(body.encode()) > 3000:
            body = body.encode()[:2997].decode("utf-8", errors="ignore") + "…"
        payload = json.dumps({"title": title, "body": body, "group": "ZOOTd"}, ensure_ascii=False).encode()
        request = Request(url, data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        with build_opener(NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(8193)
            if response.status != 200 or len(raw) > 8192:
                raise ValueError("invalid Bark response")
        result = json.loads(raw)
        if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 200:
            raise ValueError("Bark rejected notification")
        print("[zootd-bark] 推送已发送。")
        return True
    except Exception as exc:
        print(f"[zootd-bark] 推送失败（{type(exc).__name__}）。")
        return False


def daily_result(root: Path, invocation: str) -> tuple[bool, str]:
    # Choose the newest directory, then validate its chain and invocation. Never
    # search backwards for a successful run, nor trust latest-run.json's status.
    runs = root / "var/state/supervisor/runs"
    candidates = sorted((p.name for p in runs.iterdir() if RUN_ID.fullmatch(p.name)), reverse=True) if runs.exists() else []
    if not candidates:
        return False, "本次完整运行证据缺失，结果未确认。"
    run = read_run(root, candidates[0])
    if run.get("service_invocation_id") != invocation or run.get("mode") != "full":
        return False, "缺少与本次执行匹配的完整运行证据，结果未确认。"
    if run["status"] != "success":
        return False, "本次完整运行验收未通过或未结束，请查看本机面板。"
    return True, "整轮日常、刷图、普通奖励及清理已通过验收。"


def attendance_result(root: Path, invocation: str) -> tuple[bool, str]:
    view = read_attendance(root)
    record = view["record"]
    if not record or record.get("service_invocation_id") != invocation:
        return False, "本次签到记录缺失或无法校验，结果未确认。"
    lines = []
    for game in GAMES:
        entry = record["games"][game]
        status = entry["status"]
        if status == "succeeded":
            rewards = "、".join(f"{r['name']} ×{r['count']}" for r in entry["rewards"])
            detail = f"签到成功：{rewards}"
        elif status == "already_signed":
            detail = "今日已签到"
        elif status == "unbound":
            detail = "未绑定官服角色"
        elif entry["error"]:
            detail = entry["error"]["message"]
        else:
            detail = "结果未确认"
        lines.append(f"{GAME_NAMES[game]}：{detail}")
    return record["status"] == "ok", "\n".join(lines)


def notification(root: Path, kind: str, env: dict) -> tuple[str, str]:
    source = env.get("ZOOTD_BARK_SOURCE", "").strip() or socket.gethostname()
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    if kind == "test":
        return "ZOOTd 推送测试", f"[{source}] Bark 已接入早晚托管与森空岛签到结果推送。\n{timestamp}（北京时间）"
    invocation = env.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ValueError("service invocation required")
    try:
        accepted, detail = (attendance_result if kind == "attendance" else daily_result)(root, invocation)
    except (OSError, ValueError, TypeError, KeyError):
        accepted, detail = False, "本次结果证据无法读取，结果未确认。"
    result = env.get("SERVICE_RESULT", "")
    succeeded = result == "success" and accepted
    status = "成功" if succeeded else "异常"
    service_detail = RESULTS.get(result, "服务结果未知")
    if kind == "morning" and result != "success":
        detail += "\n早晨服务未成功时，不会触发自动签到。"
    return f"ZOOTd {LABELS[kind]}{status}", f"[{source}] {service_detail}\n{detail}\n{timestamp}（北京时间）"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("kind", choices=(*LABELS, "test"))
    args = parser.parse_args(argv)
    url = os.environ.get("ZOOTD_BARK_URL", "").strip()
    if not url:
        print("[zootd-bark] 未配置 Bark，跳过推送。")
        return 0
    try:
        title, body = notification(args.project_root.resolve(), args.kind, os.environ)
    except Exception as exc:
        print(f"[zootd-bark] 无法读取本次结果（{type(exc).__name__}）。")
        return 1
    return 0 if send_bark(url, title, body) else 1


if __name__ == "__main__":
    raise SystemExit(main())
