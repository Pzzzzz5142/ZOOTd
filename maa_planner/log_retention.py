"""Idle-only seven-day retention for project logs and complete audit bundles."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import shutil
import time

from .supervisor import SupervisorError, load_run_events

RETENTION_SECONDS = 7 * 24 * 60 * 60
REPORT_AREAS = {
    'var/state/host': 'managed',
    'var/state/debug': 'managed',
    'var/state/planner': 'managed',
    'var/state/supervisor': 'managed',
    'var/state/recovery': 'managed',
    'var/state/copilot': 'excluded',
    'var/state/navigation': 'excluded',
    'var/state/event-solver': 'excluded',
    'var/battle-solver': 'excluded',
    'var/state/preserved-worktrees': 'excluded',
    'var/cache': 'excluded',
    'var/data': 'excluded',
}


@dataclass
class RetentionPlan:
    deletions: list[Path]
    rotations: list[Path]
    retained_runs: dict[str, list[str]]
    stale_recoveries: list[str]


def regular_files(path: Path):
    """Never traverse links, including links in an allowlisted directory's parents."""
    if any(p.is_symlink() for p in (path, *path.parents)):
        return
    if path.is_file():
        yield path
    elif path.is_dir():
        for parent, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(parent) / d).is_symlink()]
            for name in files:
                item = Path(parent) / name
                if not item.is_symlink() and item.is_file():
                    yield item


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def plan(root: Path, now: float) -> RetentionPlan:
    """Keep live state, referenced evidence and complete parent/child run chains."""
    state = root / 'var/state'
    cutoff = now - RETENTION_SECONDS
    for area in ('supervisor/runs', 'recovery', 'debug/archive'):
        path = state / area
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError(f'retention directory must not traverse symlinks: {area}')
    runs = {}
    bundles = {}
    keep = {}
    stale_recoveries = []

    def preserve(rid: str, reason: str):
        keep.setdefault(rid, set()).add(reason)

    protected = set()
    values = []
    for area in ('planner', 'supervisor'):
        for path in regular_files(state / area):
            if path.parent == state / area and path.suffix == '.json' and not path.name.startswith(('launcher-', 'annihilation-launcher-')):
                values.append(json.loads(path.read_text()))
    for directory in sorted((state / 'supervisor/runs').glob('*')):
        if not directory.is_dir() or directory.is_symlink():
            continue
        events = load_run_events(root, directory.name)
        runs[directory.name] = events
        recovery = state / 'recovery' / directory.name
        files = list(regular_files(directory)) + list(regular_files(recovery))
        bundles[directory.name] = files
        types = {e['event_type'] for e in events}
        recent = max(p.stat().st_mtime for p in files) >= cutoff
        # Do not prune while a recovery controller may be between adapter calls.
        if 'recovery-started' in types and 'recovery-finished' not in types:
            if recent:
                raise ValueError(f'unfinished recovery; retention deferred: {directory.name}')
            # Controllers have a nine-hour budget. With no bundle writes for
            # seven days this is historical, but its audit/evidence still stay.
            preserve(directory.name, 'unfinished-recovery')
            stale_recoveries.append(directory.name)
        if recent:
            preserve(directory.name, 'recent')
        if 'run-finished' not in types:
            preserve(directory.name, 'unfinished-run')
        if (recovery / 'repair-worktree').exists():
            preserve(directory.name, 'repair-worktree')
    successes = [rid for rid, events in runs.items()
                 if events[0]['payload'].get('mode') == 'full'
                 and any(e['event_type'] == 'run-finished' and e['payload'].get('status') == 'success' for e in events)]
    if successes:
        preserve(max(successes), 'latest-full-success')
    expanded = set()
    while values or keep.keys() - expanded:
        for rid in keep.keys() - expanded:
            values.extend(runs[rid])
            expanded.add(rid)
        pending, values = values, []
        for value in pending:
            for raw in strings(value):
                if raw in runs and raw not in keep:
                    preserve(raw, 'referenced')
                relative = raw.removeprefix(str(root) + '/')
                if not relative.startswith('var/state/'):
                    continue
                path = root / relative
                if '..' in path.parts:
                    continue
                protected.add(path)
                parts = Path(relative).parts
                if (len(parts) > 4 and parts[2:4] == ('supervisor', 'runs')
                        and parts[4] in runs and parts[4] not in keep):
                    preserve(parts[4], 'referenced')
    for rid in keep:
        protected.update(bundles[rid])
    deletions = []
    for rid in runs.keys() - keep:
        deletions.append(state / 'supervisor/runs' / rid)
        recovery = state / 'recovery' / rid
        if recovery.is_dir() and not recovery.is_symlink():
            deletions.append(recovery)
    candidates = []
    for area in ('host', 'debug', 'planner/decisions', 'planner/annihilation-decisions',
                 'planner/advisor-probes', 'supervisor/probes'):
        candidates.extend(regular_files(state / area))
    for pattern in ('launcher-*.json', 'annihilation-launcher-*.json'):
        for path in (state / 'planner').glob(pattern):
            candidates.extend(regular_files(path))
    for path in candidates:
        if path not in protected and path.stat().st_mtime < cutoff:
            deletions.append(path)
    rotations = [p for name in ('asst.log', 'asst.bak.log')
                 for p in regular_files(state / 'debug' / name)
                 if p not in deletions and p not in protected and p.stat().st_size]
    return RetentionPlan(sorted(deletions), rotations,
                         {rid: sorted(reasons) for rid, reasons in sorted(keep.items())},
                         sorted(stale_recoveries))


