from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from maa_planner.capability import (
    CapabilityKey, CapabilityLedger, CapabilityLedgerError,
    extract_fight_observations,
)
from maa_planner.proxy import PROXY_RESOURCE, extract_saved_proxy
from maa_planner.runtime_contracts import validate_runtime_contracts


ROOT = Path(__file__).resolve().parents[1]
KEY = CapabilityKey("Official", "main", "a" * 24, "SR-8")
NOW = datetime(2026, 9, 5, tzinfo=UTC)


def callback(label, *, taskchain="Fight", taskid=1, **fields):
    return f"[2026-09-05 10:00:00.000] Assistant::append_callback | {label} " + json.dumps(
        {"uuid": "device", "taskid": taskid, "taskchain": taskchain, **fields},
        ensure_ascii=False,
    ) + "\n"


def drop(stars=2, stage="SR-8", **fields):
    return callback("SubTaskExtraInfo", what="StageDrops", details={
        "stage": {"stageCode": stage, "stageId": "act54side_08"},
        "stars": stars, "drops": [{"itemId": "30043", "quantity": 1}],
    }, **fields)


def screen_log(stage="SR-8", leaf=None):
    text = ""
    for index, first in enumerate(("MaaHostProxyTerminal", stage, "StageQueue@CheckPrts"), 1):
        text += callback("TaskChainStart", taskchain="Custom", taskid=index)
        task = first if index == 1 else (leaf or stage + "@SideStoryStage") if index == 2 else "UsePrtsSuccessCheck"
        text += callback("SubTaskCompleted", taskchain="Custom", taskid=index,
                         first=[first], details={"task": task,
                            "action": "ClickSelf" if index == 2 else "DoNothing",
                            "algorithm": "OcrDetect" if index == 2 else "MatchTemplate",
                            "result": {"template": "UsePrtsSuccess.png", "text": stage}})
        text += callback("TaskChainCompleted", taskchain="Custom", taskid=index)
    return text + callback("AllTasksCompleted", taskchain="Custom", taskid=3, finished_tasks=[1, 2, 3])


