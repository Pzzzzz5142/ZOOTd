from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config
from .materials import (
    MaterialRecipeError,
    validate_blue_material_chains_against_item_index,
)


class RuntimeContractError(ValueError):
    """Raised when a static maa-cli task violates a launcher contract."""


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeContractError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeContractError(f"{path} must be a TOML table")
    return payload


def _load_config(path: Path) -> dict[str, Any]:
    payload = _load_toml(path)
    if payload.get("client_type") != "Official":
        raise RuntimeContractError(f"{path} must target the Official client")
    return payload


def _load_fight_task(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _load_config(path)
    if payload.get("startup") is not False or payload.get("closedown") is not False:
        raise RuntimeContractError(f"{path} must not own startup or closedown")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise RuntimeContractError(f"{path} must contain exactly one task")
    task = tasks[0]
    if task.get("type") != "Fight":
        raise RuntimeContractError(f"{path} must contain exactly one Fight task")
    params = task.get("params")
    if not isinstance(params, dict):
        raise RuntimeContractError(f"{path} Fight task has no params table")
    return payload, params


def _require_exact_params(
    path: Path,
    params: dict[str, Any],
    expected: dict[str, Any],
    *,
    task_type: str = "Fight",
) -> None:
    if set(params) != set(expected):
        added = sorted(set(params) - set(expected))
        missing = sorted(set(expected) - set(params))
        raise RuntimeContractError(
            f"{path} {task_type} params differ from the execution contract "
            f"(added={added}, missing={missing})"
        )
    for key, value in expected.items():
        actual = params.get(key)
        if type(actual) is not type(value) or actual != value:
            raise RuntimeContractError(
                f"{path} {task_type} param {key!r} must be {value!r}"
            )


def validate_service_contracts(root: Path) -> None:
    expected_services = {
        "zootd-prereset.service": (
            "ExecStart=%h/Projects/zootd/bin/zootd run --pre-reset-slot"
        ),
        "zootd.service": (
            "ExecStart=%h/Projects/zootd/bin/zootd run --post-reset-slot"
        ),
    }
    for service_name, expected_exec in expected_services.items():
        service_path = root / "systemd" / service_name
        try:
            service_lines = service_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RuntimeContractError(f"cannot read {service_path}: {exc}") from exc
        exec_lines = [line for line in service_lines if line.startswith("ExecStart=")]
        if exec_lines != [expected_exec]:
            raise RuntimeContractError(
                f"{service_path} must invoke the shared launcher in its scheduled slot"
            )

    profile_path = root / "config/profiles/waydroid.toml"
    profile = _load_toml(profile_path)
    expected_profile = {
        "connection": {"preset": "Waydroid", "adb_path": "/usr/bin/adb"},
        "instance_options": {
            "touch_mode": "MaaTouch",
            "deployment_with_pause": False,
            "adb_lite_enabled": False,
            "kill_adb_on_exit": False,
        },
        "behavior": {"auto_reconnect": True},
    }
    if profile != expected_profile:
        raise RuntimeContractError(
            f"{profile_path} differs from the unattended Official Waydroid profile"
        )

    tasks = root / "config/tasks"

    daily_path = tasks / "daily.toml"
    daily = _load_config(daily_path)
    if daily.get("startup") is not True:
        raise RuntimeContractError(f"{daily_path} must start the game")
    if daily.get("closedown") is not False:
        raise RuntimeContractError(
            f"{daily_path} must keep the launcher-owned Waydroid session open"
        )
    daily_tasks = daily.get("tasks")
    if not isinstance(daily_tasks, list):
        raise RuntimeContractError(f"{daily_path} must contain a tasks array")
    daily_task_types = [
        task.get("type") if isinstance(task, dict) else None
        for task in daily_tasks
    ]
    if daily_task_types != ["Infrast", "Infrast", "Recruit", "Recruit", "Mall"]:
        raise RuntimeContractError(
            f"{daily_path} must contain non-dorm Infrast, protected Dorm Infrast, "
            "ordinary Recruit, Support Robot Recruit, and Mall in that order; "
            "Award belongs in award-only.toml"
        )
    infrast_tasks = [
        task
        for task in daily_tasks
        if isinstance(task, dict) and task.get("type") == "Infrast"
    ]
    if len(infrast_tasks) != 2:
        raise RuntimeContractError(
            f"{daily_path} must contain exactly two isolated Infrast tasks"
        )
    shift_params = infrast_tasks[0].get("params")
    dorm_params = infrast_tasks[1].get("params")
    if not isinstance(shift_params, dict) or not isinstance(dorm_params, dict):
        raise RuntimeContractError(f"{daily_path} Infrast task has no params table")

    shift_facilities = shift_params.get("facility")
    expected_shift_facilities = [
        "Mfg",
        "Trade",
        "Control",
        "Power",
        "Reception",
        "Office",
    ]
    shift_mode = shift_params.get("mode")
    if (
        type(shift_mode) is not int
        or shift_mode != 0
        or shift_facilities != expected_shift_facilities
    ):
        raise RuntimeContractError(
            f"{daily_path} first Infrast must use the exact non-dorm, non-Training "
            "default-mode facility list"
        )
    if shift_params.get("dorm_notstationed_enabled") is not True:
        raise RuntimeContractError(
            f"{daily_path} first Infrast must keep the defensive unstationed guard enabled"
        )
    if shift_params.get("drones") != "_NotUse":
        raise RuntimeContractError(
            f"{daily_path} committed drone target must be the non-interactive "
            "_NotUse runtime placeholder"
        )

    protected_dorm_filename = "protected-dorm.json"
    _require_exact_params(
        daily_path,
        dorm_params,
        {
            "mode": 10000,
            "facility": ["Dorm"],
            "threshold": 0.3,
            "dorm_notstationed_enabled": True,
            "dorm_trust_enabled": False,
            "filename": protected_dorm_filename,
            "plan_index": 0,
        },
        task_type="protected Dorm Infrast",
    )
    protected_dorm_path = root / "config/infrast" / protected_dorm_filename
    try:
        protected_dorm = json.loads(protected_dorm_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(
            f"cannot read {protected_dorm_path}: {exc}"
        ) from exc
    expected_dorm_room = {"operators": [], "autofill": True}
    expected_protected_dorm = {
        "title": "Protected dorm recovery from unstationed operators only",
        "plans": [
            {
                "name": "Unstationed-only autofill",
                "rooms": {"dormitory": [expected_dorm_room] * 4},
            }
        ],
    }
    if protected_dorm != expected_protected_dorm:
        raise RuntimeContractError(
            f"{protected_dorm_path} must contain exactly four empty-list autofill "
            "dorms and no other facility or named operator"
        )

    recruit_tasks = [
        task
        for task in daily_tasks
        if isinstance(task, dict) and task.get("type") == "Recruit"
    ]
    if len(recruit_tasks) != 2:
        raise RuntimeContractError(
            f"{daily_path} must contain ordinary and Support Robot Recruit tasks"
        )
    ordinary_recruit = recruit_tasks[0].get("params")
    robot_recruit = recruit_tasks[1].get("params")
    if not isinstance(ordinary_recruit, dict) or not isinstance(robot_recruit, dict):
        raise RuntimeContractError(f"{daily_path} Recruit task has no params table")
    if ordinary_recruit.get("select") != [5, 4]:
        raise RuntimeContractError(
            f"{daily_path} ordinary Recruit must select 5-star and 4-star tags"
        )
    if ordinary_recruit.get("confirm") != [5, 4, 3]:
        raise RuntimeContractError(
            f"{daily_path} ordinary Recruit must auto-confirm 3-star through 5-star only"
        )
    if ordinary_recruit.get("preserve_tags") != ["支援机械"]:
        raise RuntimeContractError(
            f"{daily_path} ordinary Recruit must reserve Support Robot for its dedicated pass"
        )
    if ordinary_recruit.get("recruitment_time") != {"3": 540, "4": 540, "5": 540}:
        raise RuntimeContractError(
            f"{daily_path} ordinary Recruit must use explicit 09:00 times"
        )
    if robot_recruit.get("refresh") is not False or robot_recruit.get("force_refresh") is not False:
        raise RuntimeContractError(
            f"{daily_path} Support Robot Recruit must never refresh tags"
        )
    if robot_recruit.get("select") != [5, 4, 1] or robot_recruit.get("confirm") != [5, 4, 3, 1]:
        raise RuntimeContractError(
            f"{daily_path} Support Robot Recruit must auto-confirm every non-6-star result"
        )
    if robot_recruit.get("first_tags") != ["支援机械"]:
        raise RuntimeContractError(
            f"{daily_path} Support Robot Recruit must explicitly prefer Support Robot"
        )
    if robot_recruit.get("preserve_tags") != []:
        raise RuntimeContractError(
            f"{daily_path} Support Robot Recruit must disable implicit robot preservation"
        )
    if robot_recruit.get("recruitment_time") != {"3": 230, "4": 540, "5": 540}:
        raise RuntimeContractError(
            f"{daily_path} Support Robot Recruit must use 03:50 for robot and 09:00 for 4/5-star"
        )
    for recruit_params in recruit_tasks:
        params = recruit_params.get("params")
        if isinstance(params, dict) and "skip_robot" in params:
            raise RuntimeContractError(
                f"{daily_path} Recruit must use explicit preserve_tags, not deprecated skip_robot"
            )

    award_path = tasks / "award-only.toml"
    award = _load_config(award_path)
    if award.get("startup") is not True or award.get("closedown") is not False:
        raise RuntimeContractError(
            f"{award_path} must start the game and keep launcher-owned Waydroid open"
        )
    award_tasks = award.get("tasks")
    if (
        not isinstance(award_tasks, list)
        or len(award_tasks) != 1
        or not isinstance(award_tasks[0], dict)
        or award_tasks[0].get("type") != "Award"
    ):
        raise RuntimeContractError(
            f"{award_path} must contain exactly one Award task"
        )
    award_params = award_tasks[0].get("params")
    if not isinstance(award_params, dict):
        raise RuntimeContractError(f"{award_path} Award task has no params table")
    _require_exact_params(
        award_path,
        award_params,
        {
            "award": True,
            "mail": False,
            "recruit": False,
            "orundum": False,
            "mining": False,
            "specialaccess": False,
        },
        task_type="Award",
    )


def validate_farming_contracts(root: Path) -> None:
    try:
        planner_config = load_config(root / "config/farming.toml")
        overlay_item_index = (
            root / "var/data/MaaResource/resource/item_index.json"
        )
        item_index = (
            overlay_item_index
            if overlay_item_index.is_file()
            else root / "var/data/resource/item_index.json"
        )
        validate_blue_material_chains_against_item_index(
            planner_config.blue_material_chains,
            item_index,
        )
    except (ConfigError, MaterialRecipeError) as exc:
        raise RuntimeContractError(
            f"blue-material inventory equivalence contract is invalid: {exc}"
        ) from exc
    tasks = root / "config/tasks"

    depot_path = tasks / "depot.toml"
    depot = _load_config(depot_path)
    depot_tasks = depot.get("tasks")
    if depot.get("startup") is not True or depot.get("closedown") is not False:
        raise RuntimeContractError(
            f"{depot_path} must combine startup and the read-only scan in one "
            "task chain without closing the launcher-owned session"
        )
    if (
        not isinstance(depot_tasks, list)
        or len(depot_tasks) != 1
        or not isinstance(depot_tasks[0], dict)
        or depot_tasks[0].get("type") != "Depot"
        or "params" in depot_tasks[0]
    ):
        raise RuntimeContractError(
            f"{depot_path} must contain exactly one unparameterized Depot task"
        )

    proxy_path = tasks / "proxy-preflight.toml"
    proxy = _load_config(proxy_path)
    if proxy.get("startup") is not False or proxy.get("closedown") is not False:
        raise RuntimeContractError(f"{proxy_path} must not own startup or closedown")
    proxy_tasks = proxy.get("tasks")
    if (
        not isinstance(proxy_tasks, list)
        or len(proxy_tasks) != 3
        or not all(isinstance(task, dict) for task in proxy_tasks)
        or [task.get("type") for task in proxy_tasks] != ["Custom"] * 3
    ):
        raise RuntimeContractError(f"{proxy_path} must contain three no-battle Custom tasks")
    stage_placeholder = "1-7"
    for task, names in zip(proxy_tasks, (
        ["MaaHostProxyTerminal"], [stage_placeholder], ["StageQueue@CheckPrts"],
    ), strict=True):
        if not isinstance(task.get("params"), dict):
            raise RuntimeContractError(f"{proxy_path} task has no params table")
        _require_exact_params(proxy_path, task["params"], {"task_names": names},
                              task_type="zero-sanity proxy Custom check")
    from .proxy import PROXY_RESOURCE
    resource_path = root / "config/resource/tasks/tasks.json"
    try:
        resource = json.loads(resource_path.read_text(encoding="utf-8"))
        resource_files = {p.relative_to(root / "config/resource").as_posix()
                          for p in (root / "config/resource").rglob("*") if p.is_file()}
    except (OSError, ValueError) as exc:
        raise RuntimeContractError(f"cannot validate proxy safety guards: {exc}") from exc
    if resource != PROXY_RESOURCE or resource_files != {"tasks/tasks.json"}:
        raise RuntimeContractError("proxy overlay must contain only the exact no-spend guards")

    sanity_path = tasks / "sanity-fight.toml"
    _, sanity = _load_fight_task(sanity_path)
    if sanity.get("stage") != stage_placeholder:
        raise RuntimeContractError(
            f"{sanity_path} stage must use the non-interactive runtime placeholder"
        )
    _require_exact_params(
        sanity_path,
        sanity,
        {
            "medicine": 0,
            "medicine_expire_days": 2,
            "stone": 0,
            "series": 0,
            "times": 2147483647,
            "stage": stage_placeholder,
        },
    )

    verify_path = tasks / "verify-fight.toml"
    _, verify = _load_fight_task(verify_path)
    if verify.get("stage") != stage_placeholder:
        raise RuntimeContractError(
            f"{verify_path} stage must use the non-interactive runtime placeholder"
        )
    _require_exact_params(
        verify_path,
        verify,
        {
            "medicine": 0,
            "medicine_expire_days": 2,
            "stone": 0,
            "times": 1,
            "series": 1,
            "stage": stage_placeholder,
        },
    )

    annihilation_path = tasks / "annihilation.toml"
    _, annihilation = _load_fight_task(annihilation_path)
    _require_exact_params(
        annihilation_path,
        annihilation,
        {
            "stage": "Annihilation",
            "medicine": 0,
            "medicine_expire_days": 2,
            "stone": 0,
            "times": 1,
            "series": 1,
        },
    )


def validate_runtime_contracts(root: Path) -> None:
    validate_service_contracts(root)
    validate_farming_contracts(root)
