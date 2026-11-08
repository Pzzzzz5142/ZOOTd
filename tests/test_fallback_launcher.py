from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = (ROOT / 'scripts/run-daily.sh').read_text()
DEPOT = LAUNCHER[LAUNCHER.index('scan_depot_inventory_once() {'):
                 LAUNCHER.index('proxy_preflight_log_is_complete() {')]
DAILY = LAUNCHER[LAUNCHER.index('run_daily_routine() {'):
                 LAUNCHER.index('annihilation_decision_is_safe() {')]


class FallbackLauncherTests(unittest.TestCase):
    def test_depot_has_only_one_approved_startup_replacement_and_no_later_scan(self):
        for approved, first_ok, retry_ok, expected, calls in (
            (False, False, True, 1, 1), (True, False, True, 0, 2),
            (True, False, False, 1, 2), (True, True, True, 0, 1)):
            with self.subTest(approved=approved, first=first_ok, retry=retry_ok), tempfile.TemporaryDirectory() as temp:
                script = DEPOT + r'''
set -Eeuo pipefail
info() { :; }
maa=fake-maa
planner=fake-planner
MAA_HOST_PROFILE=waydroid
depot_scan_attempted=false
inventory_snapshot_ready=false
attempts=0
run_farming_soft_with_timeout() {
    if [[ "$2" == fake-maa ]]; then
        attempts=$((attempts+1))
        echo attempt >>"$project_root/calls"
        return 0
    fi
    if (( attempts == 1 )); then [[ "$FIRST" == true ]]; else [[ "$RETRY" == true ]]; fi
}
request_learned_restart() {
    [[ "$2" == depot ]] || exit 9
    [[ "$APPROVED" == true ]]
}
restart_waydroid_after_game_offline() { echo restart >>"$project_root/calls"; }
finish_learned_restart() { echo "result $2" >>"$project_root/calls"; }
scan_depot_inventory_once "$project_root/scan.log"
[[ "$inventory_snapshot_ready" == true ]]
if scan_depot_inventory_once "$project_root/forbidden.log"; then exit 10; fi
'''
                result = subprocess.run(['bash', '-c', script], env={**os.environ,
                    'project_root': temp, 'APPROVED': str(approved).lower(),
                    'FIRST': str(first_ok).lower(), 'RETRY': str(retry_ok).lower()},
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                events = (Path(temp) / 'calls').read_text()
                self.assertEqual(events.count('attempt'), calls)
                self.assertEqual(events.count('restart'), int(approved and not first_ok))
                if approved and not first_ok:
                    self.assertIn('result ' + str(retry_ok).lower(), events)

    def test_daily_approved_repair_uses_existing_retry_budget_and_records_result(self):
        for approved, retry_ok in ((False, True), (True, True), (True, False)):
            with self.subTest(approved=approved, retry_ok=retry_ok), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / 'var/state/host').mkdir(parents=True)
                script = DAILY + r'''
set -Eeuo pipefail
info() { :; }
die() { exit 1; }
drone_mode=Money
daily_attempt_timeout=3h
server_game_day_now() { echo 2026-11-08; }
render_runtime_task() { :; }
daily_log_has_game_offline() { return 1; }
daily_log_is_complete() { [[ "$RETRY" == true ]]; }
request_learned_restart() { [[ "$APPROVED" == true ]]; }
restart_waydroid_after_game_offline() { echo restart >>"$project_root/calls"; }
finish_learned_restart() { echo "result $2" >>"$project_root/calls"; }
attempts=0
run_daily_attempt() {
    attempts=$((attempts+1))
    echo attempt >>"$project_root/calls"
    if (( attempts == 1 )); then return 1; fi
    [[ "$RETRY" == true ]]
}
run_daily_routine
'''
                result = subprocess.run(['bash', '-c', script], env={**os.environ,
                    'project_root': temp, 'APPROVED': str(approved).lower(), 'RETRY': str(retry_ok).lower()},
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0 if retry_ok else 1, result.stderr)
                events = (Path(temp) / 'calls').read_text()
                self.assertEqual(events.count('attempt'), 2)
                self.assertEqual(events.count('restart'), int(approved))
                self.assertEqual(events.count('result'), int(approved))


if __name__ == '__main__':
    unittest.main()
