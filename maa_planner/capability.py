from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from .util import atomic_write_json, canonical_json, isoformat, parse_iso_datetime, utc_now


LEDGER_SCHEMA_VERSION = 2
CapabilityStatus = Literal["unknown", "verified", "retry", "quarantined"]
FightOutcome = Literal["verified", "retry", "quarantined", "unknown"]
QuarantineKind = Literal["automatic", "manual"]
REGULAR_FALLBACK_ACTIVITY_INSTANCE = hashlib.sha256(
    b"regular-fallback-v1"
).hexdigest()[:24]
_CHAIN_START = "TaskChainStart"
_CHAIN_COMPLETIONS = frozenset({"TaskChainCompleted", "AllTasksCompleted"})
_CHAIN_FAILURES = frozenset(
    {"TaskChainError", "TaskChainStopped", "TaskChainFailed", "AllTasksStopped"}
)
_EVENT_LABELS = (
    "TaskChainCompleted",
    "AllTasksCompleted",
    "TaskChainStopped",
    "TaskChainFailed",
    "TaskChainError",
    "AllTasksStopped",
    "TaskChainStart",
    "SubTaskCompleted",
    "SubTaskStart",
    "SubTaskError",
)
_RELEVANT_LOG_MARKERS = _EVENT_LABELS + (
    "StageDrops",
    "SubTaskExtraInfo",
    "append_callback",
)


class CapabilityError(ValueError):
    """Base class for invalid capability evidence or persistence data."""


class CapabilityLedgerError(CapabilityError):
    """Raised when a capability ledger cannot be trusted."""


