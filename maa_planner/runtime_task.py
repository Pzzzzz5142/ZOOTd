from __future__ import annotations

import copy
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .util import atomic_write_bytes


class RuntimeTaskError(ValueError):
    """Raised when a managed task cannot be rendered without user input."""


_STAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,63}")


@dataclass(frozen=True)
class RuntimeParameter:
    task_index: int
    name: str
    placeholder: str
    validate: Callable[[str], bool]


def _valid_drone_mode(value: str) -> bool:
    return value in {"_NotUse", "PureGold", "Money"}


def _valid_stage(value: str) -> bool:
    return _STAGE_PATTERN.fullmatch(value) is not None


RUNTIME_PARAMETERS: dict[str, RuntimeParameter] = {
    "daily": RuntimeParameter(0, "drones", "_NotUse", _valid_drone_mode),
    "proxy-preflight": RuntimeParameter(0, "stage", "1-7", _valid_stage),
    "sanity-fight": RuntimeParameter(0, "stage", "1-7", _valid_stage),
    "verify-fight": RuntimeParameter(0, "stage", "1-7", _valid_stage),
}


def _task_params(config: object, task_index: int, source: Path) -> dict[str, object]:
    if not isinstance(config, dict):
        raise RuntimeTaskError(f"managed task is not a TOML table: {source}")
    tasks = config.get("tasks")
    if not isinstance(tasks, list) or task_index >= len(tasks):
        raise RuntimeTaskError(f"managed task has no task index {task_index}: {source}")
    task = tasks[task_index]
    if not isinstance(task, dict) or not isinstance(task.get("params"), dict):
        raise RuntimeTaskError(f"managed task index {task_index} has no params: {source}")
    return task["params"]


def render_runtime_task(
    project_root: Path,
    task_name: str,
    value: str,
    destination: Path,
) -> Path:
    """Render one allowlisted planner value into an isolated managed-task copy."""

    parameter = RUNTIME_PARAMETERS.get(task_name)
    if parameter is None:
        raise RuntimeTaskError(f"task does not accept a runtime parameter: {task_name!r}")
    if not parameter.validate(value):
        raise RuntimeTaskError(
            f"unsafe {parameter.name} value for task {task_name}: {value!r}"
        )

    root = project_root.resolve()
    source = root / "config/tasks" / f"{task_name}.toml"
    if destination.resolve() == source.resolve():
        raise RuntimeTaskError("runtime task destination must not replace its source")
    try:
        text = source.read_text(encoding="utf-8")
        source_config = tomllib.loads(text)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeTaskError(f"cannot read managed task {source}: {exc}") from exc

    params = _task_params(source_config, parameter.task_index, source)
    if params.get(parameter.name) != parameter.placeholder:
        raise RuntimeTaskError(
            f"managed task placeholder changed for {task_name}.{parameter.name}"
        )

    placeholder_line = f'{parameter.name} = {json.dumps(parameter.placeholder)}'
    replacement_line = f'{parameter.name} = {json.dumps(value)}'
    if text.splitlines().count(placeholder_line) != 1:
        raise RuntimeTaskError(
            f"managed task must contain exactly one canonical {parameter.name} placeholder"
        )
    rendered_text = text.replace(placeholder_line, replacement_line, 1)
    try:
        rendered_config = tomllib.loads(rendered_text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeTaskError(f"rendered task is invalid TOML: {exc}") from exc

    expected_config = copy.deepcopy(source_config)
    expected_params = _task_params(expected_config, parameter.task_index, source)
    expected_params[parameter.name] = value
    if rendered_config != expected_config:
        raise RuntimeTaskError(
            f"rendering {task_name} changed fields other than {parameter.name}"
        )

    atomic_write_bytes(destination, rendered_text.encode("utf-8"), mode=0o600)
    return destination
