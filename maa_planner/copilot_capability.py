"""Account-bound Copilot proof, using the existing capability ledger and lock.

Only an explicit proof experiment can enroll a local account alias. A Box UID
alone is never evidence of the account currently running on the device.
"""
from __future__ import annotations

import re
from pathlib import Path

from .capability import CapabilityKey, REGULAR_FALLBACK_ACTIVITY_INSTANCE
from .config import load_config
from .copilot_core import callbacks_are_fresh
from .prts import StageCatalog, decode
from .proxy import PROXY_RESOURCE
from .util import atomic_write_json, canonical_json, sha256_bytes, utc_now


def scope(root: Path, canonical: str, code: str) -> str:
    catalog = StageCatalog.load(root / 'var/data/resource/stages.json')
    if catalog.resolve(code) != canonical:
        raise ValueError('Stage scope mismatch')
    if canonical.endswith('_perm') and canonical in catalog.query_ids:
        # Permanent archives must never share the original limited event key.
        return sha256_bytes(canonical_json({'client': 'Official', 'kind': 'permanent-sidestory-v1',
                                           'zone': canonical.rsplit('_', 2)[0]}))[:24]
    if code in {'1-7', 'AP-5'}:
        return REGULAR_FALLBACK_ACTIVITY_INSTANCE
    from .cli import _activity_instance_from_snapshot
    return _activity_instance_from_snapshot(root, client='Official', stage=code, now=utc_now())


def prepare(root, box, canonical, code, *, enroll=False):
    config = load_config(root / 'config/farming.toml')
    if config.client_type != 'Official' or not re.fullmatch(r'Official:[0-9]{4,16}', box.account_id):
        raise ValueError('Invalid proof account')
    uid = box.account_id.split(':')[1]
    identity = {'client': 'Official', 'account': config.account,
                'uid_sha256': sha256_bytes(box.account_id.encode())}
    binding = root / 'var/state/copilot-account-binding.json'
    if binding.exists():
        if decode(binding.read_bytes()) != identity:
            raise ValueError('Pinned account differs from Box or configured alias')
    elif not enroll:
        raise ValueError('First proof requires explicit --bind-account enrollment')
    catalog = StageCatalog.load(root / 'var/data/resource/stages.json')
    return {'schema': 1, **identity, 'expected_uid': uid, 'stage': canonical,
            'battle_stages': sorted(k for k, v in catalog.aliases.items() if v == {canonical}),
            'stage_code': code, 'activity_instance': scope(root, canonical, code),
            'enroll': enroll}


def proof_tasks(code):
    return {
        'ZootdCopilotUID': {'algorithm': 'OcrDetect', 'text': ['UID'], 'isAscii': True,
                            'roi': [0, 0, 1280, 720], 'action': 'DoNothing', 'next': []},
        'ZootdCopilotProofStage': {'algorithm': 'OcrDetect', 'text': [code],
                                  'fullMatch': True, 'roi': [0, 60, 1280, 560],
                                  'action': 'ClickSelf', 'postDelay': 700, 'next': []},
    }


def safe_proxy_tasks(code):
    return {**PROXY_RESOURCE, **proof_tasks(code)}


def observed_uid(message, value, uid):
    detail = value.get('details', {})
    result = detail.get('result', {})
    return (message == 20002 and value.get('taskchain') == 'Custom'
            and value.get('subtask') == 'ProcessTask'
            and value.get('first') == ['ZootdCopilotUID']
            and detail.get('task') == 'ZootdCopilotUID'
            and detail.get('algorithm') == 'OcrDetect' and detail.get('action') == 'DoNothing'
            and isinstance(result, dict)
            and isinstance(result.get('text'), str)
            and re.fullmatch(r'UID[:： ]*' + re.escape(uid), result['text']) is not None)


def _custom_observations(events, uuid):
    """Only observations inside distinct successfully completed Custom chains."""
    active = None
    seen = set()
    pending = []
    completed = None
    observations = []
    for event in events:
        msg, value = event['message'], event['details']
        if msg in (0, 1, 10000, 10004, 20000, 20004) or value.get('what') == 'GameOffline':
            raise ValueError('Proof task failed')
        if value.get('taskchain') != 'Custom':
            continue
        task = value.get('taskid')
        if value.get('uuid') != uuid or type(task) is not int:
            raise ValueError('Proof device mismatch')
        if msg == 10001:
            if active is not None or completed is not None or task in seen:
                raise ValueError('Duplicate proof task')
            seen.add(task)
            active, pending = task, []
        elif msg == 20002:
            if active != task:
                raise ValueError('Unbound proof observation')
            pending.append(event)
        elif msg == 10002:
            if active != task:
                raise ValueError('Unbound proof completion')
            completed = task
            active = None
        elif msg == 3:
            if (completed != task or value.get('finished_tasks') != [task]
                    or any(type(t) is not int for t in value['finished_tasks'])):
                raise ValueError('Invalid proof terminal')
            observations.extend(pending)
            completed = None
    if active is not None or completed is not None:
        raise ValueError('Incomplete proof task')
    return observations