def _validate_key_part(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CapabilityError(f"{field} must be a non-empty trimmed string")
    if len(value) > 256 or any(ord(character) < 32 for character in value):
        raise CapabilityError(f"invalid {field}")
    return value


def _validate_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapabilityError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _parse_time(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise CapabilityLedgerError(f"{field} must be an ISO-8601 string")
    try:
        return parse_iso_datetime(value).astimezone(UTC)
    except (TypeError, ValueError) as error:
        raise CapabilityLedgerError(f"invalid {field}") from error


def _validate_json_value(value: Any, path: str = "evidence") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CapabilityError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CapabilityError(f"{path} contains a non-string key")
            _validate_json_value(child, f"{path}.{key}")
        return
    raise CapabilityError(f"{path} is not JSON-serializable")


def _copy_evidence(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CapabilityError("evidence must be a JSON object")
    _validate_json_value(value)
    return MappingProxyType(copy.deepcopy(value))


@dataclass(frozen=True, order=True)
class CapabilityKey:
    client: str
    account: str
    activity_instance: str
    stage: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "client", _validate_key_part(self.client, "client"))
        object.__setattr__(self, "account", _validate_key_part(self.account, "account"))
        object.__setattr__(
            self,
            "activity_instance",
            _validate_key_part(self.activity_instance, "activity_instance"),
        )
        object.__setattr__(self, "stage", _validate_key_part(self.stage, "stage"))


@dataclass(frozen=True)
class CapabilityRecord:
    key: CapabilityKey
    status: CapabilityStatus
    updated_at: datetime
    verified_at: datetime | None = None
    quarantined_at: datetime | None = None
    quarantine_reason: str | None = None
    quarantine_kind: QuarantineKind | None = None
    consecutive_failures: int = 0
    failure_run_id: str | None = None
    success_count: int = 0
    processed_observations: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.status not in ("unknown", "verified", "retry", "quarantined"):
            raise CapabilityError(f"invalid capability status: {self.status!r}")
        if not isinstance(self.success_count, int) or isinstance(self.success_count, bool):
            raise CapabilityError("success_count must be an integer")
        if self.success_count < 0:
            raise CapabilityError("success_count must be non-negative")
        if not isinstance(self.consecutive_failures, int) or isinstance(
            self.consecutive_failures, bool
        ):
            raise CapabilityError("consecutive_failures must be an integer")
        if self.consecutive_failures < 0:
            raise CapabilityError("consecutive_failures must be non-negative")
        if not isinstance(self.processed_observations, tuple):
            raise CapabilityError("processed_observations must be a tuple")
        if any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in self.processed_observations
        ):
            raise CapabilityError("processed_observations contains an invalid identity")
        if len(set(self.processed_observations)) != len(self.processed_observations):
            raise CapabilityError("processed_observations contains a duplicate identity")

        updated_at = _validate_time(self.updated_at, "updated_at")
        verified_at = (
            _validate_time(self.verified_at, "verified_at")
            if self.verified_at is not None
            else None
        )
        quarantined_at = (
            _validate_time(self.quarantined_at, "quarantined_at")
            if self.quarantined_at is not None
            else None
        )
        reason = self.quarantine_reason
        quarantine_kind = self.quarantine_kind

        if self.failure_run_id is not None and (
            not isinstance(self.failure_run_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.failure_run_id) is None
        ):
            raise CapabilityError("invalid failure run ID")
        if self.status == "unknown" and self.consecutive_failures != 0:
            raise CapabilityError("unknown capability cannot have a failure streak")
        if self.status == "verified":
            if verified_at is None or self.success_count < 1:
                raise CapabilityError("verified capability requires success evidence")
            if self.consecutive_failures != 0:
                raise CapabilityError("verified capability cannot have a failure streak")
        if self.status == "retry" and not 1 <= self.consecutive_failures < 3:
            raise CapabilityError("retry capability requires one or two failures")
        if self.status == "quarantined":
            if quarantined_at is None:
                raise CapabilityError("quarantined capability requires a timestamp")
            if not isinstance(reason, str) or not reason or reason != reason.strip():
                raise CapabilityError("quarantined capability requires a reason")
            if quarantine_kind not in ("automatic", "manual"):
                raise CapabilityError("quarantined capability requires a quarantine kind")
            if quarantine_kind == "automatic" and self.consecutive_failures < 3:
                raise CapabilityError(
                    "automatic quarantine requires three consecutive failures"
                )
        elif any(
            value is not None for value in (reason, quarantined_at, quarantine_kind)
        ):
            raise CapabilityError(
                "non-quarantined capability cannot have quarantine metadata"
            )

        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "quarantined_at", quarantined_at)
        object.__setattr__(self, "evidence", _copy_evidence(dict(self.evidence)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.key.client,
            "account": self.key.account,
            "activity_instance": self.key.activity_instance,
            "stage": self.key.stage,
            "status": self.status,
            "updated_at": isoformat(self.updated_at),
            "verified_at": isoformat(self.verified_at) if self.verified_at else None,
            "quarantined_at": (
                isoformat(self.quarantined_at) if self.quarantined_at else None
            ),
            "quarantine_reason": self.quarantine_reason,
            "quarantine_kind": self.quarantine_kind,
            "consecutive_failures": self.consecutive_failures,
            "failure_run_id": self.failure_run_id,
            "success_count": self.success_count,
            "processed_observations": list(self.processed_observations),
            "evidence": copy.deepcopy(dict(self.evidence)),
        }

    @classmethod
    def from_dict(cls, value: object) -> "CapabilityRecord":
        if not isinstance(value, dict):
            raise CapabilityLedgerError("ledger entry must be a JSON object")
        try:
            key = CapabilityKey(
                client=value["client"],
                account=value["account"],
                activity_instance=value["activity_instance"],
                stage=value["stage"],
            )
            status = value["status"]
            updated_at = _parse_time(value["updated_at"], "updated_at")
            verified_at = _parse_time(
                value.get("verified_at"), "verified_at", optional=True
            )
            quarantined_at = _parse_time(
                value.get("quarantined_at"), "quarantined_at", optional=True
            )
            if not isinstance(value.get("processed_observations"), list):
                raise CapabilityLedgerError("processed_observations must be a list")
            return cls(
                key=key,
                status=status,
                updated_at=updated_at,
                verified_at=verified_at,
                quarantined_at=quarantined_at,
                quarantine_reason=value.get("quarantine_reason"),
                quarantine_kind=value.get("quarantine_kind"),
                consecutive_failures=value["consecutive_failures"],
                failure_run_id=value.get("failure_run_id"),
                success_count=value["success_count"],
                processed_observations=tuple(value["processed_observations"]),
                evidence=value.get("evidence", {}),
            )
        except KeyError as error:
            raise CapabilityLedgerError(f"ledger entry is missing {error.args[0]!r}") from error
        except CapabilityError as error:
            if isinstance(error, CapabilityLedgerError):
                raise
            raise CapabilityLedgerError("invalid ledger entry") from error


@dataclass(frozen=True)
class FightProof:
    stage_code: str
    stage_id: str | None
    task_id: int | str | None
    uuid: str | None
    completed_by: Literal["TaskChainCompleted", "AllTasksCompleted"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "maa-fight-log",
            "stage_code": self.stage_code,
            "stage_id": self.stage_id,
            "task_id": self.task_id,
            "uuid": self.uuid,
            "completed_by": self.completed_by,
            "stars": 3,
        }


@dataclass(frozen=True)
class FightInstabilityProof:
    stage_code: str
    stage_id: str | None
    task_id: int | str
    uuid: str
    stars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "maa-fight-log",
            "reason": "non-three-star",
            "stage_code": self.stage_code,
            "stage_id": self.stage_id,
            "task_id": self.task_id,
            "uuid": self.uuid,
            "stars": self.stars,
        }


