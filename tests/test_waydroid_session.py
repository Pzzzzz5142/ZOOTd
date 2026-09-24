from pathlib import Path
import os
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = (ROOT / 'scripts/run-daily.sh').read_text()
SESSION = LAUNCHER[LAUNCHER.index('waydroid_session_is_running() {'):LAUNCHER.index('cleanup() {')]


class WaydroidSessionTests(unittest.TestCase):
    def run_session(self, root, *, running=True, stop='success', action=None):
        fake = root / 'waydroid'
        fake.write_text('''#!/bin/bash
if [[ "$1" == status ]]; then
    if [[ -f "$TEST_ROOT/running" ]]; then
        printf 'Session:\\tRUNNING\\nWayland display:\\t%s\\n' "$TEST_DISPLAY"
    else
        echo 'Session: STOPPED'
    fi
elif [[ "$1 $2" == 'session stop' ]]; then
    echo stop >> "$TEST_ROOT/calls"
    [[ "$TEST_STOP" != error ]] || exit 1
    [[ "$TEST_STOP" == stuck ]] || rm -f "$TEST_ROOT/running"
fi
''')
        fake.chmod(0o755)
        (root / 'var/run').mkdir(parents=True)
        if running:
            (root / 'running').touch()
        script = 'set -Eeuo pipefail\n' + SESSION + '''
info() { :; }
die() { echo "$*" >&2; exit 1; }
project_root="$TEST_ROOT"
session_started_by_launcher=false
''' + (action or '''
repair_stale_waydroid_session
printf 'owned=%s\n' "$session_started_by_launcher"
''')
        result = subprocess.run(['bash', '-c', script], env={
            **os.environ, 'PATH': str(root) + ':' + os.environ['PATH'],
            'TEST_ROOT': str(root), 'XDG_RUNTIME_DIR': str(root),
            'TEST_DISPLAY': 'gamescope-0', 'TEST_STOP': stop,
            # A valid caller display must not mask the broken session display.
            'WAYLAND_DISPLAY': 'unrelated-desktop',
        }, capture_output=True, text=True, timeout=10)
        calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
        return result, calls

    def test_dead_socket_is_reclaimed_but_live_session_is_preserved(self):
        for alive in (False, True):
            with self.subTest(alive=alive), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with socket.socket(socket.AF_UNIX) as surface:
                    surface.bind(str(root / 'gamescope-0'))
                    if alive:
                        surface.listen()
                    else:
                        surface.close()  # Reproduce SIGKILL: socket file survives.
                    result, calls = self.run_session(root)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, '' if alive else 'stop\n')
                self.assertIn('owned=false' if alive else 'owned=true', result.stdout)

    def test_missing_surface_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_session(Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, 'stop\n')
            self.assertIn('owned=true', result.stdout)

    def test_stopped_session_needs_no_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = self.run_session(Path(tmp), running=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, '')

    def test_failed_stop_blocks_replacement(self):
        for stop in ('error', 'stuck'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as tmp:
                result, calls = self.run_session(Path(tmp), stop=stop)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, 'stop\n')
                self.assertNotIn('owned=', result.stdout)

    def test_busy_surface_owner_blocks_replacement(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # run_session creates the lock's parent directory.
            lock = root / 'held-lock'
            with lock.open('w') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                action = '''
ln "$TEST_ROOT/held-lock" "$project_root/var/run/waydroid-scaled-ui.lock"
sleep() { :; }
repair_stale_waydroid_session
echo unexpected-replacement
'''
                result, calls = self.run_session(root, action=action)
            self.assertEqual(result.returncode, 1)
            self.assertIn('has not released its lock', result.stderr)
            self.assertNotIn('unexpected-replacement', result.stdout)
            self.assertEqual(calls, 'stop\n')
