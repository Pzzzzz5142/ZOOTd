from __future__ import annotations

import math
import re
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .materials import (
    BlueMaterialChain,
    MaterialRecipeError,
    load_blue_material_chains,
)
from .models import StockTarget


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ActivityPolicyConfig:
    require_sources: tuple[str, ...]
    end_safety_margin_minutes: int
    window_tolerance_seconds: int


@dataclass(frozen=True)
class AnnihilationConfig:
    execution_budget_minutes: int
    transaction_timeout_minutes: int
    max_transactions_per_run: int


@dataclass(frozen=True)
class FreshnessConfig:
    official_seconds: int
    maa_activity_seconds: int
    yituliu_stage_seconds: int
    yituliu_matrix_seconds: int
    yituliu_value_seconds: int
    inventory_seconds: int


@dataclass(frozen=True)
class PolicyConfig:
    when_satisfied: str
    default_target: int
    minimum_drop_samples: int
    series: int
    medicine: int
    medicine_expire_days: int
    stone: int
    large_deficit_runs: int


@dataclass(frozen=True)
class SourceConfig:
    maa_activity_url: str
    official_list_url: str
    official_article_url: str
    official_max_articles: int
    official_article_lookback_days: int
    yituliu_stage_url: str
    yituliu_matrix_url: str
    yituliu_value_url: str


@dataclass(frozen=True)
class AgentConfig:
    enabled: bool
    command: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class SupervisorConfig:
    enabled: bool
    required_on_exception: bool
    command: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class PlannerConfig:
    schema_version: int
    client_type: str
    account: str
    mode: str
    activity: ActivityPolicyConfig
    annihilation: AnnihilationConfig
    freshness: FreshnessConfig
    policy: PolicyConfig
    sources: SourceConfig
    agent: AgentConfig
    supervisor: SupervisorConfig
    blue_material_chains: dict[str, BlueMaterialChain]
    targets: dict[str, StockTarget]


