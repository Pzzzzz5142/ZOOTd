from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherSignalTests(unittest.TestCase):
    def run_cleanup(self, action, *, mode="full", nested=False, finish=1,
                    recovery=0, audit_error=False):
        launcher = (ROOT / "scripts/run-daily.sh").read_text()
        # Execute the production cleanup and signal handlers; stub only device
        # operations and the planner so no test touches the game or a model.
        stop = launcher[launcher.index("stop_daily_attempt() {"):
                        launcher.index("run_daily_attempt() {")]
        section = stop + launcher[launcher.index("cleanup() {"):
                           launcher.index("wait_for_waydroid() {")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "var/state/host").mkdir(parents=True)
            script = r'''
set -Eeuo pipefail
supervisor_run_id=test-run
supervisor_active_phase=daily
supervisor_active_evidence=""
waydroid_serial=""
waydroid_ui_pid=""
waydroid_ui_log=""
session_started_by_launcher=false
evening_slot=false
morning_slot=false
launcher_stop_signal=""
planner=fake_planner
info() { printf '%s\n' "$*"; }
remove_runtime_task_config_view() { return 0; }
supervisor_begin_phase() { :; }
supervisor_record_phase() { printf 'phase %s\n' "$*" >>"$project_root/calls"; }
fake_planner() {
    printf '%s\n' "$*" >>"$project_root/calls"
    if [[ "$1" == supervisor-finish ]]; then
        return "$finish"
    fi
    # Repeated hangups during recovery must not terminate it or its children.
    kill -HUP "$$"
    bash -c 'kill -HUP $$; echo child-survived' >>"$project_root/calls"
    return "$recovery"
}
exec 8>"$project_root/lock8"
exec 9>"$project_root/lock9"
'''
            result = subprocess.run(
                ["bash", "-c", script + section + "\n" + action],
                env={**os.environ, "project_root": directory,
                     "supervisor_mode": mode,
                     "MAA_RECOVERY_ACTIVE": str(nested).lower(),
                     "supervisor_audit_error": str(audit_error).lower(),
                     "finish": str(finish), "recovery": str(recovery)},
                capture_output=True, text=True, timeout=10,
            )
            calls = (root / "calls").read_text()
            hangup = root / "var/state/host/test-run-hangup.log"
            return result, calls, hangup.read_text() if hangup.exists() else None

    def test_hangup_records_failure_and_runs_recovery(self):
        result, calls, log = self.run_cleanup('kill -HUP "$$"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exit_status=129", calls)
        self.assertIn("--process-status 129 --defer-to-recovery", calls)
        self.assertEqual(calls.count("supervisor-recover-run"), 1)
        self.assertIn("--slot manual", calls)
        self.assertIn("child-survived", calls)
        self.assertIn("independent recovery verification", log)

    def test_exit_codes_alone_do_not_mean_operator_cancelled(self):
        for status in (1, 129, 130, 143):
            with self.subTest(status=status):
                result, calls, _ = self.run_cleanup(f"exit {status}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls.count("supervisor-recover-run"), 1)

    def test_explicit_stop_signals_do_not_restart_work(self):
        for signal in ("INT", "TERM"):
            for audit_error in (False, True):
                with self.subTest(signal=signal, audit_error=audit_error):
                    result, calls, _ = self.run_cleanup(
                        f'kill -{signal} "$$"', audit_error=audit_error)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("supervisor-recover-run", calls)

    def test_explicit_stop_reaps_the_monitored_daily_process(self):
        result, calls, _ = self.run_cleanup(
            'setsid sleep 60 & daily_attempt_pid=$!; sleep 0.05; '
            'echo "daily-pid $daily_attempt_pid" >>"$project_root/calls"; kill -TERM "$$"')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("supervisor-recover-run", calls)
        pid = next(line.split()[1] for line in calls.splitlines()
                   if line.startswith("daily-pid "))
        self.assertFalse(Path(f"/proc/{pid}").exists())

    def test_failed_recovery_does_not_become_success(self):
        result, calls, log = self.run_cleanup('kill -HUP "$$"', recovery=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("supervisor-recover-run", calls)
        self.assertIn("operational recovery ended with status 1", log)

    def test_success_nonfull_and_nested_runs_do_not_start_recovery(self):
        for options in ({"finish": 0}, {"mode": "check-device"}, {"nested": True}):
            with self.subTest(options=options):
                _, calls, _ = self.run_cleanup("exit 0", **options)
                self.assertNotIn("supervisor-recover-run", calls)


if __name__ == "__main__":
    unittest.main()
