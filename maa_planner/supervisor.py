from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any, Iterable, Mapping, Sequence

from .util import (
    atomic_write_json,
    canonical_json,
    isoformat,
    load_json,
    sha256_bytes,
    utc_now,
)


class SupervisorError(RuntimeError):
    pass


PHASES = (
    "runtime-readiness",
    "device-readiness",
    "depot",
    "daily",
    "source-refresh",
    "annihilation",
    "farming",
    "award",
    "cleanup",
)
RUN_MODES: dict[str, tuple[str, ...]] = {
    "full": PHASES,
    "award": (
        "runtime-readiness",
        "device-readiness",
        "award",
        "cleanup",
    ),
    "dry-run": ("runtime-readiness", "cleanup"),
    "device": ("device-readiness", "cleanup"),
}
PHASE_RESULTS = frozenset(
    {"succeeded", "policy-resolved", "not-applicable", "degraded", "failed"}
)
ACCEPTED_RESULTS = frozenset({"succeeded", "policy-resolved", "not-applicable"})

_RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}[.][0-9]{6}Z-[0-9a-f]{8}")
_DETAIL_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_OUTCOME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:/+ -]{0,511}")
_MAX_EXTERNAL_OUTPUT = 128 * 1024


@dataclass(frozen=True)
class SupervisorDiagnosis:
    classification: str
    summary: str
    affected_phases: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    safe_to_retry_whole_run: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "classification": self.classification,
            "summary": self.summary,
            "affected_phases": list(self.affected_phases),
            "evidence_refs": list(self.evidence_refs),
            "recommended_actions": list(self.recommended_actions),
            "safe_to_retry_whole_run": self.safe_to_retry_whole_run,
            "authorization": "diagnostic-only",
        }


@dataclass(frozen=True)
class SupervisorFinish:
    run_id: str
    status: str
    llm_invoked: bool
    event_path: Path
    diagnosis: SupervisorDiagnosis | None = None


def _run_root(root: Path) -> Path:
    return root / "var/state/supervisor/runs"


def _run_dir(root: Path, run_id: str) -> Path:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise SupervisorError("invalid supervisor run id")
    return _run_root(root) / run_id


def _event_core(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    previous_sha256: str | None,
    payload: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "sequence": sequence,
        "event_type": event_type,
        "recorded_at": isoformat(now),
        "previous_event_sha256": previous_sha256,
        "payload": dict(payload),
    }