class ProxyLedgerTests(unittest.TestCase):
    def test_cli_reconcile_status_reset_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "config", root / "config")
            log = root / "fight.log"

            def call(*args):
                process = subprocess.run(
                    [str(ROOT / "bin/maa-planner"), "--project-root", directory, *args],
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                return json.loads(process.stdout)

            key = ("--stage", "SR-8", "--activity-instance", KEY.activity_instance)
            text = ""
            for failure in range(1, 4):
                text += drop(taskid=failure)
                log.write_text(text)
                result = call("reconcile-fight", "--log", str(log), *key)
                self.assertEqual(result["consecutive_failures"], failure)
                replay = call("reconcile-fight", "--log", str(log), *key)
                self.assertFalse(replay["recorded"])
                self.assertEqual(replay["consecutive_failures"], failure)
            self.assertEqual(call("proxy-status", *key)["status"], "quarantined")
            from unittest.mock import patch
            with patch.dict(os.environ, {"MAA_PROXY_RUN_ID": "next-run"}):
                self.assertEqual(call("proxy-status", *key)["status"], "unknown")
                log.write_text(text + drop(taskid=10))
                self.assertEqual(call("reconcile-fight", "--log", str(log), *key)["consecutive_failures"], 1)
                self.assertEqual(call("proxy-status", *key)["status"], "retry")
            self.assertEqual(call("proxy-reset", *key)["status"], "unknown")

    def test_launcher_assigns_new_supervisor_run_to_all_planner_commands(self):
        launcher = (ROOT / "scripts/run-daily.sh").read_text()
        assignment = 'export MAA_PROXY_RUN_ID="${supervisor_run_id}"'
        self.assertEqual(launcher.count(assignment), 1)
        self.assertIn('reconcile-fight --failure-unit entry', launcher)
        self.assertIn('"$((deadline - SECONDS))"', launcher)
        self.assertLess(launcher.index(' supervisor-start --mode '), launcher.index(assignment))
        self.assertLess(launcher.index(assignment), launcher.index('if [[ "${check_device}" != true ]]; then', launcher.index(assignment)))

    def test_new_run_expires_automatic_failures_and_preserves_deduplication(self):
        for failure_count in (1, 2, 3):
            with self.subTest(failure_count=failure_count):
                ledger = CapabilityLedger().for_run("run-a")
                text = "".join(drop(taskid=i) for i in range(1, failure_count + 1))
                self.reconcile(ledger, text)
                before = ledger.query(KEY)
                ledger.for_run("run-a")
                self.assertEqual(ledger.query(KEY), before)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "ledger.json"
                    ledger.save(path)
                    ledger = CapabilityLedger.load(path).for_run("run-b")
                self.assertEqual(ledger.status(KEY), "unknown")
                self.assertEqual(ledger.query(KEY).consecutive_failures, 0)
                self.assertEqual(ledger.query(KEY).evidence, before.evidence)
                self.assertFalse(self.reconcile(ledger, text).recorded)
                result = self.reconcile(ledger, text + drop(taskid=10))
                self.assertEqual(result.consecutive_failures, 1)
                self.assertEqual(result.outcome, "retry")
                self.assertEqual(ledger.query(KEY).failure_run_id, "run-b")

    def test_new_run_expires_legacy_automatic_block_but_keeps_manual_and_success(self):
        ledger = CapabilityLedger()
        self.reconcile(ledger, "".join(drop(taskid=i) for i in range(3)))
        ledger.for_run("new-run")
        self.assertEqual(ledger.status(KEY), "unknown")
        ledger.mark_verified(KEY, observed_at=NOW)
        before = ledger.query(KEY)
        ledger.for_run("next-run")
        self.assertEqual(ledger.query(KEY), before)
        ledger.mark_quarantined(KEY, "operator-blocked", observed_at=NOW)
        before = ledger.query(KEY)
        ledger.for_run("another-run")
        self.assertEqual(ledger.query(KEY), before)

    def test_native_entry_counts_once_and_keeps_all_battle_evidence(self):
        ledger = CapabilityLedger().for_run("native-run")
        def entry(text, offset=0):
            return ledger.reconcile_entry(
                KEY, extract_fight_observations(text, KEY.stage),
                log_device=1, log_inode=2, suffix_start_byte=offset, observed_at=NOW,
            )
        failures = "".join(drop(taskid=i) for i in range(3))
        self.assertEqual(entry(failures).consecutive_failures, 1)
        self.assertEqual(len(ledger.query(KEY).processed_observations), 3)
        self.assertFalse(entry(failures).recorded)
        self.assertEqual(entry(failures, 10000).consecutive_failures, 2)
        recovered = drop(taskid=10) + drop(3, taskid=11) + drop(3, taskid=12)
        self.assertEqual(entry(recovered, 20000).outcome, "verified")
        self.assertEqual(ledger.query(KEY).success_count, 2)
        self.assertEqual(ledger.query(KEY).consecutive_failures, 0)
        for number in range(1, 4):
            result = entry(failures, 30000 + number * 10000)
            self.assertEqual(result.consecutive_failures, number)
        self.assertEqual(result.outcome, "quarantined")
        ledger.for_run("next-native-run")
        self.assertEqual(ledger.status(KEY), "unknown")
        self.assertFalse(entry(failures, 60000).recorded)

    def reconcile(self, ledger, text, offset=0, key=KEY):
        return ledger.reconcile(key, extract_fight_observations(text, key.stage),
                                log_device=1, log_inode=2, suffix_start_byte=offset,
                                observed_at=NOW)

    def test_threshold_is_three_and_same_evidence_is_idempotent(self):
        ledger = CapabilityLedger()
        text = drop()
        self.assertEqual(self.reconcile(ledger, text).outcome, "retry")
        self.assertFalse(self.reconcile(ledger, text).recorded)
        self.assertEqual(ledger.query(KEY).consecutive_failures, 1)
        text += drop()
        self.assertEqual(self.reconcile(ledger, text).consecutive_failures, 2)
        text += drop()
        self.assertEqual(self.reconcile(ledger, text).outcome, "quarantined")
        record = ledger.query(KEY)
        self.assertEqual(record.consecutive_failures, 3)
        self.assertEqual(record.quarantine_kind, "automatic")
        # Historical successes cannot clear an already quarantined proxy.
        self.assertEqual(self.reconcile(ledger, drop(3), len(text)).outcome, "quarantined")
        self.assertTrue(ledger.reset(KEY))
        self.assertEqual(ledger.status(KEY), "unknown")

    def test_overlap_cursor_and_multibyte_log_deduplicate(self):
        ledger = CapabilityLedger()
        prefix = "中文前缀\n"
        event = drop()
        self.reconcile(ledger, prefix + event)
        self.assertFalse(self.reconcile(ledger, event, len(prefix.encode())).recorded)
        self.assertEqual(ledger.query(KEY).consecutive_failures, 1)

    def test_later_success_in_same_fresh_execution_resets_three_failures(self):
        ledger = CapabilityLedger()
        result = self.reconcile(ledger, drop() + drop() + drop() + drop(3))
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(result.consecutive_failures, 0)

    def test_success_resets_even_if_later_navigation_fails(self):
        ledger = CapabilityLedger()
        text = drop() + drop() + drop(3) + callback("TaskChainError") + drop()
        result = self.reconcile(ledger, text)
        self.assertEqual(result.outcome, "retry")
        self.assertEqual(result.consecutive_failures, 1)
        self.assertEqual(ledger.query(KEY).success_count, 1)

    def test_unknown_network_other_stage_and_preflight_never_poison(self):
        ledger = CapabilityLedger()
        text = drop()
        self.reconcile(ledger, text)
        unknown = drop(0) + drop(1) + drop(True) + drop(2, "SR-6")
        unknown += callback("SubTaskExtraInfo", what="GameOffline")
        unknown += callback("TaskChainError") + screen_log()
        result = self.reconcile(ledger, unknown, len(text.encode()))
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.recorded)
        self.assertEqual(result.consecutive_failures, 1)

    def test_refund_requires_explicit_same_execution_failed_screen(self):
        failed = callback("SubTaskCompleted", details={"task": "Fight@FightMissionFailed"})
        self.assertEqual(len(extract_fight_observations(failed + drop(0), "SR-8")), 1)
        self.assertEqual(extract_fight_observations(drop(0), "SR-8"), ())
        self.assertEqual(extract_fight_observations(failed + drop(0, taskid=2), "SR-8"), ())
        offline = callback("SubTaskExtraInfo", what="GameOffline")
        self.assertEqual(extract_fight_observations(failed + offline + drop(0), "SR-8"), ())
        self.assertEqual(extract_fight_observations(failed + callback("TaskChainError") + drop(0), "SR-8"), ())

    def test_state_is_scoped_and_survives_restart(self):
        ledger = CapabilityLedger()
        self.reconcile(ledger, drop() + drop())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger.save(path)
            ledger = CapabilityLedger.load(path)
            self.assertEqual(ledger.query(KEY).consecutive_failures, 2)
            other = CapabilityKey("Official", "other", KEY.activity_instance, KEY.stage)
            self.assertEqual(ledger.status(other), "unknown")
            self.assertEqual(self.reconcile(ledger, drop(), offset=9999).outcome, "quarantined")

    def test_legacy_automatic_quarantine_becomes_first_failure_only(self):
        ledger = CapabilityLedger()
        ledger.mark_quarantined(KEY, "proxy-non-three-star", observed_at=NOW)
        payload = ledger.as_dict()
        payload["schema_version"] = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text(json.dumps(payload))
            migrated = CapabilityLedger.load(path)
            self.assertEqual(migrated.status(KEY), "retry")
            self.assertEqual(migrated.query(KEY).consecutive_failures, 1)
            payload["entries"][0]["quarantine_reason"] = "operator-blocked"
            path.write_text(json.dumps(payload))
            manual = CapabilityLedger.load(path)
            self.assertEqual(manual.status(KEY), "quarantined")
            self.assertEqual(manual.query(KEY).quarantine_kind, "manual")

    def test_corrupt_state_fails_closed(self):
        ledger = CapabilityLedger()
        self.reconcile(ledger, drop())
        payload = ledger.as_dict()
        payload["entries"][0]["consecutive_failures"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text(json.dumps(payload))
            with self.assertRaises(CapabilityLedgerError):
                CapabilityLedger.load(path)
            payload["entries"][0]["consecutive_failures"] = 1
            for invalid in (None, {}, "abc"):
                payload["entries"][0]["processed_observations"] = invalid
                path.write_text(json.dumps(payload))
                with self.assertRaises(CapabilityLedgerError):
                    CapabilityLedger.load(path)


class ProxyScreenTests(unittest.TestCase):
    def test_fresh_ordered_screen_is_no_battle_evidence(self):
        for stage, leaf in (("SR-8", "SideStoryStage"), ("1-7", "Stage1-7"), ("AP-5", "Stage")):
            proof = extract_saved_proxy(screen_log(stage, leaf), stage)
            self.assertIsNotNone(proof)
            self.assertFalse(proof["consumes_sanity"])

    def test_wrong_stage_or_partial_or_error_is_not_authorization(self):
        valid = screen_log()
        self.assertIsNone(extract_saved_proxy(valid, "SR-6"))
        self.assertIsNone(extract_saved_proxy(valid[:valid.rindex("[2026")], "SR-8"))
        self.assertIsNone(extract_saved_proxy(valid.replace('"text": "SR-8"', '"text": "SR-6"'), "SR-8"))
        self.assertIsNone(extract_saved_proxy(callback("TaskChainStart") + valid, "SR-8"))
        self.assertIsNone(extract_saved_proxy(valid.replace("UsePrtsSuccess.png", "UnableToAgent2.png"), "SR-8"))

    def test_guard_overlay_and_static_contracts(self):
        self.assertEqual(json.loads((ROOT / "config/resource/tasks/tasks.json").read_text()), PROXY_RESOURCE)
        validate_runtime_contracts(ROOT)


def shell_function(name):
    launcher = (ROOT / "scripts/run-daily.sh").read_text()
    start = launcher.index(name + "() {")
    return launcher[start:launcher.index("\n}\n", start) + 3]


class ProxyLauncherTests(unittest.TestCase):
    def run_shell(self, body, mode="retry_then_success"):
        functions = "\n".join(shell_function(name) for name in (
            "fight_log_observes_sanity_below", "fight_log_proves_sanity_below",
            "run_stage_with_proxy_retries", "run_regular_fallback", "run_planned_activity_candidates",
        ))
        common = r'''
set -eu
project_root="$TEST_ROOT"
mkdir -p "$project_root/var/state/host"
planner=fake-planner
core_log="$project_root/core.log"
core_log_cursor_args=()
farming_contracts_ready=true
planner_helpers_ready=true
farming_sanity_cleared=false
regular_proxy_scope=29c76524662bd6e6e945f273
regular_fallback_stages=(AP-5 1-7)
declare -A counts=()
info() { :; }
capture_core_log_cursor() { fight_core_offset=0; return 0; }
game_client_has_saved_proxy() {
    printf '%s\n' "$1" >> "$project_root/screens"
    [[ "$MODE" != screen_failure ]]
}
run_sanity_fight() {
    local code="$1" sanity=50
    counts[$code]=$(( ${counts[$code]:-0} + 1 ))
    printf '%s\n' "$code" >> "$project_root/battles"
    [[ "$MODE" != tail ]] || { [[ "$code" != AP-5 ]] || sanity=10; [[ "$code" != 1-7 ]] || sanity=4; }
    [[ "$MODE" != retry_then_success || ${counts[$code]} -lt 3 ]] || sanity=4
    [[ "$MODE" != reset || ${counts[$code]} -lt 6 ]] || sanity=4
    printf '%s\n' 'Fight Start' "Current sanity: $sanity/210" 'Fight Completed' 'AllTasksCompleted' > "$2"
    [[ "$MODE" != partial_success_error ]] || { printf '%s\n' 'Fight Error' >> "$2"; return 1; }
    [[ "$MODE" != error_low ]] || { printf '%s\n' 'Current sanity: 4/210' 'Fight Error' >> "$2"; return 1; }
    return 0
}
timeout() {
    while [[ "$1" != fake-planner ]]; do shift; done
    shift
    if [[ "$1" == proxy-status ]]; then
        if [[ "$MODE" == quarantined ]]; then printf '%s\n' '{"status":"quarantined"}';
        else printf '%s\n' '{"status":"unknown"}'; fi
        return 0
    fi
    local code outcome=unknown streak=0 count
    while [[ "$1" != --stage ]]; do shift; done
    code="$2"
    count=${counts[$code]:-0}
    case "$MODE" in
        partial_success_error) outcome=verified ;;
        retry_then_success|all_failed)
            outcome=retry; streak=$count
            if (( count >= 3 )); then
                if [[ "$MODE" == all_failed ]]; then outcome=quarantined; else outcome=verified; streak=0; fi
            fi ;;
        reset)
            if (( count % 3 == 0 )); then outcome=verified; else outcome=retry; streak=$((count % 3)); fi ;;
    esac
    printf '{"outcome":"%s","recorded":true,"consecutive_failures":%s}\n' "$outcome" "$streak"
}
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(["bash", "-c", functions + common + body],
                                    env={**os.environ, "TEST_ROOT": directory, "MODE": mode},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            read = lambda name: (Path(directory) / name).read_text().splitlines() if (Path(directory) / name).exists() else []
            return result.stdout.splitlines(), read("screens"), read("battles")

    def test_same_candidate_retried_before_switching(self):
        out, screens, battles = self.run_shell('run_stage_with_proxy_retries SR-8 scope "$TEST_ROOT/test"\nprintf "%s\\n" "$farming_sanity_cleared"\n')
        self.assertEqual(battles, ["SR-8"] * 3)
        self.assertEqual(screens, battles)
        self.assertEqual(out, ["true"])

    def test_success_resets_local_failure_streak(self):
        _, _, battles = self.run_shell('run_stage_with_proxy_retries SR-8 scope "$TEST_ROOT/test"\n', "reset")
        self.assertEqual(battles, ["SR-8"] * 6)

    def test_three_failed_tasks_stop_even_with_partial_battle_success(self):
        _, _, battles = self.run_shell(
            'if run_stage_with_proxy_retries SR-8 scope "$TEST_ROOT/test"; then exit 99; fi\n',
            "partial_success_error",
        )
        self.assertEqual(battles, ["SR-8"] * 3)

    def test_all_activity_candidates_then_regular_fallback(self):
        body = r'''
printf '%s\n' '{"evidence":{"execution_candidates":[{"stage_code":"SR-8","item_id":"a","activity_instance":"scope"},{"stage_code":"SR-6","item_id":"b","activity_instance":"scope"},{"stage_code":"SR-7","item_id":"c","activity_instance":"scope"}]}}' > "$TEST_ROOT/decision.json"
if run_planned_activity_candidates "$TEST_ROOT/decision.json" now; then exit 99; fi
if run_regular_fallback; then exit 98; fi
'''
        _, _, battles = self.run_shell(body, "all_failed")
        self.assertEqual(battles, [s for s in ("SR-8", "SR-6", "SR-7", "AP-5", "1-7") for _ in range(3)])

    def test_screen_failure_spends_nothing_and_quarantine_skips_screen(self):
        for mode, expected in (("screen_failure", 3), ("quarantined", 0)):
            _, screens, battles = self.run_shell('if run_stage_with_proxy_retries SR-8 scope "$TEST_ROOT/test"; then exit 99; fi\n', mode)
            self.assertEqual(len(screens), expected)
            self.assertEqual(battles, [])

    def test_tail_requires_fresh_clean_below_six(self):
        out, _, battles = self.run_shell('run_regular_fallback\nprintf "%s\\n" "$regular_fallback_outcome"\n', "tail")
        self.assertEqual(battles, ["AP-5", "1-7"])
        self.assertEqual(out, ["sanity-below-global-minimum"])
        _, _, battles = self.run_shell('if run_regular_fallback; then exit 99; fi\n', "error_low")
        self.assertEqual(battles, ["AP-5"] * 3 + ["1-7"] * 3)


if __name__ == "__main__":
    unittest.main()
