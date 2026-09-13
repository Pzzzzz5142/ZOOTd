from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .inventory import InventorySnapshot


CRAFTABLE_CREDIT_PERCENT = 80


class MaterialRecipeError(ValueError):
    """Raised when a blue-material equivalence recipe is unsafe or malformed."""


@dataclass(frozen=True)
class BlueMaterialChain:
    """One deterministic T1 -> T2 -> T3 (blue) workshop chain."""

    t3_item_id: str
    t3_name: str
    t2_item_id: str
    t2_name: str
    t2_per_t3: int
    t1_item_id: str
    t1_name: str
    t1_per_t2: int


@dataclass(frozen=True)
class BlueEquivalentInventory:
    """An inventory observation expressed in T3/blue-material units."""

    t3_item_id: str
    direct_t3: int
    direct_t2: int | None
    direct_t1: int | None
    t2_from_t1: int
    t2_available: int
    craftable_t3: int
    credited_craftable_t3: int
    effective_t3: int
    recipe_applied: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": "t3-blue-material",
            **asdict(self),
            "missing_lower_tiers_are_zero_credit": True,
            "workshop_byproducts_included": False,
            "crafting_performed": False,
            "white_materials_included": False,
            "craftable_credit_percent": CRAFTABLE_CREDIT_PERCENT,
        }


def _strict_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        added = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise MaterialRecipeError(
            f"{context} fields differ from schema (added={added}, missing={missing})"
        )


def _item_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,20}", value):
        raise MaterialRecipeError(f"{field} must be a numeric item id")
    return value


def _name(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MaterialRecipeError(f"{field} must be a non-empty trimmed name")
    return value


def _ratio(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise MaterialRecipeError(f"{field} must be an integer between 1 and 100")
    return value


def load_blue_material_chains(path: Path) -> dict[str, BlueMaterialChain]:
    """Load a deliberately small, auditable set of deterministic recipes."""

    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MaterialRecipeError(f"failed to load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MaterialRecipeError("material recipe root must be a table")
    _strict_keys(payload, {"schema_version", "unit", "chains"}, "recipe root")
    if payload["schema_version"] != 1:
        raise MaterialRecipeError("unsupported material recipe schema_version")
    if payload["unit"] != "t3-blue-material":
        raise MaterialRecipeError("material recipe unit must be t3-blue-material")
    raw_chains = payload["chains"]
    if not isinstance(raw_chains, list) or not raw_chains:
        raise MaterialRecipeError("material recipes must contain at least one chain")

    fields = {
        "t3_item_id",
        "t3_name",
        "t2_item_id",
        "t2_name",
        "t2_per_t3",
        "t1_item_id",
        "t1_name",
        "t1_per_t2",
    }
    result: dict[str, BlueMaterialChain] = {}
    claimed_item_ids: set[str] = set()
    for index, raw in enumerate(raw_chains):
        context = f"chains[{index}]"
        if not isinstance(raw, dict):
            raise MaterialRecipeError(f"{context} must be a table")
        _strict_keys(raw, fields, context)
        chain = BlueMaterialChain(
            t3_item_id=_item_id(raw["t3_item_id"], f"{context}.t3_item_id"),
            t3_name=_name(raw["t3_name"], f"{context}.t3_name"),
            t2_item_id=_item_id(raw["t2_item_id"], f"{context}.t2_item_id"),
            t2_name=_name(raw["t2_name"], f"{context}.t2_name"),
            t2_per_t3=_ratio(raw["t2_per_t3"], f"{context}.t2_per_t3"),
            t1_item_id=_item_id(raw["t1_item_id"], f"{context}.t1_item_id"),
            t1_name=_name(raw["t1_name"], f"{context}.t1_name"),
            t1_per_t2=_ratio(raw["t1_per_t2"], f"{context}.t1_per_t2"),
        )
        chain_ids = {chain.t3_item_id, chain.t2_item_id, chain.t1_item_id}
        if len(chain_ids) != 3:
            raise MaterialRecipeError(f"{context} must use three distinct item ids")
        duplicates = sorted(chain_ids & claimed_item_ids)
        if duplicates:
            raise MaterialRecipeError(
                f"{context} reuses item ids from another chain: {duplicates}"
            )
        claimed_item_ids.update(chain_ids)
        result[chain.t3_item_id] = chain
    return result


def validate_blue_material_chains_against_item_index(
    chains: Mapping[str, BlueMaterialChain], path: Path
) -> None:
    """Fail closed if an updated MAA generation disagrees with recipe identity."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaterialRecipeError(f"failed to load MAA item index {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MaterialRecipeError(f"MAA item index {path} must be an object")
    for chain in chains.values():
        for item_id, expected_name in (
            (chain.t3_item_id, chain.t3_name),
            (chain.t2_item_id, chain.t2_name),
            (chain.t1_item_id, chain.t1_name),
        ):
            item = payload.get(item_id)
            if (
                not isinstance(item, dict)
                or item.get("name") != expected_name
                or item.get("classifyType") != "MATERIAL"
            ):
                raise MaterialRecipeError(
                    f"MAA item index disagrees with recipe item {item_id} "
                    f"({expected_name})"
                )


def blue_equivalent_inventory(
    snapshot: InventorySnapshot,
    t3_item_id: str,
    chains: Mapping[str, BlueMaterialChain],
) -> BlueEquivalentInventory | None:
    """Return conservative T3 inventory, or ``None`` if T3 was unobserved.

    An absent lower-tier observation contributes no credit.  An absent target
    remains unknown so a large lower-tier count can never mask a failed Depot
    observation of the material whose target is being enforced.
    """

    direct_t3 = snapshot.quantity(t3_item_id)
    if direct_t3 is None:
        return None
    chain = chains.get(t3_item_id)
    if chain is None:
        return BlueEquivalentInventory(
            t3_item_id=t3_item_id,
            direct_t3=direct_t3,
            direct_t2=None,
            direct_t1=None,
            t2_from_t1=0,
            t2_available=0,
            craftable_t3=0,
            credited_craftable_t3=0,
            effective_t3=direct_t3,
            recipe_applied=False,
        )

    direct_t2 = snapshot.quantity(chain.t2_item_id)
    direct_t1 = snapshot.quantity(chain.t1_item_id)
    # White materials remain observable for audit but never earn stock credit.
    t2_from_t1 = 0
    t2_available = direct_t2 or 0
    craftable_t3 = t2_available // chain.t2_per_t3
    credited_craftable_t3 = craftable_t3 * CRAFTABLE_CREDIT_PERCENT // 100
    return BlueEquivalentInventory(
        t3_item_id=t3_item_id,
        direct_t3=direct_t3,
        direct_t2=direct_t2,
        direct_t1=direct_t1,
        t2_from_t1=t2_from_t1,
        t2_available=t2_available,
        craftable_t3=craftable_t3,
        credited_craftable_t3=credited_craftable_t3,
        effective_t3=direct_t3 + credited_craftable_t3,
        recipe_applied=True,
    )
