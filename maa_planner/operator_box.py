"""Provider-independent player progression. None means unknown, never zero."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class SkillProgress:
    mastery: int | None


@dataclass(frozen=True)
class ModuleProgress:
    level: int | None
    unlocked: bool | None


@dataclass(frozen=True)
class Operator:
    id: str
    elite: int | None
    level: int | None
    potential: int | None  # Displayed potential, 1..6.
    main_skill_level: int | None
    skills: dict[str, SkillProgress] | None  # Canonical skill IDs, NOT list positions.
    modules: dict[str, ModuleProgress] | None


@dataclass(frozen=True)
class OperatorBox:
    source: str
    fetched_at: str
    account_id: str  # Namespaced Official UID; local/private, never ordinary logs.
    operators: dict[str, Operator]
    schema: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


class BoxProvider(Protocol):
    def fetch_box(self) -> OperatorBox: ...
