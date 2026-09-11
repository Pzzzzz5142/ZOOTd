from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Iterable, Mapping

from .inventory import InventorySnapshot
from .materials import BlueMaterialChain, blue_equivalent_inventory
from .models import (
    Activity,
    CandidateReport,
    Decision,
    StageEfficiency,
    StockTarget,
)
from .util import version_at_least


def _candidate_selection_key(item: CandidateReport) -> tuple[int, float, float, int, str]:
    return (
        item.score_bucket,
        item.score,
        item.overall_efficiency or 0.0,
        item.sample_size or 0,
        item.stage_code,
    )


def _execution_candidate(
    candidate: CandidateReport,
    *,
    efficiencies: Mapping[str, StageEfficiency],
    configured_series: int,
    large_deficit_runs: int,
    medicine: int,
    medicine_expire_days: int,
    stone: int,
) -> dict[str, object]:
    # A drop target is an OR stop condition in MaaCore.  When imminent
    # medicine is enabled it must not terminate the task before all eligible
    # medicine has been consumed; inventory still selects the best stage.
    drop_goal = (
        candidate.deficit
        if candidate.deficit > 0 and medicine_expire_days == 0
        else None
    )
    # The managed task delegates batching and continuation to MaaCore.
    series = 0
    return {
        "stage_code": candidate.stage_code,
        "item_id": candidate.item_id,
        "activity_instance": candidate.activity_instance,
        "drop_goal": drop_goal,
        "series": series,
        "times_per_transaction": 2147483647,
        "medicine": medicine,
        "medicine_expire_days": medicine_expire_days,
        "stone": stone,
        "authorization": "game-client-proxy-required",
    }