def _table(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return value


def _integer(table: dict[str, Any], key: str, default: int, *, minimum: int = 0) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


def _string(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _source_url(
    table: dict[str, Any],
    key: str,
    default: str,
    *,
    host: str,
    cid_template: bool = False,
) -> str:
    value = _string(table, key, default)
    if cid_template:
        if value.count("{cid}") != 1 or value.replace("{cid}", "").count("{") or "}" in value.replace("{cid}", ""):
            raise ConfigError(f"{key} must contain exactly one {{cid}} placeholder")
        parsed_value = value.replace("{cid}", "probe")
    else:
        if "{" in value or "}" in value:
            raise ConfigError(f"{key} must not contain template placeholders")
        parsed_value = value
    parsed = urllib.parse.urlparse(parsed_value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path
        or parsed.fragment
    ):
        raise ConfigError(f"{key} must be an HTTPS URL on {host}")
    return value


def load_config(path: Path) -> PlannerConfig:
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"failed to load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("configuration root must be a table")
    schema_version = _integer(payload, "schema_version", 1, minimum=1)
    if schema_version != 1:
        raise ConfigError(f"unsupported schema_version: {schema_version}")
    client_type = _string(payload, "client_type", "Official")
    if client_type != "Official":
        raise ConfigError(
            "planner schema v1 supports only Official; other clients lack an equivalent verified activity gate"
        )
    account = _string(payload, "account", "default")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", account):
        raise ConfigError("account must be a safe local identifier")
    mode = _string(payload, "mode", "auto")
    if mode not in {"auto", "off"}:
        raise ConfigError("mode must be auto or off")

    activity_raw = _table(payload, "activity")
    required = activity_raw.get("require_sources", ["official", "maa"])
    if not isinstance(required, list) or not all(item in {"official", "maa"} for item in required):
        raise ConfigError("activity.require_sources may only contain official and maa")
    if set(required) != {"official", "maa"}:
        raise ConfigError("automatic farming requires both official and maa activity sources")
    activity = ActivityPolicyConfig(
        require_sources=tuple(dict.fromkeys(required)),
        end_safety_margin_minutes=_integer(activity_raw, "end_safety_margin_minutes", 15),
        window_tolerance_seconds=_integer(activity_raw, "window_tolerance_seconds", 300),
    )

    annihilation_raw = _table(payload, "annihilation")
    annihilation = AnnihilationConfig(
        execution_budget_minutes=_integer(
            annihilation_raw, "execution_budget_minutes", 120, minimum=1
        ),
        transaction_timeout_minutes=_integer(
            annihilation_raw, "transaction_timeout_minutes", 30, minimum=1
        ),
        max_transactions_per_run=_integer(
            annihilation_raw, "max_transactions_per_run", 10, minimum=1
        ),
    )
    if annihilation.execution_budget_minutes > 360:
        raise ConfigError("annihilation.execution_budget_minutes must be <= 360")
    if annihilation.transaction_timeout_minutes != 30:
        raise ConfigError("annihilation.transaction_timeout_minutes must be 30")
    if (
        annihilation.transaction_timeout_minutes
        > annihilation.execution_budget_minutes
    ):
        raise ConfigError(
            "annihilation transaction timeout cannot exceed its execution budget"
        )
    if annihilation.max_transactions_per_run > 20:
        raise ConfigError("annihilation.max_transactions_per_run must be <= 20")

    fresh = _table(payload, "freshness")
    freshness = FreshnessConfig(
        official_seconds=_integer(fresh, "official_seconds", 1800, minimum=1),
        maa_activity_seconds=_integer(fresh, "maa_activity_seconds", 1800, minimum=1),
        yituliu_stage_seconds=_integer(fresh, "yituliu_stage_seconds", 86400, minimum=1),
        yituliu_matrix_seconds=_integer(fresh, "yituliu_matrix_seconds", 259200, minimum=1),
        yituliu_value_seconds=_integer(fresh, "yituliu_value_seconds", 86400, minimum=1),
        inventory_seconds=_integer(fresh, "inventory_seconds", 1800, minimum=0),
    )

    policy_raw = _table(payload, "policy")
    when_satisfied = _string(policy_raw, "when_satisfied", "best_event")
    if when_satisfied not in {"best_event", "skip"}:
        raise ConfigError("policy.when_satisfied must be best_event or skip")
    series = _integer(policy_raw, "series", 0)
    if series != 0:
        raise ConfigError("policy.series must be 0 (automatic)")
    medicine = _integer(policy_raw, "medicine", 0)
    medicine_expire_days = _integer(policy_raw, "medicine_expire_days", 2)
    stone = _integer(policy_raw, "stone", 0)
    if medicine != 0 or stone != 0:
        raise ConfigError(
            "planner schema v1 forbids normal medicine and Originite Prime "
            "(medicine = stone = 0)"
        )
    if medicine_expire_days != 2:
        raise ConfigError("policy.medicine_expire_days must be 2")
    policy = PolicyConfig(
        when_satisfied=when_satisfied,
        default_target=_integer(policy_raw, "default_target", 200),
        minimum_drop_samples=_integer(policy_raw, "minimum_drop_samples", 300, minimum=1),
        series=series,
        medicine=medicine,
        medicine_expire_days=medicine_expire_days,
        stone=stone,
        large_deficit_runs=_integer(policy_raw, "large_deficit_runs", 6, minimum=1),
    )

    equivalence_raw = _table(payload, "inventory_equivalence")
    if set(equivalence_raw) - {"recipes_file"}:
        raise ConfigError(
            "inventory_equivalence may contain only recipes_file"
        )
    recipes_file = _string(
        equivalence_raw, "recipes_file", "material-recipes.toml"
    )
    relative_recipes = Path(recipes_file)
    if relative_recipes.is_absolute() or ".." in relative_recipes.parts:
        raise ConfigError(
            "inventory_equivalence.recipes_file must stay inside the config directory"
        )
    config_directory = path.parent.resolve()
    recipes_path = (config_directory / relative_recipes).resolve()
    if not recipes_path.is_relative_to(config_directory):
        raise ConfigError(
            "inventory_equivalence.recipes_file resolves outside the config directory"
        )
    try:
        blue_material_chains = load_blue_material_chains(recipes_path)
    except MaterialRecipeError as exc:
        raise ConfigError(str(exc)) from exc

    sources_raw = _table(payload, "sources")
    maa_raw = _table(sources_raw, "maa")
    official_raw = _table(sources_raw, "official")
    yituliu_raw = _table(sources_raw, "yituliu")
    sources = SourceConfig(
        maa_activity_url=_source_url(
            maa_raw,
            "activity_url",
            "https://api.maa.plus/MaaAssistantArknights/api/gui/StageActivityV2.json",
            host="api.maa.plus",
        ),
        official_list_url=_source_url(
            official_raw,
            "list_url",
            "https://ak-webview.hypergryph.com/api/game/bulletinList?target=Android",
            host="ak-webview.hypergryph.com",
        ),
        official_article_url=_source_url(
            official_raw,
            "article_url",
            "https://ak-webview.hypergryph.com/api/game/bulletin/{cid}",
            host="ak-webview.hypergryph.com",
            cid_template=True,
        ),
        official_max_articles=_integer(official_raw, "max_articles", 12, minimum=1),
        official_article_lookback_days=_integer(official_raw, "article_lookback_days", 120, minimum=1),
        yituliu_stage_url=_source_url(
            yituliu_raw,
            "stage_url",
            "https://backend.yituliu.cn/stage/info",
            host="backend.yituliu.cn",
        ),
        yituliu_matrix_url=_source_url(
            yituliu_raw,
            "matrix_url",
            "https://cos.yituliu.cn/arknights/stage-drop/matrix.json",
            host="cos.yituliu.cn",
        ),
        yituliu_value_url=_source_url(
            yituliu_raw,
            "value_url",
            "https://backend.yituliu.cn/item/v7/value",
            host="backend.yituliu.cn",
        ),
    )

    agent_raw = _table(payload, "agent")
    enabled = agent_raw.get("enabled", False)
    command = agent_raw.get("command", [])
    if not isinstance(enabled, bool) or not isinstance(command, list) or not all(isinstance(x, str) and x for x in command):
        raise ConfigError("agent.enabled/command have invalid types")
    agent = AgentConfig(
        enabled=enabled,
        command=tuple(command),
        timeout_seconds=_integer(agent_raw, "timeout_seconds", 60, minimum=1),
    )
    if agent.enabled and not agent.command:
        raise ConfigError("agent.command is required when the agent is enabled")

    supervisor_raw = _table(payload, "supervisor")
    supervisor_enabled = supervisor_raw.get("enabled", False)
    supervisor_required = supervisor_raw.get("required_on_exception", False)
    supervisor_command = supervisor_raw.get("command", [])
    if (
        not isinstance(supervisor_enabled, bool)
        or not isinstance(supervisor_required, bool)
        or not isinstance(supervisor_command, list)
        or not all(isinstance(x, str) and x for x in supervisor_command)
    ):
        raise ConfigError("supervisor enabled/required/command values have invalid types")
    supervisor = SupervisorConfig(
        enabled=supervisor_enabled,
        required_on_exception=supervisor_required,
        command=tuple(supervisor_command),
        timeout_seconds=_integer(
            supervisor_raw, "timeout_seconds", 90, minimum=1
        ),
    )
    if supervisor.enabled and not supervisor.command:
        raise ConfigError(
            "supervisor.command is required when the supervisor is enabled"
        )
    if supervisor.required_on_exception and not supervisor.enabled:
        raise ConfigError(
            "supervisor.required_on_exception requires supervisor.enabled"
        )

    targets_raw = payload.get("targets", [])
    if not isinstance(targets_raw, list):
        raise ConfigError("targets must be an array of tables")
    targets: dict[str, StockTarget] = {}
    for raw in targets_raw:
        if not isinstance(raw, dict):
            raise ConfigError("each target must be a table")
        item_id = _string(raw, "item_id", "")
        if not re.fullmatch(r"\d+", item_id):
            raise ConfigError(f"target item_id must be numeric: {item_id!r}")
        low = _integer(raw, "low", 0)
        target = _integer(raw, "target", 0)
        reserved = _integer(raw, "reserved", 0)
        priority = raw.get("priority", 1.0)
        if (
            not isinstance(priority, (int, float))
            or isinstance(priority, bool)
            or not math.isfinite(float(priority))
            or priority <= 0
        ):
            raise ConfigError(f"priority for {item_id} must be positive")
        if target < low:
            raise ConfigError(f"target for {item_id} must be >= low")
        if item_id in targets:
            raise ConfigError(f"duplicate target item_id: {item_id}")
        targets[item_id] = StockTarget(item_id, low, target, float(priority), reserved)

    return PlannerConfig(
        schema_version=schema_version,
        client_type=client_type,
        account=account,
        mode=mode,
        activity=activity,
        annihilation=annihilation,
        freshness=freshness,
        policy=policy,
        sources=sources,
        agent=agent,
        supervisor=supervisor,
        blue_material_chains=blue_material_chains,
        targets=targets,
    )
