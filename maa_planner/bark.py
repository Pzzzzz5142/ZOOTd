"""Optional, bounded Bark delivery for scheduled results and recovery lifecycle."""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .dashboard import RUN_ID, read_run
from .skland_attendance import GAMES, read_attendance
from .supervisor import SupervisorError, load_run_events

LABELS = {"morning": "早晨托管", "evening": "晚间托管", "attendance": "森空岛签到"}
GAME_NAMES = {"arknights": "明日方舟", "endfield": "终末地"}
RESULTS = {"success": "正常结束", "timeout": "执行超时", "signal": "被信号中止",
           "exit-code": "执行失败", "core-dump": "进程崩溃", "resources": "资源不足",
           "start-limit-hit": "启动次数受限", "oom-kill": "内存不足被终止"}
BLOCKERS = {"manual-login": "需要手动登录", "captcha-or-terms": "需要验证码或人工确认条款",
            "six-star-recruitment": "需要人工确认六星公招", "unsupported-client": "客户端无法安全更新",
            "proxy-unavailable": "没有可用代理", "stage-closed": "关卡不可用",
            "privileged-host-change": "需要人工处理宿主权限", "destructive-game-data": "操作会破坏游戏数据",
            "hard-policy": "需要人工处理授权边界", "time-window-expired": "恢复时间窗口已耗尽"}


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


def _message_body(text: str, env: Mapping[str, str]) -> str:
    source = env.get("ZOOTD_BARK_SOURCE", socket.gethostname()).strip()
    prefix = f"[{source}] " if source else ""
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    return f"{prefix}{text}\n{timestamp}（北京时间）"


def notify_recovery(
    failed_run_id: str, slot: str, status: str, *,
    successful_run_id: str | None = None, scope_blocker: str = "none",
) -> bool:
    """Project only controller state and validated run IDs, never agent prose."""
    url = os.environ.get("ZOOTD_BARK_URL", "").strip()
    if not url:
        return False
    try:
        if not RUN_ID.fullmatch(failed_run_id):
            raise ValueError("invalid failed run identity")
        if status == "started":
            title = "ZOOTd 恢复 agent 进场"
            text = f"{LABELS.get(slot, '手动运行')}出现故障，开始诊断与恢复。"
        else:
            label, text = {
                "recovered": ("成功", "新一轮完整运行已通过独立验收。"),
                "scope-blocked": ("需人工处理", BLOCKERS.get(scope_blocker, "恢复受阻，需要人工处理") + "。"),
                "failed": ("失败", "恢复未通过验收，请查看恢复审计。"),
                "interrupted": ("已中断", "恢复被中断，结果未确认。"),
            }.get(status, ("结果未确认", "请查看恢复审计。"))
            title = f"ZOOTd 恢复 agent 退出（{label}）"
        text += f"\n失败运行：{failed_run_id}"
        if status == "recovered":
            if not isinstance(successful_run_id, str) or not RUN_ID.fullmatch(successful_run_id):
                raise ValueError("invalid successful run identity")
            text += f"\n成功运行：{successful_run_id}"
        return send_bark(url, title, _message_body(text, os.environ))
    except Exception as exc:
        print(f"[zootd-bark] 恢复状态推送失败（{type(exc).__name__}）。")
        return False


def _compact(value: str, budget: int) -> str:
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    if len(text.encode()) <= budget:
        return text
    return text.encode()[:budget - 3].decode("utf-8", errors="ignore") + "…"