@dataclass(frozen=True)
class FightObservation:
    """One explicit target-stage result in its original log order.

    Settled result-screen observations are independent of a later navigation
    error. They do not by themselves prove that the launcher run completed.
    ``event_byte_offset`` points at the callback JSON object within the supplied
    log suffix; callers add the suffix's absolute byte cursor when deriving the
    persistent identity.
    """

    stage_code: str
    stage_id: str | None
    task_id: int | str
    uuid: str
    stars: Literal[0, 2, 3]
    event_byte_offset: int
    event_prefix_sha256: str
    completed_by: Literal["TaskChainCompleted", "AllTasksCompleted"] | None = None

    def __post_init__(self) -> None:
        if self.stars not in (0, 2, 3):
            raise CapabilityError("invalid fight observation stars")
        if self.stars == 2 and self.completed_by is not None:
            raise CapabilityError("two-star observation cannot claim Fight completion")
        if not isinstance(self.event_byte_offset, int) or isinstance(
            self.event_byte_offset, bool
        ):
            raise CapabilityError("event_byte_offset must be an integer")
        if self.event_byte_offset < 0:
            raise CapabilityError("event_byte_offset must be non-negative")
        if re.fullmatch(r"[0-9a-f]{64}", self.event_prefix_sha256) is None:
            raise CapabilityError("invalid event prefix digest")

    def observation_id(
        self,
        *,
        log_device: int,
        log_inode: int,
        suffix_start_byte: int,
    ) -> str:
        for value, field in (
            (log_device, "log_device"),
            (log_inode, "log_inode"),
            (suffix_start_byte, "suffix_start_byte"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CapabilityError(f"{field} must be a non-negative integer")
        # The inode and absolute callback offset are stable when callers replay
        # the same suffix or submit a larger overlapping suffix.  Callback
        # identity fields make accidental offset reuse after sparse-file edits
        # fail closed.  Prefix/timestamp evidence is retained in ``as_dict`` but
        # deliberately excluded here so a cursor beginning at ``{`` still
        # deduplicates the same callback.
        return hashlib.sha256(
            canonical_json(
                {
                    "log_device": log_device,
                    "log_inode": log_inode,
                    "event_byte_offset": suffix_start_byte
                    + self.event_byte_offset,
                    "uuid": self.uuid,
                    "task_id": self.task_id,
                    "stage_code": self.stage_code,
                    "stage_id": self.stage_id,
                    "stars": self.stars,
                }
            )
        ).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": "maa-fight-log",
            "stage_code": self.stage_code,
            "stage_id": self.stage_id,
            "task_id": self.task_id,
            "uuid": self.uuid,
            "stars": self.stars,
            "event_byte_offset": self.event_byte_offset,
            "event_prefix_sha256": self.event_prefix_sha256,
        }
        if self.stars != 3:
            result["reason"] = "mission-failed" if self.stars == 0 else "non-three-star"
        else:
            result["completed_by"] = self.completed_by
        return result


@dataclass(frozen=True)
class FightReconciliation:
    outcome: FightOutcome
    recorded: bool
    consecutive_failures: int
    observations: int


@dataclass(frozen=True)
class AnnihilationProgressProof:
    stage_code: str | None
    task_id: int | str
    uuid: str
    current: int
    total: int
    # MaaCore v6.16.x emits 0 when star-template OCR is inconclusive, 2 for an
    # explicit non-three-star result, and 3 for a confirmed three-star result.
    # Weekly progress is independently OCRed and remains authoritative for all
    # three values.
    stars: Literal[0, 2, 3]
    completed_by: Literal["TaskChainCompleted", "AllTasksCompleted"]

    @property
    def complete(self) -> bool:
        return self.current == self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "maa-annihilation-log",
            "stage_code": self.stage_code,
            "task_id": self.task_id,
            "uuid": self.uuid,
            "current": self.current,
            "total": self.total,
            "stars": self.stars,
            "complete": self.complete,
            "completed_by": self.completed_by,
        }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


