from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterator


class RuntimeReceiptError(ValueError):
    """Raised when the promoted runtime no longer matches its validation receipt."""


RECEIPT_SCHEMA_VERSION = 3
FINGERPRINT_SCHEMA_VERSION = 1
VALIDATED_TASKS = (
    "daily",
    "award-only",
    "annihilation",
    "depot",
    "proxy-preflight",
    "sanity-fight",
    "verify-fight",
    "material-recipe-item-index",
)

_MANAGED_CONFIG_FILES = (
    "config/cli.toml",
    "config/infrast/protected-dorm.json",
    "config/profiles/waydroid.toml",
    "config/tasks/annihilation.toml",
    "config/tasks/award-only.toml",
    "config/tasks/daily.toml",
    "config/tasks/depot.toml",
    "config/tasks/proxy-preflight.toml",
    "config/tasks/sanity-fight.toml",
    "config/tasks/verify-fight.toml",
)
_MANAGED_CONFIG_DIRECTORIES: tuple[str, ...] = ()
_RUNTIME_DIRECTORIES = ("lib", "resource", "cache")
_OPTIONAL_RUNTIME_DIRECTORIES = ("MaaResource",)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _regular_directory(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeReceiptError(f"cannot inspect {label}: {path}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise RuntimeReceiptError(f"{label} must be a real directory: {path}")
    return metadata


def _regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeReceiptError(f"cannot inspect {label}: {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RuntimeReceiptError(f"{label} must be a regular file: {path}")
    return metadata


def _hash_file(path: Path, label: str) -> str:
    before = _regular_file(path, label)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
    except OSError as exc:
        raise RuntimeReceiptError(f"cannot hash {label}: {path}: {exc}") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise RuntimeReceiptError(f"{label} changed while it was being hashed: {path}")
    return digest.hexdigest()


def _walk_regular_files(root: Path, label: str) -> Iterator[Path]:
    _regular_directory(root, label)
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        directory_path = Path(directory)
        for name in directory_names:
            child = directory_path / name
            metadata = child.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or child.is_symlink():
                raise RuntimeReceiptError(
                    f"{label} contains a non-directory or symlinked directory: {child}"
                )
        for name in file_names:
            child = directory_path / name
            _regular_file(child, label)
            yield child


def _managed_config_sha256(project_root: Path) -> str:
    files: list[Path] = []
    for relative in _MANAGED_CONFIG_FILES:
        path = project_root / relative
        _regular_file(path, "managed configuration")
        files.append(path)
    for relative in _MANAGED_CONFIG_DIRECTORIES:
        files.extend(
            _walk_regular_files(project_root / relative, "managed configuration")
        )

    digest = hashlib.sha256(b"maa-managed-config-v1\0")
    for path in sorted(files, key=lambda item: item.relative_to(project_root).as_posix()):
        relative = path.relative_to(project_root).as_posix()
        before = _regular_file(path, "managed configuration")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(before.st_mode):o}".encode("ascii"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.stat()
        except OSError as exc:
            raise RuntimeReceiptError(
                f"cannot hash managed configuration: {path}: {exc}"
            ) from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeReceiptError(
                f"managed configuration changed while it was being hashed: {path}"
            )
        digest.update(b"\0")
    return digest.hexdigest()


def _runtime_tree_sha256(data_dir: Path) -> str:
    digest = hashlib.sha256(b"maa-runtime-tree-v1\0")
    roots: list[Path] = []
    for name in _RUNTIME_DIRECTORIES:
        root = data_dir / name
        _regular_directory(root, f"runtime {name}")
        roots.append(root)
    for name in _OPTIONAL_RUNTIME_DIRECTORIES:
        root = data_dir / name
        if root.exists() or root.is_symlink():
            _regular_directory(root, f"runtime {name}")
            roots.append(root)

    files: list[Path] = []
    for root in roots:
        files.extend(_walk_regular_files(root, "runtime generation"))
    for path in sorted(files, key=lambda item: item.relative_to(data_dir).as_posix()):
        relative = path.relative_to(data_dir).as_posix()
        metadata = path.stat()
        record = (
            f"{relative}\0{metadata.st_dev}\0{metadata.st_ino}\0"
            f"{stat.S_IMODE(metadata.st_mode):o}\0{metadata.st_size}\0"
            f"{metadata.st_mtime_ns}\0"
        )
        digest.update(record.encode("utf-8"))
    return digest.hexdigest()


def runtime_generation_fingerprint(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    data_dir = root / "var/data"
    data_metadata = _regular_directory(data_dir, "runtime generation")
    core_library = data_dir / "lib/libMaaCore.so"
    hot_tasks = data_dir / "cache/resource/tasks/tasks.json"
    activity = data_dir / "cache/StageActivityV2.json"

    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "runtime_root": {
            "device": str(data_metadata.st_dev),
            "inode": str(data_metadata.st_ino),
        },
        "runtime_tree_sha256": _runtime_tree_sha256(data_dir),
        "managed_config_sha256": _managed_config_sha256(root),
        "core_library_sha256": _hash_file(core_library, "MaaCore library"),
        "hot_cache": {
            "tasks_sha256": _hash_file(hot_tasks, "MAA hot task cache"),
            "activity_sha256": _hash_file(activity, "MAA activity cache"),
        },
    }


def _load_receipt(path: Path) -> dict[str, Any]:
    _regular_file(path, "runtime receipt")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeReceiptError(f"cannot read runtime receipt {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeReceiptError("runtime receipt must be a JSON object")
    return value


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _validate_common_receipt(value: dict[str, Any]) -> None:
    if value.get("status") not in {"active", "kept-previous"}:
        raise RuntimeReceiptError(
            f"runtime receipt status is not runnable: {value.get('status')!r}"
        )
    core = value.get("core")
    if (
        not isinstance(core, dict)
        or core.get("channel") != "stable"
        or not isinstance(core.get("active_version"), str)
        or not core["active_version"]
    ):
        raise RuntimeReceiptError("runtime receipt has no validated stable Core")
    validation = value.get("validation")
    if not isinstance(validation, dict):
        raise RuntimeReceiptError("runtime receipt has no validation record")


def _validate_legacy_receipt(project_root: Path, value: dict[str, Any]) -> str:
    """Accept the already-validated schema-2 generation until the next updater run.

    Schema 2 predates the complete generation seal. The live wrapper already
    prevents Core/resource mutation, so matching both promoted hot-cache hashes
    plus the static execution contracts is a safe, read-only migration bridge.
    A successful runtime update always replaces this with schema 3.
    """

    _validate_common_receipt(value)
    data_dir = project_root / "var/data"
    _regular_directory(data_dir, "legacy runtime generation")
    _regular_directory(data_dir / "lib", "legacy runtime library")
    _regular_directory(data_dir / "resource", "legacy runtime resource")
    _regular_file(data_dir / "lib/libMaaCore.so", "legacy MaaCore library")

    hot_cache = value.get("hot_cache")
    if not isinstance(hot_cache, dict):
        raise RuntimeReceiptError("legacy receipt has no hot-cache evidence")
    expected_tasks = hot_cache.get("tasks_sha256")
    expected_activity = hot_cache.get("activity_sha256")
    if not _valid_sha256(expected_tasks) or not _valid_sha256(expected_activity):
        raise RuntimeReceiptError("legacy receipt has invalid hot-cache hashes")
    actual_tasks = _hash_file(
        data_dir / "cache/resource/tasks/tasks.json", "legacy MAA hot task cache"
    )
    actual_activity = _hash_file(
        data_dir / "cache/StageActivityV2.json", "legacy MAA activity cache"
    )
    if actual_tasks != expected_tasks or actual_activity != expected_activity:
        raise RuntimeReceiptError("legacy promoted hot cache differs from its receipt")
    return "legacy-schema-2"


def validate_runtime_receipt(project_root: Path) -> str:
    root = project_root.resolve()
    value = _load_receipt(root / "var/state/runtime/maa-resource.json")
    schema_version = value.get("schema_version")
    if schema_version == 2:
        return _validate_legacy_receipt(root, value)
    if schema_version != RECEIPT_SCHEMA_VERSION:
        raise RuntimeReceiptError(
            f"unsupported runtime receipt schema: {schema_version!r}"
        )

    _validate_common_receipt(value)
    validation = value["validation"]
    if validation.get("mode") != "candidate MaaCore dry-run" or validation.get(
        "tasks"
    ) != list(VALIDATED_TASKS):
        raise RuntimeReceiptError("runtime receipt does not cover every managed task")

    recorded = value.get("generation")
    if not isinstance(recorded, dict):
        raise RuntimeReceiptError("runtime receipt has no generation fingerprint")
    actual = runtime_generation_fingerprint(root)
    if recorded != actual:
        raise RuntimeReceiptError(
            "live Core/resource/cache/config generation differs from its validated receipt"
        )

    hot_cache = value.get("hot_cache")
    if not isinstance(hot_cache, dict):
        raise RuntimeReceiptError("runtime receipt has no hot-cache evidence")
    if (
        hot_cache.get("tasks_sha256") != actual["hot_cache"]["tasks_sha256"]
        or hot_cache.get("activity_sha256")
        != actual["hot_cache"]["activity_sha256"]
    ):
        raise RuntimeReceiptError("runtime receipt hot-cache evidence is inconsistent")
    return "sealed-schema-3"
