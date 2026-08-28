from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .supervisor import (
    ACCEPTED_RESULTS,
    SupervisorError,
    load_run_events,
    record_recovery_report,
    record_recovery_started,
)
from .util import canonical_json, load_json, parse_iso_datetime, sha256_bytes


class RecoveryError(RuntimeError):
    pass


_RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}[.][0-9]{6}Z-[0-9a-f]{8}")
_CLASSIFICATIONS = frozenset(
    {
        "environment",
        "network",
        "waydroid",
        "game-client",
        "runtime",
        "configuration",
        "game-state",
        "scope",
        "unknown",
    }
)
_BLOCKERS = frozenset(
    {
        "none",
        "manual-login",
        "captcha-or-terms",
        "six-star-recruitment",
        "client-package-update",
        "unsupported-client",
        "proxy-unavailable",
        "stage-closed",
        "privileged-host-change",
        "destructive-game-data",
        "hard-policy",
        "time-window-expired",
        "unknown",
    }
)
_MAX_EXTERNAL_OUTPUT = 256 * 1024


@dataclass(frozen=True)
class RecoveryOutcome:
    failed_run_id: str
    status: str
    event_path: Path
    summary: str
    successful_run_id: str | None = None
    scope_blocker: str = "none"


def _bounded_string(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise RecoveryError(f"recovery report field {field} is invalid")
    return value


def _bounded_strings(value: object, field: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise RecoveryError(f"recovery report field {field} is not a bounded array")
    return [
        _bounded_string(item, f"{field}[]", maximum=2000)
        for item in value
    ]


def validate_recovery_report(value: object, *, failed_run_id: str) -> dict[str, Any]:
    required = {
        "schema_version",
        "failed_run_id",
        "status",
        "classification",
        "summary",
        "actions_taken",
        "verification",
        "scope_blocker",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RecoveryError("recovery report does not match the required object shape")
    if value.get("schema_version") != 1 or value.get("failed_run_id") != failed_run_id:
        raise RecoveryError("recovery report changed its schema or failed run id")
    status = value.get("status")
    if status not in {"recovered", "scope-blocked", "failed"}:
        raise RecoveryError("recovery report status is invalid")
    classification = value.get("classification")
    if classification not in _CLASSIFICATIONS:
        raise RecoveryError("recovery report classification is invalid")
    summary = _bounded_string(value.get("summary"), "summary", maximum=8000)
    actions = _bounded_strings(value.get("actions_taken"), "actions_taken", maximum=64)
    if not actions:
        raise RecoveryError("recovery report must describe at least one action")
    blocker = value.get("scope_blocker")
    if blocker not in _BLOCKERS:
        raise RecoveryError("recovery report scope blocker is invalid")

    verification = value.get("verification")
    verification_fields = {
        "command",
        "exit_status",
        "successful_run_id",
        "audit_path",
    }
    if not isinstance(verification, dict) or set(verification) != verification_fields:
        raise RecoveryError("recovery verification object is invalid")
    command = verification.get("command")
    if not isinstance(command, str) or len(command) > 2000 or "\x00" in command:
        raise RecoveryError("recovery verification command is invalid")
    exit_status = verification.get("exit_status")
    if exit_status is not None and (
        not isinstance(exit_status, int)
        or isinstance(exit_status, bool)
        or not 0 <= exit_status <= 255
    ):
        raise RecoveryError("recovery verification exit status is invalid")
    successful_run_id = verification.get("successful_run_id")
    if successful_run_id is not None and (
        not isinstance(successful_run_id, str)
        or not _RUN_ID_RE.fullmatch(successful_run_id)
    ):
        raise RecoveryError("recovery verification run id is invalid")
    audit_path = verification.get("audit_path")
    if audit_path is not None and (
        not isinstance(audit_path, str)
        or not audit_path
        or len(audit_path) > 2000
        or "\x00" in audit_path
    ):
        raise RecoveryError("recovery verification audit path is invalid")

    if status == "recovered":
        if (
            blocker != "none"
            or exit_status != 0
            or successful_run_id is None
            or audit_path is None
            or not command
        ):
            raise RecoveryError("a recovered report lacks complete success evidence")
    elif status == "scope-blocked":
        if (
            blocker in {"none", "unknown"}
            or classification != "scope"
            or successful_run_id is not None
        ):
            raise RecoveryError("a scope-blocked report lacks a concrete blocker")
    elif blocker not in {"none", "unknown"}:
        raise RecoveryError("a failed report must not masquerade as a scope decision")

    return {
        "schema_version": 1,
        "failed_run_id": failed_run_id,
        "status": status,
        "classification": classification,
        "summary": summary,
        "actions_taken": actions,
        "verification": {
            "command": command,
            "exit_status": exit_status,
            "successful_run_id": successful_run_id,
            "audit_path": audit_path,
        },
        "scope_blocker": blocker,
    }


def _failed_run(events: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    start = events[0].get("payload")
    if not isinstance(start, dict) or start.get("mode") != "full":
        raise RecoveryError("only a full supervisor run can enter operational recovery")
    finished = [event for event in events if event.get("event_type") == "run-finished"]
    if len(finished) != 1:
        raise RecoveryError("failed run does not have exactly one terminal event")
    final_payload = finished[0].get("payload")
    if not isinstance(final_payload, dict) or final_payload.get("status") != "failed":
        raise RecoveryError("operational recovery refuses a non-failed run")
    if any(
        event.get("event_type") in {"recovery-started", "recovery-finished"}
        for event in events
    ):
        raise RecoveryError("this failed run already has a recovery attempt")
    return start, finished[0]


def _retry_argv(slot: str) -> list[str]:
    if slot == "pre-reset":
        return ["./bin/maa-host", "run", "--pre-reset-slot"]
    if slot == "post-reset":
        return ["./bin/maa-host", "run", "--post-reset-slot"]
    if slot == "manual":
        return ["./bin/maa-host", "run"]
    raise RecoveryError(f"invalid recovery slot: {slot}")


def _recorded_at(event: Mapping[str, Any]) -> datetime:
    value = event.get("recorded_at")
    if not isinstance(value, str):
        raise RecoveryError("supervisor event has no valid timestamp")
    try:
        return parse_iso_datetime(value)
    except ValueError as exc:
        raise RecoveryError("supervisor event timestamp is invalid") from exc


def _incident_screenshots(
    root: Path, *, started: datetime, finished: datetime
) -> list[str]:
    interface_dir = root / "var/state/debug/interface"
    if not interface_dir.is_dir() or interface_dir.is_symlink():
        return []
    lower = started.timestamp() - 120
    upper = finished.timestamp() + 120
    candidates: list[tuple[float, Path]] = []
    for path in interface_dir.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if lower <= modified <= upper:
            candidates.append((modified, path))
    candidates.sort(key=lambda item: item[0])
    return [
        path.relative_to(root).as_posix()
        for _, path in candidates[-4:]
    ]


def _incident_evidence(
    root: Path,
    failed_run_id: str,
    events: Sequence[Mapping[str, Any]],
    *,
    slot: str,
) -> dict[str, Any]:
    start, finished = _failed_run(events)
    scope_path = root / "docs/llm-recovery-scope.md"
    try:
        scope_bytes = scope_path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"cannot read recovery scope: {scope_path}") from exc
    phase_results = [
        event.get("payload")
        for event in events
        if event.get("event_type") == "phase-finished"
        and isinstance(event.get("payload"), dict)
    ]
    evidence_files: list[Mapping[str, Any]] = []
    seen_paths: set[str] = set()
    for phase in phase_results:
        for descriptor in phase.get("evidence_files", []):
            if (
                isinstance(descriptor, dict)
                and isinstance(descriptor.get("path"), str)
                and descriptor["path"] not in seen_paths
            ):
                evidence_files.append(descriptor)
                seen_paths.add(descriptor["path"])
    retry = _retry_argv(slot)
    recovery_head, recovery_dirty = _git_identity(root)
    if recovery_dirty:
        raise RecoveryError(
            "operational recovery requires a clean controller working tree"
        )
    return {
        "schema_version": 1,
        "kind": "operational-recovery",
        "failed_run_id": failed_run_id,
        "slot": slot,
        "retry_argv": retry,
        "retry_command": shlex.join(retry),
        "failed_audit_path": (
            f"var/state/supervisor/runs/{failed_run_id}/events"
        ),
        "start": start,
        "run_finished": finished.get("payload"),
        "failed_chain_head_sha256": finished.get("event_sha256"),
        "phase_results": phase_results,
        "evidence_files": evidence_files,
        "screenshot_paths": _incident_screenshots(
            root,
            started=_recorded_at(events[0]),
            finished=_recorded_at(finished),
        ),
        "additional_evidence_locations": {
            "maa_core_log": "var/state/debug/asst.log",
            "host_logs": "var/state/host",
            "waydroid_anr_directory": str(
                Path.home() / ".local/share/waydroid/data/anr"
            ),
            "journal_units": [
                "maa-waydroid.service",
                "waydroid-container.service",
            ],
        },
        "scope": {
            "path": "docs/llm-recovery-scope.md",
            "sha256": sha256_bytes(scope_bytes),
            "version": 2,
        },
        "controller_repository": {
            "head": recovery_head,
            "dirty": False,
        },
        "reentrancy_policy": {
            "all_managed_stages": True,
            "daily": True,
            "whole_run_replay": True,
        },
        "controller_success_requirement": (
            "A different full run must independently validate every expected phase, "
            "a zero process status, the recovery-start clean Git HEAD, and a successful final audit."
        ),
    }


def _run_adapter(
    command: Sequence[str],
    evidence: Mapping[str, Any],
    *,
    root: Path,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
) -> object:
    if not command:
        raise RecoveryError("recovery command is empty")
    child_env = os.environ.copy()
    child_env["MAA_CODEX_RECOVERY_TIMEOUT_SECONDS"] = str(timeout_seconds)
    try:
        completed = runner(
            tuple(command),
            input=canonical_json(evidence),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds + 60,
            check=False,
            cwd=root,
            env=child_env,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"recovery adapter invocation failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-4000:].strip()
        suffix = f": {detail}" if detail else ""
        raise RecoveryError(
            f"recovery adapter exited with status {completed.returncode}{suffix}"
        )
    if len(completed.stdout) > _MAX_EXTERNAL_OUTPUT:
        raise RecoveryError("recovery adapter output is too large")
    try:
        return json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery adapter did not return one JSON object") from exc


def _git_identity(root: Path) -> tuple[str, bool]:
    head_result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    status_result = subprocess.run(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if head_result.returncode != 0 or status_result.returncode != 0:
        raise RecoveryError("controller could not verify the recovery Git identity")
    try:
        head = head_result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RecoveryError("controller observed an invalid Git HEAD") from exc
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise RecoveryError("controller observed an invalid Git HEAD")
    return head, bool(status_result.stdout)


def _resolve_claimed_audit(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    candidate = path if path.is_absolute() else root / path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RecoveryError("agent's claimed success audit is invalid") from exc
    return resolved


def verify_recovered_run(
    root: Path,
    failed_run_id: str,
    report: Mapping[str, Any],
    *,
    expected_command: str,
    expected_git_head: str,
) -> dict[str, Any]:
    verification = report.get("verification")
    if not isinstance(verification, dict):
        raise RecoveryError("agent supplied no verification object")
    if verification.get("command") != expected_command:
        raise RecoveryError("agent did not verify with the supplied full retry command")
    if verification.get("exit_status") != 0:
        raise RecoveryError("agent's full retry command did not exit zero")
    successful_run_id = verification.get("successful_run_id")
    if (
        not isinstance(successful_run_id, str)
        or successful_run_id == failed_run_id
        or not _RUN_ID_RE.fullmatch(successful_run_id)
    ):
        raise RecoveryError("agent did not identify a different successful run")

    try:
        latest = load_json(root / "var/state/supervisor/latest-run.json")
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("controller cannot read the latest-run index") from exc
    if (
        not isinstance(latest, dict)
        or latest.get("run_id") != successful_run_id
        or latest.get("status") != "success"
    ):
        raise RecoveryError("latest-run does not independently confirm the claimed success")

    failed_events = load_run_events(root, failed_run_id)
    success_events = load_run_events(root, successful_run_id)
    if success_events[-1].get("event_type") != "run-finished":
        raise RecoveryError("successful run has no final run-finished event")
    start = success_events[0].get("payload")
    if not isinstance(start, dict) or start.get("mode") != "full":
        raise RecoveryError("claimed recovery audit is not a full run")
    expected = start.get("expected_phases")
    if (
        not isinstance(expected, list)
        or not expected
        or len(set(expected)) != len(expected)
        or not all(isinstance(phase, str) for phase in expected)
    ):
        raise RecoveryError("successful run expected-phase contract is invalid")
    phases: dict[str, Mapping[str, Any]] = {}
    for event in success_events:
        if event.get("event_type") != "phase-finished":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("phase"), str):
            raise RecoveryError("successful run contains an invalid phase event")
        if payload["phase"] in phases:
            raise RecoveryError("successful run contains duplicate phase results")
        phases[payload["phase"]] = payload
    if set(phases) != set(expected) or any(
        phases[phase].get("result") not in ACCEPTED_RESULTS for phase in expected
    ):
        raise RecoveryError("not every full-run phase has an accepted terminal result")

    final_payload = success_events[-1].get("payload")
    if (
        not isinstance(final_payload, dict)
        or final_payload.get("status") != "success"
        or final_payload.get("process_status") != 0
        or final_payload.get("missing_phases") != []
        or final_payload.get("unacceptable_phases") != []
    ):
        raise RecoveryError("successful run's final deterministic verdict is invalid")
    if latest.get("event_sha256") != success_events[-1].get("event_sha256"):
        raise RecoveryError("latest-run index does not identify the successful chain head")

    success_repository = start.get("repository")
    if (
        not isinstance(success_repository, dict)
        or success_repository.get("head") != expected_git_head
        or success_repository.get("dirty") is not False
    ):
        raise RecoveryError("recovery did not preserve its clean recovery-start Git HEAD")
    current_head, current_dirty = _git_identity(root)
    if current_dirty or current_head != expected_git_head:
        raise RecoveryError("repository changed during unattended recovery")

    actual_audit = (
        root
        / "var/state/supervisor/runs"
        / successful_run_id
        / "events"
        / f"{len(success_events) - 1:04d}-run-finished.json"
    ).resolve(strict=True)
    claimed_audit = _resolve_claimed_audit(root, str(verification.get("audit_path")))
    if claimed_audit != actual_audit:
        raise RecoveryError("agent's claimed audit path is not the successful final event")

    return {
        "status": "success",
        "successful_run_id": successful_run_id,
        "audit_path": actual_audit.relative_to(root).as_posix(),
        "git_head": current_head,
        "accepted_phases": expected,
        "event_sha256": success_events[-1].get("event_sha256"),
    }


def recover_failed_run(
    root: Path,
    failed_run_id: str,
    *,
    slot: str,
    command: Sequence[str],
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> RecoveryOutcome:
    root = root.resolve()
    try:
        events = load_run_events(root, failed_run_id)
        evidence = _incident_evidence(root, failed_run_id, events, slot=slot)
    except (OSError, SupervisorError, ValueError) as exc:
        raise RecoveryError(f"cannot build failed-run recovery evidence: {exc}") from exc

    record_recovery_started(
        root,
        failed_run_id,
        {
            "schema_version": 1,
            "policy": "unsandboxed-scoped-v2",
            "slot": slot,
            "scope": evidence["scope"],
            "controller_repository": evidence["controller_repository"],
            "reentrancy_policy": evidence["reentrancy_policy"],
            "retry_command": evidence["retry_command"],
            "timeout_seconds": timeout_seconds,
            "screenshot_paths": evidence["screenshot_paths"],
        },
    )

    report: dict[str, Any] | None = None
    adapter_error: str | None = None
    controller: dict[str, Any] = {"status": "not-run"}
    overall_status = "failed"
    summary = "Recovery adapter did not produce a valid terminal result."
    blocker = "none"
    successful_run_id: str | None = None
    try:
        raw_report = _run_adapter(
            command,
            evidence,
            root=root,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        report = validate_recovery_report(raw_report, failed_run_id=failed_run_id)
        summary = report["summary"]
        blocker = report["scope_blocker"]
        if report["status"] == "recovered":
            try:
                controller = verify_recovered_run(
                    root,
                    failed_run_id,
                    report,
                    expected_command=evidence["retry_command"],
                    expected_git_head=evidence["controller_repository"]["head"],
                )
            except (OSError, SupervisorError, RecoveryError) as exc:
                controller = {"status": "rejected", "error": str(exc)}
                summary = f"Agent claimed recovery, but controller rejected it: {exc}"
            else:
                overall_status = "recovered"
                successful_run_id = controller["successful_run_id"]
        elif report["status"] == "scope-blocked":
            current_head, current_dirty = _git_identity(root)
            expected_head = evidence["controller_repository"]["head"]
            if current_dirty or current_head != expected_head:
                controller = {
                    "status": "rejected",
                    "error": "repository changed before the scope decision",
                }
                summary = (
                    "Agent reported a scope blocker, but the repository changed "
                    "during unattended recovery."
                )
                overall_status = "failed"
            else:
                controller = {
                    "status": "scope-accepted",
                    "scope_blocker": blocker,
                    "git_head": current_head,
                }
                overall_status = "scope-blocked"
        else:
            overall_status = "failed"
    except RecoveryError as exc:
        adapter_error = str(exc)
        summary = adapter_error

    audit_payload: dict[str, Any] = {
        "schema_version": 1,
        "status": overall_status,
        "policy": "unsandboxed-scoped-v2",
        "scope": evidence["scope"],
        "agent": {
            "invoked": True,
            "status": report["status"] if report is not None else "error",
        },
        "controller_verification": controller,
    }
    if report is not None:
        audit_payload["agent"]["report"] = report
    if adapter_error is not None:
        audit_payload["agent"]["error"] = adapter_error
    event_path = record_recovery_report(root, failed_run_id, audit_payload)
    return RecoveryOutcome(
        failed_run_id=failed_run_id,
        status=overall_status,
        event_path=event_path,
        summary=summary,
        successful_run_id=successful_run_id,
        scope_blocker=blocker,
    )
