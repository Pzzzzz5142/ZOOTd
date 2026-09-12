from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .supervisor import (
    ACCEPTED_RESULTS,
    SupervisorError,
    load_run_events,
    record_recovery_report,
    record_recovery_started,
    runtime_snapshot,
)
from .util import canonical_json, load_json, parse_iso_datetime, sha256_bytes


class RecoveryError(RuntimeError):
    pass


PullRequestVerifier = Callable[[Path, str, str, str, str], Mapping[str, Any]]


_RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}[.][0-9]{6}Z-[0-9a-f]{8}")
_GIT_HASH_RE = re.compile(r"[0-9a-f]{40,64}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_APPLIED_REPAIR_PATHS = frozenset(
    {
        "config/tasks/annihilation.toml",
        "config/tasks/award-only.toml",
        "config/tasks/daily.toml",
        "config/tasks/depot.toml",
        "config/tasks/proxy-preflight.toml",
        "config/tasks/sanity-fight.toml",
        "config/tasks/verify-fight.toml",
    }
)
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
_MAX_EXTERNAL_OUTPUT = 128 * 1024


@dataclass(frozen=True)
class RecoveryOutcome:
    failed_run_id: str
    status: str
    event_path: Path
    summary: str
    successful_run_id: str | None = None
    scope_blocker: str = "none"
    repair_branch: str | None = None
    pull_request_url: str | None = None


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


def _optional_bounded_string(
    value: object, field: str, *, maximum: int
) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, field, maximum=maximum)


def _valid_branch_name(value: str) -> bool:
    return bool(
        _BRANCH_RE.fullmatch(value)
        and ".." not in value
        and "//" not in value
        and "@{" not in value
        and not value.endswith(("/", ".", ".lock"))
    )