def complete_proof(events, battle, context, *, started_ns, finished_ns):
    result = dict(battle, first_clear='unknown', ledger_recorded=False)
    try:
        if battle.get('status') != 'observed' or battle.get('three_star') is not True:
            raise ValueError('battle_not_proven')
        if not callbacks_are_fresh(events, run_id=battle['run_id'], started_ns=started_ns,
                                   finished_ns=finished_ns):
            raise ValueError('stale_proof_callbacks')
        if (battle['stage'].casefold() not in context['battle_stages']
                or context['client'] != 'Official'
                or sha256_bytes(('Official:' + context['expected_uid']).encode()) != context['uid_sha256']):
            raise ValueError('proof_context_mismatch')
        # Copilot's optional NotUsePrts probe is handled by battle_proof. All
        # surrounding account and proxy tasks must be error-free.
        before = [e for e in events if e['sequence'] < battle['start_sequence']]
        after = [e for e in events if e['sequence'] > battle['end_sequence']]
        pre = _custom_observations(before, battle['uuid'])
        post = _custom_observations(after, battle['uuid'])
        uid = context['expected_uid']
        if (not any(observed_uid(e['message'], e['details'], uid) for e in pre)
                or not any(observed_uid(e['message'], e['details'], uid) for e in post)):
            raise ValueError('account_not_proven')
        result.update(account_binding='verified', activity_binding='verified',
                      account=context['account'], activity_instance=context['activity_instance'])
        if battle['support_used']:
            raise ValueError('support_cannot_grant_saved_proxy')
        stage_sequence = None
        proxy_sequence = None
        for e in post:
            v = e['details']
            d = v.get('details', {})
            match = d.get('result', {})
            if not isinstance(match, dict):
                continue
            if (v.get('first') == ['ZootdCopilotProofStage']
                    and d.get('task') == 'ZootdCopilotProofStage'
                    and d.get('algorithm') == 'OcrDetect' and d.get('action') == 'ClickSelf'
                    and match.get('text') == context['stage_code']):
                stage_sequence = e['sequence']
            if (stage_sequence is not None and v.get('first') == ['StageQueue@CheckPrts']
                    and d.get('task') == 'UsePrtsSuccessCheck'
                    and d.get('algorithm') == 'MatchTemplate' and d.get('action') == 'DoNothing'
                    and match.get('template') == 'UsePrtsSuccess.png'):
                proxy_sequence = e['sequence']
        if proxy_sequence is None:
            raise ValueError('saved_proxy_not_proven')
        return dict(result, status='verified', saved_proxy='verified', reason='fresh_bound_saved_proxy',
                    proxy_sequence=proxy_sequence,
                    proof_callbacks_sha256=sha256_bytes(canonical_json(events)))
    except (ValueError, KeyError, TypeError) as exc:
        return dict(result, reason=str(exc))


def record(root, context, proof):
    """Called only by the live parent reducer while it holds the device lock."""
    if (proof.get('status') != 'verified' or proof.get('saved_proxy') != 'verified'
            or proof.get('support_used') is not False):
        return False
    if scope(root, context['stage'], context['stage_code']) != context['activity_instance']:
        raise ValueError('Activity scope expired during battle')
    config = load_config(root / 'config/farming.toml')
    if config.account != context['account'] or config.client_type != context['client']:
        raise ValueError('Account configuration changed')
    identity = {k: context[k] for k in ('client', 'account', 'uid_sha256')}
    binding = root / 'var/state/copilot-account-binding.json'
    if binding.exists():
        if decode(binding.read_bytes()) != identity:
            raise ValueError('Account binding changed')
    elif context['enroll']:
        atomic_write_json(binding, identity, mode=0o600)
    else:
        raise ValueError('Account enrollment absent')
    from .cli import _locked_ledger, _ledger_path
    key = CapabilityKey(context['client'], context['account'], context['activity_instance'], context['stage_code'])
    with _locked_ledger(root) as ledger:
        old = ledger.query(key)
        if old is not None and old.quarantine_kind == 'manual':
            raise ValueError('Manual quarantine must be cleared explicitly')
        recorded = ledger.mark_verified_observation(
            key, observation_id=sha256_bytes(('copilot:' + proof['run_id']).encode()),
            evidence={**proof, 'source': 'maa-copilot-bound-proxy', 'ledger_recorded': True,
                      'uid_sha256': context['uid_sha256']})
        if not recorded:
            return False
        ledger.save(_ledger_path(root))
    return True
