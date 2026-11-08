"""Bounded, read-only native evidence for conservative startup repairs."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import re

from .util import sha256_bytes

SIGNALS = frozenset({'startup_failed', 'fresh_official_input_anr', 'game_offline',
                     'native_motion_timeout', 'native_key_timeout', 'depot_failed'})
ANCHORS = frozenset({'fresh_official_input_anr', 'game_offline'})
PACKAGE = 'com.hypergryph.arknights'
RULE_ID = re.compile(r'[a-z][a-z0-9-]{0,63}')


def rules(root: Path) -> list[dict]:
    """Literal-only Python prevents generated rules from executing arbitrary code."""
    path = root / 'maa_planner/fallback_rules.py'
    if path.is_symlink() or path.stat().st_size > 32_768:
        raise ValueError('invalid fallback rule source')
    module = ast.parse(path.read_text())
    statements = [n for n in module.body if not (isinstance(n, ast.Expr)
                  and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
    if (len(statements) != 1 or not isinstance(statements[0], ast.Assign)
            or len(statements[0].targets) != 1
            or not isinstance(statements[0].targets[0], ast.Name)
            or statements[0].targets[0].id != 'RULES'):
        raise ValueError('fallback rules must contain one literal RULES assignment')
    values = ast.literal_eval(statements[0].value)
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ValueError('invalid fallback rule count')
    seen = set()
    for rule in values:
        if (not isinstance(rule, dict) or set(rule) != {'id', 'version', 'label', 'requires', 'action'}
                or not isinstance(rule['id'], str) or not RULE_ID.fullmatch(rule['id'])
                or rule['id'] in seen or type(rule['version']) is not int or rule['version'] < 1
                or not isinstance(rule['label'], str) or not 1 <= len(rule['label']) <= 40
                or not all(c.isprintable() for c in rule['label'])
                or rule['action'] != 'restart-waydroid-session'):
            raise ValueError('unsupported fallback rule')
        required = rule['requires']
        if (not isinstance(required, dict) or not required or not set(required) <= SIGNALS
                or any(type(v) is not bool for v in required.values())
                or not any(required.get(key) is True for key in ANCHORS)):
            raise ValueError('fallback rule lacks a precise native evidence anchor')
        seen.add(rule['id'])
    return values


def revision(rule: dict) -> str:
    from .util import canonical_json
    return sha256_bytes(canonical_json(rule))


def matches(rule: dict, signals: dict) -> bool:
    return all(type(signals.get(k)) is bool and signals[k] == v
               for k, v in rule['requires'].items())


def startup_signals(log: bytes) -> dict:
    if len(log) > 2 * 1024 * 1024:
        raise ValueError('startup log too large')
    text = log.decode('utf-8', errors='replace')
    event = lambda name: bool(re.search(r'^\[[^\]\n]+\]\s+' + name + r'\s*$', text, re.M))
    # Recovery is only offered before any useful daily action or battle ended.
    pristine = not re.search(r'\b(?:StartUp|Depot|Infrast|Recruit|Mall|Fight) Completed\b|Mission started', text)
    return {'startup_failed': pristine and event('StartUp Start') and event('StartUp Error'),
            'game_offline': pristine and event('GameOffline'),
            'fresh_official_input_anr': False, 'native_motion_timeout': None,
            'native_key_timeout': None, 'depot_failed': False}


def anr_evidence(directory: Path, *, since: datetime, now: datetime) -> dict | None:
    """Require an official-package input timeout with an in-run native timestamp."""
    if not directory.exists() or any(p.is_symlink() for p in (directory, *directory.parents)):
        return None
    for path in sorted(directory.glob('anr_*'), reverse=True)[:16]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            with path.open('rb') as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
        except (PermissionError, FileNotFoundError):
            continue
        if len(raw) > 4 * 1024 * 1024:
            continue
        header = raw[:32768].decode('utf-8', errors='replace')
        if not re.search(r'^Subject: Input dispatching timed out \([^\n]*\b'
                         + re.escape(PACKAGE) + r'/[^\n]+\(server\) is not responding\.', header, re.M):
            continue
        stamp = re.search(r'^----- pid \d+ at ([^\n]+) -----$', header, re.M)
        if not stamp:
            continue
        try:
            at = datetime.fromisoformat(stamp[1])
        except ValueError:
            continue
        if at.tzinfo is None or not since <= at <= now:
            continue
        return {'kind': 'official-input-anr', 'at': at.astimezone(timezone.utc).isoformat(),
                'sha256': sha256_bytes(raw), 'size': len(raw),
                'input_kind': 'motion' if re.search(r'^Subject: .*for MotionEvent', header, re.M)
                else 'key' if re.search(r'^Subject: .*for KeyEvent', header, re.M) else 'unknown'}
    return None