def _validate_code_repair(value: object) -> dict[str, Any]:
    fields = {
        "status",
        "base_head",
        "branch",
        "commit",
        "applied_to_runtime",
        "pull_request_url",
        "pull_request_error",
        "validation_commands",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RecoveryError("recovery code_repair object is invalid")
    status = value.get("status")
    if status not in {"not-needed", "pr-opened", "pr-failed"}:
        raise RecoveryError("recovery code_repair status is invalid")
    base_head = value.get("base_head")
    if not isinstance(base_head, str) or not _GIT_HASH_RE.fullmatch(base_head):
        raise RecoveryError("recovery code_repair base head is invalid")
    branch = _optional_bounded_string(value.get("branch"), "code_repair.branch", maximum=200)
    if branch is not None and not _valid_branch_name(branch):
        raise RecoveryError("recovery code_repair branch is invalid")
    commit = value.get("commit")
    if commit is not None and (
        not isinstance(commit, str) or not _GIT_HASH_RE.fullmatch(commit)
    ):
        raise RecoveryError("recovery code_repair commit is invalid")
    applied = value.get("applied_to_runtime")
    if not isinstance(applied, bool):
        raise RecoveryError("recovery code_repair applied flag is invalid")
    pull_request_url = _optional_bounded_string(
        value.get("pull_request_url"), "code_repair.pull_request_url", maximum=2000
    )
    if pull_request_url is not None and not pull_request_url.startswith("https://"):
        raise RecoveryError("recovery pull request URL must use HTTPS")
    pull_request_error = _optional_bounded_string(
        value.get("pull_request_error"), "code_repair.pull_request_error", maximum=4000
    )
    commands = _bounded_strings(
        value.get("validation_commands"), "code_repair.validation_commands", maximum=32
    )

    if status == "not-needed":
        if any(
            item is not None
            for item in (branch, commit, pull_request_url, pull_request_error)
        ) or applied or commands:
            raise RecoveryError("a not-needed code repair contains change metadata")
    elif branch is None or commit is None or not commands:
        raise RecoveryError("a changed code repair lacks branch, commit, or validation")
    elif status == "pr-opened" and (
        pull_request_url is None or pull_request_error is not None
    ):
        raise RecoveryError("an opened code repair lacks one unambiguous PR URL")
    elif status == "pr-failed" and (
        pull_request_url is not None or pull_request_error is None
    ):
        raise RecoveryError("a failed PR attempt lacks one concrete error")

    return {
        "status": status,
        "base_head": base_head,
        "branch": branch,
        "commit": commit,
        "applied_to_runtime": applied,
        "pull_request_url": pull_request_url,
        "pull_request_error": pull_request_error,
        "validation_commands": commands,
    }


def validate_recovery_report(value: object, *, failed_run_id: str) -> dict[str, Any]:
    required = {
        "schema_version",
        "failed_run_id",
        "status",
        "classification",
        "summary",
        "actions_taken",
        "verification",
        "code_repair",
        "scope_blocker",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RecoveryError("recovery report does not match the required object shape")
    if value.get("schema_version") != 2 or value.get("failed_run_id") != failed_run_id:
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
    code_repair = _validate_code_repair(value.get("code_repair"))

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
    if status != "recovered" and code_repair["applied_to_runtime"]:
        raise RecoveryError("an incomplete recovery cannot leave a repair applied")

    return {
        "schema_version": 2,
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
        "code_repair": code_repair,
        "scope_blocker": blocker,
    }


def _failed_run(
    events: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
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
        return ["./bin/zootd", "run", "--pre-reset-slot"]
    if slot == "post-reset":
        return ["./bin/zootd", "run", "--post-reset-slot"]
    if slot == "manual":
        return ["./bin/zootd", "run"]
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


def _phase_results(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        payload
        for event in events
        if event.get("event_type") == "phase-finished"
        and isinstance((payload := event.get("payload")), dict)
    ]


def _recent_successful_full_run(
    root: Path, failed_run_id: str, *, failed_started: datetime
) -> dict[str, Any] | None:
    runs_root = root / "var/state/supervisor/runs"
    if not runs_root.is_dir() or runs_root.is_symlink():
        return None
    candidates = sorted(
        (
            path.name
            for path in runs_root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and _RUN_ID_RE.fullmatch(path.name)
            and path.name != failed_run_id
        ),
        reverse=True,
    )
    for run_id in candidates:
        try:
            run_events = load_run_events(root, run_id)
            start = run_events[0].get("payload")
            final = run_events[-1]
            final_payload = final.get("payload")
            if _recorded_at(run_events[0]) >= failed_started:
                continue
        except (OSError, SupervisorError, RecoveryError, ValueError):
            continue
        if (
            not isinstance(start, dict)
            or start.get("mode") != "full"
            or final.get("event_type") != "run-finished"
            or not isinstance(final_payload, dict)
            or final_payload.get("status") != "success"
        ):
            continue
        return {
            "run_id": run_id,
            "started_at": run_events[0].get("recorded_at"),
            "finished_at": final.get("recorded_at"),
            "audit_path": (
                f"var/state/supervisor/runs/{run_id}/events/"
                f"{len(run_events) - 1:04d}-run-finished.json"
            ),
            "start": start,
            "run_finished": final_payload,
            "phase_results": _phase_results(run_events),
        }
    return None


def _phase_comparison(
    expected: object,
    failed: Sequence[Mapping[str, Any]],
    recent_success: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(expected, list) or not all(
        isinstance(item, str) for item in expected
    ):
        return []
    failed_by_phase = {
        item["phase"]: item
        for item in failed
        if isinstance(item.get("phase"), str)
    }
    recent_raw = recent_success.get("phase_results", []) if recent_success else []
    recent_by_phase = {
        item["phase"]: item
        for item in recent_raw
        if isinstance(item, dict) and isinstance(item.get("phase"), str)
    }
    result: list[dict[str, Any]] = []
    for phase in expected:
        previous = recent_by_phase.get(phase)
        current = failed_by_phase.get(phase)
        previous_summary = (
            {"result": previous.get("result"), "outcome": previous.get("outcome")}
            if previous is not None
            else None
        )
        current_summary = (
            {"result": current.get("result"), "outcome": current.get("outcome")}
            if current is not None
            else None
        )
        result.append(
            {
                "phase": phase,
                "recent_success": previous_summary,
                "failed_run": current_summary,
                "changed": previous_summary != current_summary,
            }
        )
    return result


def _runtime_identity(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("available") is not True:
        return {"available": False}
    receipt = snapshot.get("receipt")
    if not isinstance(receipt, dict):
        return {"available": False}
    core = receipt.get("core") if isinstance(receipt.get("core"), dict) else {}
    resource = (
        receipt.get("resource") if isinstance(receipt.get("resource"), dict) else {}
    )
    transition = (
        receipt.get("transition")
        if isinstance(receipt.get("transition"), dict)
        else {}
    )
    generation = (
        receipt.get("generation")
        if isinstance(receipt.get("generation"), dict)
        else {}
    )
    return {
        "available": True,
        "status": receipt.get("status"),
        "checked_at": receipt.get("checked_at"),
        "activated_at": transition.get("activated_at"),
        "core_version": core.get("active_version"),
        "resource_commit": resource.get("active_commit"),
        "runtime_tree_sha256": generation.get("runtime_tree_sha256"),
        "managed_config_sha256": generation.get("managed_config_sha256"),
        "core_library_sha256": generation.get("core_library_sha256"),
        "receipt_sha256": snapshot.get("receipt_sha256"),
    }


def _runtime_comparison(
    *, failed_start: object, recovery_start: object, recent_success: object
) -> dict[str, Any]:
    identities = {
        "recent_success": _runtime_identity(recent_success),
        "failed_run_start": _runtime_identity(failed_start),
        "recovery_start": _runtime_identity(recovery_start),
    }
    changed_fields: list[str] = []
    # Receipt timestamps and status can change after a validation-only update.
    # Keep them in each identity for diagnosis, but only flag substantive
    # runtime/configuration identity changes as possible regression boundaries.
    identity_keys = (
        "core_version",
        "resource_commit",
        "runtime_tree_sha256",
        "managed_config_sha256",
        "core_library_sha256",
    )
    for key in identity_keys:
        values = [
            identity.get(key)
            for identity in identities.values()
            if identity.get("available") is True
        ]
        if len(values) >= 2 and len({json.dumps(item, sort_keys=True) for item in values}) > 1:
            changed_fields.append(key)
    return {"identities": identities, "changed_fields": changed_fields}


def _retained_previous_runtime(
    root: Path, recovery_runtime: Mapping[str, Any]
) -> dict[str, Any]:
    runtime = root / "var/cache/MaaRuntime.previous"
    result: dict[str, Any] = {
        "available": False,
        "path": "var/cache/MaaRuntime.previous",
    }
    if not runtime.is_dir() or runtime.is_symlink():
        result["reason"] = "retained-runtime-missing"
        return result
    transition: Mapping[str, Any] = {}
    receipt = recovery_runtime.get("receipt")
    if isinstance(receipt, dict) and isinstance(receipt.get("transition"), dict):
        transition = receipt["transition"]
    result.update(
        {
            "available": True,
            "resource_commit": transition.get("previous_resource_commit"),
        }
    )
    maa = root / "bin/zootd-maa"
    if not maa.is_file() or not os.access(maa, os.X_OK):
        result["core_version"] = transition.get("previous_core_version")
        result["version_probe"] = "unavailable"
        return result
    environment = os.environ.copy()
    environment.update(
        {
            "MAA_DATA_DIR": str(runtime),
            "MAA_CACHE_DIR": str(runtime / "cache"),
            "MAA_CONFIG_DIR": str(root / "config"),
        }
    )
    try:
        completed = subprocess.run(
            (str(maa), "version"),
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    match = (
        re.search(rb"(?m)^MaaCore[ \t]+([^\r\n ]+)", completed.stdout)
        if completed is not None and completed.returncode == 0
        else None
    )
    result["core_version"] = (
        match.group(1).decode("utf-8", errors="replace")
        if match is not None
        else transition.get("previous_core_version")
    )
    result["version_probe"] = "success" if match is not None else "failed"
    return result
def _git_text(root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace").strip() or None


def _safe_remote_url(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if hostname is None:
            return None
        netloc = hostname
        if port is not None:
            netloc = f"{netloc}:{port}"
        sanitized = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        return sanitized if len(sanitized) <= 2000 else None
    if re.fullmatch(r"git@github[.]com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw):
        return raw
    return None


def _github_pull_prefix(remote: str | None) -> str | None:
    if remote is None:
        return None
    patterns = (
        r"https://github[.]com/([^/]+)/([^/]+?)(?:[.]git)?$",
        r"git@github[.]com:([^/]+)/([^/]+?)(?:[.]git)?$",
        r"ssh://git@github[.]com/([^/]+)/([^/]+?)(?:[.]git)?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, remote)
        if match is not None:
            return f"https://github.com/{match.group(1)}/{match.group(2)}/pull/"
    return None


def _git_repository_context(root: Path, head: str) -> dict[str, Any]:
    branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch is None or not _valid_branch_name(branch):
        raise RecoveryError("operational recovery requires a named safe base branch")
    default_ref = _git_text(
        root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
    )
    default_branch = (
        default_ref.removeprefix("origin/") if default_ref is not None else branch
    )
    if not _valid_branch_name(default_branch):
        default_branch = branch
    remote = _safe_remote_url(_git_text(root, "remote", "get-url", "origin"))
    return {
        "head": head,
        "dirty": False,
        "branch": branch,
        "default_branch": default_branch,
        "remote_name": "origin" if remote is not None else None,
        "remote_url": remote,
        "pull_request_url_prefix": _github_pull_prefix(remote),
    }


def _incident_evidence(
    root: Path,
    failed_run_id: str,
    events: Sequence[Mapping[str, Any]],
    *,
    slot: str,
    attempt_id: str,
) -> dict[str, Any]:
    start, finished = _failed_run(events)
    scope_path = root / "docs/llm-recovery-scope.md"
    try:
        scope_bytes = scope_path.read_bytes()
    except OSError as exc:
        raise RecoveryError(f"cannot read recovery scope: {scope_path}") from exc
    phase_results = _phase_results(events)
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
    controller_repository = _git_repository_context(root, recovery_head)
    failed_started = _recorded_at(events[0])
    recent_success = _recent_successful_full_run(
        root, failed_run_id, failed_started=failed_started
    )
    recovery_runtime = runtime_snapshot(root)
    recent_runtime: object = None
    if recent_success is not None:
        recent_start = recent_success.get("start")
        if isinstance(recent_start, dict):
            recent_runtime = recent_start.get("runtime")
    runtime_context = {
        "failed_run_start": start.get("runtime"),
        "recovery_start": recovery_runtime,
        "retained_previous": _retained_previous_runtime(root, recovery_runtime),
        "comparison": _runtime_comparison(
            failed_start=start.get("runtime"),
            recovery_start=recovery_runtime,
            recent_success=recent_runtime,
        ),
    }
    return {
        "schema_version": 2,
        "kind": "operational-recovery",
        "failed_run_id": failed_run_id,
        "recovery_attempt": {"attempt_id": attempt_id, "slot": slot},
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
        "recent_successful_full_run": recent_success,
        "phase_comparison": _phase_comparison(
            start.get("expected_phases"), phase_results, recent_success
        ),
        "runtime_context": runtime_context,
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
                "zootd.service",
                "waydroid-container.service",
            ],
        },
        "scope": {
            "path": "docs/llm-recovery-scope.md",
            "sha256": sha256_bytes(scope_bytes),
            "version": 10,
        },
        "controller_repository": controller_repository,
        "repair_policy": {
            "operational_success_first": True,
            "tracked_changes_allowed": True,
            "branch_is_agent_selected": True,
            "commit_required_before_retry": True,
            "push_remote": controller_repository["remote_name"],
            "open_pull_request": controller_repository["pull_request_url_prefix"]
            is not None,
            "merge_pull_request": False,
            "restore_base_when_not_recovered": True,
            "suggested_worktree_path": (
                f"var/state/recovery/{failed_run_id}/repair-worktree"
            ),
        },
        "agent_session": {
            "ephemeral": False,
            "thread_state_path": (
                f"var/state/recovery/{failed_run_id}/codex-thread.json"
            ),
        },
        "reentrancy_policy": {
            "all_managed_stages": True,
            "daily": True,
            "whole_run_replay": True,
        },
        "client_update_policy": {
            "in_scope": True,
            "package": "com.hypergryph.arknights",
            "official_apk_url": (
                "https://ak.hypergryph.com/downloads/android_lastest"
            ),
            "allowed_https_domain_suffixes": [
                "hypergryph.com",
                "hycdn.cn",
            ],
            "replace_existing": True,
            "preserve_app_data": True,
            "install_argv_prefix": [
                "adb",
                "install",
                "--no-streaming",
                "-r",
            ],
            "fallback_install_argv_prefix": ["adb", "install", "-r"],
            "allow_uninstall": False,
            "allow_downgrade": False,
        },
        "runtime_recovery_policy": {
            "transactional_update_command": "./bin/zootd runtime-update",
            "validated_rollback_command": "./bin/zootd runtime-rollback",
            "manual_runtime_receipt_edits": False,
        },
        "controller_success_requirement": (
            "A different full run must independently validate every expected phase, "
            "a zero process status, a controller-verified clean base or repair commit, "
            "and a successful final audit. A pull request alone is never recovery."
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
    if not _GIT_HASH_RE.fullmatch(head):
        raise RecoveryError("controller observed an invalid Git HEAD")
    return head, bool(status_result.stdout)


def _verify_github_pull_request(
    root: Path,
    url: str,
    branch: str,
    commit: str,
    base_branch: str,
) -> Mapping[str, Any]:
    fields = "url,state,headRefName,headRefOid,baseRefName,isCrossRepository"
    try:
        completed = subprocess.run(
            ("gh", "pr", "view", url, "--json", fields),
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unverified", "error": str(exc)[-2000:]}
    if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
        detail = completed.stderr.decode("utf-8", errors="replace")[-2000:].strip()
        return {
            "status": "unverified",
            "error": detail or f"gh pr view exited {completed.returncode}",
        }
    try:
        metadata = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "unverified", "error": f"invalid gh output: {exc}"}
    expected = {
        "url": url,
        "state": "OPEN",
        "headRefName": branch,
        "headRefOid": commit,
        "baseRefName": base_branch,
        "isCrossRepository": False,
    }
    if not isinstance(metadata, dict) or any(
        metadata.get(key) != value for key, value in expected.items()
    ):
        return {
            "status": "invalid",
            "expected": expected,
            "observed": metadata if isinstance(metadata, dict) else None,
        }
    return {"status": "verified", "metadata": expected}


def _verify_code_repair(
    root: Path,
    repair: Mapping[str, Any],
    repository: Mapping[str, Any],
    *,
    recovered: bool,
    pull_request_verifier: PullRequestVerifier,
) -> dict[str, Any]:
    base_head = repository.get("head")
    base_branch = repository.get("branch")
    if repair.get("base_head") != base_head:
        raise RecoveryError("agent's code repair changed the recovery base HEAD")
    if not isinstance(base_branch, str) or not _valid_branch_name(base_branch):
        raise RecoveryError("controller recovery base branch is invalid")

    current_head, current_dirty = _git_identity(root)
    current_branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current_dirty:
        raise RecoveryError("agent left the controller repository dirty")

    if repair.get("status") == "not-needed":
        if current_head != base_head or current_branch != base_branch:
            raise RecoveryError("agent changed Git state without declaring a code repair")
        return {
            "status": "not-needed",
            "active_head": current_head,
            "active_branch": current_branch,
        }

    branch = repair.get("branch")
    commit = repair.get("commit")
    if not isinstance(branch, str) or not isinstance(commit, str):
        raise RecoveryError("agent's changed code repair lacks Git identity")
    if branch == base_branch:
        raise RecoveryError("agent committed an unattended repair on the base branch")
    branch_commit = _git_text(
        root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
    )
    if branch_commit != commit:
        raise RecoveryError("agent's repair branch does not identify its claimed commit")
    ancestor = subprocess.run(
        ("git", "merge-base", "--is-ancestor", str(base_head), commit),
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RecoveryError("agent's repair commit is not descended from the recovery base")
    changed = subprocess.run(
        (
            "git",
            "diff",
            "--no-renames",
            "--name-status",
            "-z",
            str(base_head),
            commit,
            "--",
        ),
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if changed.returncode != 0 or not changed.stdout:
        raise RecoveryError("agent's repair commit has no verifiable tracked changes")
    try:
        fields = [
            item.decode("utf-8") for item in changed.stdout.split(b"\0") if item
        ]
    except UnicodeDecodeError as exc:
        raise RecoveryError("agent's repair commit contains an invalid path") from exc
    if len(fields) % 2 != 0:
        raise RecoveryError("agent's repair commit has invalid Git diff metadata")
    changed_entries = list(zip(fields[0::2], fields[1::2], strict=True))
    changed_paths = [path for _status, path in changed_entries]
    if len(changed_paths) > 256 or any(
        not path or path.startswith("/") or "\x00" in path for path in changed_paths
    ):
        raise RecoveryError("agent's repair commit changed an unsafe path set")

    pull_request_url = repair.get("pull_request_url")
    pull_request_verification: Mapping[str, Any] | None = None
    if repair.get("status") == "pr-opened":
        prefix = repository.get("pull_request_url_prefix")
        if (
            not isinstance(prefix, str)
            or not isinstance(pull_request_url, str)
            or re.fullmatch(re.escape(prefix) + r"[1-9][0-9]*", pull_request_url)
            is None
        ):
            raise RecoveryError("agent's pull request URL does not match the configured remote")
        default_branch = repository.get("default_branch")
        if not isinstance(default_branch, str) or not _valid_branch_name(default_branch):
            raise RecoveryError("controller default branch is invalid")
        assert isinstance(branch, str)
        assert isinstance(commit, str)
        pull_request_verification = pull_request_verifier(
            root,
            pull_request_url,
            branch,
            commit,
            default_branch,
        )
        if pull_request_verification.get("status") not in {
            "verified",
            "invalid",
            "unverified",
        }:
            raise RecoveryError("pull request verifier returned an invalid result")

    applied = repair.get("applied_to_runtime") is True
    if applied and not recovered:
        raise RecoveryError("agent left an incomplete repair active")
    if applied:
        if any(
            status != "M" or path not in _APPLIED_REPAIR_PATHS
            for status, path in changed_entries
        ):
            raise RecoveryError(
                "an applied repair may modify only existing declarative task files"
            )
        for path in changed_paths:
            tree_entry = subprocess.run(
                ("git", "ls-tree", "-z", commit, "--", path),
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            prefix = b"100644 blob "
            if (
                tree_entry.returncode != 0
                or not tree_entry.stdout.startswith(prefix)
                or not tree_entry.stdout.endswith(b"\0")
            ):
                raise RecoveryError(
                    "an applied task repair must remain a regular tracked file"
                )
    expected_head = commit if applied else base_head
    expected_branch = branch if applied else base_branch
    if current_head != expected_head or current_branch != expected_branch:
        raise RecoveryError("agent did not leave the declared verified checkout active")
    return {
        "status": repair.get("status"),
        "base_head": base_head,
        "branch": branch,
        "commit": commit,
        "applied_to_runtime": applied,
        "active_head": current_head,
        "active_branch": current_branch,
        "changed_paths": changed_paths,
        "pull_request_url": pull_request_url,
        "pull_request_verification": pull_request_verification,
        "pull_request_error": repair.get("pull_request_error"),
        "validation_commands": repair.get("validation_commands"),
    }


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
    expected_recovery_context: Mapping[str, str],
    recovery_started_at: datetime,
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
    if start.get("recovery_context") != dict(expected_recovery_context):
        raise RecoveryError("claimed success is not linked to this recovery attempt")
    if _recorded_at(success_events[0]) <= recovery_started_at:
        raise RecoveryError("claimed success predates this recovery attempt")
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
    pull_request_verifier: PullRequestVerifier = _verify_github_pull_request,
) -> RecoveryOutcome:
    root = root.resolve()
    attempt_id = token_hex(16)
    try:
        events = load_run_events(root, failed_run_id)
        evidence = _incident_evidence(
            root,
            failed_run_id,
            events,
            slot=slot,
            attempt_id=attempt_id,
        )
    except (OSError, SupervisorError, ValueError) as exc:
        raise RecoveryError(f"cannot build failed-run recovery evidence: {exc}") from exc

    recovery_started_path = record_recovery_started(
        root,
        failed_run_id,
        {
            "schema_version": 1,
            "policy": "unsandboxed-scoped-v4",
            "slot": slot,
            "recovery_attempt": evidence["recovery_attempt"],
            "scope": evidence["scope"],
            "controller_repository": evidence["controller_repository"],
            "repair_policy": evidence["repair_policy"],
            "agent_session": evidence["agent_session"],
            "reentrancy_policy": evidence["reentrancy_policy"],
            "client_update_policy": evidence["client_update_policy"],
            "runtime_recovery_policy": evidence["runtime_recovery_policy"],
            "retry_command": evidence["retry_command"],
            "timeout_seconds": timeout_seconds,
            "max_adapter_attempts": 2,
            "screenshot_paths": evidence["screenshot_paths"],
        },
    )
    try:
        recovery_started_at = _recorded_at(load_json(recovery_started_path))
    except (OSError, json.JSONDecodeError, RecoveryError) as exc:
        raise RecoveryError("cannot reload the recovery attempt identity") from exc

    report: dict[str, Any] | None = None
    adapter_error: str | None = None
    controller: dict[str, Any] = {"status": "not-run"}
    overall_status = "failed"
    summary = "Recovery adapter did not produce a valid terminal result."
    blocker = "none"
    successful_run_id: str | None = None
    repair_branch: str | None = None
    pull_request_url: str | None = None
    adapter_attempt_errors: list[str] = []
    try:
        deadline = time.monotonic() + timeout_seconds
        raw_report: object | None = None
        for attempt in (1, 2):
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                raise RecoveryError("recovery adapter exhausted its total time budget")
            attempt_evidence = dict(evidence)
            attempt_evidence["adapter_attempt"] = {
                "number": attempt,
                "maximum": 2,
                "prior_errors": list(adapter_attempt_errors),
            }
            try:
                raw_report = _run_adapter(
                    command,
                    attempt_evidence,
                    root=root,
                    timeout_seconds=remaining,
                    runner=runner,
                )
            except RecoveryError as exc:
                adapter_attempt_errors.append(str(exc))
                if attempt == 1 and deadline - time.monotonic() >= 30:
                    continue
                raise
            break
        if raw_report is None:
            raise RecoveryError("recovery adapter produced no report")
        report = validate_recovery_report(raw_report, failed_run_id=failed_run_id)
        summary = report["summary"]
        blocker = report["scope_blocker"]
        repair = report["code_repair"]
        repair_branch = repair["branch"]
        pull_request_url = repair["pull_request_url"]
        try:
            repair_verification = _verify_code_repair(
                root,
                repair,
                evidence["controller_repository"],
                recovered=report["status"] == "recovered",
                pull_request_verifier=pull_request_verifier,
            )
        except RecoveryError as exc:
            controller = {"status": "rejected", "error": str(exc)}
            summary = f"Agent returned with invalid repository state: {exc}"
        else:
            publication = repair_verification.get("pull_request_verification")
            if (
                repair.get("status") == "pr-opened"
                and isinstance(publication, Mapping)
                and publication.get("status") != "verified"
            ):
                pull_request_url = None
                summary = (
                    f"{summary} The local repair is retained, but its pull request "
                    "could not be independently verified."
                )
            controller = {
                "status": "repository-verified",
                "code_repair": repair_verification,
            }
        if report["status"] == "recovered" and controller["status"] != "rejected":
            expected_git_head = (
                repair["commit"]
                if repair["applied_to_runtime"]
                else evidence["controller_repository"]["head"]
            )
            try:
                run_verification = verify_recovered_run(
                    root,
                    failed_run_id,
                    report,
                    expected_command=evidence["retry_command"],
                    expected_git_head=expected_git_head,
                    expected_recovery_context={
                        "parent_run_id": failed_run_id,
                        "attempt_id": attempt_id,
                        "slot": slot,
                    },
                    recovery_started_at=recovery_started_at,
                )
            except (OSError, SupervisorError, RecoveryError) as exc:
                controller = {
                    "status": "rejected",
                    "error": str(exc),
                    "code_repair": repair_verification,
                }
                summary = f"Agent claimed recovery, but controller rejected it: {exc}"
            else:
                controller = {
                    "status": "success",
                    "run": run_verification,
                    "code_repair": repair_verification,
                }
                overall_status = "recovered"
                successful_run_id = run_verification["successful_run_id"]
        elif report["status"] == "scope-blocked" and controller["status"] != "rejected":
            controller = {
                "status": "scope-accepted",
                "scope_blocker": blocker,
                "code_repair": repair_verification,
            }
            overall_status = "scope-blocked"
        elif report["status"] == "failed" and controller["status"] != "rejected":
            controller = {
                "status": "failure-state-verified",
                "code_repair": repair_verification,
            }
            overall_status = "failed"
    except RecoveryError as exc:
        adapter_error = str(exc)
        summary = adapter_error

    audit_payload: dict[str, Any] = {
        "schema_version": 1,
        "status": overall_status,
        "policy": "unsandboxed-scoped-v4",
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
    if adapter_attempt_errors:
        audit_payload["agent"]["attempt_errors"] = adapter_attempt_errors
    event_path = record_recovery_report(root, failed_run_id, audit_payload)
    return RecoveryOutcome(
        failed_run_id=failed_run_id,
        status=overall_status,
        event_path=event_path,
        summary=summary,
        successful_run_id=successful_run_id,
        scope_blocker=blocker,
        repair_branch=repair_branch,
        pull_request_url=pull_request_url,
    )
