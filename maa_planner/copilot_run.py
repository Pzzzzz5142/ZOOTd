"""Explicit, bounded-candidate Copilot experiment. Never called by daily/recovery."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .box_cli import load_secret
from .copilot_core import callbacks_are_fresh, terminal_result
from .copilot_proof import battle_proof
from .copilot_retry import RetryLimits, RetryBudget, classify_failure, failure
from .copilot_navigation import archive_tasks
from .copilot_matcher import match_candidate, rank_candidates
from .copilot_static import fetch_catalog
from .prts import PrtsError, CopilotCandidate, PrtsCopilotClient, StageCatalog, decode, operators
from .runtime_receipt import validate_runtime_receipt
from .skland import SklandBoxProvider, SklandClient
from .util import atomic_write_json, canonical_json, sha256_bytes


class ExperimentError(RuntimeError):
    pass


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=20, **kwargs).stdout


def load_policy(root: Path, profile: str | None) -> dict:
    config = tomllib.loads((root / 'config/copilot.toml').read_text())
    profile = profile or config['default_profile']
    policy = config['profiles'][profile]
    if set(policy) != {'allow_support'} or type(policy['allow_support']) is not bool:
        raise ExperimentError('Invalid experimental support policy')
    return {'profile': profile, **policy}


@contextmanager
def device_lock(root: Path):
    directory = root / 'var/run'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'zootd.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExperimentError('Another MAA run/runtime update holds the device lock') from None
        yield


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=25)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


@contextmanager
def device(root: Path, run: Path):
    ui = None
    with (run / 'device.log').open('x') as output:
        try:
            if not re.search(r'Session:\s+RUNNING', command(['waydroid', 'status'])):
                ui = subprocess.Popen([str(root / 'scripts/show-waydroid-scaled.sh')],
                                      stdout=output, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 120
            address = None
            while time.monotonic() < deadline:
                if ui is not None and ui.poll() is not None:
                    raise ExperimentError('Waydroid surface exited during startup')
                status = command(['waydroid', 'status'])
                ip = re.search(r'IP address:\s+(\d+\.\d+\.\d+\.\d+)', status)
                if ip:
                    address = ip[1] + ':5555'
                    subprocess.run(['waydroid', 'adb', 'connect'], stdout=output,
                                   stderr=subprocess.STDOUT, timeout=15, check=False)
                    if re.search(r'^' + re.escape(address) + r'\s+device$',
                                 command(['adb', 'devices']), re.M):
                        break
                time.sleep(1)
            else:
                raise ExperimentError('Waydroid/ADB not ready within 120 seconds')
            package = command(['adb', '-s', address, 'shell', 'pm', 'path', 'com.hypergryph.arknights'])
            if not package.strip().startswith('package:'):
                raise ExperimentError('Official CN client is unavailable')
            command(['adb', '-s', address, 'shell', 'wm', 'size', '1280x720'])
            size = command(['adb', '-s', address, 'shell', 'wm', 'size'])
            if not size.strip().endswith('1280x720'):
                raise ExperimentError('Device is not 1280x720')
            yield address
        finally:
            stop_child(ui)


def full_candidate(content: dict, candidate: CopilotCandidate) -> CopilotCandidate:
    return CopilotCandidate(candidate.id, candidate.stage, candidate.title,
                            operators(content.get('opers', [])),
                            [{'name': g['name'], 'operators': operators(g['opers'])}
                             for g in content.get('groups', [])],
                            content.get('difficulty'), candidate.metadata)


def select_candidate(box, candidates, catalog, allow_support):
    ranked = rank_candidates(box, candidates, catalog)
    selected = next((r for r in ranked if r.status == 'exact' or
                     (allow_support and r.status == 'support_one')), None)
    return selected, ranked


def bind_formation(content, result, catalog):
    """Constrain every group to the matcher's assignment, retaining action names."""
    content = copy.deepcopy(content)
    assignments = dict(result.assignments)
    if result.support_slot:
        assignments[result.support_slot] = result.support_operator
    for i, group in enumerate(content.get('groups', [])):
        slot = f'group:{i}:{group["name"]}'
        selected = [o for o in group['opers'] if catalog.resolve(o['name']) is not None
                    and catalog.resolve(o['name']).id == assignments[slot]]
        # Different skills/requirements for the same identity are alternatives
        # too: retain only an option proven by this exact member match.
        members = result.slots[slot]
        selected = [o for o in selected if any(m.name == o['name'] and
                    (m.status == 'yes' or slot == result.support_slot) for m in members)]
        if len(selected) != 1:
            raise ExperimentError('Ambiguous executable group assignment')
        group['opers'] = selected
    return content


