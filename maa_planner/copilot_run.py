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
from . import copilot_capability
from .copilot_retry import RetryLimits, RetryBudget, classify_failure, failure
from .copilot_navigation import navigation_tasks
from .navigation_catalog import load_navigation
from .copilot_matcher import match_candidate, rank_candidates
from .copilot_static import fetch_catalog
from .prts import PrtsError, CopilotCandidate, PrtsCopilotClient, decode, operators
from .runtime_receipt import validate_runtime_receipt
from .skland import SklandBoxProvider, SklandClient
from .util import atomic_write_json, canonical_json, sha256_bytes


ACCOUNT_NOTICE = ('账号提示：请确保森空岛 Box 与游戏当前登录的是同一国服官服账号；'
                  '程序不自动识别或核对游戏 UID，能力记录归入配置的本地 account 别名。')


def failure_message(result):
    """Human-readable, bounded diagnostics; never echo raw worker/API errors."""
    attempt = (result.get('attempts') or [result])[-1]
    phase = attempt.get('failure_phase', result.get('failure_phase'))
    category = attempt.get('failure', {}).get('category')
    if attempt.get('worker_exit_code') == 124:
        return '执行超时，结果未确认；请查看本次运行记录和游戏状态。'
    if attempt.get('worker_exit_code') == 130 or result.get('error') == 'KeyboardInterrupt':
        return '运行已中断；请查看本次运行记录确认进度。'
    if phase == 'ledger_record':
        return '能力账本登记失败；请检查活动有效期、本地配置、人工隔离状态及账本文件权限。'
    if attempt.get('worker_phase') == 'proxy_proof' or phase == 'capability_proof':
        return '通关或已保存代理的证明未完成；请检查战斗结果、客户端代理开关及本次运行记录。'
    if category == 'stage_locked':
        return '目标关卡的前置尚未解锁；请先完成游戏要求的前置关卡。'
    if category == 'stage_not_found_on_map':
        return '未在地图上找到并确认目标关卡；已停止扫描，请查看本次地图识别记录。'
    if category == 'navigation_failure':
        return '游戏启动或关卡导航失败；请检查游戏登录、更新/公告弹窗及关卡入口。'
    if category == 'adb_failure' or phase == 'device':
        return '设备连接失败；请检查 Waydroid 和 ADB 状态。'
    if category in {'formation_missing_operator', 'formation_requirement_unsatisfied', 'copilot_schema_failure'}:
        return '作业或编队不满足要求；请检查干员练度、森空岛与游戏账号是否一致，并查看本次记录。'
    if category == 'battle_failed':
        return '本次战斗未通过；请查看战斗结果及作业适配情况。'
    return {
        'readiness': '运行准备失败；请检查工作区是否干净、运行锁及 MAA runtime 状态。',
        'box': '森空岛 Box 同步失败；请检查登录凭据、绑定角色和网络。',
        'proof_context': '无法准备能力记录；请检查本地 account 配置和关卡活动数据。',
        'static_catalog': '干员静态数据获取失败；请检查网络和资源文件。',
        'query_match': '作业查询或匹配失败；请检查网络、候选作业及 Box 练度是否适用。',
    }.get(phase, '运行失败，未确认成功；请查看本次运行记录中的失败阶段和错误类别。')


class ExperimentError(RuntimeError):
    pass


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=20, **kwargs).stdout


