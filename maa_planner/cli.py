from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

from .agent import AgentAdviceError, run_advisor
from .annihilation import (
    game_week,
    operator_annihilation_confirmation,
    plan_annihilation,
    valid_annihilation_state,
)
from .capability import (
    CapabilityError,
    CapabilityKey,
    CapabilityLedger,
    CapabilityLedgerError,
    REGULAR_FALLBACK_ACTIVITY_INSTANCE,
    extract_fight_observations,
    extract_annihilation_progress,
    extract_successful_fight,
    extract_unstable_fight,
)
from .config import ConfigError, PlannerConfig, load_config
from .inventory import (
    InventoryError,
    extract_depot_snapshot,
    load_snapshot,
    save_snapshot,
    select_drone_mode,
)
from .materials import (
    MaterialRecipeError,
    validate_blue_material_chains_against_item_index,
)
from .models import Decision
from .orchestrator import (
    SourceBundle,
    current_core_version,
    load_activity_calendar,
    load_source_bundle,
    navigation_stages,
    refresh_maa_resources,
)
from .policy import select_farming_plan
from .recovery import RecoveryError, recover_failed_run
from .runtime_contracts import (
    RuntimeContractError,
    validate_farming_contracts,
    validate_runtime_contracts,
    validate_service_contracts,
)
from .runtime_receipt import (
    RuntimeReceiptError,
    runtime_generation_fingerprint,
    validate_runtime_receipt,
)
from .runtime_task import RUNTIME_PARAMETERS, RuntimeTaskError, render_runtime_task
from .sources import SourceError
from .supervisor import (
    PHASE_RESULTS,
    PHASES,
    RUN_MODES,
    SupervisorError,
    describe_evidence_files,
    finish_run,
    record_phase,
    run_diagnostic,
    start_run,
)
from .util import (
    atomic_write_json,
    canonical_json,
    isoformat,
    load_json,
    parse_iso_datetime,
    sha256_bytes,
    utc_now,
)


def _config(root: Path, raw_path: str | None) -> PlannerConfig:
    path = Path(raw_path) if raw_path else root / "config/farming.toml"
    if not path.is_absolute():
        path = root / path
    return load_config(path)