def execute(root, run, address):
    child = None
    with (run / 'worker.log').open('x') as output:
        env = dict(os.environ, PYTHONPATH=str(root),
                   LD_LIBRARY_PATH=str(root / 'var/data/lib'))
        try:
            child = subprocess.Popen([sys.executable, '-m', 'maa_planner.copilot_core',
                                      str(root), str(run), address],
                                     cwd=run, env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                return child.wait(timeout=1200)
            except subprocess.TimeoutExpired:
                # Preserve/reduce this attempt's callbacks even on timeout.
                return 124
        finally:
            stop_child(child)


class CandidateRejected(ExperimentError):
    """A validated candidate is unusable; no device action has happened."""


def attempt(root, run, *, candidate, selected, box, catalog, prts, canonical,
            code, route, budget, get_address, restart, snapshot_sha256):
    audit = {'status': 'failed', 'copilot_id': candidate.id, 'run_dir': str(run),
             'snapshot_sha256': snapshot_sha256}
    phase = 'download_recheck'
    try:
        content = prts.get(candidate.id, stage=canonical)
        full = full_candidate(content, candidate)
        if (full.operators, full.groups, full.difficulty) != (candidate.operators, candidate.groups, candidate.difficulty):
            raise CandidateRejected('Selected candidate changed between query and download')
        checked = match_candidate(box, full, catalog)
        if checked != selected:
            raise CandidateRejected('Full copilot compatibility differs from selected candidate')
        audit['compatibility'] = checked.to_dict()
        audit['copilot_sha256'] = sha256_bytes(canonical_json(content))
        atomic_write_json(run / 'source.json', content, mode=0o600)
        content = bind_formation(content, checked, catalog)
        audit['execution_sha256'] = sha256_bytes(canonical_json(content))
        filename = run / 'execution.json'
        atomic_write_json(filename, content, mode=0o600)
        params = {'copilot_list': [{'filename': str(filename), 'stage_name': code, 'is_raid': False}],
                  'formation': True, 'loop_times': 1, 'use_sanity_potion': False,
                  'add_trust': False, 'ignore_requirements': False,
                  'support_unit_usage': 2 if checked.support_needed else 0}
        if checked.support_needed:
            support = next(m for m in checked.slots[checked.support_slot]
                           if m.operator_id == checked.support_operator)
            params['support_unit_name'] = support.name
            audit['support_operator'] = support.name
        atomic_write_json(run / 'params.json', params, mode=0o600)
        overlay = run / 'navigation/resource/tasks'
        overlay.mkdir(parents=True)
        atomic_write_json(overlay / 'tasks.json', archive_tasks(route['activity'], route['map_marker']))
        phase = 'budget'
        if not budget.reserve_battle():
            audit['failure'] = failure('budget_exhausted')
            raise ExperimentError('Battle count or sanity budget exhausted')
        audit['budget'] = budget.as_dict()
        atomic_write_json(run / 'selection.json', audit, mode=0o600)
        phase = 'device'
        address = get_address()
        if restart:
            # Only reached after the preceding worker exited with a proven
            # candidate failure. Reset UI before fresh startup/navigation.
            phase = 'reset'
            command(['adb', '-s', address, 'shell', 'am', 'force-stop', 'com.hypergryph.arknights'])
        phase = 'execution'
        started_ns = time.monotonic_ns()
        status = execute(root, run, address)
        finished_ns = time.monotonic_ns()
        audit['worker_exit_code'] = status
        phase = 'terminal'
        raw = (run / 'callbacks.jsonl').read_bytes() if (run / 'callbacks.jsonl').exists() else b''
        audit['callbacks_sha256'] = sha256_bytes(raw)
        events = [decode(line) for line in raw.splitlines()]
        task_id = decode((run / 'task-id.json').read_bytes()) if (run / 'task-id.json').exists() else None
        worker_phase = None
        receipt = run / 'worker-result.json'
        if receipt.exists():
            worker = decode(receipt.read_bytes())
            if worker.get('run_id') == run.name and worker.get('exit_code') == status:
                worker_phase = worker.get('phase')
        fresh = callbacks_are_fresh(events, run_id=run.name, started_ns=started_ns, finished_ns=finished_ns)
        result = (terminal_result(events, task_id=task_id, stage=content['stage_name'], filename=str(filename))
                  if fresh and type(task_id) is int else {'status': 'failed', 'errors': ['invalid_callback_evidence']})
        result['exit_code'] = status
        audit['execution'] = result
        audit['battle_proof'] = battle_proof(
            events, run_id=run.name, task_id=task_id, stage=content['stage_name'],
            filename=str(filename), copilot_id=candidate.id,
            copilot_sha256=audit['copilot_sha256'], execution_sha256=audit['execution_sha256'],
            started_ns=started_ns, finished_ns=finished_ns, exit_code=status,
            support_used=checked.support_needed)
        if status != 0 or result['status'] != 'success':
            audit['failure'] = classify_failure(
                events, run_id=run.name, started_ns=started_ns, finished_ns=finished_ns,
                task_id=task_id, stage=content['stage_name'], filename=str(filename),
                exit_code=status, worker_phase=worker_phase)
            raise ExperimentError('MaaCore did not produce a complete successful Copilot terminal')
        audit['status'] = 'success'
    except (Exception, KeyboardInterrupt) as exc:
        audit['failure_phase'] = phase
        audit['error'] = str(exc) if isinstance(exc, ExperimentError) else type(exc).__name__
        if 'failure' not in audit:
            if isinstance(exc, CandidateRejected) or (isinstance(exc, PrtsError) and exc.category == 'copilot_not_found'):
                audit['failure'] = failure('copilot_schema_failure', retryable=True)
            else:
                category = ('adb_failure' if phase in {'device', 'reset'} else
                            'unknown_execution_failure' if phase == 'terminal' else 'runtime_failure')
                audit['failure'] = failure(category)
    finally:
        audit['budget'] = budget.as_dict()
        atomic_write_json(run / 'result.json', audit, mode=0o600)
    return audit


def experiment(root: Path, stage: str, profile: str | None, *, limits: RetryLimits | None = None) -> dict:
    limits = limits or RetryLimits()
    policy = load_policy(root, profile)
    directory = root / 'var/state/copilot'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = directory / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12])
    run.mkdir(mode=0o700)
    audit = {'schema': 2, 'experimental': True, 'requested_stage': stage,
             'policy': policy, 'run_dir': str(run), 'status': 'failed', 'attempts': [],
             'authorization': {'max_candidates': limits.max_candidates, 'max_battles': limits.max_battles,
                               'sanity_budget': limits.sanity_budget, 'medicine': 0, 'stone': 0, 'raid': False}}
    phase = 'readiness'
    budget = None
    try:
        with device_lock(root):
            if command(['git', '-C', str(root), 'status', '--porcelain']).strip():
                raise ExperimentError('Copilot execution requires a clean Git worktree')
            audit['git_head'] = command(['git', '-C', str(root), 'rev-parse', 'HEAD']).strip()
            audit['runtime'] = validate_runtime_receipt(root)
            audit['runtime_receipt_sha256'] = sha256_bytes((root / 'var/state/runtime/maa-resource.json').read_bytes())
            stages = StageCatalog.load(root / 'var/data/resource/stages.json')
            canonical = stages.resolve(stage)
            audit['stage'] = canonical
            rows = decode((root / 'var/data/resource/stages.json').read_bytes())
            codes = {r['code'] for r in rows if r['stageId'] == canonical}
            if len(codes) != 1:
                raise ExperimentError('Ambiguous navigation code')
            code = next(iter(codes))
            route = tomllib.loads((root / 'config/copilot.toml').read_text()).get('navigation', {}).get(code)
            if (not isinstance(route, dict) or set(route) != {'activity', 'map_marker'}
                    or any(not isinstance(v, str) or not v for v in route.values())):
                raise ExperimentError('No verified automatic navigation route for this stage')
            audit['navigation'] = route
            costs = [r.get('apCost') for r in rows if r['stageId'] == canonical]
            if not costs or any(type(c) is not int or c != costs[0] for c in costs):
                raise ExperimentError('Missing or ambiguous stage sanity cost')
            budget = RetryBudget(limits, costs[0])
            audit['authorization']['sanity_budget'] = budget.sanity_limit
            audit['stage_catalog_sha256'] = sha256_bytes((root / 'var/data/resource/stages.json').read_bytes())
            phase = 'box'
            client = SklandClient()
            credentials = client.authenticate(**load_secret(root))
            box = SklandBoxProvider(client, credentials, None).fetch_box()
            payload = box.to_dict()
            atomic_write_json(root / 'var/state/operator-box.json', payload, mode=0o600)
            audit['box'] = {'sha256': sha256_bytes(canonical_json(payload)), 'fetched_at': box.fetched_at}
            phase = 'static_catalog'
            catalog, audit['static_sources'] = fetch_catalog(decode((root / 'var/data/resource/battle_data.json').read_bytes()))
            phase = 'query_match'
            prts = PrtsCopilotClient(stages)
            page = prts.query(canonical, limit=50)
            candidates = [CopilotCandidate(**c) for c in page['candidates']
                          if c['difficulty'] in (None, 0, 1, 3)]
            selected, ranked = select_candidate(box, candidates, catalog, policy['allow_support'])
            audit['query'] = {k: v for k, v in page.items() if k != 'candidates'}
            audit['ranking'] = [r.to_dict() for r in ranked]
            if len({c.id for c in candidates}) != len(candidates):
                raise ExperimentError('Duplicate candidate identity in query snapshot')
            snapshot = {'schema': 1, 'stage': canonical, 'stage_code': code,
                        'authorization': audit['authorization'],
                        'stage_catalog_sha256': audit['stage_catalog_sha256'],
                        'query': page, 'ranking': audit['ranking'], 'box': audit['box'],
                        'static_sources': audit['static_sources']}
            atomic_write_json(run / 'snapshot.json', snapshot, mode=0o600)
            audit['snapshot_sha256'] = sha256_bytes(canonical_json(snapshot))
            if selected is None:
                raise ExperimentError('No executable candidate in the first 50 results under this policy')

            choices = [r for r in ranked if r.status == 'exact' or
                       (policy['allow_support'] and r.status == 'support_one')]
            phase = 'attempts'
            with ExitStack() as stack:
                address = None
                dispatched = False
                def get_address():
                    nonlocal address
                    if address is None:
                        address = stack.enter_context(device(root, run))
                    return address
                for chosen in choices:
                    if not budget.take_candidate():
                        audit['stop_reason'] = 'candidate_budget_exhausted'
                        break
                    attempt_dir = run / 'attempts' / f'{run.name}-{budget.candidates:02d}'
                    attempt_dir.mkdir(parents=True, mode=0o700)
                    candidate = next(c for c in candidates if c.id == chosen.copilot_id)
                    outcome = attempt(root, attempt_dir, candidate=candidate, selected=chosen,
                                      box=box, catalog=catalog, prts=prts, canonical=canonical,
                                      code=code, route=route, budget=budget, get_address=get_address,
                                      restart=dispatched, snapshot_sha256=audit['snapshot_sha256'])
                    dispatched |= 'worker_exit_code' in outcome
                    audit['attempts'].append(outcome)
                    audit['copilot_id'] = candidate.id
                    if outcome['status'] == 'success':
                        audit['status'] = 'success'
                        audit['stop_reason'] = 'success'
                        break
                    if not outcome['failure']['retryable']:
                        audit['stop_reason'] = outcome['failure']['category']
                        break
                else:
                    audit['stop_reason'] = 'candidates_exhausted'
            if audit['status'] != 'success':
                audit['failure_phase'] = 'attempts'
                audit['error'] = audit['stop_reason']
    except (Exception, KeyboardInterrupt) as exc:
        audit['status'] = 'failed'
        audit['failure_phase'] = phase
        audit['error'] = str(exc) if isinstance(exc, ExperimentError) else type(exc).__name__
    finally:
        if budget is not None:
            audit['budget'] = budget.as_dict()
        atomic_write_json(run / 'result.json', audit, mode=0o600)
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description='Experimental bounded Copilot attempts; may spend stage sanity')
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('stage')
    parser.add_argument('--profile', choices=('no-support', 'allow-support'))
    parser.add_argument('--max-candidates', type=int, default=1)
    parser.add_argument('--max-battles', type=int, default=1)
    parser.add_argument('--sanity-budget', type=int)
    args = parser.parse_args(argv)
    try:
        limits = RetryLimits(args.max_candidates, args.max_battles, args.sanity_budget)
    except ValueError as exc:
        parser.error(str(exc))
    os.umask(0o077)
    def cancelled(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancelled)
    try:
        result = experiment(args.project_root.resolve(), args.stage, args.profile, limits=limits)
        print(json.dumps({k: result[k] for k in ('status', 'run_dir', 'copilot_id', 'failure_phase', 'error')
                          if k in result}, ensure_ascii=False))
        return 0 if result['status'] == 'success' else 1
    except Exception:
        print('{"status":"failed","error":"Invalid configuration or audit storage unavailable"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