def _exclusive_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SupervisorError(f"cannot create immutable supervisor event: {path}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _append_event(
    root: Path,
    run_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> Path:
    events = load_run_events(root, run_id)
    sequence = len(events)
    previous = events[-1]["event_sha256"] if events else None
    event = _event_core(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        previous_sha256=previous,
        payload=payload,
        now=(now or utc_now()).astimezone(UTC),
    )
    event["event_sha256"] = sha256_bytes(canonical_json(event))
    path = _run_dir(root, run_id) / "events" / f"{sequence:04d}-{event_type}.json"
    _exclusive_write_json(path, event)
    return path


def load_run_events(root: Path, run_id: str) -> list[dict[str, Any]]:
    run_dir = _run_dir(root.resolve(), run_id)
    events_dir = run_dir / "events"
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise SupervisorError(f"supervisor run does not exist: {run_id}")
    if not events_dir.is_dir() or events_dir.is_symlink():
        raise SupervisorError(f"supervisor event directory is invalid: {run_id}")

    paths = sorted(events_dir.glob("*.json"))
    events: list[dict[str, Any]] = []
    previous: str | None = None
    for sequence, path in enumerate(paths):
        if path.is_symlink() or not path.is_file():
            raise SupervisorError("supervisor history contains a non-regular event")
        try:
            event = load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise SupervisorError(f"cannot read supervisor event: {path}") from exc
        if not isinstance(event, dict):
            raise SupervisorError("supervisor event is not an object")
        event_sha = event.get("event_sha256")
        core = dict(event)
        core.pop("event_sha256", None)
        if (
            event.get("schema_version") != 1
            or event.get("run_id") != run_id
            or event.get("sequence") != sequence
            or event.get("previous_event_sha256") != previous
            or not isinstance(event_sha, str)
            or sha256_bytes(canonical_json(core)) != event_sha
        ):
            raise SupervisorError(f"supervisor hash chain is invalid at {path.name}")
        previous = event_sha
        events.append(event)

    if not events or events[0].get("event_type") != "run-started":
        raise SupervisorError("supervisor run has no valid start event")
    return events


def _git_snapshot(root: Path) -> dict[str, Any]:
    def run_git(*args: str) -> bytes:
        completed = subprocess.run(
            ("git", *args),
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise SupervisorError(f"cannot capture Git history identity: git {' '.join(args)}")
        return completed.stdout

    head = run_git("rev-parse", "HEAD").decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise SupervisorError("Git HEAD is invalid")
    status = run_git("status", "--porcelain=v1", "--untracked-files=all")
    diff = run_git("diff", "--binary", "HEAD")
    return {
        "head": head,
        "dirty": bool(status),
        "status_sha256": sha256_bytes(status),
        "tracked_diff_sha256": sha256_bytes(diff),
    }


def start_run(root: Path, mode: str, *, now: datetime | None = None) -> str:
    root = root.resolve()
    expected = RUN_MODES.get(mode)
    if expected is None:
        raise SupervisorError(f"unsupported supervisor mode: {mode}")
    repository = _git_snapshot(root)
    if repository["dirty"]:
        raise SupervisorError(
            "working tree is dirty; commit the code change before a managed run"
        )
    timestamp = (now or utc_now()).astimezone(UTC)
    run_id = f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{token_hex(4)}"
    run_dir = _run_dir(root, run_id)
    try:
        (run_dir / "events").mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise SupervisorError(f"cannot create supervisor run: {run_id}") from exc

    payload = {
        "mode": mode,
        "expected_phases": list(expected),
        "repository": repository,
        "llm_policy": "exception-only",
    }
    event = _event_core(
        run_id=run_id,
        sequence=0,
        event_type="run-started",
        previous_sha256=None,
        payload=payload,
        now=timestamp,
    )
    event["event_sha256"] = sha256_bytes(canonical_json(event))
    _exclusive_write_json(run_dir / "events/0000-run-started.json", event)
    atomic_write_json(
        root / "var/state/supervisor/latest-run.json",
        {"schema_version": 1, "run_id": run_id, "status": "running"},
    )
    return run_id


def _start_payload(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    payload = events[0].get("payload")
    if not isinstance(payload, dict):
        raise SupervisorError("supervisor start payload is invalid")
    return payload


def describe_evidence_files(root: Path, raw_paths: Iterable[str]) -> list[dict[str, Any]]:
    root = root.resolve()
    described: list[dict[str, Any]] = []
    for raw in raw_paths:
        source = Path(raw)
        source = source if source.is_absolute() else root / source
        if source.is_symlink():
            raise SupervisorError(f"evidence path must not be a symlink: {source}")
        try:
            resolved = source.resolve(strict=True)
            relative = resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SupervisorError(f"evidence path is outside the project: {source}") from exc
        if not resolved.is_file():
            raise SupervisorError(f"evidence path is not a regular file: {source}")
        digest = hashlib.sha256()
        size = 0
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        described.append(
            {
                "path": relative.as_posix(),
                "size": size,
                "sha256": digest.hexdigest(),
            }
        )
    return described


def record_phase(
    root: Path,
    run_id: str,
    *,
    phase: str,
    result: str,
    outcome: str,
    details: Mapping[str, str] | None = None,
    evidence_files: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> Path:
    root = root.resolve()
    events = load_run_events(root, run_id)
    if any(event.get("event_type") == "run-finished" for event in events):
        raise SupervisorError("supervisor run is already finished")
    start = _start_payload(events)
    expected = start.get("expected_phases")
    if not isinstance(expected, list) or phase not in expected:
        raise SupervisorError(f"phase is not part of this run: {phase}")
    if result not in PHASE_RESULTS:
        raise SupervisorError(f"invalid phase result: {result}")
    if not _OUTCOME_RE.fullmatch(outcome):
        raise SupervisorError("phase outcome is empty or invalid")
    if any(
        event.get("event_type") == "phase-finished"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("phase") == phase
        for event in events
    ):
        raise SupervisorError(f"phase already has a terminal event: {phase}")

    clean_details: dict[str, str] = {}
    for key, value in (details or {}).items():
        if not _DETAIL_KEY_RE.fullmatch(key):
            raise SupervisorError(f"invalid phase detail key: {key}")
        if not isinstance(value, str) or len(value) > 2000 or "\x00" in value:
            raise SupervisorError(f"invalid phase detail value: {key}")
        clean_details[key] = value
    if len(clean_details) > 32 or len(evidence_files) > 16:
        raise SupervisorError("phase evidence exceeds the bounded audit schema")

    return _append_event(
        root,
        run_id,
        "phase-finished",
        {
            "phase": phase,
            "result": result,
            "outcome": outcome,
            "details": clean_details,
            "evidence_files": list(evidence_files),
        },
        now=now,
    )


def _strings(value: object, field: str, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SupervisorError(f"{field} must be a bounded array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 2000:
            raise SupervisorError(f"invalid entry in {field}")
        result.append(item)
    return tuple(result)


def validate_diagnosis(
    value: object, *, run_id: str, expected_phases: Iterable[str]
) -> SupervisorDiagnosis:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SupervisorError("supervisor diagnosis has an unsupported schema")
    if value.get("run_id") != run_id:
        raise SupervisorError("supervisor diagnosis changed the run id")
    classification = value.get("classification")
    if classification not in {
        "environment",
        "runtime",
        "configuration",
        "upstream",
        "game-state",
        "evidence",
        "unknown",
    }:
        raise SupervisorError("invalid supervisor diagnosis classification")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > 4000:
        raise SupervisorError("supervisor diagnosis summary is invalid")
    affected = _strings(value.get("affected_phases"), "affected_phases", maximum=16)
    allowed = set(expected_phases)
    if (
        not affected
        or len(set(affected)) != len(affected)
        or any(phase not in allowed for phase in affected)
    ):
        raise SupervisorError("supervisor diagnosis invented an affected phase")
    evidence_refs = _strings(value.get("evidence_refs"), "evidence_refs", maximum=32)
    actions = _strings(value.get("recommended_actions"), "recommended_actions", maximum=8)
    safe_retry = value.get("safe_to_retry_whole_run")
    if not isinstance(safe_retry, bool):
        raise SupervisorError("safe_to_retry_whole_run must be boolean")
    return SupervisorDiagnosis(
        classification=classification,
        summary=summary,
        affected_phases=affected,
        evidence_refs=evidence_refs,
        recommended_actions=actions,
        safe_to_retry_whole_run=safe_retry,
    )


def run_diagnostic(
    command: Sequence[str],
    evidence: Mapping[str, Any],
    *,
    run_id: str,
    expected_phases: Iterable[str],
    timeout_seconds: int,
) -> SupervisorDiagnosis:
    if not command:
        raise SupervisorError("supervisor command is empty")
    try:
        completed = subprocess.run(
            tuple(command),
            input=canonical_json(evidence),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError(f"supervisor invocation failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-2000:].strip()
        raise SupervisorError(
            f"supervisor exited with {completed.returncode}: {detail}"
        )
    if len(completed.stdout) > _MAX_EXTERNAL_OUTPUT:
        raise SupervisorError("supervisor output is too large")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorError("supervisor did not return one JSON object") from exc
    return validate_diagnosis(
        value, run_id=run_id, expected_phases=expected_phases
    )


def _phase_payloads(events: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "phase-finished":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("phase"), str):
            raise SupervisorError("supervisor phase event is invalid")
        if payload["phase"] in result:
            raise SupervisorError("supervisor history has duplicate phase terminals")
        result[payload["phase"]] = payload
    return result


def finish_run(
    root: Path,
    run_id: str,
    *,
    process_status: int,
    supervisor_enabled: bool,
    supervisor_required: bool,
    command: Sequence[str],
    timeout_seconds: int,
    now: datetime | None = None,
) -> SupervisorFinish:
    root = root.resolve()
    if not 0 <= process_status <= 255:
        raise SupervisorError("process status is outside the shell exit range")
    events = load_run_events(root, run_id)
    if any(event.get("event_type") == "run-finished" for event in events):
        raise SupervisorError("supervisor run is already finished")
    start = _start_payload(events)
    expected_raw = start.get("expected_phases")
    if not isinstance(expected_raw, list) or not all(
        isinstance(phase, str) and phase in PHASES for phase in expected_raw
    ):
        raise SupervisorError("supervisor expected phase list is invalid")
    if len(set(expected_raw)) != len(expected_raw):
        raise SupervisorError("supervisor expected phase list has duplicates")
    expected = tuple(expected_raw)
    phases = _phase_payloads(events)
    missing = [phase for phase in expected if phase not in phases]
    unacceptable = [
        phase
        for phase in expected
        if phase in phases and phases[phase].get("result") not in ACCEPTED_RESULTS
    ]
    deterministic_success = process_status == 0 and not missing and not unacceptable

    llm_invoked = False
    diagnosis: SupervisorDiagnosis | None = None
    llm_error: str | None = None
    if not deterministic_success and supervisor_enabled:
        llm_invoked = True
        evidence = {
            "schema_version": 1,
            "kind": "exception-diagnosis",
            "run_id": run_id,
            "process_status": process_status,
            "expected_phases": list(expected),
            "missing_phases": missing,
            "unacceptable_phases": unacceptable,
            "start": start,
            "phase_results": [phases[phase] for phase in expected if phase in phases],
            "chain_head_sha256": events[-1]["event_sha256"],
            "hard_safety_rules": [
                "never replay daily after any state-changing marker",
                "never move training-room operators through Dorm automation",
                "only six-star recruitment requires manual confirmation",
                "normal medicine and Originite Prime remain disabled",
                "Fight requires deterministic client proxy evidence",
            ],
        }
        try:
            diagnosis = run_diagnostic(
                command,
                evidence,
                run_id=run_id,
                expected_phases=expected,
                timeout_seconds=timeout_seconds,
            )
            problem_phases = set(missing) | set(unacceptable)
            if problem_phases and not set(diagnosis.affected_phases) <= problem_phases:
                raise SupervisorError(
                    "supervisor diagnosis named a phase outside the deterministic problem set"
                )
            if diagnosis.safe_to_retry_whole_run and "daily" in phases:
                raise SupervisorError(
                    "supervisor attempted to authorize a whole-run retry after daily"
                )
        except SupervisorError as exc:
            llm_error = str(exc)
    elif not deterministic_success and supervisor_required:
        llm_error = "exception diagnosis is required but the supervisor is disabled"

    status = "success" if deterministic_success else "failed"
    payload: dict[str, Any] = {
        "status": status,
        "process_status": process_status,
        "missing_phases": missing,
        "unacceptable_phases": unacceptable,
        "llm": {
            "policy": "exception-only",
            "enabled": supervisor_enabled,
            "required_on_exception": supervisor_required,
            "invoked": llm_invoked,
            "status": (
                "not-needed"
                if deterministic_success
                else "success"
                if diagnosis is not None
                else "error"
                if llm_error is not None
                else "disabled"
            ),
        },
    }
    if diagnosis is not None:
        payload["llm"]["diagnosis"] = diagnosis.as_dict()
    if llm_error is not None:
        payload["llm"]["error"] = llm_error
    path = _append_event(root, run_id, "run-finished", payload, now=now)
    atomic_write_json(
        root / "var/state/supervisor/latest-run.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "llm_invoked": llm_invoked,
            "event_sha256": load_json(path)["event_sha256"],
        },
    )
    return SupervisorFinish(
        run_id=run_id,
        status=status,
        llm_invoked=llm_invoked,
        event_path=path,
        diagnosis=diagnosis,
    )