def _project_path(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else root / path


def _validated_stage(raw_stage: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,63}", raw_stage):
        raise ValueError(f"unsafe stage code: {raw_stage!r}")
    return raw_stage


def _validated_activity_instance(raw_instance: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{24}", raw_instance):
        raise ValueError(f"invalid activity instance: {raw_instance!r}")
    return raw_instance


def _read_log_suffix(
    path: Path,
    since_byte: int,
    *,
    expected_device: int | None = None,
    expected_inode: int | None = None,
    log_was_missing: bool = False,
) -> str:
    if since_byte < 0:
        raise ValueError("since-byte must be non-negative")
    if log_was_missing:
        if (
            since_byte != 0
            or expected_device is not None
            or expected_inode is not None
        ):
            raise ValueError("missing-log cursor must start at byte zero without an inode")
    elif (expected_device is None) != (expected_inode is None):
        raise ValueError("log device and inode must be supplied together")
    with path.open("rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("log path is not a regular file")
        if expected_device is not None and (
            metadata.st_dev != expected_device or metadata.st_ino != expected_inode
        ):
            raise ValueError("log was replaced or rotated after the recorded cursor")
        if since_byte > metadata.st_size:
            raise ValueError(
                "log became shorter than the recorded offset "
                f"({metadata.st_size} < {since_byte})"
            )
        handle.seek(since_byte)
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _read_log_cursor(args: argparse.Namespace, path: Path) -> str:
    return _read_log_suffix(
        path,
        args.since_byte,
        expected_device=getattr(args, "log_device", None),
        expected_inode=getattr(args, "log_inode", None),
        log_was_missing=getattr(args, "log_was_missing", False),
    )


def _activity_instance_from_snapshot(
    root: Path,
    *,
    client: str,
    stage: str,
    now: datetime,
) -> str:
    """Resolve bookkeeping scope from the last atomic source snapshot.

    This deliberately does not refresh Yituliu or MAA activity data after a fight.
    The snapshot must have been generated recently (the launcher syncs it before
    starting the device), and the exact activity must still be active.
    """

    path = root / "var/state/planner/latest-sources.json"
    payload = load_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("latest source snapshot has an unsupported schema")
    generated_raw = payload.get("generated_at")
    if not isinstance(generated_raw, str):
        raise ValueError("latest source snapshot has no generation time")
    generated_at = parse_iso_datetime(generated_raw).astimezone(UTC)
    age = now.astimezone(UTC) - generated_at
    if age < timedelta(minutes=-5) or age > timedelta(hours=6):
        raise ValueError(f"latest source snapshot is outside bookkeeping freshness: {age}")
    raw_activities = payload.get("activities")
    if not isinstance(raw_activities, list):
        raise ValueError("latest source snapshot has no activities")

    matches: list[str] = []
    for raw in raw_activities:
        if not isinstance(raw, dict) or raw.get("client") != client:
            continue
        raw_stages = raw.get("stages")
        if not isinstance(raw_stages, list) or not any(
            isinstance(candidate, dict) and candidate.get("code") == stage
            for candidate in raw_stages
        ):
            continue
        try:
            start = parse_iso_datetime(str(raw["start"])).astimezone(UTC)
            end = parse_iso_datetime(str(raw["end"])).astimezone(UTC)
            key = raw["key"]
            name = raw["name"]
            instance = raw["instance_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"latest source snapshot has an invalid activity: {exc}") from exc
        if not all(isinstance(value, str) and value for value in (key, name, instance)):
            raise ValueError("latest source snapshot has an invalid activity identity")
        expected = sha256_bytes(
            canonical_json(
                {
                    "client": client,
                    "key": key,
                    "name": name,
                    "start": isoformat(start),
                    "end": isoformat(end),
                }
            )
        )[:24]
        if instance != expected or not re.fullmatch(r"[0-9a-f]{24}", instance):
            raise ValueError("latest source snapshot activity identity does not verify")
        if start <= now.astimezone(UTC) < end:
            matches.append(instance)
    if len(matches) != 1:
        raise ValueError(f"expected one active activity for {stage}, found {len(matches)}")
    return matches[0]


def _inventory_path(root: Path) -> Path:
    return root / "var/state/planner/inventory.json"


def _ledger_path(root: Path) -> Path:
    return root / "var/state/planner/capabilities.json"


def _annihilation_state_path(root: Path) -> Path:
    return root / "var/state/planner/annihilation.json"


def _load_ledger(root: Path) -> CapabilityLedger:
    path = _ledger_path(root)
    ledger = CapabilityLedger.load(path) if path.exists() else CapabilityLedger()
    run_id = os.environ.get("MAA_PROXY_RUN_ID")
    return ledger.for_run(run_id) if run_id else ledger


def _write_decision(root: Path, decision: Decision, output: str | None) -> Path:
    if output:
        destination = Path(output)
        if not destination.is_absolute():
            destination = root / destination
    else:
        destination = root / "var/state/planner/latest-decision.json"
    payload = decision.as_dict()
    atomic_write_json(destination, payload)
    stamp = decision.generated_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S-%f")
    archive = root / f"var/state/planner/decisions/{stamp}.json"
    atomic_write_json(archive, payload)
    return destination


def _print_decision(decision: Decision, path: Path) -> None:
    if decision.decision == "FIGHT":
        goal = f", drop goal {decision.drop_goal}" if decision.drop_goal else ""
        print(f"FIGHT {decision.selected_stage} ({decision.selected_item}{goal}); decision: {path}")
    else:
        print(f"NOOP {decision.reason}; decision: {path}")
        for candidate in decision.candidates:
            if candidate.rejected_by:
                print(f"  {candidate.stage_code}: {', '.join(candidate.rejected_by)}")


def _noop(now: datetime, reason: str, error: Exception | str | None = None) -> Decision:
    evidence = {}
    if error is not None:
        evidence["error"] = str(error)
    return Decision("NOOP", reason, now, evidence=evidence)


_AGENT_DIAGNOSTIC_REASONS = frozenset({"SOURCE_UNAVAILABLE"})
_AGENT_DIAGNOSTIC_REJECTIONS = frozenset(
    {
        "INVALID_ITEM_ID",
        "INVALID_STAGE_CODE",
        "MAA_NAVIGATION_UNSUPPORTED",
        "NO_NUMERIC_MATERIAL_STAGE",
        "YITULIU_STAGE_UNAVAILABLE",
    }
)


def _should_run_advisor(decision: Decision) -> bool:
    """Use the LLM only for upstream schema, conflict, or mapping diagnostics."""

    if decision.decision != "NOOP":
        return False
    if decision.reason in _AGENT_DIAGNOSTIC_REASONS:
        return True
    return any(
        rejection in _AGENT_DIAGNOSTIC_REJECTIONS
        for candidate in decision.candidates
        for rejection in candidate.rejected_by
    )


def _apply_agent_advice(config: PlannerConfig | None, decision: Decision) -> None:
    """Attach an audit record even when the bounded advisor is not invoked."""

    eligible = _should_run_advisor(decision)
    enabled = config is not None and config.agent.enabled
    audit: dict[str, object] = {
        "schema_version": 1,
        "enabled": enabled,
        "eligible": eligible,
        "invoked": False,
        "authorization": "advisory-only",
    }
    if config is None:
        audit["status"] = "configuration_unavailable"
    elif not enabled:
        audit["status"] = "disabled"
    elif not eligible:
        audit["status"] = "not_applicable"
    else:
        audit["invoked"] = True
        try:
            # Freeze the exact pre-advice evidence. Later audit annotations must
            # not mutate the object handed to an in-process or external adapter.
            advisor_evidence = json.loads(canonical_json(decision.as_dict()))
            advice = run_advisor(
                config.agent.command,
                advisor_evidence,
                allowed_stages=[
                    candidate.stage_code for candidate in decision.candidates
                ],
                timeout_seconds=config.agent.timeout_seconds,
            )
            decision.evidence["agent_advice"] = advice.as_dict()
            audit["status"] = "success"
        except AgentAdviceError as exc:
            decision.evidence["agent_error"] = str(exc)
            audit["status"] = "error"
    decision.evidence["agent"] = audit


def _write_advisor_probe(root: Path, payload: dict[str, object], now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S-%f")
    archive = root / f"var/state/planner/advisor-probes/{stamp}.json"
    latest = root / "var/state/planner/latest-advisor-probe.json"
    atomic_write_json(archive, payload)
    atomic_write_json(latest, payload)
    return latest


def command_advisor_check(args: argparse.Namespace) -> int:
    """Perform one bounded real-model probe without touching Waydroid or the game."""

    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    started = time.monotonic()
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at": isoformat(now),
        "status": "configuration_error",
        "enabled": False,
        "invoked": False,
        "authorization": "advisory-only",
        "probe": {
            "kind": "synthetic-connectivity",
            "game_action_authorized": False,
        },
    }
    return_code = 1
    try:
        config = _config(root, args.config)
        payload["enabled"] = config.agent.enabled
        if not config.agent.enabled:
            payload["status"] = "disabled"
            payload["error"] = "the configured advisor is disabled"
        else:
            decision = _noop(
                now,
                "SOURCE_UNAVAILABLE",
                "synthetic connectivity probe; no live source failure",
            )
            decision.evidence["probe"] = payload["probe"]
            evidence = decision.as_dict()
            payload["evidence_sha256"] = sha256_bytes(canonical_json(evidence))
            payload["invoked"] = True
            try:
                advice = run_advisor(
                    config.agent.command,
                    evidence,
                    allowed_stages=(),
                    timeout_seconds=config.agent.timeout_seconds,
                )
            except AgentAdviceError as exc:
                payload["status"] = "error"
                payload["error"] = str(exc)
            else:
                payload["status"] = "success"
                payload["advice"] = advice.as_dict()
                return_code = 0
    except ConfigError as exc:
        payload["error"] = str(exc)

    payload["duration_ms"] = round((time.monotonic() - started) * 1000)
    try:
        path = _write_advisor_probe(root, payload, now)
    except OSError as exc:
        print(f"cannot write advisor probe: {exc}", file=sys.stderr)
        return 1
    if return_code == 0:
        print(f"advisor probe succeeded: {path}")
    else:
        print(
            f"advisor probe failed ({payload['status']}): {path}",
            file=sys.stderr,
        )
    return return_code


def _supervisor_probe_path(root: Path, payload: dict[str, object], now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S-%f")
    archive = root / f"var/state/supervisor/probes/{stamp}.json"
    latest = root / "var/state/supervisor/latest-probe.json"
    atomic_write_json(archive, payload)
    atomic_write_json(latest, payload)
    return latest


def command_supervisor_check(args: argparse.Namespace) -> int:
    """Exercise the exception adapter without touching Waydroid or game state."""

    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    run_id = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{secrets.token_hex(4)}"
    evidence: dict[str, object] = {
        "schema_version": 1,
        "kind": "exception-diagnosis",
        "run_id": run_id,
        "process_status": 1,
        "expected_phases": ["runtime-readiness"],
        "missing_phases": ["runtime-readiness"],
        "unacceptable_phases": [],
        "start": {
            "mode": "synthetic-probe",
            "llm_policy": "exception-only",
        },
        "phase_results": [],
        "chain_head_sha256": "0" * 64,
        "hard_safety_rules": [
            "synthetic probe cannot authorize or execute a game action"
        ],
    }
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at": isoformat(now),
        "status": "configuration-error",
        "run_id": run_id,
        "invoked": False,
        "authorization": "diagnostic-only",
        "game_action_authorized": False,
        "evidence_sha256": sha256_bytes(canonical_json(evidence)),
    }
    return_code = 1
    try:
        config = _config(root, args.config)
        payload["enabled"] = config.supervisor.enabled
        if not config.supervisor.enabled:
            payload["status"] = "disabled"
            payload["error"] = "the configured exception supervisor is disabled"
        else:
            payload["invoked"] = True
            try:
                diagnosis = run_diagnostic(
                    config.supervisor.command,
                    evidence,
                    run_id=run_id,
                    expected_phases=("runtime-readiness",),
                    timeout_seconds=config.supervisor.timeout_seconds,
                )
            except SupervisorError as exc:
                payload["status"] = "error"
                payload["error"] = str(exc)
            else:
                payload["status"] = "success"
                payload["diagnosis"] = diagnosis.as_dict()
                return_code = 0
    except ConfigError as exc:
        payload["error"] = str(exc)

    try:
        path = _supervisor_probe_path(root, payload, now)
    except OSError as exc:
        print(f"cannot write supervisor probe: {exc}", file=sys.stderr)
        return 1
    if return_code == 0:
        print(f"exception supervisor probe succeeded: {path}")
    else:
        print(
            f"exception supervisor probe failed ({payload['status']}): {path}",
            file=sys.stderr,
        )
    return return_code


def command_supervisor_start(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        if config.supervisor.required_on_exception and not config.supervisor.enabled:
            raise SupervisorError("required exception supervisor is disabled")
        run_id = start_run(root, args.mode)
    except (ConfigError, OSError, SupervisorError) as exc:
        print(f"cannot start phase supervisor: {exc}", file=sys.stderr)
        return 1
    print(run_id)
    return 0


def _phase_details(raw_values: Sequence[str]) -> dict[str, str]:
    details: dict[str, str] = {}
    for raw in raw_values:
        if "=" not in raw:
            raise SupervisorError("phase detail must use KEY=VALUE")
        key, value = raw.split("=", 1)
        if key in details:
            raise SupervisorError(f"duplicate phase detail: {key}")
        details[key] = value
    return details


def command_supervisor_phase(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        path = record_phase(
            root,
            args.run_id,
            phase=args.phase,
            result=args.result,
            outcome=args.outcome,
            details=_phase_details(args.detail),
            evidence_files=describe_evidence_files(root, args.evidence_file),
        )
    except (OSError, SupervisorError) as exc:
        print(f"cannot record supervisor phase: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def command_supervisor_finish(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        if args.defer_to_recovery and not config.supervisor.recovery_enabled:
            raise SupervisorError(
                "cannot defer diagnosis because operational recovery is disabled"
            )
        outcome = finish_run(
            root,
            args.run_id,
            process_status=args.process_status,
            supervisor_enabled=config.supervisor.enabled,
            supervisor_required=config.supervisor.required_on_exception,
            command=config.supervisor.command,
            timeout_seconds=config.supervisor.timeout_seconds,
            diagnose=not args.defer_to_recovery,
        )
    except (ConfigError, OSError, SupervisorError) as exc:
        print(f"cannot finish phase supervisor: {exc}", file=sys.stderr)
        return 2
    if outcome.status == "success":
        print(
            f"all deterministic phases succeeded; LLM not needed: {outcome.event_path}"
        )
        return 0
    if outcome.diagnosis is not None:
        print(
            f"run failed; LLM diagnosed {outcome.diagnosis.classification}: "
            f"{outcome.diagnosis.summary}; audit: {outcome.event_path}",
            file=sys.stderr,
        )
    else:
        print(f"run failed; inspect supervisor audit: {outcome.event_path}", file=sys.stderr)
    return 1


def command_supervisor_recover_run(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        if not config.supervisor.recovery_enabled:
            raise RecoveryError("operational recovery is disabled")
        outcome = recover_failed_run(
            root,
            args.run_id,
            slot=args.slot,
            command=config.supervisor.recovery_command,
            timeout_seconds=config.supervisor.recovery_timeout_seconds,
        )
    except (ConfigError, OSError, RecoveryError, SupervisorError) as exc:
        print(f"cannot recover failed supervisor run: {exc}", file=sys.stderr)
        return 1
    if outcome.status == "recovered":
        repair = (
            f"; repair PR: {outcome.pull_request_url}"
            if outcome.pull_request_url
            else f"; local repair branch: {outcome.repair_branch}"
            if outcome.repair_branch
            else ""
        )
        print(
            f"recovery completed through full run {outcome.successful_run_id}; "
            f"audit: {outcome.event_path}{repair}"
        )
        return 0
    if outcome.status == "scope-blocked":
        print(
            f"recovery reached scope blocker {outcome.scope_blocker}: "
            f"{outcome.summary}; audit: {outcome.event_path}",
            file=sys.stderr,
        )
        return 2
    print(
        f"recovery did not complete: {outcome.summary}; audit: {outcome.event_path}"
        + (
            f"; repair PR: {outcome.pull_request_url}"
            if outcome.pull_request_url
            else ""
        ),
        file=sys.stderr,
    )
    return 1


def _run_sync(root: Path, config: PlannerConfig, *, offline: bool, skip_hot_update: bool) -> SourceBundle:
    if not offline and not skip_hot_update:
        ok, output = refresh_maa_resources(root)
        if not ok:
            print(
                f"warning: validated MAA resource update failed: {output[-1000:]}",
                file=sys.stderr,
            )
    return load_source_bundle(root, config, online=not offline)


def command_sync(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        bundle = _run_sync(root, config, offline=args.offline, skip_hot_update=args.skip_maa_hot_update)
    except (ConfigError, SourceError) as exc:
        print(f"source sync failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"sources ready: {len(bundle.activities)} MAA activities, "
        f"{len(bundle.efficiencies)} efficiency records"
    )
    return 0


def command_sync_calendar(args: argparse.Namespace) -> int:
    """Refresh the MAA activity source shared by both planners."""

    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        calendar = load_activity_calendar(root, config, online=not args.offline)
    except (ConfigError, SourceError, OSError) as exc:
        print(f"activity-calendar sync failed: {exc}", file=sys.stderr)
        return 1
    print(f"activity calendar ready: {len(calendar.activities)} MAA activities")
    return 0


def command_inventory(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        log_path = _project_path(root, args.log)
        text = log_path.read_text(encoding="utf-8", errors="replace")
        captured_at = parse_iso_datetime(args.captured_at) if args.captured_at else utc_now()
        snapshot = extract_depot_snapshot(
            text,
            captured_at=captured_at,
            client=config.client_type,
            account=config.account,
        )
        destination = _project_path(root, args.output) if args.output else _inventory_path(root)
        save_snapshot(destination, snapshot)
    except (OSError, ConfigError, InventoryError, ValueError) as exc:
        print(f"inventory extraction failed: {exc}", file=sys.stderr)
        return 1
    print(f"inventory saved: {len(snapshot.items)} observed items -> {destination}")
    return 0


def command_select_drones(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    try:
        config = _config(root, args.config)
        inventory_path = (
            _project_path(root, args.inventory) if args.inventory else _inventory_path(root)
        )
        snapshot = load_snapshot(inventory_path)
        if snapshot.client not in {None, config.client_type} or snapshot.account not in {
            None,
            config.account,
        }:
            raise InventoryError("inventory snapshot belongs to another client/account")
        age = now - snapshot.captured_at.astimezone(UTC)
        if age < timedelta(minutes=-5) or age > timedelta(
            seconds=config.freshness.inventory_seconds
        ):
            raise InventoryError(f"inventory snapshot age is outside policy: {age}")
        mode, quantity = select_drone_mode(snapshot, threshold=args.threshold)
    except (OSError, ValueError, ConfigError, InventoryError) as exc:
        print(f"cannot select drone target: {exc}", file=sys.stderr)
        return 1

    if args.value_only:
        print(mode)
    else:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "drones": mode,
                    "pure_gold_item_id": "3003",
                    "pure_gold_quantity": quantity,
                    "threshold": args.threshold,
                    "inventory_captured_at": isoformat(snapshot.captured_at),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


def command_render_runtime_task(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    destination = _project_path(root, args.output)
    try:
        render_runtime_task(root, args.task, args.value, destination)
    except (OSError, RuntimeTaskError) as exc:
        print(f"cannot render runtime MAA task: {exc}", file=sys.stderr)
        return 1
    print(destination)
    return 0


def command_record_fight(args: argparse.Namespace) -> int:
    # Compatibility entry point: all automatic writes use the same ordered,
    # idempotent reducer, so an old caller cannot bypass the failure threshold.
    if not args.activity_instance:
        root = Path(args.project_root).resolve()
        try:
            config = _config(root, args.config)
            args.activity_instance = (
                REGULAR_FALLBACK_ACTIVITY_INSTANCE if args.stage in {"AP-5", "1-7"}
                else _activity_instance_from_snapshot(
                    root, client=config.client_type, stage=args.stage, now=utc_now()))
        except (OSError, ValueError, ConfigError) as exc:
            print(f"cannot resolve fight scope: {exc}", file=sys.stderr)
            return 1
    return command_reconcile_fight(args)


def command_quarantine(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    now = utc_now()
    try:
        config = _config(root, args.config)
        stage = _validated_stage(args.stage)
        if args.activity_instance:
            activity_instance = _validated_activity_instance(args.activity_instance)
        else:
            activity_instance = _activity_instance_from_snapshot(
                root, client=config.client_type, stage=stage, now=now
            )
        key = CapabilityKey(config.client_type, config.account, activity_instance, stage)
        with _locked_ledger(root) as ledger:
            ledger.mark_quarantined(key, args.reason, observed_at=now)
            ledger.save(_ledger_path(root))
    except (OSError, ValueError, ConfigError, SourceError, CapabilityLedgerError) as exc:
        print(f"cannot quarantine stage: {exc}", file=sys.stderr)
        return 1
    print(f"stage quarantined: {stage} in activity {activity_instance}")
    return 0


def command_quarantine_fight(args: argparse.Namespace) -> int:
    return command_reconcile_fight(args)


def command_check_fight(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        stage = _validated_stage(args.stage)
        log_path = _project_path(root, args.log)
        text = _read_log_cursor(args, log_path)
        proof = extract_successful_fight(text, stage)
    except (OSError, ValueError) as exc:
        print(f"cannot check fight proof: {exc}", file=sys.stderr)
        return 1
    if proof is None:
        print(f"no fresh three-star fight proof for {stage}", file=sys.stderr)
        return 1
    print(json.dumps(proof.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def command_reconcile_fight(args: argparse.Namespace) -> int:
    """Process fresh actual battle results once, in order, under a ledger lock."""
    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    try:
        config = _config(root, args.config)
        stage = _validated_stage(args.stage)
        activity_instance = _validated_activity_instance(args.activity_instance)
        log_path = _project_path(root, args.log)
        metadata = log_path.stat()
        text = _read_log_cursor(args, log_path)
        after = log_path.stat()
        if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("log changed during evidence read")
        observations = extract_fight_observations(text, stage)
        with _locked_ledger(root) as ledger:
            result = ledger.reconcile(
                CapabilityKey(config.client_type, config.account, activity_instance, stage),
                observations, log_device=metadata.st_dev, log_inode=metadata.st_ino,
                suffix_start_byte=args.since_byte, observed_at=now,
            )
            if result.recorded:
                ledger.save(_ledger_path(root))
    except (OSError, ValueError, ConfigError, CapabilityError) as exc:
        print(f"cannot reconcile fight evidence: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "schema_version": 2, "outcome": result.outcome, "recorded": result.recorded,
        "consecutive_failures": result.consecutive_failures,
        "observations": result.observations, "stage": stage,
        "activity_instance": activity_instance,
    }, ensure_ascii=False, sort_keys=True))
    return 0


@contextmanager
def _locked_ledger(root: Path):
    path = _ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield _load_ledger(root)


def command_proxy_state(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        stage = _validated_stage(args.stage)
        instance = (_validated_activity_instance(args.activity_instance)
                    if args.activity_instance else REGULAR_FALLBACK_ACTIVITY_INSTANCE
                    if stage in {"AP-5", "1-7"} else _activity_instance_from_snapshot(
                        root, client=config.client_type, stage=stage, now=utc_now()))
        key = CapabilityKey(config.client_type, config.account, instance, stage)
        with _locked_ledger(root) as ledger:
            if args.command == "proxy-reset":
                ledger.reset(key)
                ledger.save(_ledger_path(root))
            record = ledger.query(key)
            payload = {"stage": stage, "activity_instance": instance,
                       "status": ledger.status(key),
                       "consecutive_failures": record.consecutive_failures if record else 0}
    except (OSError, ValueError, ConfigError, CapabilityError) as exc:
        print(f"cannot read/reset proxy state: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def command_check_proxy(args: argparse.Namespace) -> int:
    from .proxy import extract_saved_proxy
    root = Path(args.project_root).resolve()
    try:
        stage = _validated_stage(args.stage)
        proof = extract_saved_proxy(_read_log_cursor(args, _project_path(root, args.log)), stage)
    except (OSError, ValueError) as exc:
        print(f"cannot check proxy screen: {exc}", file=sys.stderr)
        return 1
    if proof is None:
        print(f"no fresh saved-proxy screen proof for {stage}", file=sys.stderr)
        return 1
    print(json.dumps(proof, ensure_ascii=False, sort_keys=True))
    return 0


def command_plan_annihilation(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    try:
        config = _config(root, args.config)
    except ConfigError as exc:
        print(f"cannot plan Annihilation: {exc}", file=sys.stderr)
        return 1

    state: object = None
    state_path = _annihilation_state_path(root)
    if state_path.exists():
        try:
            state = load_json(state_path)
        except (OSError, json.JSONDecodeError):
            state = None

    source_available = True
    source_error: str | None = None
    activities = ()
    saved_state = valid_annihilation_state(
        state,
        client=config.client_type,
        account=config.account,
        week=game_week(now),
    )
    if saved_state is None or saved_state.get("status") not in {
        "complete",
        "unstable",
    }:
        try:
            calendar = load_activity_calendar(
                root,
                config,
                online=not args.offline,
            )
            activities = calendar.activities
        except (SourceError, OSError) as exc:
            # Weekly completion has a deterministic Monday/deadline fallback
            # even when the activity calendar is unavailable. LLM advice is
            # never on this execution path.
            source_available = False
            source_error = str(exc)

    try:
        decision = plan_annihilation(
            now=now,
            activities=activities,
            client=config.client_type,
            account=config.account,
            state=state,
            source_available=source_available,
            source_error=source_error,
            execution_budget=timedelta(
                minutes=config.annihilation.execution_budget_minutes
            ),
            transaction_timeout=timedelta(
                minutes=config.annihilation.transaction_timeout_minutes
            ),
            max_transactions_per_run=(
                config.annihilation.max_transactions_per_run
            ),
            medicine_expire_days=config.policy.medicine_expire_days,
        )
    except ValueError as exc:
        print(f"cannot plan Annihilation: {exc}", file=sys.stderr)
        return 1

    destination = (
        _project_path(root, args.output)
        if args.output
        else root / "var/state/planner/latest-annihilation-decision.json"
    )
    atomic_write_json(destination, decision)
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    atomic_write_json(
        root / f"var/state/planner/annihilation-decisions/{stamp}.json",
        decision,
    )
    print(
        f"{decision['decision']} {decision['reason']}; "
        f"Annihilation decision: {destination}"
    )
    return 0


def command_record_annihilation(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    try:
        config = _config(root, args.config)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.week_start_game_day):
            raise ValueError("invalid expected game-week key")
        week = game_week(now)
        if week.key != args.week_start_game_day:
            raise ValueError(
                "Annihilation transaction crossed the 04:00 weekly boundary "
                f"({args.week_start_game_day} -> {week.key})"
            )
        if not getattr(args, "log_was_missing", False) and (
            getattr(args, "log_device", None) is None
            or getattr(args, "log_inode", None) is None
        ):
            raise ValueError(
                "Annihilation recording requires a device+inode log cursor"
            )
        log_path = _project_path(root, args.log)
        text = _read_log_cursor(args, log_path)
        proof = extract_annihilation_progress(text)
        if proof is None:
            print(
                "fresh Fight log has no completed Annihilation progress proof",
                file=sys.stderr,
            )
            return 1
        destination = (
            _project_path(root, args.output)
            if args.output
            else _annihilation_state_path(root)
        )
        previous: object = None
        if destination.exists():
            try:
                previous = load_json(destination)
            except (OSError, json.JSONDecodeError):
                previous = None
        previous = valid_annihilation_state(
            previous,
            client=config.client_type,
            account=config.account,
            week=week,
        )
        if previous is not None:
            previous_current = previous.get("current")
            previous_total = previous.get("total")
            if (
                isinstance(previous_total, int)
                and not isinstance(previous_total, bool)
                and previous_total != proof.total
            ):
                raise ValueError("weekly total changed inside one game week")
            if (
                isinstance(previous_current, int)
                and not isinstance(previous_current, bool)
                and proof.current < previous_current
            ):
                raise ValueError("weekly progress regressed inside one game week")

        payload = {
            "schema_version": 1,
            "client": config.client_type,
            "account": config.account,
            "week_start_game_day": week.key,
            "week_start": isoformat(week.start),
            "week_end": isoformat(week.end),
            "status": (
                "complete"
                if proof.complete
                else "unstable"
                if proof.stars == 2
                else "progress"
            ),
            "current": proof.current,
            "total": proof.total,
            "observed_at": isoformat(now),
            "completed_at": isoformat(now) if proof.complete else None,
            "evidence": {
                **proof.as_dict(),
                "log_path": str(log_path),
                "since_byte": args.since_byte,
                "log_device": getattr(args, "log_device", None),
                "log_inode": getattr(args, "log_inode", None),
                "log_was_missing": getattr(args, "log_was_missing", False),
                "log_suffix_sha256": sha256_bytes(
                    text.encode("utf-8", errors="replace")
                ),
                "core_version": current_core_version(root),
            },
        }
        atomic_write_json(destination, payload)
    except (OSError, ValueError, ConfigError, SourceError) as exc:
        print(f"cannot record Annihilation progress: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def command_confirm_annihilation_complete(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    destination = _annihilation_state_path(root)
    try:
        config = _config(root, args.config)
        week = game_week(now)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.week_start_game_day):
            raise ValueError("invalid expected game-week key")
        if args.week_start_game_day != week.key:
            raise ValueError(
                "operator confirmation is not for the current game week "
                f"({args.week_start_game_day} -> {week.key})"
            )

        previous: object = None
        previous_state_sha256: str | None = None
        if destination.exists():
            try:
                previous = load_json(destination)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "existing Annihilation state cannot be read; refusing to overwrite it"
                ) from exc
            valid_previous = valid_annihilation_state(
                previous,
                client=config.client_type,
                account=config.account,
                week=week,
            )
            if valid_previous is not None and valid_previous.get("status") == "complete":
                print(json.dumps(valid_previous, ensure_ascii=False, sort_keys=True))
                return 0
            if valid_previous is None:
                previous_week = (
                    previous.get("week_start_game_day")
                    if isinstance(previous, dict)
                    else None
                )
                if (
                    not isinstance(previous_week, str)
                    or re.fullmatch(r"\d{4}-\d{2}-\d{2}", previous_week) is None
                    or previous_week >= week.key
                ):
                    raise ValueError(
                        "existing current/future Annihilation state is invalid; "
                        "refusing to overwrite it"
                    )
            previous_state_sha256 = sha256_bytes(canonical_json(previous))

        confirmation_id = secrets.token_hex(16)
        payload = operator_annihilation_confirmation(
            now=now,
            client=config.client_type,
            account=config.account,
            expected_week_start_game_day=args.week_start_game_day,
            reason=args.reason,
            confirmation_id=confirmation_id,
            previous_state_sha256=previous_state_sha256,
        )
        stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
        archive = (
            root
            / "var/state/planner/annihilation-confirmations"
            / f"{stamp}-{week.key}-{confirmation_id}.json"
        )
        atomic_write_json(archive, payload)
        atomic_write_json(destination, payload)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"cannot confirm Annihilation completion: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def command_plan(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    now = parse_iso_datetime(args.now).astimezone(UTC) if args.now else utc_now()
    decision: Decision
    config: PlannerConfig | None = None
    try:
        config = _config(root, args.config)
        if config.mode == "off":
            decision = _noop(now, "FARMING_DISABLED")
        else:
            bundle = _run_sync(
                root,
                config,
                offline=args.offline,
                skip_hot_update=args.skip_maa_hot_update,
            )
            inventory_path = (
                _project_path(root, args.inventory) if args.inventory else _inventory_path(root)
            )
            snapshot = load_snapshot(inventory_path)
            if not snapshot.complete:
                raise InventoryError("inventory snapshot is incomplete")
            if snapshot.client not in {None, config.client_type} or snapshot.account not in {None, config.account}:
                raise InventoryError("inventory snapshot belongs to another client/account")
            age = now - snapshot.captured_at.astimezone(UTC)
            if age < timedelta(minutes=-5) or age > timedelta(seconds=config.freshness.inventory_seconds):
                raise InventoryError(f"inventory snapshot age is outside policy: {age}")
            ledger = _load_ledger(root)
            verified: set[tuple[str, str]] = set()
            quarantined: set[tuple[str, str]] = set()
            for record in ledger.entries():
                if record.key.client != config.client_type or record.key.account != config.account:
                    continue
                key = (record.key.activity_instance, record.key.stage)
                if record.status == "verified":
                    verified.add(key)
                elif record.status == "quarantined":
                    quarantined.add(key)
            source_evidence = {
                **bundle.evidence,
                "inventory": {
                    "captured_at": isoformat(snapshot.captured_at),
                    "sha256": sha256_bytes(canonical_json(snapshot.as_dict())),
                    "observed_items": len(snapshot.items),
                },
                "capability_ledger": {
                    "verified_for_account": len(verified),
                    "quarantined_for_account": len(quarantined),
                },
            }
            decision = select_farming_plan(
                now=now,
                activities=bundle.activities,
                efficiencies=bundle.efficiencies,
                inventory=snapshot,
                targets=config.targets,
                default_target=config.policy.default_target,
                blue_material_chains=config.blue_material_chains,
                core_version=current_core_version(root),
                verified_stages=verified,
                quarantined_stages=quarantined,
                navigation_stages=navigation_stages(root),
                end_safety_margin=timedelta(minutes=config.activity.end_safety_margin_minutes),
                when_satisfied=config.policy.when_satisfied,
                configured_series=config.policy.series,
                large_deficit_runs=config.policy.large_deficit_runs,
                medicine=config.policy.medicine,
                medicine_expire_days=config.policy.medicine_expire_days,
                stone=config.policy.stone,
                evidence=source_evidence,
            )
    except ConfigError as exc:
        decision = _noop(now, "CONFIG_INVALID", exc)
    except SourceError as exc:
        decision = _noop(now, "SOURCE_UNAVAILABLE", exc)
    except InventoryError as exc:
        decision = _noop(now, "INVENTORY_UNAVAILABLE", exc)
    except CapabilityLedgerError as exc:
        decision = _noop(now, "CAPABILITY_LEDGER_INVALID", exc)
    except OSError as exc:
        decision = _noop(now, "LOCAL_STATE_UNAVAILABLE", exc)

    _apply_agent_advice(config, decision)

    path = _write_decision(root, decision, args.output)
    _print_decision(decision, path)
    return 0


def command_capabilities(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        ledger = _load_ledger(root)
    except CapabilityLedgerError as exc:
        print(f"cannot load capabilities: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(ledger.as_dict(), ensure_ascii=False, indent=2))
    return 0


def _add_log_cursor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-device", type=int)
    parser.add_argument("--log-inode", type=int)
    parser.add_argument(
        "--log-was-missing",
        action="store_true",
        help="the append-only log did not exist when the operation began",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic MAA farming planner")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config", help="planner TOML path (defaults to config/farming.toml)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="refresh and validate all source data")
    sync.add_argument("--offline", action="store_true", help="validate fresh cached data only")
    sync.add_argument("--skip-maa-hot-update", action="store_true")
    sync.set_defaults(func=command_sync)

    sync_calendar = subparsers.add_parser(
        "sync-calendar",
        help="refresh the MAA activity-window source",
    )
    sync_calendar.add_argument(
        "--offline", action="store_true", help="validate fresh cached data only"
    )
    sync_calendar.set_defaults(func=command_sync_calendar)

    inventory = subparsers.add_parser("inventory-from-log", help="extract a final Depot snapshot")
    inventory.add_argument("--log", required=True)
    inventory.add_argument("--output")
    inventory.add_argument("--captured-at")
    inventory.set_defaults(func=command_inventory)

    select_drones = subparsers.add_parser(
        "select-drones", help="select PureGold or Money from a fresh Depot snapshot"
    )
    select_drones.add_argument("--inventory")
    select_drones.add_argument("--threshold", type=int, default=150)
    select_drones.add_argument("--now")
    select_drones.add_argument("--value-only", action="store_true")
    select_drones.set_defaults(func=command_select_drones)

    render_task = subparsers.add_parser(
        "render-runtime-task",
        help="write one non-interactive task copy with an allowlisted planner value",
    )
    render_task.add_argument("--task", choices=tuple(RUNTIME_PARAMETERS), required=True)
    render_task.add_argument("--value", required=True)
    render_task.add_argument("--output", required=True)
    render_task.set_defaults(func=command_render_runtime_task)

    plan = subparsers.add_parser("plan", help="write an audited FIGHT or NOOP decision")
    plan.add_argument("--offline", action="store_true", help="use fresh source cache only")
    plan.add_argument("--skip-maa-hot-update", action="store_true")
    plan.add_argument("--inventory")
    plan.add_argument("--output")
    plan.add_argument("--now", help="override the clock with an offset-aware ISO timestamp")
    plan.set_defaults(func=command_plan)

    record = subparsers.add_parser("record-fight", help="record a verified proxy fight from a log")
    record.add_argument("--log", required=True)
    record.add_argument("--stage", required=True)
    record.add_argument("--activity-instance")
    record.add_argument(
        "--since-byte",
        type=int,
        default=0,
        help="ignore bytes already present in an append-only MaaCore log",
    )
    _add_log_cursor_arguments(record)
    record.add_argument("--now")
    record.set_defaults(func=command_record_fight)

    quarantine = subparsers.add_parser("quarantine", help="block a stage for the current activity")
    quarantine.add_argument("--stage", required=True)
    quarantine.add_argument("--reason", required=True)
    quarantine.add_argument("--activity-instance")
    quarantine.set_defaults(func=command_quarantine)

    quarantine_fight = subparsers.add_parser(
        "quarantine-fight",
        help="quarantine only from a fresh non-three-star Fight result",
    )
    quarantine_fight.add_argument("--log", required=True)
    quarantine_fight.add_argument("--stage", required=True)
    quarantine_fight.add_argument("--activity-instance", required=True)
    quarantine_fight.add_argument("--since-byte", type=int, default=0)
    _add_log_cursor_arguments(quarantine_fight)
    quarantine_fight.add_argument("--now")
    quarantine_fight.set_defaults(func=command_quarantine_fight)

    check_fight = subparsers.add_parser(
        "check-fight", help="check fresh three-star proof without changing the ledger"
    )
    check_fight.add_argument("--log", required=True)
    check_fight.add_argument("--stage", required=True)
    check_fight.add_argument("--since-byte", type=int, default=0)
    _add_log_cursor_arguments(check_fight)
    check_fight.set_defaults(func=command_check_fight)

    check_proxy = subparsers.add_parser("check-proxy", help="check fresh no-battle proxy screen evidence")
    check_proxy.add_argument("--log", required=True)
    check_proxy.add_argument("--stage", required=True)
    check_proxy.add_argument("--since-byte", type=int, default=0)
    _add_log_cursor_arguments(check_proxy)
    check_proxy.set_defaults(func=command_check_proxy)

    for name in ("proxy-status", "proxy-reset"):
        proxy_state = subparsers.add_parser(name)
        proxy_state.add_argument("--stage", required=True)
        proxy_state.add_argument("--activity-instance")
        proxy_state.set_defaults(func=command_proxy_state)

    reconcile_fight = subparsers.add_parser(
        "reconcile-fight",
        help="classify one fresh Fight suffix and atomically update its activity ledger",
    )
    reconcile_fight.add_argument("--log", required=True)
    reconcile_fight.add_argument("--stage", required=True)
    reconcile_fight.add_argument("--activity-instance", required=True)
    reconcile_fight.add_argument("--since-byte", type=int, default=0)
    _add_log_cursor_arguments(reconcile_fight)
    reconcile_fight.add_argument("--now")
    reconcile_fight.set_defaults(func=command_reconcile_fight)

    annihilation_plan = subparsers.add_parser(
        "plan-annihilation",
        help="plan the weekly Annihilation slot from activity windows",
    )
    annihilation_plan.add_argument("--offline", action="store_true")
    annihilation_plan.add_argument("--skip-maa-hot-update", action="store_true")
    annihilation_plan.add_argument("--output")
    annihilation_plan.add_argument("--now")
    annihilation_plan.set_defaults(func=command_plan_annihilation)

    record_annihilation = subparsers.add_parser(
        "record-annihilation",
        help="record client-observed weekly Annihilation progress",
    )
    record_annihilation.add_argument("--log", required=True)
    record_annihilation.add_argument("--since-byte", type=int, default=0)
    _add_log_cursor_arguments(record_annihilation)
    record_annihilation.add_argument("--week-start-game-day", required=True)
    record_annihilation.add_argument("--now")
    record_annihilation.add_argument("--output")
    record_annihilation.set_defaults(func=command_record_annihilation)

    confirm_annihilation = subparsers.add_parser(
        "confirm-annihilation-complete",
        help="record an explicit operator confirmation for the current game week",
    )
    confirm_annihilation.add_argument("--week-start-game-day", required=True)
    confirm_annihilation.add_argument("--reason", required=True)
    confirm_annihilation.add_argument("--now")
    confirm_annihilation.set_defaults(func=command_confirm_annihilation_complete)

    capabilities = subparsers.add_parser("capabilities", help="show the account capability ledger")
    capabilities.set_defaults(func=command_capabilities)

    advisor_check = subparsers.add_parser(
        "advisor-check",
        help="run and archive one bounded real-model connectivity probe",
    )
    advisor_check.add_argument("--now", help="override the audit clock with an ISO timestamp")
    advisor_check.set_defaults(func=command_advisor_check)

    supervisor_check = subparsers.add_parser(
        "supervisor-check",
        help="run one synthetic exception through the read-only LLM supervisor",
    )
    supervisor_check.add_argument(
        "--now", help="override the audit clock with an ISO timestamp"
    )
    supervisor_check.set_defaults(func=command_supervisor_check)

    supervisor_start = subparsers.add_parser(
        "supervisor-start", help="start an append-only launcher phase audit"
    )
    supervisor_start.add_argument("--mode", choices=tuple(RUN_MODES), required=True)
    supervisor_start.set_defaults(func=command_supervisor_start)

    supervisor_phase = subparsers.add_parser(
        "supervisor-phase", help="append one terminal phase result to a launcher audit"
    )
    supervisor_phase.add_argument("--run-id", required=True)
    supervisor_phase.add_argument("--phase", choices=PHASES, required=True)
    supervisor_phase.add_argument(
        "--result", choices=tuple(sorted(PHASE_RESULTS)), required=True
    )
    supervisor_phase.add_argument("--outcome", required=True)
    supervisor_phase.add_argument("--detail", action="append", default=[])
    supervisor_phase.add_argument("--evidence-file", action="append", default=[])
    supervisor_phase.set_defaults(func=command_supervisor_phase)

    supervisor_finish = subparsers.add_parser(
        "supervisor-finish",
        help="close a phase audit and diagnose only if deterministic checks failed",
    )
    supervisor_finish.add_argument("--run-id", required=True)
    supervisor_finish.add_argument("--process-status", type=int, required=True)
    supervisor_finish.add_argument(
        "--defer-to-recovery",
        action="store_true",
        help="record a failed full run without invoking the read-only classifier",
    )
    supervisor_finish.set_defaults(func=command_supervisor_finish)

    supervisor_recover = subparsers.add_parser(
        "supervisor-recover-run",
        help="repair one failed full run and independently verify a new complete run",
    )
    supervisor_recover.add_argument("--run-id", required=True)
    supervisor_recover.add_argument(
        "--slot", choices=("pre-reset", "post-reset", "manual"), required=True
    )
    supervisor_recover.set_defaults(func=command_supervisor_recover_run)

    validate = subparsers.add_parser("validate-config", help="validate planner configuration")
    validate.set_defaults(func=lambda args: command_validate(args))
    material_recipes = subparsers.add_parser(
        "validate-material-recipes",
        help="cross-check blue-material recipes with one MAA item index",
    )
    material_recipes.add_argument("--item-index", required=True)
    material_recipes.set_defaults(func=command_validate_material_recipes)
    runtime = subparsers.add_parser(
        "validate-runtime-contracts",
        help="validate the static MAA Fight tasks against spending policy",
    )
    runtime.set_defaults(func=command_validate_runtime_contracts)
    service = subparsers.add_parser(
        "validate-service-contracts",
        help="validate managed daily and final Award-only task isolation",
    )
    service.set_defaults(func=command_validate_service_contracts)
    fingerprint = subparsers.add_parser(
        "runtime-fingerprint",
        help="print the exact live runtime/config generation fingerprint",
    )
    fingerprint.set_defaults(func=command_runtime_fingerprint)
    readiness = subparsers.add_parser(
        "validate-service-readiness",
        help="validate static contracts and the promoted runtime receipt without MaaCore",
    )
    readiness.add_argument("--value-only", action="store_true")
    readiness.set_defaults(func=command_validate_service_readiness)
    return parser


def command_validate(args: argparse.Namespace) -> int:
    try:
        _config(Path(args.project_root).resolve(), args.config)
    except ConfigError as exc:
        print(f"invalid planner configuration: {exc}", file=sys.stderr)
        return 1
    print("planner configuration is valid")
    return 0


def command_validate_material_recipes(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        config = _config(root, args.config)
        validate_blue_material_chains_against_item_index(
            config.blue_material_chains,
            _project_path(root, args.item_index),
        )
    except (ConfigError, MaterialRecipeError) as exc:
        print(f"invalid blue-material recipe contract: {exc}", file=sys.stderr)
        return 1
    print("blue-material recipes match the MAA item index")
    return 0


def command_validate_runtime_contracts(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        validate_runtime_contracts(root)
    except RuntimeContractError as exc:
        print(f"invalid runtime execution contract: {exc}", file=sys.stderr)
        return 1
    print("runtime execution contracts are valid")
    return 0


def command_validate_service_contracts(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        validate_service_contracts(root)
    except RuntimeContractError as exc:
        print(f"invalid service execution contract: {exc}", file=sys.stderr)
        return 1
    print("service execution contracts are valid")
    return 0


def command_runtime_fingerprint(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        fingerprint = runtime_generation_fingerprint(root)
    except (OSError, RuntimeReceiptError) as exc:
        print(f"cannot fingerprint live runtime generation: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True))
    return 0


def command_validate_service_readiness(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    try:
        validate_service_contracts(root)
        receipt_mode = validate_runtime_receipt(root)
    except (OSError, RuntimeContractError, RuntimeReceiptError) as exc:
        print(f"service readiness validation failed: {exc}", file=sys.stderr)
        return 1
    farming_ready = True
    try:
        validate_farming_contracts(root)
    except (OSError, RuntimeContractError) as exc:
        farming_ready = False
        print(
            f"optional farming contracts are invalid; daily remains runnable: {exc}",
            file=sys.stderr,
        )
    if args.value_only:
        print(f"{receipt_mode}\t{'true' if farming_ready else 'false'}")
    else:
        suffix = "farming-ready" if farming_ready else "farming-disabled"
        print(f"service readiness is valid ({receipt_mode}, {suffix})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