_STRICT_DECODER = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)


def _iter_log_records_with_offsets(
    log_text: str,
) -> Iterator[tuple[str | None, dict[str, Any], int, str]]:
    cursor = 0
    text_length = len(log_text)
    while cursor < text_length:
        line_end = log_text.find("\n", cursor)
        if line_end < 0:
            line_end = text_length
        line = log_text[cursor:line_end]
        opening_brace = line.find("{")
        starts_pretty_object = opening_brace >= 0 and not line[opening_brace + 1 :].strip()
        if not starts_pretty_object and not any(
            marker in line for marker in _RELEVANT_LOG_MARKERS
        ):
            cursor = line_end + 1
            continue
        object_start = log_text.find("{", cursor, line_end + 1)
        if object_start < 0:
            cursor = line_end + 1
            continue

        prefix = log_text[cursor:object_start]
        try:
            value, object_end = _STRICT_DECODER.raw_decode(log_text, object_start)
        except (json.JSONDecodeError, CapabilityError):
            cursor = line_end + 1
            continue
        cursor = max(object_end, line_end + 1)
        if not isinstance(value, dict):
            continue

        label: str | None = None
        for candidate in _EVENT_LABELS:
            if candidate in prefix:
                label = candidate
                break
        if label is None:
            for field in ("type", "event", "message"):
                if value.get(field) in _EVENT_LABELS:
                    label = value[field]
                    break
        byte_offset = len(
            log_text[:object_start].encode("utf-8", errors="replace")
        )
        yield label, value, byte_offset, prefix


def _iter_log_records(log_text: str) -> Iterator[tuple[str | None, dict[str, Any]]]:
    for label, value, _byte_offset, _prefix in _iter_log_records_with_offsets(
        log_text
    ):
        yield label, value


def _event_body(value: dict[str, Any]) -> dict[str, Any]:
    # Wrappers commonly put the MaaCore callback object in ``details``.
    details = value.get("details")
    if (
        isinstance(details, dict)
        and details.get("what") == "StageDrops"
        and value.get("what") != "StageDrops"
    ):
        merged = dict(details)
        for field in ("taskchain", "taskid", "uuid"):
            if field not in merged and field in value:
                merged[field] = value[field]
        return merged
    return value