def summarize(root: Path, planned: RetentionPlan) -> dict:
    """Count bytes before mutation; rotations preserve bytes rather than reclaim them."""
    expired = {p for item in planned.deletions for p in regular_files(item)}
    rotated = set(planned.rotations)
    result = {}
    for relative, policy in REPORT_AREAS.items():
        row = {'policy': policy, 'files': 0, 'bytes': 0,
               'expired_files': 0, 'bytes_expired': 0,
               'rotated_files': 0, 'bytes_rotated': 0}
        for path in regular_files(root / relative):
            size = path.stat().st_size
            row['files'] += 1
            row['bytes'] += size
            if path in expired:
                row['expired_files'] += 1
                row['bytes_expired'] += size
            if path in rotated:
                row['rotated_files'] += 1
                row['bytes_rotated'] += size
        result[relative] = row
    return result


def retain(root: Path, *, dry_run: bool = False, now: float | None = None) -> dict:
    root = root.resolve()
    now = time.time() if now is None else now
    lock_dir = root / 'var/run'
    lock_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        # Same order as launcher/SDK updater; never wait while holding another lock.
        for name in ('maa-daily.lock', 'zootd.lock', 'codex-sdk.lock'):
            lock = stack.enter_context((lock_dir / name).open('a'))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'busy', 'busy_lock': name, 'deleted': [], 'rotated': []}
        try:
            planned = plan(root, now)
        except (ValueError, SupervisorError) as exc:
            return {'status': 'deferred', 'reason': str(exc), 'deleted': [], 'rotated': []}
        deletions, rotations = planned.deletions, planned.rotations
        summary = summarize(root, planned)
        reclaimed = sum(p.stat().st_size for item in deletions for p in regular_files(item))
        if not dry_run:
            for path in deletions:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            for path in rotations:
                archive = path.parent / 'archive'
                archive.mkdir(exist_ok=True)
                if archive.is_symlink():
                    raise ValueError('log archive must not be a symlink')
                # Rename preserves content and mtime. Exclusive link avoids overwrites.
                target = archive / f'{path.name}.{time.time_ns()}'
                os.link(path, target)
                path.unlink()
        return {'status': 'dry-run' if dry_run else 'ok', 'retention_days': 7,
                'bytes_expired': reclaimed,
                'summary': summary, 'retained_runs': planned.retained_runs,
                'stale_recoveries': planned.stale_recoveries,
                'deleted': [str(p.relative_to(root)) for p in deletions],
                'rotated': [str(p.relative_to(root)) for p in rotations]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--summary', action='store_true', help='show category totals instead of file lists')
    args = parser.parse_args(argv)
    result = retain(args.project_root, dry_run=args.dry_run)
    if not args.dry_run or args.summary:
        result['deleted_entries'] = len(result.pop('deleted'))
    if args.summary:
        result['rotated_entries'] = len(result.pop('rotated'))
        kept = result.pop('retained_runs', {})
        result['retained_runs'] = len(kept)
        result['retained_run_reasons'] = {
            reason: sum(reason in reasons for reasons in kept.values())
            for reason in sorted({r for reasons in kept.values() for r in reasons})}
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.summary else None))


if __name__ == '__main__':
    main()
