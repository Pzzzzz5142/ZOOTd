from __future__ import annotations

import copy
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from .util import atomic_write_json, isoformat, parse_iso_datetime, utc_now


LEDGER_SCHEMA_VERSION = 1
CapabilityStatus = Literal["unknown", "verified", "quarantined"]
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
    status: Literal["verified", "quarantined"]
    updated_at: datetime
    verified_at: datetime | None = None
    quarantined_at: datetime | None = None
    quarantine_reason: str | None = None
    success_count: int = 0
    evidence: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.status not in ("verified", "quarantined"):
            raise CapabilityError(f"invalid capability status: {self.status!r}")
        if not isinstance(self.success_count, int) or isinstance(self.success_count, bool):
            raise CapabilityError("success_count must be an integer")
        if self.success_count < 0:
            raise CapabilityError("success_count must be non-negative")

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

        if self.status == "verified" and (verified_at is None or self.success_count < 1):
            raise CapabilityError("verified capability requires success evidence")
        if self.status == "quarantined":
            if quarantined_at is None:
                raise CapabilityError("quarantined capability requires a timestamp")
            if not isinstance(reason, str) or not reason or reason != reason.strip():
                raise CapabilityError("quarantined capability requires a reason")
        elif reason is not None:
            raise CapabilityError("verified capability cannot have a quarantine reason")

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
            "success_count": self.success_count,
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
            return cls(
                key=key,
                status=status,
                updated_at=updated_at,
                verified_at=verified_at,
                quarantined_at=quarantined_at,
                quarantine_reason=value.get("quarantine_reason"),
                success_count=value["success_count"],
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


def _iter_log_records(log_text: str) -> Iterator[tuple[str | None, dict[str, Any]]]:
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
        self._records: dict[CapabilityKey, CapabilityRecord] = {}
        for record in records or ():
            if record.key in self._records:
                raise CapabilityLedgerError(f"duplicate capability key: {record.key}")
            self._records[record.key] = record

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
            success_count=previous.success_count if previous else 0,
            evidence=evidence or {},
        )
        self._records[key] = record
        return record

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
        if value.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise CapabilityLedgerError("unsupported capability ledger schema")
        entries = value.get("entries")
        if not isinstance(entries, list):
            raise CapabilityLedgerError("capability ledger entries must be a list")
        return cls(iter(CapabilityRecord.from_dict(entry) for entry in entries))