def _chain_body(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("taskchain") is not None:
        return value
    details = value.get("details")
    if not isinstance(details, dict):
        return value
    merged = dict(details)
    for field in ("taskchain", "taskid", "uuid"):
        if field not in merged and field in value:
            merged[field] = value[field]
    return merged


def _execution_key(value: dict[str, Any]) -> tuple[object, object]:
    return value.get("uuid"), value.get("taskid")


def _stage_proof(value: dict[str, Any], expected_stage: str | None) -> FightProof | None:
    body = _event_body(value)
    if body.get("what") != "StageDrops" or body.get("taskchain") != "Fight":
        return None
    uuid = body.get("uuid")
    task_id = body.get("taskid")
    if not isinstance(uuid, str) or not uuid:
        return None
    if (
        isinstance(task_id, bool)
        or not isinstance(task_id, (int, str))
        or (isinstance(task_id, str) and not task_id)
    ):
        return None
    details = body.get("details")
    if not isinstance(details, dict):
        return None
    stars = details.get("stars")
    if isinstance(stars, bool) or not isinstance(stars, int) or stars != 3:
        return None
    stage = details.get("stage")
    if not isinstance(stage, dict):
        return None
    stage_code = stage.get("stageCode")
    if not isinstance(stage_code, str) or not stage_code:
        return None
    if expected_stage is not None and stage_code != expected_stage:
        return None
    stage_id = stage.get("stageId")
    if stage_id is not None and not isinstance(stage_id, str):
        return None
    return FightProof(
        stage_code=stage_code,
        stage_id=stage_id,
        task_id=task_id,
        uuid=uuid,
        completed_by="TaskChainCompleted",  # Replaced when completion is observed.
    )


def _stage_instability(
    value: dict[str, Any], expected_stage: str | None
) -> FightInstabilityProof | None:
    body = _event_body(value)
    if body.get("what") != "StageDrops" or body.get("taskchain") != "Fight":
        return None
    uuid = body.get("uuid")
    task_id = body.get("taskid")
    if not isinstance(uuid, str) or not uuid:
        return None
    if (
        isinstance(task_id, bool)
        or not isinstance(task_id, (int, str))
        or (isinstance(task_id, str) and not task_id)
    ):
        return None
    details = body.get("details")
    if not isinstance(details, dict):
        return None
    stars = details.get("stars")
    # Only an explicit result-screen observation can create the higher-priority
    # negative override.  Missing proxy buttons, navigation errors and invalid
    # OCR remain unknown and are never persisted as instability.
    if isinstance(stars, bool) or not isinstance(stars, int) or stars != 2:
        return None
    stage = details.get("stage")
    if not isinstance(stage, dict):
        return None
    stage_code = stage.get("stageCode")
    if not isinstance(stage_code, str) or not stage_code:
        return None
    if expected_stage is not None and stage_code != expected_stage:
        return None
    stage_id = stage.get("stageId")
    if stage_id is not None and not isinstance(stage_id, str):
        return None
    return FightInstabilityProof(
        stage_code=stage_code,
        stage_id=stage_id,
        task_id=task_id,
        uuid=uuid,
        stars=stars,
    )


def extract_successful_fight(
    log_text: str, expected_stage: str | None = None
) -> FightProof | None:
    """Return proof only for a three-star drop followed by Fight completion.

    Records are associated by ``uuid`` and ``taskid`` and consumed in log order.
    A new start or an error clears older pending evidence for that execution key.
    """

    if not isinstance(log_text, str):
        raise TypeError("log_text must be a string")
    if expected_stage is not None:
        _validate_key_part(expected_stage, "expected_stage")

    pending: dict[tuple[object, object], FightProof] = {}
    invalid_executions: set[tuple[object, object]] = set()
    for label, raw_value in _iter_log_records(log_text):
        value = _event_body(raw_value)
        chain = _chain_body(value)
        key = _execution_key(chain)

        if label == _CHAIN_START and chain.get("taskchain") == "Fight":
            pending.pop(key, None)
            invalid_executions.discard(key)
            continue

        if value.get("what") == "StageDrops" and value.get("taskchain") == "Fight":
            drop_key = _execution_key(value)
            proof = _stage_proof(value, expected_stage)
            if proof is None:
                pending.pop(drop_key, None)
                invalid_executions.add(drop_key)
            elif drop_key not in invalid_executions:
                pending[drop_key] = proof
            continue

        if label in _CHAIN_FAILURES and chain.get("taskchain") == "Fight":
            pending.pop(key, None)
            invalid_executions.discard(key)
            continue

        if label in _CHAIN_COMPLETIONS and chain.get("taskchain") == "Fight":
            proof = pending.pop(key, None)
            invalid = key in invalid_executions
            invalid_executions.discard(key)
            if proof is not None and not invalid:
                return FightProof(
                    stage_code=proof.stage_code,
                    stage_id=proof.stage_id,
                    task_id=proof.task_id,
                    uuid=proof.uuid,
                    completed_by=label,
                )
    return None


def fight_succeeded(log_text: str, expected_stage: str | None = None) -> bool:
    return extract_successful_fight(log_text, expected_stage) is not None


def extract_unstable_fight(
    log_text: str, expected_stage: str | None = None
) -> FightInstabilityProof | None:
    """Return only strong evidence that a selected proxy produced <3 stars."""

    if not isinstance(log_text, str):
        raise TypeError("log_text must be a string")
    if expected_stage is not None:
        _validate_key_part(expected_stage, "expected_stage")
    for _label, value in _iter_log_records(log_text):
        proof = _stage_instability(value, expected_stage)
        if proof is not None:
            return proof
    return None


def extract_fight_observations(
    log_text: str, expected_stage: str
) -> tuple[FightObservation, ...]:
    """Read settled battle outcomes, not whole-run health.

    A real three-star result resets the streak even if subsequent navigation
    fails. OCR stars=0 remains unknown unless the same execution also explicitly
    recognized the mission-failed screen. Refund-only drops alone are not proof.
    """
    _validate_key_part(expected_stage, "expected_stage")
    failed_screen: set[tuple[object, object]] = set()
    observations: list[FightObservation] = []
    for label, raw_value, byte_offset, prefix in _iter_log_records_with_offsets(log_text):
        value = _event_body(raw_value)
        if value.get("taskchain") != "Fight":
            continue
        key = _execution_key(value)
        details = value.get("details")
        if not isinstance(details, dict):
            details = {}
        if (label == _CHAIN_START or label in _CHAIN_FAILURES
                or label == "SubTaskError" or value.get("what") == "GameOffline"):
            failed_screen.discard(key)
        if label == "SubTaskCompleted" and str(details.get("task", "")).split("@")[-1] in {
            "FightMissionFailed", "FightMissionFailedAndStop",
        }:
            failed_screen.add(key)
        if value.get("what") != "StageDrops":
            continue
        success = _stage_proof(value, expected_stage)
        failure = _stage_instability(value, expected_stage)
        stars = details.get("stars")
        if type(stars) is int and stars == 0 and key in failed_screen:
            # Reuse the strict stage/execution identity validation.
            classified = copy.deepcopy(value)
            classified["details"]["stars"] = 2
            failure = _stage_instability(classified, expected_stage)
        failed_screen.discard(key)
        proof = success or failure
        if proof is None:
            continue
        observations.append(FightObservation(
            stage_code=proof.stage_code, stage_id=proof.stage_id,
            task_id=proof.task_id, uuid=proof.uuid,
            stars=stars, event_byte_offset=byte_offset,
            event_prefix_sha256=hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
        ))
    return tuple(observations)


def extract_annihilation_progress(log_text: str) -> AnnihilationProgressProof | None:
    """Return client-observed weekly progress followed by Fight completion.

    A normal three-star Annihilation result is not enough to mark the week
    complete.  The proof carries MaaCore's independent OCR of ``current /
    total`` so the caller can distinguish partial progress from the weekly cap
    without a hard-coded Orundum limit.  A star value of 0 is an OCR-unknown
    sentinel in MaaCore and does not invalidate the weekly counter; an explicit
    value of 2 is retained so the launcher can stop further transactions.
    """

    if not isinstance(log_text, str):
        raise TypeError("log_text must be a string")

    pending: dict[tuple[object, object], AnnihilationProgressProof] = {}
    invalid_executions: set[tuple[object, object]] = set()
    for label, raw_value in _iter_log_records(log_text):
        value = _event_body(raw_value)
        chain = _chain_body(value)
        key = _execution_key(chain)

        if label == _CHAIN_START and chain.get("taskchain") == "Fight":
            pending.pop(key, None)
            invalid_executions.discard(key)
            continue

        if value.get("what") == "StageDrops" and value.get("taskchain") == "Fight":
            drop_key = _execution_key(value)
            details = value.get("details")
            uuid, task_id = drop_key
            valid_identity = (
                isinstance(uuid, str)
                and bool(uuid)
                and not isinstance(task_id, bool)
                and isinstance(task_id, (int, str))
                and (not isinstance(task_id, str) or bool(task_id))
            )
            progress = (
                details.get("annihilation_weekly_process")
                if isinstance(details, dict)
                else None
            )
            stars = details.get("stars") if isinstance(details, dict) else None
            valid_progress = (
                isinstance(progress, list)
                and len(progress) == 2
                and all(
                    isinstance(item, int) and not isinstance(item, bool)
                    for item in progress
                )
                and 0 <= progress[0] <= 1_000_000
                and 0 < progress[1] <= 1_000_000
                and progress[0] <= progress[1]
            )
            valid_stars = (
                isinstance(stars, int)
                and not isinstance(stars, bool)
                and stars in {0, 2, 3}
            )
            if not valid_identity or not valid_stars or not valid_progress:
                pending.pop(drop_key, None)
                invalid_executions.add(drop_key)
                continue

            stage = details.get("stage")
            stage_code = stage.get("stageCode") if isinstance(stage, dict) else None
            if stage_code is not None and not isinstance(stage_code, str):
                pending.pop(drop_key, None)
                invalid_executions.add(drop_key)
                continue
            assert isinstance(uuid, str)
            assert isinstance(task_id, (int, str)) and not isinstance(task_id, bool)
            assert stars in {0, 2, 3}
            pending[drop_key] = AnnihilationProgressProof(
                stage_code=stage_code or None,
                task_id=task_id,
                uuid=uuid,
                current=progress[0],
                total=progress[1],
                stars=stars,
                completed_by="TaskChainCompleted",
            )
            continue

        if label in _CHAIN_FAILURES and chain.get("taskchain") == "Fight":
            pending.pop(key, None)
            invalid_executions.discard(key)
            continue

        if label in _CHAIN_COMPLETIONS and chain.get("taskchain") == "Fight":
            proof = pending.pop(key, None)
            invalid = key in invalid_executions
            invalid_executions.discard(key)
            if proof is not None and not invalid:
                return AnnihilationProgressProof(
                    stage_code=proof.stage_code,
                    task_id=proof.task_id,
                    uuid=proof.uuid,
                    current=proof.current,
                    total=proof.total,
                    stars=proof.stars,
                    completed_by=label,
                )
    return None


class CapabilityLedger:
    """Account- and activity-instance-scoped proxy-play capability ledger."""

    def __init__(self, records: Iterator[CapabilityRecord] | None = None) -> None:
        self._run_id: str | None = None
        self._records: dict[CapabilityKey, CapabilityRecord] = {}
        for record in records or ():
            if record.key in self._records:
                raise CapabilityLedgerError(f"duplicate capability key: {record.key}")
            self._records[record.key] = record

    def for_run(self, run_id: str) -> "CapabilityLedger":
        """Expire automatic failures from other runs without discarding evidence."""
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id) is None:
            raise CapabilityError("invalid failure run ID")
        self._run_id = run_id
        for key, record in self._records.items():
            if (record.failure_run_id != run_id
                    and (record.status == "retry" or record.quarantine_kind == "automatic")):
                self._records[key] = replace(
                    record, status="unknown", consecutive_failures=0,
                    failure_run_id=run_id, quarantined_at=None,
                    quarantine_reason=None, quarantine_kind=None,
                )
        return self

    def query(self, key: CapabilityKey) -> CapabilityRecord | None:
        return self._records.get(key)

    def status(self, key: CapabilityKey) -> CapabilityStatus:
        record = self.query(key)
        return record.status if record is not None else "unknown"

    def is_verified(self, key: CapabilityKey) -> bool:
        return self.status(key) == "verified"

    def entries(self) -> tuple[CapabilityRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def mark_verified(
        self,
        key: CapabilityKey,
        *,
        evidence: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> CapabilityRecord:
        now = _validate_time(observed_at or utc_now(), "observed_at")
        previous = self.query(key)
        record = CapabilityRecord(
            key=key,
            status="verified",
            updated_at=now,
            verified_at=now,
            quarantined_at=None,
            quarantine_reason=None,
            success_count=(previous.success_count if previous else 0) + 1,
            processed_observations=previous.processed_observations if previous else (),
            evidence=evidence or {},
        )
        self._records[key] = record
        return record

    def mark_verified_from_log(
        self,
        key: CapabilityKey,
        log_text: str,
        *,
        evidence: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> bool:
        proof = extract_successful_fight(log_text, key.stage)
        if proof is None:
            return False
        combined_evidence = proof.as_dict()
        if evidence:
            combined_evidence["context"] = copy.deepcopy(evidence)
        self.mark_verified(
            key, evidence=combined_evidence, observed_at=observed_at
        )
        return True

    def mark_quarantined(
        self,
        key: CapabilityKey,
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> CapabilityRecord:
        reason = _validate_key_part(reason, "quarantine reason")
        now = _validate_time(observed_at or utc_now(), "observed_at")
        previous = self.query(key)
        record = CapabilityRecord(
            key=key,
            status="quarantined",
            updated_at=now,
            verified_at=previous.verified_at if previous else None,
            quarantined_at=now,
            quarantine_reason=reason,
            quarantine_kind="manual",
            consecutive_failures=previous.consecutive_failures if previous else 0,
            processed_observations=previous.processed_observations if previous else (),
            success_count=previous.success_count if previous else 0,
            evidence=evidence or {},
        )
        self._records[key] = record
        return record

    def reset(self, key: CapabilityKey) -> bool:
        return self._records.pop(key, None) is not None

    def reconcile(
        self, key: CapabilityKey, observations: tuple[FightObservation, ...], *,
        log_device: int, log_inode: int, suffix_start_byte: int,
        observed_at: datetime | None = None,
    ) -> FightReconciliation:
        now = _validate_time(observed_at or utc_now(), "observed_at")
        previous = self.query(key)
        seen = set(previous.processed_observations if previous else ())
        was_quarantined = previous is not None and previous.status == "quarantined"
        recorded = 0
        outcome: FightOutcome = "unknown"
        for observation in observations:
            identity = observation.observation_id(
                log_device=log_device, log_inode=log_inode,
                suffix_start_byte=suffix_start_byte,
            )
            if identity in seen:
                continue
            # Once the threshold is reached, stop for this run. A new run
            # expires automatic blocks; replayed evidence cannot undo a block.
            if was_quarantined:
                outcome = "quarantined"
                break
            seen.add(identity)
            failures = 0 if observation.stars == 3 else (
                previous.consecutive_failures if previous else 0
            ) + 1
            status = "verified" if failures == 0 else "retry" if failures < 3 else "quarantined"
            previous = CapabilityRecord(
                key=key, status=status, updated_at=now,
                verified_at=now if failures == 0 else previous.verified_at if previous else None,
                success_count=(previous.success_count if previous else 0) + (failures == 0),
                consecutive_failures=failures, failure_run_id=self._run_id,
                quarantined_at=now if failures >= 3 else None,
                quarantine_reason="three-consecutive-proxy-failures" if failures >= 3 else None,
                quarantine_kind="automatic" if failures >= 3 else None,
                processed_observations=tuple(sorted(seen)), evidence=observation.as_dict(),
            )
            self._records[key] = previous
            outcome = status
            recorded += 1
        return FightReconciliation(outcome, recorded > 0,
                                   previous.consecutive_failures if previous else 0, recorded)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "entries": [record.as_dict() for record in self.entries()],
        }

    def save(self, path: str | Path) -> None:
        atomic_write_json(Path(path), self.as_dict())

    @classmethod
    def load(cls, path: str | Path) -> "CapabilityLedger":
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
        except (OSError, json.JSONDecodeError, CapabilityError) as error:
            raise CapabilityLedgerError(f"cannot load capability ledger: {path}") from error
        if not isinstance(value, dict):
            raise CapabilityLedgerError("capability ledger must be a JSON object")
        if type(value.get("schema_version")) is not int or value["schema_version"] not in (1, LEDGER_SCHEMA_VERSION):
            raise CapabilityLedgerError("unsupported capability ledger schema")
        entries = value.get("entries")
        if not isinstance(entries, list):
            raise CapabilityLedgerError("capability ledger entries must be a list")
        if value["schema_version"] == 1:
            for entry in entries:
                if not isinstance(entry, dict):
                    raise CapabilityLedgerError("invalid legacy capability entry")
                entry["processed_observations"] = []
                entry["consecutive_failures"] = 0
                entry["quarantine_kind"] = None
                if entry.get("status") == "quarantined":
                    if entry.get("quarantine_reason") == "proxy-non-three-star":
                        entry.update(status="retry", consecutive_failures=1,
                                     quarantined_at=None, quarantine_reason=None)
                    else:
                        entry["quarantine_kind"] = "manual"
        return cls(iter(CapabilityRecord.from_dict(entry) for entry in entries))
