from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = (ROOT / 'scripts/run-daily.sh').read_text()
WATCHDOG = LAUNCHER[LAUNCHER.index('daily_log_has_game_offline() {'):LAUNCHER.index('restart_waydroid_after_game_offline() {')]
RESTART = LAUNCHER[LAUNCHER.index('restart_waydroid_after_game_offline() {'):LAUNCHER.index('run_with_timeout() {')]
DAILY = LAUNCHER[LAUNCHER.index('run_daily_routine() {'):LAUNCHER.index('annihilation_decision_is_safe() {')]


class DailyOfflineTests(unittest.TestCase):
    def test_watchdog_reaps_descendants_when_the_leader_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'maa'
            fake.write_text('#!/bin/bash\nsleep 60 &\necho $! > "$CHILD_PID"\nexit 7\n')
            fake.chmod(0o755)
            result = subprocess.run(
                ['bash', '-c', WATCHDOG + '\ninfo() { :; }; die() { exit 1; }; daily_attempt_pid=""; run_daily_attempt 5s "$TEST_LOG"'],
                env={**os.environ, 'maa': str(fake), 'runtime_task_config_dir': directory,
                     'MAA_HOST_TASK': 'daily', 'MAA_HOST_PROFILE': 'waydroid',
                     'CHILD_PID': str(root / 'child'), 'TEST_LOG': str(root / 'daily.log')},
                capture_output=True, text=True, timeout=9,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            pid = (root / 'child').read_text().strip()
            stat = Path(f'/proc/{pid}/stat')
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')

    def test_watchdog_interrupts_offline_and_kills_stubborn_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'maa'
            fake.write_text('''#!/usr/bin/env python3
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); time.sleep(60)'])
Path(os.environ['CHILD_PID']).write_text(str(child.pid))
log = next(a.split('=',1)[1] for a in sys.argv if a.startswith('--log-file='))
Path(log).write_text('[2026-09-22 02:00:00 WARN ] GameOffline\\n')
time.sleep(60)
''')
            fake.chmod(0o755)
            start = time.monotonic()
            result = subprocess.run(['bash', '-c', WATCHDOG + '''
info() { :; }
die() { exit 1; }
daily_attempt_pid=""
run_daily_attempt 30s "$TEST_LOG"
'''], env={**os.environ, 'maa': str(fake), 'runtime_task_config_dir': directory,
                  'MAA_HOST_TASK': 'daily', 'MAA_HOST_PROFILE': 'waydroid',
                  'CHILD_PID': str(root / 'child'), 'TEST_LOG': str(root / 'daily.log')},
                  capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertLess(time.monotonic() - start, 9)
            pid = (root / 'child').read_text()
            stat = Path(f'/proc/{pid}/stat')
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')
            self.assertIn('GameOffline', (root / 'daily.log').read_text())

    def test_watchdog_preserves_normal_status_and_rejects_old_logs(self):
        for content, status, existing, expected in (
            ('[now INFO ] AllTasksCompleted', 0, False, 0),
            ('unrelated mention GameOffline', 7, False, 7),
            ('[now WARN ] GameOffline', 0, False, 75),
            ('', 0, True, 1),
        ):
            with self.subTest(content=content, existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fake = root / 'maa'
                fake.write_text('#!/bin/bash\nprintf "%s\\n" "$CONTENT" > "${1#--log-file=}"\nexit "$STATUS"\n')
                fake.chmod(0o755)
                log = root / 'daily.log'
                if existing:
                    log.write_text('[old WARN ] GameOffline\n')
                result = subprocess.run(['bash', '-c', WATCHDOG + '\ninfo() { :; }; die() { exit 1; }; daily_attempt_pid=""; run_daily_attempt 5s "$TEST_LOG"'],
                    env={**os.environ, 'maa': str(fake), 'runtime_task_config_dir': directory,
                         'MAA_HOST_TASK': 'daily', 'MAA_HOST_PROFILE': 'waydroid',
                         'CONTENT': content, 'STATUS': str(status), 'TEST_LOG': str(log)},
                    capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_daily_restarts_once_only_for_fresh_offline_evidence(self):
        for first, retry, rebuild_fails, expected, rebuilds in (
            ('success', 'success', False, 0, 0),
            ('offline', 'success', False, 0, 1),
            ('offline', 'offline', False, 1, 1),
            ('offline', 'success', True, 1, 1),
            ('other', 'success', False, 0, 0),
            ('other', 'other', False, 1, 0),
        ):
            with self.subTest(first=first, retry=retry, rebuild_fails=rebuild_fails), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / 'var/state/host').mkdir(parents=True)
                script = WATCHDOG + DAILY + r'''
set -Eeuo pipefail
info() { :; }
die() { exit 1; }
server_game_day_now() { echo 2026-09-21; }
render_runtime_task() { :; }
daily_log_is_complete() { grep -q success "$1"; }
attempts=0
run_daily_attempt() {
    attempts=$((attempts+1))
    result="$FIRST"
    if (( attempts > 1 )); then result="$RETRY"; fi
    echo "attempt $attempts" >>"$project_root/calls"
    if [[ "$result" == offline ]]; then
        echo '[now WARN ] GameOffline' >"$2"
        return 75
    fi
    echo "$result" >"$2"
    [[ "$result" == success ]]
}
restart_waydroid_after_game_offline() {
    echo rebuild >>"$project_root/calls"
    [[ "$REBUILD_FAILS" != true ]] || die failed
}
run_daily_routine
'''
                result = subprocess.run(['bash', '-c', script], env={**os.environ,
                    'project_root': directory, 'FIRST': first, 'RETRY': retry,
                    'REBUILD_FAILS': str(rebuild_fails).lower(), 'drone_mode': 'Money',
                    'daily_attempt_timeout': '3h'}, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                calls = (Path(directory) / 'calls').read_text()
                self.assertEqual(calls.count('rebuild'), rebuilds)
                self.assertLessEqual(calls.count('attempt'), 2)
                if first == 'offline' and not rebuild_fails:
                    self.assertEqual(calls.splitlines(), ['attempt 1', 'rebuild', 'attempt 2'])

    def test_rebuild_rechecks_device_and_never_changes_network_or_account(self):
        for stop_fails, busy, expected in ((False, False, 0), (True, False, 1), (False, True, 1)):
            with self.subTest(stop_fails=stop_fails, busy=busy), tempfile.TemporaryDirectory() as directory:
                script = RESTART + r'''
set -Eeuo pipefail
info() { :; }
die() { exit 1; }
waydroid_serial=old:5555
waydroid_ui_pid=""
official_package=com.hypergryph.arknights
session_started_by_launcher=false
sleep() { :; }
timeout() {
    echo "$*" >>"$project_root/calls"
    if [[ "$*" == *'session stop'* && "$STOP_FAILS" == true ]]; then return 1; fi
}
waydroid_session_is_running() { return 1; }
flock() { [[ "$BUSY" != true ]]; }
prepare_waydroid_device() {
    [[ -z "$waydroid_serial" && "$session_started_by_launcher" == false ]]
    echo revalidate >>"$project_root/calls"
}
restart_waydroid_after_game_offline
'''
                result = subprocess.run(['bash', '-c', script], env={**os.environ,
                    'project_root': directory, 'STOP_FAILS': str(stop_fails).lower(),
                    'BUSY': str(busy).lower()}, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                calls = (Path(directory) / 'calls').read_text()
                self.assertIn('am force-stop com.hypergryph.arknights', calls)
                self.assertIn('waydroid session stop', calls)
                self.assertEqual('revalidate' in calls, expected == 0)
                self.assertNotIn('clear', calls)


if __name__ == '__main__':
    unittest.main()
