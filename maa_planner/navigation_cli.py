"""Explicit zero-battle navigation; persist plans and evidence only under var/."""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from .copilot_core import callbacks_are_fresh, map_recognized
from .copilot_navigation import navigation_tasks
from .copilot_run import command, device, device_lock, execute
from .navigation_catalog import load_navigation
from .prts import PrtsError
from .proxy import PROXY_RESOURCE
from .runtime_receipt import validate_runtime_receipt
from .util import atomic_write_json


def navigate(root, stage, *, plan_only=False, refresh=False):
    run = root / 'var/state/navigation' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, mode=0o700)
    audit = {'status': 'failed', 'requested_stage': stage, 'run_dir': str(run),
             'consumes_sanity': False}
    try:
        with device_lock(root):
            catalog = load_navigation(root, refresh=refresh)
            route = catalog.route(stage)
            route['navigation_only'] = True
            audit.update(navigation=route, sources=catalog.evidence)
            atomic_write_json(run / 'navigation.json', route, mode=0o600)
            if plan_only:
                audit['status'] = 'planned'
                return audit
            if command(['git', '-C', str(root), 'status', '--porcelain']).strip():
                raise PrtsError('dirty_worktree', 'Navigation requires a committed, clean checkout.')
            audit['git_head'] = command(['git', '-C', str(root), 'rev-parse', 'HEAD']).strip()
            audit['runtime'] = validate_runtime_receipt(root)
            tasks = navigation_tasks(route)
            tasks.update(PROXY_RESOURCE)
            atomic_write_json(run / 'navigation/resource/tasks/tasks.json', tasks, mode=0o600)
            with device(root, run) as address:
                start = time.monotonic_ns()
                exit_code = execute(root, run, address)
                end = time.monotonic_ns()
            events_path = run / 'callbacks.jsonl'
            events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
            completed = any(map_recognized(e['message'], e['details'], route['code']) for e in events)
            failures = any(e['message'] in (0, 1, 10000, 10004)
                           or e['details'].get('what') == 'GameOffline' for e in events)
            worker = json.loads((run / 'worker-result.json').read_bytes())
            audit['exit_code'] = exit_code
            if (exit_code == 0 and completed and not failures
                    and worker.get('phase') == 'navigation_complete'
                    and callbacks_are_fresh(events, run_id=run.name, started_ns=start, finished_ns=end)):
                audit['status'] = 'success'
            else:
                audit['category'] = 'stage_locked' if worker.get('phase') == 'stage_locked' else 'navigation_failure'
                audit['error'] = ('Stage prerequisite is locked.' if audit['category'] == 'stage_locked' else
                                  'Target detail panel was not proven; inspect this run’s callbacks.')
    except PrtsError as exc:
        audit.update(category=exc.category, error=str(exc))
    except (Exception, KeyboardInterrupt) as exc:
        audit['error'] = type(exc).__name__
    finally:
        atomic_write_json(run / 'result.json', audit, mode=0o600)
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('stage')
    parser.add_argument('--plan', action='store_true', help='Resolve data only, without starting the game')
    parser.add_argument('--refresh', action='store_true', help='Refresh the game data snapshot immediately')
    args = parser.parse_args(argv)
    result = navigate(args.project_root.resolve(), args.stage, plan_only=args.plan, refresh=args.refresh)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('success', 'planned') else 1


if __name__ == '__main__':
    raise SystemExit(main())
