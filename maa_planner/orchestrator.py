from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import PlannerConfig
from .models import Activity, StageEfficiency
from .sources import (
    HttpCache,
    SourceError,
    build_yituliu_efficiencies,
    parse_maa_activities,
)
from .util import atomic_write_json, isoformat, sha256_bytes, utc_now


@dataclass(frozen=True)
class ActivityCalendarBundle:
    activities: tuple[Activity, ...]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": isoformat(utc_now()),
            "activities": [activity.as_dict() for activity in self.activities],
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class SourceBundle:
    activities: tuple[Activity, ...]
    efficiencies: dict[str, StageEfficiency]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": isoformat(utc_now()),
            "activities": [activity.as_dict() for activity in self.activities],
            "efficiencies": {
                stage: efficiency.as_dict()
                for stage, efficiency in sorted(self.efficiencies.items())
            },
            "evidence": self.evidence,
        }


def _validate_maa(value: Any, client: str) -> None:
    source_client = "Official" if client == "Bilibili" else client
    if not isinstance(value, dict) or not isinstance(value.get(source_client), dict):
        raise SourceError(f"MAA activity response has no {source_client} section")
    if not isinstance(value[source_client].get("sideStoryStage"), dict):
        raise SourceError("MAA activity response has no sideStoryStage table")


def _validate_yituliu_envelope(value: Any, name: str) -> None:
    if not isinstance(value, dict) or value.get("code") != 200 or not isinstance(value.get("data"), list):
        raise SourceError(f"{name} returned an invalid envelope")


def _validate_matrix(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("matrix"), list):
        raise SourceError("yituliu matrix returned an invalid envelope")


def load_activity_calendar(
    root: Path, config: PlannerConfig, *, online: bool
) -> ActivityCalendarBundle:
    """Load MAA's StageActivityV2 activity-stage windows.

    Weekly Annihilation scheduling must not be degraded by an unrelated item
    value or drop-efficiency outage, so this path intentionally excludes every
    Yituliu endpoint.
    """

    cache = HttpCache(
        root / "var/cache/planner/http",
        allowed_hosts={"api.maa.plus"},
        network_enabled=online,
    )
    maa_payload, maa_fetch = cache.fetch_json(
        "maa-activity",
        config.sources.maa_activity_url,
        max_stale=timedelta(seconds=config.freshness.maa_activity_seconds),
        validator=lambda value: _validate_maa(value, config.client_type),
    )
    activities = parse_maa_activities(maa_payload, config.client_type, maa_fetch.sha256)

    evidence = {
        "maa_activity": {
            "url": config.sources.maa_activity_url,
            "sha256": maa_fetch.sha256,
            "fetched_at": maa_fetch.metadata["fetched_at"],
            "network_validated": maa_fetch.network_validated,
        },
    }
    bundle = ActivityCalendarBundle(tuple(activities), evidence)
    atomic_write_json(
        root / "var/state/planner/latest-activity-calendar.json",
        bundle.as_dict(),
    )
    return bundle


def load_source_bundle(root: Path, config: PlannerConfig, *, online: bool) -> SourceBundle:
    calendar = load_activity_calendar(root, config, online=online)
    cache = HttpCache(
        root / "var/cache/planner/http",
        allowed_hosts={"backend.yituliu.cn", "cos.yituliu.cn"},
        network_enabled=online,
    )

    stage_payload, stage_fetch = cache.fetch_json(
        "yituliu-stage-info",
        config.sources.yituliu_stage_url,
        max_stale=timedelta(seconds=config.freshness.yituliu_stage_seconds),
        validator=lambda value: _validate_yituliu_envelope(value, "yituliu stage info"),
    )
    matrix_payload, matrix_fetch = cache.fetch_json(
        "yituliu-matrix",
        config.sources.yituliu_matrix_url,
        max_stale=timedelta(seconds=config.freshness.yituliu_matrix_seconds),
        validator=_validate_matrix,
    )
    value_payload, value_fetch = cache.fetch_json(
        "yituliu-item-values",
        config.sources.yituliu_value_url,
        max_stale=timedelta(seconds=config.freshness.yituliu_value_seconds),
        validator=lambda value: _validate_yituliu_envelope(value, "yituliu item values"),
        method="POST",
        json_body={
            "source": "penguin",
            "version": "v1.0",
            "useActivityAverageStage": False,
            "sampleSize": config.policy.minimum_drop_samples,
            "stageBlacklist": [],
        },
    )
    candidates = [
        stage for activity in calendar.activities for stage in activity.stages
    ]
    yituliu_sha = sha256_bytes(
        (stage_fetch.sha256 + matrix_fetch.sha256 + value_fetch.sha256).encode()
    )
    efficiencies = build_yituliu_efficiencies(
        stage_payload,
        matrix_payload,
        value_payload,
        candidates,
        minimum_samples=config.policy.minimum_drop_samples,
        source_sha256=yituliu_sha,
    )
    evidence = {
        **calendar.evidence,
        "yituliu": {
            "stage": {
                "url": config.sources.yituliu_stage_url,
                "sha256": stage_fetch.sha256,
                "fetched_at": stage_fetch.metadata["fetched_at"],
                "network_validated": stage_fetch.network_validated,
            },
            "matrix": {
                "url": config.sources.yituliu_matrix_url,
                "sha256": matrix_fetch.sha256,
                "fetched_at": matrix_fetch.metadata["fetched_at"],
                "network_validated": matrix_fetch.network_validated,
            },
            "values": {
                "url": config.sources.yituliu_value_url,
                "sha256": value_fetch.sha256,
                "fetched_at": value_fetch.metadata["fetched_at"],
                "network_validated": value_fetch.network_validated,
                "profile": "penguin-v1.0-default",
                "request_sha256": value_fetch.metadata.get("request_sha256"),
            },
        },
    }
    bundle = SourceBundle(
        calendar.activities,
        efficiencies,
        evidence,
    )
    atomic_write_json(root / "var/state/planner/latest-sources.json", bundle.as_dict())
    return bundle


def refresh_maa_resources(root: Path, timeout_seconds: int = 600) -> tuple[bool, str]:
    host = root / "bin/zootd"
    try:
        completed = subprocess.run(
            [str(host), "runtime-update"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = completed.stdout[-4000:]
    return completed.returncode == 0, output


def current_core_version(root: Path) -> str:
    try:
        completed = subprocess.run(
            [str(root / "bin/zootd-maa"), "version", "core"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"cannot query MaaCore version: {exc}") from exc
    if completed.returncode != 0:
        raise SourceError(f"cannot query MaaCore version: {completed.stdout[-1000:]}")
    match = re.search(r"MaaCore\s+(v?\d+(?:\.\d+)+)", completed.stdout)
    if not match:
        raise SourceError("MaaCore version output is unrecognized")
    return match.group(1)


def navigation_stages(root: Path) -> set[str]:
    result: set[str] = set()
    directories = (
        root / "var/data/resource/tasks/Stages",
        root / "var/data/MaaResource/resource/tasks/Stages",
    )
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for key in payload:
                if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9]+-[A-Za-z0-9]+", key):
                    result.add(key)
    return result
