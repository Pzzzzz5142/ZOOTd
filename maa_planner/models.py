from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from .util import canonical_json, isoformat, sha256_bytes


@dataclass(frozen=True)
class ActivityStage:
    code: str
    item_id: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Activity:
    client: str
    key: str
    name: str
    tip: str
    start: datetime
    end: datetime
    minimum_required: str
    stages: tuple[ActivityStage, ...]
    source_sha256: str

    @property
    def instance_id(self) -> str:
        identity = {
            "client": self.client,
            "key": self.key,
            "name": self.name,
            "start": isoformat(self.start),
            "end": isoformat(self.end),
        }
        return sha256_bytes(canonical_json(identity))[:24]

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "key": self.key,
            "name": self.name,
            "tip": self.tip,
            "start": isoformat(self.start),
            "end": isoformat(self.end),
            "minimum_required": self.minimum_required,
            "instance_id": self.instance_id,
            "stages": [stage.as_dict() for stage in self.stages],
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class StageEfficiency:
    stage_code: str
    stage_id: str
    item_id: str
    ap_cost: int
    drop_rate: float
    expected_ap_per_item: float
    overall_efficiency: float
    sample_size: int
    source_sha256: str
    calculation_profile: str = "yituliu-v7-value-v1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StockTarget:
    item_id: str
    low: int
    target: int
    priority: float = 1.0
    reserved: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateReport:
    stage_code: str
    item_id: str
    activity_instance: str | None = None
    activity_name: str | None = None
    eligible: bool = False
    rejected_by: list[str] = field(default_factory=list)
    # ``inventory`` is the effective T3/blue-material quantity used by policy.
    inventory: int | None = None
    direct_inventory: int | None = None
    craftable_equivalent: int = 0
    inventory_breakdown: dict[str, Any] | None = None
    low: int | None = None
    target: int | None = None
    deficit: int = 0
    priority: float = 1.0
    expected_ap_per_item: float | None = None
    overall_efficiency: float | None = None
    sample_size: int | None = None
    score_bucket: int = 0
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    decision: Literal["FIGHT", "NOOP"]
    reason: str
    generated_at: datetime
    activity: Activity | None = None
    selected_stage: str | None = None
    selected_item: str | None = None
    drop_goal: int | None = None
    series: int = 1
    medicine: int = 0
    medicine_expire_days: int = 0
    stone: int = 0
    candidates: list[CandidateReport] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision": self.decision,
            "reason": self.reason,
            "generated_at": isoformat(self.generated_at),
            "activity": self.activity.as_dict() if self.activity else None,
            "selected_stage": self.selected_stage,
            "selected_item": self.selected_item,
            "drop_goal": self.drop_goal,
            "series": self.series,
            "medicine": self.medicine,
            "medicine_expire_days": self.medicine_expire_days,
            "stone": self.stone,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "evidence": self.evidence,
        }