def load_policy(root: Path, profile: str | None) -> dict:
    path = root / 'var/config/copilot.toml'
    config = (tomllib.loads(path.read_text()) if path.exists() else {
        'default_profile': 'no-support', 'profiles': {
            'no-support': {'allow_support': False}, 'allow-support': {'allow_support': True}}})
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
                    if (re.search(r'^' + re.escape(address) + r'\s+device$',
                                  command(['adb', 'devices']), re.M)
                            and command(['adb', '-s', address, 'shell', 'getprop',
                                         'sys.boot_completed']).strip() == '1'):
                        break
                time.sleep(1)
            else:
                raise ExperimentError('Waydroid/ADB/Android boot not ready within 120 seconds')
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
        if not group['opers']:
            continue  # MaaCore ignores empty formation groups.
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
            python = root / '.venv/bin/python'
            interpreter = str(python) if python.exists() else sys.executable
            child = subprocess.Popen([interpreter, '-m', 'maa_planner.copilot_core',
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


def acceptance_formation_failure(content, box, catalog, battle):
    """Explicit hardware test: an impossible low-rarity level, never fake callbacks."""
    changed = copy.deepcopy(content)
    members = changed.get('opers', []) + [o for g in changed.get('groups', []) for o in g['opers']]
    for member in members:
        identity = catalog.resolve(member['name'])
        if identity is None or identity.id not in box.operators:
            continue
        rarity = battle['chars'].get(identity.id, {}).get('rarity')
        # Installed MAA rarity is 1..6. Five-star and lower units cannot reach
        # E2 level 90, even if remote progression was stale. Never use six-stars.
        if type(rarity) is int and 1 <= rarity <= 5:
            before = copy.deepcopy(member.get('requirements', {}))
            member['requirements'] = dict(before, elite=2, level=90)
            return changed, {'kind': 'impossible_formation_level', 'oper_name': member['name'],
                             'rarity': rarity, 'original_requirements': before,
                             'injected_requirements': member['requirements']}
    raise ExperimentError('Acceptance check needs an owned five-star-or-lower assigned operator')


def attempt(root, run, *, candidate, selected, box, catalog, prts, canonical,
            code, route, budget, get_address, restart, snapshot_sha256, acceptance_failure=False, proof_context=None):
    audit = {'status': 'failed', 'copilot_id': candidate.id, 'run_dir': str(run),
             'snapshot_sha256': snapshot_sha256}
    phase = 'download_recheck'
    reservation = None
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
        if acceptance_failure:
            phase = 'acceptance_fixture'
            if checked.status != 'exact':
                raise ExperimentError('Acceptance failure injection requires an exact candidate without support')
            content, audit['acceptance_injection'] = acceptance_formation_failure(
                content, box, catalog, decode((root / 'var/data/resource/battle_data.json').read_bytes()))
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
        tasks = navigation_tasks(route)
        atomic_write_json(run / 'navigation.json', route, mode=0o600)
        if proof_context:
            tasks.update(copilot_capability.proof_tasks(code))
            atomic_write_json(run / 'proof-context.json', proof_context, mode=0o600)
        atomic_write_json(overlay / 'tasks.json', tasks)
        phase = 'budget'
        if not budget.reserve_battle():
            audit['failure'] = failure('budget_exhausted')
            raise ExperimentError('Battle count or sanity budget exhausted')
        reservation = budget.battle_reservations
        audit['budget'] = budget.as_dict()
        atomic_write_json(run / 'selection.json', audit, mode=0o600)
        phase = 'device'
        address = get_address()
        if restart:
            # The previous worker has exited with a proven candidate failure.
            # Fresh Core StartUp returns from formation/results to home before
            # navigating again. Force-stop can strand Waydroid's game process.
            audit['reset_strategy'] = 'fresh_core_startup_navigation'
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
                if worker_phase in {'runtime', 'adb', 'navigation', 'stage_locked', 'stage_not_found_on_map', 'execution', 'proxy_proof'}:
                    audit['worker_phase'] = worker_phase
        fresh = callbacks_are_fresh(events, run_id=run.name, started_ns=started_ns, finished_ns=finished_ns)
        # The battle reducer ends at its own AllTasksCompleted. Later Custom
        # tasks have independent terminals and are checked by the strong proof.
        battle_events = events
        if proof_context:
            for i, event in enumerate(events):
                v = event['details']
                if event['message'] == 3 and task_id in v.get('finished_tasks', []):
                    battle_events = events[:i + 1]
                    break
        result = (terminal_result(battle_events, task_id=task_id, stage=content['stage_name'], filename=str(filename))
                  if fresh and type(task_id) is int else {'status': 'failed', 'errors': ['invalid_callback_evidence']})
        result['exit_code'] = status
        audit['execution'] = result
        audit['battle_proof'] = battle_proof(
            battle_events, run_id=run.name, task_id=task_id, stage=content['stage_name'],
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
        if proof_context:
            phase = 'capability_proof'
            proof = copilot_capability.complete_proof(
                events, audit['battle_proof'], proof_context,
                started_ns=started_ns, finished_ns=finished_ns)
            audit['battle_proof'] = proof
            if proof['status'] != 'verified':
                raise ExperimentError('Saved proxy proof incomplete: ' + proof['reason'])
            phase = 'ledger_record'
            proof['ledger_recorded'] = copilot_capability.record(root, proof_context, proof)
        audit['status'] = 'success'
    except (Exception, KeyboardInterrupt) as exc:
        audit['failure_phase'] = phase
        audit['error'] = (str(exc) if isinstance(exc, ExperimentError) else
                          exc.category if isinstance(exc, PrtsError) else type(exc).__name__)
        if 'failure' not in audit:
            if isinstance(exc, CandidateRejected) or (isinstance(exc, PrtsError) and exc.category == 'copilot_not_found'):
                audit['failure'] = failure('copilot_schema_failure', retryable=True)
            else:
                category = ('adb_failure' if phase in {'device', 'reset'} else
                            'unknown_execution_failure' if phase == 'terminal' else 'runtime_failure')
                audit['failure'] = failure(category)
    finally:
        if reservation is not None:
            outcome = audit.get('failure', {}).get('sanity_outcome', 'charged_or_unknown')
            released = budget.settle(reservation, zero_cost=outcome in {'not_spent', 'refunded'})
            audit['sanity_settlement'] = {'reservation': reservation, 'outcome': outcome,
                                          'released': released}
        audit['budget'] = budget.as_dict()
        if audit['status'] != 'success':
            audit['message'] = failure_message(audit)
        atomic_write_json(run / 'result.json', audit, mode=0o600)
    return audit


def experiment(root: Path, stage: str, profile: str | None, *, limits: RetryLimits | None = None,
               acceptance_failure: bool = False, prove_capability: bool = False) -> dict:
    limits = limits or RetryLimits()
    policy = load_policy(root, profile)
    print(ACCOUNT_NOTICE, file=sys.stderr, flush=True)
    if prove_capability and (policy['allow_support'] or acceptance_failure):
        raise ExperimentError('Capability proof requires no-support and no failure injection')
    if acceptance_failure and (limits.max_candidates != 2 or limits.max_battles != 2
                               or limits.sanity_budget != 18):
        raise ExperimentError('Acceptance check requires exactly 2 candidates, 2 executions, 18 sanity')
    directory = root / 'var/state/copilot'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = directory / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12])
    run.mkdir(mode=0o700)
    audit = {'schema': 2, 'experimental': True, 'requested_stage': stage,
             'policy': policy, 'run_dir': str(run), 'status': 'failed', 'attempts': [],
             'proof_requested': prove_capability, 'account_binding': 'user_managed',
             'authorization': {'max_candidates': limits.max_candidates, 'max_battles': limits.max_battles,
                               'sanity_budget': limits.sanity_budget, 'medicine': 0, 'stone': 0, 'raid': False}}
    if acceptance_failure:
        audit['acceptance_check'] = 'injected_formation_failure_then_unmodified_candidate'
    phase = 'readiness'
    budget = None
    try:
        with device_lock(root):
            if command(['git', '-C', str(root), 'status', '--porcelain']).strip():
                raise ExperimentError('Copilot execution requires a clean Git worktree')
            audit['git_head'] = command(['git', '-C', str(root), 'rev-parse', 'HEAD']).strip()
            audit['runtime'] = validate_runtime_receipt(root)
            audit['runtime_receipt_sha256'] = sha256_bytes((root / 'var/state/runtime/maa-resource.json').read_bytes())
            stages = load_navigation(root)
            canonical = stages.resolve(stage)
            route = stages.route(canonical, require_tiles=True)
            audit['stage'] = canonical
            code = route['code']
            if acceptance_failure and code != 'NL-8':
                raise ExperimentError('Acceptance check is restricted to NL-8')
            audit['navigation'] = route
            audit['navigation_sources'] = stages.evidence
            budget = RetryBudget(limits, route['ap_cost'])
            audit['authorization']['sanity_budget'] = budget.sanity_limit
            audit['stage_catalog_sha256'] = stages.evidence['sha256']
            phase = 'box'
            client = SklandClient()
            credentials = client.authenticate(**load_secret(root))
            box = SklandBoxProvider(client, credentials, None).fetch_box()
            payload = box.to_dict()
            atomic_write_json(root / 'var/state/operator-box.json', payload, mode=0o600)
            audit['box'] = {'sha256': sha256_bytes(canonical_json(payload)), 'fetched_at': box.fetched_at}
            phase = 'proof_context'
            proof_context = (copilot_capability.prepare(root, canonical, code)
                             if prove_capability else None)
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
            if acceptance_failure:
                snapshot['acceptance_check'] = audit['acceptance_check']
            atomic_write_json(run / 'snapshot.json', snapshot, mode=0o600)
            audit['snapshot_sha256'] = sha256_bytes(canonical_json(snapshot))
            if selected is None:
                raise ExperimentError('No executable candidate in the first 50 results under this policy')

            choices = [r for r in ranked if r.status == 'exact' or
                       (policy['allow_support'] and r.status == 'support_one')]
            if acceptance_failure and (len(choices) < 2 or choices[0].status != 'exact'):
                raise ExperimentError('Acceptance check needs two candidates, with an exact first candidate')
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
                                      restart=dispatched, snapshot_sha256=audit['snapshot_sha256'],
                                      acceptance_failure=acceptance_failure and budget.candidates == 1,
                                      proof_context=proof_context)
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
            if acceptance_failure:
                attempts = audit['attempts']
                audit['acceptance_passed'] = (
                    len(attempts) == 2 and attempts[0].get('acceptance_injection') is not None
                    and attempts[0].get('failure', {}).get('category') in (
                        'formation_requirement_unsatisfied', 'formation_missing_operator')
                    and any(e.get('oper_name', e.get('name')) ==
                            attempts[0]['acceptance_injection']['oper_name']
                            for e in attempts[0].get('failure', {}).get('evidence', []))
                    and attempts[0].get('sanity_settlement', {}).get('outcome') == 'not_spent'
                    and attempts[1]['status'] == 'success'
                    and 'acceptance_injection' not in attempts[1])
                if not audit['acceptance_passed']:
                    audit.update(status='failed', stop_reason='acceptance_not_proven',
                                 failure_phase='acceptance', error='Acceptance retry chain was not proven')
    except (Exception, KeyboardInterrupt) as exc:
        audit['status'] = 'failed'
        audit['failure_phase'] = phase
        audit['error'] = (str(exc) if isinstance(exc, ExperimentError) else
                          exc.category if isinstance(exc, PrtsError) else type(exc).__name__)
    finally:
        if budget is not None:
            audit['budget'] = budget.as_dict()
        if audit['status'] != 'success':
            audit['message'] = failure_message(audit)
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
    parser.add_argument('--acceptance-formation-failure', action='store_true',
                        help='NL-8 hardware test only: inject an impossible formation level into candidate A')
    parser.add_argument('--prove-capability', action='store_true',
                        help='Record capability after battle and zero-sanity saved proxy proof')
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
        result = experiment(args.project_root.resolve(), args.stage, args.profile, limits=limits,
                            acceptance_failure=args.acceptance_formation_failure,
                            prove_capability=args.prove_capability)
        if result['status'] != 'success':
            print(result.get('message') or failure_message(result), file=sys.stderr)
        print(json.dumps({k: result[k] for k in ('status', 'run_dir', 'copilot_id', 'failure_phase', 'error', 'message')
                          if k in result}, ensure_ascii=False))
        return 0 if result['status'] == 'success' else 1
    except ExperimentError as exc:
        print('Copilot 运行条件不满足：' + str(exc), file=sys.stderr)
        return 1
    except Exception:
        print('配置或运行记录存储不可用，请检查配置、目录权限与可用空间。', file=sys.stderr)
        print('{"status":"failed","error":"Invalid configuration or audit storage unavailable"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