def _recovery_result(root: Path, run: dict, events: list, invocation: str) -> tuple[bool, str] | None:
    """Follow only the newest run's exact parent; never hunt for an old success."""
    context = events[0]["payload"].get("recovery_context")
    parent_id = context.get("parent_run_id") if isinstance(context, dict) else run["run_id"]
    if not isinstance(parent_id, str) or not RUN_ID.fullmatch(parent_id):
        return None
    parent = read_run(root, parent_id) if context else run
    if (parent.get("service_invocation_id") != invocation or parent.get("mode") != "full"
            or parent["status"] != "failed"):
        return None
    parent_events = load_run_events(root, parent_id) if context else events
    starts = [e for e in parent_events if e["event_type"] == "recovery-started"]
    if len(starts) != 1:
        return None
    attempt = starts[0]["payload"]["recovery_attempt"]
    if (not isinstance(attempt, dict) or not isinstance(attempt.get("attempt_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", attempt["attempt_id"])
            or attempt.get("slot") not in {"morning", "evening", "manual"}):
        raise ValueError("invalid recovery attempt")
    if context and (context != {"parent_run_id": parent_id, **attempt}
                    or datetime.fromisoformat(events[0]["recorded_at"])
                    <= datetime.fromisoformat(starts[0]["recorded_at"])):
        raise ValueError("recovery child identity mismatch")
    finishes = [e for e in parent_events if e["event_type"] == "recovery-finished"]
    if len(finishes) != 1 or parent_events[-1] != finishes[0]:
        return False, "诊断：agent 尚无终态报告。\n修复：结果未确认，请查看本次恢复审计。"
    final = finishes[0]
    payload = final["payload"]
    if payload.get("schema_version") != 1:
        return False, "诊断：恢复审计版本不受支持。\n修复：结果未确认，请检查本次恢复审计。"
    controller = payload.get("controller_verification", {})
    agent = payload.get("agent", {})
    report = agent.get("report", {})
    if report and report.get("schema_version") != 2:
        return False, "诊断：agent 报告版本不受支持。\n修复：结果未确认，请检查本次恢复审计。"
    summary = report.get("summary")
    actions = report.get("actions_taken")
    diagnosis = _compact(summary, 720) if isinstance(summary, str) and summary.strip() else "agent 未提交有效诊断报告"
    repairs = list(dict.fromkeys(a for a in actions if isinstance(a, str) and a.strip())) if isinstance(actions, list) else []
    shown = repairs[:6]
    fix = "；".join(_compact(a, 900 // len(shown)) for a in shown) if shown else "未确认，请查看恢复审计"
    if len(repairs) > len(shown):
        fix += f"（另 {len(repairs) - len(shown)} 项见审计）"
    accepted = False
    if payload.get("status") == "recovered" and controller.get("status") == "success":
        proof = controller.get("run", {})
        accepted = bool(context and run["status"] == "success"
                        and proof.get("successful_run_id") == run["run_id"]
                        and proof.get("event_sha256") == events[-1]["event_sha256"])
        verdict = (f"新一轮 {len(run['phases'])} 阶段及清理通过独立验收；{run['run_id']}"
                   if accepted else "恢复成功声明与本次运行证据不符，结果未确认")
    elif payload.get("status") == "scope-blocked" and controller.get("status") == "scope-accepted":
        verdict = BLOCKERS.get(payload.get("scope_blocker", report.get("scope_blocker")), "恢复受阻，需要人工处理")
    else:
        reason = controller.get("error") or agent.get("error")
        verdict = "恢复未通过验收" + (f"；{_compact(reason, 360)}" if isinstance(reason, str) else "")
    repair = controller.get("code_repair", {})
    code_detail = ""
    if repair.get("status") in {"pr-opened", "pr-failed"}:
        publication = repair.get("pull_request_verification") or {}
        reference = (repair.get("pull_request_url") if publication.get("status") == "verified"
                     else f"提交 {repair.get('commit', '')[:12]}；PR 未发布或未核验")
        applied = "本轮已应用" if repair.get("applied_to_runtime") else "本轮未应用"
        code_detail = f"\n代码修复：{_compact(reference, 300)}（{applied}）"
    audit = f"var/state/supervisor/runs/{parent_id}/events/{final['sequence']:04d}-recovery-finished.json"
    return accepted, f"诊断（agent）：{diagnosis}\n修复：{fix}{code_detail}\n验收：{verdict}\n审计：{audit}"


def daily_result(root: Path, invocation: str) -> tuple[bool, str]:
    # Choose the newest directory, then validate its chain and invocation. Never
    # search backwards for a successful run, nor trust latest-run.json's status.
    runs = root / "var/state/supervisor/runs"
    candidates = sorted((p.name for p in runs.iterdir() if RUN_ID.fullmatch(p.name)), reverse=True) if runs.exists() else []
    if not candidates:
        return False, "本次完整运行证据缺失，结果未确认。"
    run = read_run(root, candidates[0])
    if run["status"] != "invalid" and run.get("mode") == "full":
        events = load_run_events(root, candidates[0])
        recovery = _recovery_result(root, run, events, invocation)
        if recovery is not None:
            return recovery
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
    if kind == "test":
        return "ZOOTd 推送测试", _message_body("Bark 已接入早晚托管、森空岛签到与恢复 agent 进出状态推送。", env)
    invocation = env.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ValueError("service invocation required")
    try:
        accepted, detail = (attendance_result if kind == "attendance" else daily_result)(root, invocation)
    except (OSError, SupervisorError, ValueError, TypeError, KeyError, AttributeError):
        accepted, detail = False, "本次结果证据无法读取，结果未确认。"
    result = env.get("SERVICE_RESULT", "")
    succeeded = result == "success" and accepted
    status = "成功" if succeeded else "异常"
    service_detail = RESULTS.get(result, "服务结果未知")
    if kind == "morning" and result != "success":
        detail += "\n早晨服务未成功时，不会触发自动签到。"
    return f"ZOOTd {LABELS[kind]}{status}", _message_body(f"{service_detail}\n{detail}", env)


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
