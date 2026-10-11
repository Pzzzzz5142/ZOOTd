"""Explicit zero-battle navigation; persist plans and evidence only under var/."""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from .copilot_core import callbacks_are_fresh, map_recognized, special_panel_complete
from .copilot_navigation import navigation_tasks, zero_result_recovery_tasks, install_navigation_resources
from .copilot_run import command, device, device_lock, execute
from .navigation_catalog import load_navigation
from .prts import PrtsError
from .proxy import PROXY_RESOURCE
from .runtime_receipt import validate_runtime_receipt
from .util import atomic_write_json


def _startup_zero_limit(value):
    """Native StartUp disables these nodes instead of executing their clicks."""
    detail = value.get('details', {})
    return (value.get('taskchain') == 'StartUp' and value.get('subtask') == 'ProcessTask'
            and value.get('what') == 'ExceededLimit' and isinstance(detail, dict)
            and detail.get('task') in ('ReturnButton', 'StartButton1')
            and type(detail.get('exec_times')) is int and detail['exec_times'] == 0
            and type(detail.get('max_times')) is int and detail['max_times'] == 0
            and detail.get('action') is None)


def navigation_complete(events, code, *, route=None):
    """Bind target OCR to the same completed Custom chain and final task list."""
    active = completed = None
    matched = finished = False
    chain_start = 0
    startup = transition_device = None
    startup_process_ids = set()
    startup_completed = startup_transition = False
    for index, event in enumerate(events):
        msg, value = event['message'], event['details']
        key = (value.get('uuid'), value.get('taskid'))
        if (msg == 20003 and startup is not None and not startup_completed
                and key[0] == startup[0] and type(key[1]) is int
                and key[1] in startup_process_ids and _startup_zero_limit(value)):
            # A Runout notification follows an observed native ProcessTask;
            # it is harmless only if the enclosing StartUp later completes.
            startup_transition = True
            transition_device = startup[0]
            continue
        if (msg in (0, 1, 10000, 10004, 20000, 20004)
                or value.get('what') in ('GameOffline', 'Disconnect', 'Reconnecting', 'ExceededLimit')
                or value.get('taskchain') in ('Fight', 'Copilot')):
            return False
        if msg == 10001:
            if startup_transition or (transition_device is not None and key[0] != transition_device):
                return False
            startup = key if (value.get('taskchain') == 'StartUp' and type(key[1]) is int
                              and key[1] >= 0 and isinstance(key[0], str) and key[0]) else None
            startup_process_ids.clear()
            startup_completed = False
            completed = None
            finished = False
            active = key if (value.get('taskchain') == 'Custom' and type(key[1]) is int
                             and isinstance(key[0], str) and key[0]) else None
            matched = False
            chain_start = index
        if startup is not None and key[0] == startup[0] and value.get('taskchain') == 'StartUp':
            if (msg in (20001, 20002) and value.get('subtask') == 'ProcessTask'
                    and type(key[1]) is int and key[1] >= 0 and not startup_completed):
                startup_process_ids.add(key[1])
            if msg == 10002 and type(key[1]) is int and key == startup:
                startup_completed = True
        if msg == 3 and startup is not None:
            if startup_transition:
                if (not startup_completed or type(key[1]) is not int or key != startup
                        or value.get('taskchain') != 'StartUp'
                        or value.get('finished_tasks') != [startup[1]]
                        or type(value['finished_tasks'][0]) is not int):
                    return False
                startup_transition = False
            startup = None
        if (active is not None and key == active and value.get('first') == ['ZootdNavigate']
                and map_recognized(msg, value, code)):
            matched = True
        if msg == 10002 and key == active and matched and value.get('taskchain') == 'Custom':
            completed = active
        if msg == 3 and completed is not None:
            finished = (key == completed and value.get('taskchain') == 'Custom'
                        and value.get('finished_tasks') == [completed[1]]
                        and type(value['finished_tasks'][0]) is int)
        if (msg == 3 and active is not None and special_panel_complete(
                events[chain_start:index + 1], task_id=active[1], code=code, route=route)):
            finished = True
    return finished and not startup_transition


def navigate(root, stage, *, plan_only=False, refresh=False):
    run = root / 'var/state/navigation' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, mode=0o700)
    audit = {'status': 'failed', 'requested_stage': stage, 'run_dir': str(run),
             'consumes_sanity': False}
    started = time.monotonic()
    try:
        with device_lock(root):
            catalog = load_navigation(root, refresh=refresh)
            route = catalog.route(stage)
            route['navigation_only'] = True
            route['recover_zero_result'] = True
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
            tasks.update(zero_result_recovery_tasks())
            tasks.update(PROXY_RESOURCE)
            # Legacy refill alias is absent from current native tasks. Keep
            # its fail-closed stop without inventing a nonexistent template
            # when the navigation overlay has its own template directory.
            tasks['ExpiringMedicineConfirm'] = {**tasks['ExpiringMedicineConfirm'],
                                                'algorithm': 'JustReturn'}
            atomic_write_json(run / 'navigation/resource/tasks/tasks.json', tasks, mode=0o600)
            install_navigation_resources(run / 'navigation/resource')
            with device(root, run) as address:
                start = time.monotonic_ns()
                exit_code = execute(root, run, address)
                end = time.monotonic_ns()
            events_path = run / 'callbacks.jsonl'
            events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
            completed = navigation_complete(events, route['code'], route=route)
            worker = json.loads((run / 'worker-result.json').read_bytes())
            audit['exit_code'] = exit_code
            if (exit_code == 0 and completed
                    and worker.get('phase') == 'navigation_complete'
                    and callbacks_are_fresh(events, run_id=run.name, started_ns=start, finished_ns=end)):
                audit['status'] = 'success'
                audit['evidence_type'] = 'fresh_detail_ocr_completed_custom_chain'
            else:
                audit['category'] = (worker['phase'] if worker.get('phase') in
                                     {'stage_locked', 'stage_not_found_on_map'} else 'navigation_failure')
                audit['error'] = ('Stage prerequisite is locked.' if audit['category'] == 'stage_locked' else
                                  'Target detail panel was not proven; inspect this run’s callbacks.')
    except PrtsError as exc:
        audit.update(category=exc.category, error=str(exc))
    except (Exception, KeyboardInterrupt) as exc:
        audit['error'] = type(exc).__name__
    finally:
        audit['elapsed_seconds'] = round(time.monotonic() - started, 3)
        vision = run / 'map-vision-1/result.json'
        if vision.exists():
            audit['map_search'] = json.loads(vision.read_text())
        elif audit['status'] == 'success':
            audit['map_search'] = {'branch': 'maa_task_graph'}
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