def select_farming_plan(
    *,
    now: datetime,
    activities: Iterable[Activity],
    efficiencies: Mapping[str, StageEfficiency],
    inventory: InventorySnapshot,
    targets: Mapping[str, StockTarget],
    default_target: int = 0,
    blue_material_chains: Mapping[str, BlueMaterialChain] | None = None,
    core_version: str,
    verified_stages: set[tuple[str, str]],
    quarantined_stages: set[tuple[str, str]],
    navigation_stages: set[str],
    end_safety_margin: timedelta,
    when_satisfied: str,
    configured_series: int,
    large_deficit_runs: int,
    medicine: int,
    medicine_expire_days: int,
    stone: int,
    evidence: dict[str, object] | None = None,
) -> Decision:
    """Return a reproducible FIGHT or NOOP decision without doing any I/O."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    material_chains = blue_material_chains or {}
    equivalence_evidence = {
        "unit": "t3-blue-material",
        "configured_item_ids": sorted(material_chains),
        "lower_tiers": "deterministic-workshop-recipes-only",
        "missing_lower_tiers": "zero-credit",
        "workshop_byproducts_included": False,
        "crafting_performed": False,
    }
    candidates: list[CandidateReport] = []
    activity_by_instance: dict[str, Activity] = {}
    active_activities: list[Activity] = []

    for activity in activities:
        activity_by_instance[activity.instance_id] = activity
        if activity.start.astimezone(UTC) <= now < activity.end.astimezone(UTC):
            active_activities.append(activity)
    if not active_activities:
        return Decision("NOOP", "NO_ACTIVE_MAA_ACTIVITY", now, evidence=evidence or {})

    # If a configured target that can be farmed right now was not observed by
    # Depot, choosing a different material as "best event" could silently
    # bypass the true urgent deficit.  Treat that uncertainty as global for the
    # current candidate set and fail closed.
    relevant_target_items = {
        stage.item_id
        for activity in active_activities
        for stage in activity.stages
        if stage.item_id in targets or default_target > 0
    }
    missing_relevant_items = sorted(
        item_id
        for item_id in relevant_target_items
        if inventory.quantity(item_id) is None
    )

    for activity in active_activities:
        common_rejections: list[str] = []
        if missing_relevant_items:
            common_rejections.append("INVENTORY_REQUIRED_ITEM_MISSING")
        if now + end_safety_margin >= activity.end.astimezone(UTC):
            common_rejections.append("END_SAFETY_MARGIN")
        if not version_at_least(core_version, activity.minimum_required):
            common_rejections.append("CORE_VERSION_TOO_OLD")
        if not activity.stages:
            common_rejections.append("NO_NUMERIC_MATERIAL_STAGE")
        for stage in activity.stages:
            report = CandidateReport(
                stage_code=stage.code,
                item_id=stage.item_id,
                activity_instance=activity.instance_id,
                activity_name=activity.name,
                rejected_by=list(common_rejections),
            )
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,63}", stage.code):
                report.rejected_by.append("INVALID_STAGE_CODE")
            if not re.fullmatch(r"\d+", stage.item_id):
                report.rejected_by.append("INVALID_ITEM_ID")
            capability_key = (activity.instance_id, stage.code)
            if stage.code not in navigation_stages:
                report.rejected_by.append("MAA_NAVIGATION_UNSUPPORTED")
            if capability_key in quarantined_stages:
                report.rejected_by.append("STAGE_QUARANTINED")
            # The game client is the ground truth for whether proxy play is
            # saved.  A missing positive local record must never block a
            # stage.  Only an observed unstable proxy is a higher-priority
            # local override.

            efficiency = efficiencies.get(stage.code)
            if efficiency is None or efficiency.item_id != stage.item_id:
                report.rejected_by.append("YITULIU_STAGE_UNAVAILABLE")
            else:
                report.expected_ap_per_item = efficiency.expected_ap_per_item
                report.overall_efficiency = efficiency.overall_efficiency
                report.sample_size = efficiency.sample_size

            target = targets.get(stage.item_id)
            if target is None and default_target > 0:
                target = StockTarget(
                    item_id=stage.item_id,
                    low=0,
                    target=default_target,
                )
            if target is not None:
                equivalent = blue_equivalent_inventory(
                    inventory, stage.item_id, material_chains
                )
                report.low = target.low
                report.target = target.target
                report.priority = target.priority
                if equivalent is None:
                    report.rejected_by.append("INVENTORY_ITEM_MISSING")
                else:
                    report.inventory = equivalent.effective_t3
                    report.direct_inventory = equivalent.direct_t3
                    report.craftable_equivalent = equivalent.craftable_t3
                    report.inventory_breakdown = equivalent.as_dict()
                    effective = max(0, equivalent.effective_t3 - target.reserved)
                    report.deficit = max(0, target.target - effective)
                    if effective < target.low:
                        report.score_bucket = 3
                    elif report.deficit > 0:
                        report.score_bucket = 2
                    elif when_satisfied == "best_event":
                        report.score_bucket = 1
            elif when_satisfied == "best_event":
                report.score_bucket = 1

            if report.score_bucket == 0:
                report.rejected_by.append("TARGETS_SATISFIED")
            if efficiency is not None and report.score_bucket >= 2:
                target_size = max(report.target or 1, 1)
                normalized_deficit = report.deficit / target_size
                report.score = (
                    report.priority
                    * normalized_deficit
                    / max(efficiency.expected_ap_per_item, 0.000001)
                )
            elif efficiency is not None and report.score_bucket == 1:
                report.score = efficiency.overall_efficiency
            report.eligible = not report.rejected_by
            candidates.append(report)

    eligible = [candidate for candidate in candidates if candidate.eligible]
    if not eligible:
        reasons = sorted({reason for candidate in candidates for reason in candidate.rejected_by})
        reason = reasons[0] if len(reasons) == 1 else "NO_ELIGIBLE_STAGE"
        return Decision(
            "NOOP",
            reason,
            now,
            activity=active_activities[0],
            candidates=candidates,
            evidence={
                **(evidence or {}),
                "inventory_equivalence_policy": equivalence_evidence,
                "rejection_reasons": reasons,
                "missing_relevant_inventory_items": missing_relevant_items,
                "client_proxy_policy": {
                    "ground_truth": "game-client",
                    "unknown_allowed": True,
                    "positive_ledger_required": False,
                    "local_quarantine_overrides": True,
                    "automatic_quarantine_scope": "run",
                    "preflight": "custom-navigation-plus-use-prts-success-check",
                    "preflight_consumes_sanity": False,
                    "consecutive_failure_limit": 3,
                    "failure_unit": "maa-fight-task",
                },
                "activity_window_policy": {
                    "source": "maa-stage-activity-v2",
                    "half_open_interval": True,
                },
            },
        )

    ordered = sorted(eligible, key=_candidate_selection_key, reverse=True)
    selected = ordered[0]
    activity = activity_by_instance[selected.activity_instance or ""]
    execution_candidates = [
        _execution_candidate(
            candidate,
            efficiencies=efficiencies,
            configured_series=configured_series,
            large_deficit_runs=large_deficit_runs,
            medicine=medicine,
            medicine_expire_days=medicine_expire_days,
            stone=stone,
        )
        for candidate in ordered
    ]
    selected_execution = execution_candidates[0]

    return Decision(
        "FIGHT",
        "ELIGIBLE_STAGE_SELECTED",
        now,
        activity=activity,
        selected_stage=selected.stage_code,
        selected_item=selected.item_id,
        drop_goal=selected_execution["drop_goal"],
        series=selected_execution["series"],
        medicine=medicine,
        medicine_expire_days=medicine_expire_days,
        stone=stone,
        candidates=candidates,
        evidence={
            **(evidence or {}),
            "inventory_equivalence_policy": equivalence_evidence,
            "client_proxy_policy": {
                "ground_truth": "game-client",
                "unknown_allowed": True,
                "positive_ledger_required": False,
                "local_quarantine_overrides": True,
                "automatic_quarantine_scope": "run",
                "preflight": "custom-navigation-plus-use-prts-success-check",
                "preflight_consumes_sanity": False,
                "consecutive_failure_limit": 3,
                "failure_unit": "maa-fight-task",
            },
            "execution_candidates": execution_candidates,
            "activity_window_policy": {
                "source": "maa-stage-activity-v2",
                "half_open_interval": True,
            },
            "selection_order": [
                "hard_floor",
                "target_deficit",
                "yituliu_overall_efficiency",
                "sample_size",
            ],
        },
    )
