"""Recurring-failure ledger, gated repairs and prospective outcome monitoring."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .dashboard import RUN_ID, read_run
from .fallback_signals import anr_evidence, matches, revision, rules, startup_signals
from .supervisor import ACCEPTED_RESULTS, SupervisorError, load_run_events
from .util import atomic_write_json, canonical_json, parse_iso_datetime, sha256_bytes

WINDOW_DAYS = 28
MIN_CASES = 3
MIN_SPAN_DAYS = 7
ROOT = Path(__file__).resolve().parents[1]


def _directory(root):
    return root / 'var/state/fallback'


def _read(path, default=None):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('fallback state may not traverse symlinks')
    if not path.exists():
        return default
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('invalid fallback state file')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value.get('schema_version') != 1):
        raise ValueError('unsupported fallback state version')
    return value


def _record(path, value):
    """Exclusive-create immutable observations; never replace an earlier result."""
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('fallback observations may not traverse symlinks')
    value = _seal(value)

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with path.open('xb') as stream:
            os.chmod(path, 0o600)
            stream.write(canonical_json(value) + b'\n')
    except FileExistsError:
        return False
    return True


def _seal(value):
    value = {'schema_version': 1, **value}
    value['record_sha256'] = sha256_bytes(canonical_json(value))
    return value


def _observation(path):
    value = _read(path)
    if value is not None:
        core = {k: v for k, v in value.items() if k != 'record_sha256'}
        if value.get('record_sha256') != sha256_bytes(canonical_json(core)):
            raise ValueError('fallback observation hash mismatch')
    return value


@contextmanager
def ledger_lock(root):
    path = root / 'var/run/fallback.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _log(root, name):
    path = (root / name).resolve(strict=True)
    path.relative_to(root / 'var/state/host')
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('invalid startup evidence file')
    raw = path.read_bytes()
    return raw, {'path': path.relative_to(root).as_posix(), 'size': len(raw),
                 'sha256': sha256_bytes(raw)}


def probe(root, run_id, log, *, anr_dir=None, owned=False, now=None, phase='daily'):
    """Observe every eligible case; authorize a fixed action only with fresh policy."""
    now = now or datetime.now(timezone.utc)
    events = load_run_events(root, run_id)
    start = events[0]['payload']
    since = parse_iso_datetime(events[0]['recorded_at'])
    if (start.get('mode') != 'full' or start.get('recovery_context')
            or start.get('repository', {}).get('dirty') is not False
            or any(e['event_type'] == 'run-finished' for e in events)
            or not since <= now <= since + timedelta(minutes=590)):
        return False
    raw, reference = _log(root, log)
    modified = datetime.fromtimestamp((root / reference['path']).stat().st_mtime, tz=timezone.utc)
    if not since <= modified <= now + timedelta(seconds=2):
        return False
    signals = startup_signals(raw)
    if not signals['startup_failed'] and not signals['game_offline']:
        return False
    native = anr_evidence(anr_dir or Path.home() / '.local/share/waydroid/data/anr',
                          since=since, now=now)
    signals['fresh_official_input_anr'] = native is not None
    known_input = bool(native and native['input_kind'] in {'motion', 'key'})
    signals['native_motion_timeout'] = native['input_kind'] == 'motion' if known_input else None
    signals['native_key_timeout'] = native['input_kind'] == 'key' if known_input else None
    signals['depot_failed'] = phase == 'depot' or any(e['event_type'] == 'phase-finished'
        and e['payload']['phase'] == 'depot' and e['payload']['result'] not in ACCEPTED_RESULTS for e in events)
    directory = _directory(root) / 'observations' / run_id
    with ledger_lock(root):
        if not signals['fresh_official_input_anr'] and not signals['game_offline']:
            _record(directory / 'symptom.json', {'run_id': run_id, 'at': now.isoformat(),
                    'signals': signals, 'log': reference})
            return False
        _record(directory / 'detected.json', {
            'run_id': run_id, 'at': now.isoformat(), 'signals': signals,
            'native_evidence': native, 'log': reference, 'phase': phase,
        })
        # A late second observation cannot silently change the recorded diagnosis.
        detected = _observation(directory / 'detected.json')
        if detected['signals'] != signals or detected['log'] != reference:
            return False
        policy = _read(_directory(root) / 'policy.json', {})
        if (not owned or signals['game_offline'] or policy.get('schema_version') != 1
                or not now - timedelta(hours=36) <= parse_iso_datetime(policy['at']) <= now):
            return False
        history = _read(_directory(root) / 'history.json')
        if (history is None or policy.get('invalid_runs') != []
                or policy.get('history_sha256') != sha256_bytes(canonical_json(history))):
            return False
        for rule in rules(root):
            state = policy.get('rules', {}).get(rule['id'], {})
            if (state.get('revision') == revision(rule) and state.get('status') in {'trial', 'effective'}
                    and matches(rule, signals)):
                return _record(directory / 'action-started.json', {
                    'run_id': run_id, 'at': now.isoformat(), 'rule_id': rule['id'],
                    'revision': revision(rule), 'observation_sha256': detected['record_sha256'],
                    'action': 'restart-waydroid-session', 'phase': phase,
                })
    return False


def finish_action(root, run_id, log, accepted):
    directory = _directory(root) / 'observations' / run_id
    with ledger_lock(root):
        started = _observation(directory / 'action-started.json')
        if started is None:
            return
        _, reference = _log(root, log)
        _record(directory / 'action-finished.json', {
            'run_id': run_id, 'at': datetime.now(timezone.utc).isoformat(),
            'action_sha256': started['record_sha256'], 'stage_accepted': accepted,
            'retry_log': reference,
        })


def _game_day(stamp):
    return (parse_iso_datetime(stamp).astimezone(ZoneInfo('Asia/Shanghai')) - timedelta(hours=4)).date()


def recurring(records):
    records = list({r['run_id']: r for r in records}.values())
    days = sorted({_game_day(r['at']) for r in records})
    return (len(records) >= MIN_CASES and len(days) >= MIN_CASES
            and (days[-1] - days[0]).days >= MIN_SPAN_DAYS)


def _projection(root, run_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    run = read_run(root, run_id)
    if run['status'] == 'invalid':
        raise ValueError('invalid source audit')
    events = load_run_events(root, run_id)
    if (run.get('mode') != 'full' or events[0]['payload'].get('recovery_context')):
        return None
    if (run['status'] == 'unfinished'
            and now - parse_iso_datetime(run['started_at']) <= timedelta(minutes=600)):
        return None
    bad = [{'phase': p['phase'], 'outcome': p['outcome']} for p in run['phases']
           if p['result'] not in ACCEPTED_RESULTS]
    daily = next((p for p in run['phases'] if p['phase'] == 'daily'), None)
    return {'run_id': run_id, 'at': run['started_at'], 'status': run['status'],
            'finished_at': run['finished_at'],
            'chain_head': events[-1]['event_sha256'], 'failures': bad,
            'phase_evidence': {p['phase']: {k: p[k] for k in ('result', 'evidence_files')}
                               for p in run['phases'] if p['phase'] in {'daily', 'depot'}},
            'daily': {k: daily[k] for k in ('result', 'evidence_files')} if daily else None}


def _historical_observation(root, record):
    if record['status'] != 'failed':
        return None
    native = anr_evidence(Path.home() / '.local/share/waydroid/data/anr',
                          since=parse_iso_datetime(record['at']),
                          now=parse_iso_datetime(record['finished_at']))
    if not native:
        return None
    references = [(phase, ref) for phase, proof in record.get('phase_evidence', {}).items()
                  for ref in proof['evidence_files']]
    for phase, reference in references:
        try:
            raw, actual = _log(root, reference['path'])
        except (OSError, ValueError):
            continue
        if actual != reference:
            continue
        signals = startup_signals(raw)
        if signals['startup_failed']:
            signals['fresh_official_input_anr'] = True
            known_input = native['input_kind'] in {'motion', 'key'}
            signals['native_motion_timeout'] = native['input_kind'] == 'motion' if known_input else None
            signals['native_key_timeout'] = native['input_kind'] == 'key' if known_input else None
            signals['depot_failed'] = any(f['phase'] == 'depot' for f in record['failures'])
            return _seal({'run_id': record['run_id'], 'at': native['at'],
                          'signals': signals, 'native_evidence': native, 'log': reference, 'phase': phase})
    return None


def _outcome(record, detected, started, finished):
    if not started:
        return None
    if record['status'] == 'unfinished':
        return 'unconfirmed'
    if (started.get('observation_sha256') != detected['record_sha256'] or not finished
            or finished.get('action_sha256') != started['record_sha256']):
        return 'unconfirmed'
    target = started.get('phase', 'daily')
    phase = record.get('phase_evidence', {}).get(target) or (record.get('daily') if target == 'daily' else None) or {}
    bound = finished.get('retry_log') in phase.get('evidence_files', [])
    if finished.get('stage_accepted') is not True:
        return 'failed'
    if not bound or phase.get('result') != 'succeeded':
        return 'unconfirmed'
    return 'success' if record['status'] == 'success' else 'other-phase-failed'


def assess(root, *, now=None):
    """Durable projections survive seven-day log cleanup; never modify source audits."""
    now = now or datetime.now(timezone.utc)
    state_dir = _directory(root)
    cache = _read(state_dir / 'history.json', {'schema_version': 1, 'runs': {}})
    cutoff = now - timedelta(days=90)
    history = {rid: r for rid, r in cache['runs'].items()
               if RUN_ID.fullmatch(rid) and cutoff <= parse_iso_datetime(r['at']) <= now}
    invalid = []
    for path in sorted((root / 'var/state/supervisor/runs').glob('*')):
        if not RUN_ID.fullmatch(path.name):
            continue
        try:
            record = _projection(root, path.name, now=now)
        except (OSError, ValueError, KeyError, TypeError, SupervisorError):
            invalid.append(path.name)
            history.pop(path.name, None)
            continue
        if record and cutoff <= parse_iso_datetime(record['at']) <= now:
            history[path.name] = record
    rows, imports = [], []
    for rid, record in sorted(history.items()):
        directory = state_dir / 'observations' / rid
        detected = _observation(directory / 'detected.json')
        if detected is None:
            detected = _historical_observation(root, record)
            if detected:
                imports.append(detected)
        if detected:
            started = _observation(directory / 'action-started.json')
            finished = _observation(directory / 'action-finished.json')
            rows.append({**record, 'signals': detected['signals'],
                         'action': started, 'outcome': _outcome(record, detected, started, finished)})
    previous = _read(state_dir / 'policy.json', {'schema_version': 1, 'rules': {}})
    states = {}
    notices = []
    for rule in rules(root):
        digest = revision(rule)
        old = previous['rules'].get(rule['id'], {})
        if old.get('revision') != digest:
            old = {}
        relevant = [r for r in rows if matches(rule, r['signals'])
                    and now - timedelta(days=WINDOW_DAYS) <= parse_iso_datetime(r['at']) <= now]
        baseline = [r for r in relevant if r['status'] == 'failed' and r['action'] is None]
        trials = [r for r in relevant if r['action'] and r['action']['revision'] == digest]
        status = old.get('status', 'observing')
        if invalid:
            status = 'evidence-invalid'
        elif status == 'observing' and recurring(baseline):
            status = 'trial'
        # Missing terminal evidence is never a positive result. Two consecutive
        # failed/unconfirmed interventions pause this exact rule revision.
        if len(trials) >= 2 and all(r['outcome'] in {'failed', 'unconfirmed'} for r in trials[-2:]):
            status = 'paused'
        successes = [r for r in trials if r['outcome'] == 'success']
        acceptable_rate = not trials or len(successes) * 5 >= len(trials) * 4
        if len(trials) >= 5 and not acceptable_rate:
            status = 'paused'
        elif status == 'effective' and not acceptable_rate:
            status = 'trial'
        if (status == 'trial' and acceptable_rate and len(successes) >= 3
                and len({_game_day(r['at']) for r in successes}) >= 3):
            status = 'effective'
        state = {'revision': digest, 'status': status, 'baseline_cases': len(baseline),
                 'baseline_days': len({_game_day(r['at']) for r in baseline}),
                 'attempts': len(trials), 'successes': len(successes),
                 'failures': sum(r['outcome'] == 'failed' for r in trials),
                 'unconfirmed': sum(r['outcome'] == 'unconfirmed' for r in trials),
                 'other_phase_failures': sum(r['outcome'] == 'other-phase-failed' for r in trials),
                 'case_run_ids': [r['run_id'] for r in baseline],
                 'activated_at': old.get('activated_at') or (now.isoformat() if status == 'trial' else None)}
        states[rule['id']] = state
        if status != old.get('status', 'observing'):
            notices.append({'rule_id': rule['id'], 'label': rule['label'], **state})
    # Repeated unknown symptoms are review inputs, never direct action authority.
    groups = {}
    for record in history.values():
        if record['status'] != 'failed' or not now - timedelta(days=WINDOW_DAYS) <= parse_iso_datetime(record['at']) <= now:
            continue
        for failure in record['failures']:
            key = failure['phase'] + ':' + failure['outcome']
            groups.setdefault(key, []).append(record)
    candidates = {key: [r['run_id'] for r in values] for key, values in groups.items() if recurring(values)}
    recent_failures = sum(r['status'] == 'failed' and now - timedelta(days=WINDOW_DAYS)
                          <= parse_iso_datetime(r['at']) <= now for r in history.values())
    return {'schema_version': 1, 'at': now.isoformat(), 'rules': states,
            'window_days': WINDOW_DAYS, 'failed_root_runs': recent_failures,
            'invalid_runs': invalid, 'candidates': candidates, 'notices': notices,
            'history': history, 'samples': rows, 'imports': imports}


def notify(text, title='ZOOTd 自动 fallback'):
    from .bark import _message_body, send_bark
    url = os.environ.get('ZOOTD_BARK_URL', '').strip()
    return bool(url) and send_bark(url, title, _message_body(text, os.environ))


def _save_assessment(root, result):
    state_dir = _directory(root)
    previous = _read(state_dir / 'policy.json', {})
    now = parse_iso_datetime(result['at'])
    last = previous.get('last_report_at')
    due = last is None or now - parse_iso_datetime(last) >= timedelta(days=7)
    result['last_report_at'] = now.isoformat() if due or result['notices'] else last
    for imported in result['imports']:
        _record(state_dir / 'observations' / imported['run_id'] / 'detected.json',
                {k: v for k, v in imported.items() if k not in {'schema_version', 'record_sha256'}})
    history = {'schema_version': 1, 'runs': result['history']}
    result['history_sha256'] = sha256_bytes(canonical_json(history))
    atomic_write_json(state_dir / 'history.json', history)
    atomic_write_json(state_dir / 'policy.json', {k: v for k, v in result.items()
                      if k not in {'history', 'samples', 'imports'}})
    for n in result['notices']:
        labels = {'trial': '进入有限试运行', 'effective': '已有后续同类成功样本',
                  'paused': '连续失败、成功率不足或证据缺失，已停用', 'evidence-invalid': '源审计异常，已停用'}
        notify(f"{n['label']}：{labels.get(n['status'], n['status'])}。\n"
               f"历史 {n['baseline_cases']} 次/{n['baseline_days']} 天；后续 {n['successes']}/{n['attempts']} 次整轮成功。\n"
               '动作：仅重建受管 Waydroid 会话并重试一次，保留游戏数据。')
    if due and not result['notices']:
        attempts = sum(s['attempts'] for s in result['rules'].values())
        successes = sum(s['successes'] for s in result['rules'].values())
        active = sum(s['status'] in {'trial', 'effective'} for s in result['rules'].values())
        notify(f"近 28 天独立失败 {result['failed_root_runs']} 次；启用规则 {active} 条。\n"
               f"未来同类干预整轮成功 {successes}/{attempts} 次。"
               + ('仍待后续同类样本验证。' if not attempts else '未再发不作为已解决证明。'),
               'ZOOTd fallback 定期效果观察')


def monitor(root, *, now=None, dry_run=False, improve=False):
    root = root.resolve()
    with ExitStack() as stack:
        for name in ('maa-daily.lock', 'zootd.lock', 'codex-sdk.lock'):
            path = root / 'var/run' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(path.open('a'))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'busy', 'lock': name}
        if not dry_run and (root / '.git').exists():
            from .fallback_maintenance import _clean_main
            try:
                _clean_main(root)
            except ValueError:
                return {'status': 'busy', 'lock': 'clean-main-checkout'}
        with ledger_lock(root):
            result = assess(root, now=now)
            if not dry_run:
                _save_assessment(root, result)
        if improve and not dry_run and not result['invalid_runs']:
            from .fallback_maintenance import improve_rules
            result['maintenance'] = improve_rules(root, result)
            if result['maintenance']['status'] == 'deployed':
                maintenance = result['maintenance']
                with ledger_lock(root):
                    result = assess(root, now=now)
                    _save_assessment(root, result)
                result['maintenance'] = maintenance
    return {k: v for k, v in result.items() if k not in {'history', 'samples', 'imports'}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('probe')
    p.add_argument('--run-id', required=True)
    p.add_argument('--log', required=True)
    p.add_argument('--owned-session', action='store_true')
    p.add_argument('--phase', choices=('daily', 'depot'), default='daily')
    p = sub.add_parser('finish')
    p.add_argument('--run-id', required=True)
    p.add_argument('--log', required=True)
    p.add_argument('--accepted', action='store_true')
    p = sub.add_parser('monitor')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--improve', action='store_true')
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    try:
        if args.command == 'probe':
            return 0 if probe(root, args.run_id, args.log, owned=args.owned_session, phase=args.phase) else 1
        if args.command == 'finish':
            finish_action(root, args.run_id, args.log, args.accepted)
        else:
            print(json.dumps(monitor(root, dry_run=args.dry_run, improve=args.improve), ensure_ascii=False))
        return 0
    except Exception as exc:
        # Evidence/state problems fall back to the existing agent path.
        print(f'[zootd-fallback] unavailable ({type(exc).__name__})')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
