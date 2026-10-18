"""Read-only backups of pre-existing edits before startup recovery."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from typing import Any, Mapping

from .util import sha256_bytes


class PreservationError(RuntimeError):
    pass


def worktree_snapshot(root: Path) -> dict[str, bytes]:
    def git(*args: str) -> bytes:
        result = subprocess.run(
            ("git", *args), cwd=root, capture_output=True, check=False,
        )
        if result.returncode:
            raise PreservationError("cannot snapshot pre-existing worktree changes")
        return result.stdout

    status = git("status", "--porcelain=v1", "--untracked-files=all", "-z")
    worktree = git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")
    index = git("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--")
    paths = git("ls-files", "--others", "--exclude-standard", "-z")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", dereference=False) as output:
        for raw in sorted(filter(None, paths.split(b"\0"))):
            relative = Path(os.fsdecode(raw))
            if relative.is_absolute() or ".." in relative.parts:
                raise PreservationError("unsafe untracked path in worktree snapshot")
            path = root / relative
            if not path.is_symlink() and not path.is_file():
                raise PreservationError("untracked entry is not a regular file or symlink")
            output.add(path, arcname=str(relative), recursive=False)
    return {
        "status.bin": status,
        "worktree.patch": worktree,
        "index.patch": index,
        "untracked.tar": archive.getvalue(),
    }


def preserve_worktree(root: Path, run_id: str, attempt_id: str) -> dict[str, Any]:
    # This function never resets, stashes, moves, commits, or runs user files.
    content = worktree_snapshot(root)
    # User edits are not logs: keep them outside the seven-day recovery cleanup.
    directory = root / "var/state/preserved-worktrees" / run_id / attempt_id
    if not directory.resolve().is_relative_to(root.resolve()):
        raise PreservationError("worktree backup is outside the project")
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    descriptors: dict[str, Any] = {}
    for name, raw in content.items():
        path = directory / name
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(raw)
        descriptors[name] = {
            "path": str(path.relative_to(root)),
            "size": len(raw),
            "sha256": sha256_bytes(raw),
        }
    if content != worktree_snapshot(root):
        raise PreservationError("working tree changed while creating recovery backup")
    head = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=root).decode().strip()
    manifest = {"schema_version": 1, "base_head": head, "files": descriptors}
    raw_manifest = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    path = directory / "manifest.json"
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(raw_manifest)
    descriptors["manifest.json"] = {
        "path": str(path.relative_to(root)), "size": len(raw_manifest),
        "sha256": sha256_bytes(raw_manifest),
    }
    return {"schema_version": 1, "files": descriptors}


def verify_preservation(root: Path, backup: Mapping[str, Any]) -> None:
    for descriptor in backup["files"].values():
        path = root / descriptor["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise PreservationError("worktree backup path was replaced")
        raw = path.read_bytes()
        if len(raw) != descriptor["size"] or sha256_bytes(raw) != descriptor["sha256"]:
            raise PreservationError("pre-existing worktree backup was modified")


def worktree_matches_backup(root: Path, backup: Mapping[str, Any]) -> bool:
    return all(
        sha256_bytes(raw) == backup["files"][name]["sha256"]
        for name, raw in worktree_snapshot(root).items()
    )
