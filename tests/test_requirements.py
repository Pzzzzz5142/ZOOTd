from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from maa_planner.annihilation import (
    game_week,
    operator_annihilation_confirmation,
    plan_annihilation,
    valid_annihilation_state,
)
from maa_planner.capability import (
    CapabilityKey,
    CapabilityLedger,
    extract_annihilation_progress,
    extract_successful_fight,
    extract_unstable_fight,
)
from maa_planner.cli import _read_log_suffix
from maa_planner.codex_advisor import CodexAdvisorError, run_codex_advisor
from maa_planner.codex_sdk import CodexSDKRequest
from maa_planner.codex_supervisor import (
    CodexSupervisorError,
    run_codex_supervisor,
)
from maa_planner.inventory import (
    InventoryParseError,
    InventorySnapshot,
    InventoryValidationError,
    extract_depot_snapshot,
    select_drone_mode,
)
from maa_planner.materials import load_blue_material_chains
from maa_planner.models import (
    Activity,
    ActivityStage,
    StageEfficiency,
    StockTarget,
)
from maa_planner.policy import select_farming_plan
from maa_planner.runtime_contracts import validate_runtime_contracts
from maa_planner.runtime_task import RuntimeTaskError, render_runtime_task
from maa_planner.sources import (
    HttpCache,
    SourceError,
    build_yituliu_efficiencies,
    parse_maa_activities,
)
from maa_planner.supervisor import (
    SupervisorError,
    finish_run,
    load_run_events,
    record_phase,
    start_run,
)
from maa_planner.util import (
    atomic_write_bytes,
    atomic_write_json,
    isoformat,
    sha256_bytes,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[1]
BLUE_MATERIAL_CHAINS = load_blue_material_chains(
    ROOT / "config/material-recipes.toml"
)
START = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
END = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


def make_activity(
    *,
    start: datetime = START,
    end: datetime = END,
    stages: tuple[ActivityStage, ...] = (
        ActivityStage("AT-6", "30013"),
        ActivityStage("AT-7", "30053"),
    ),
) -> Activity:
    return Activity(
        client="Official",
        key="SSReopen-AT",
        name="墟",
        tip="活动关卡开放中",
        start=start,
        end=end,
        minimum_required="v6.15.1",
        stages=stages,
        source_sha256="maa-sha",
    )


def efficiency(stage: str, item: str, expected_ap: float, overall: float) -> StageEfficiency:
    return StageEfficiency(
        stage_code=stage,
        stage_id=f"stage-id-{stage}",
        item_id=item,
        ap_cost=21,
        drop_rate=21 / expected_ap,
        expected_ap_per_item=expected_ap,
        overall_efficiency=overall,
        sample_size=1000,
        source_sha256="ytl-sha",
    )


def select_plan(**overrides: object):
    activity = overrides.pop("activity", make_activity())
    assert isinstance(activity, Activity)
    defaults: dict[str, object] = {
        "now": START + timedelta(hours=1),
        "activities": [activity],
        "efficiencies": {
            "AT-6": efficiency("AT-6", "30013", 20.0, 1.1),
            "AT-7": efficiency("AT-7", "30053", 27.0, 1.2),
        },
        "inventory": InventorySnapshot(
            items={"30013": 10, "30053": 50},
            captured_at=START,
            complete=True,
            client="Official",
            account="main",
        ),
        "targets": {
            "30013": StockTarget("30013", low=30, target=100),
            "30053": StockTarget("30053", low=30, target=100),
        },
        "blue_material_chains": BLUE_MATERIAL_CHAINS,
        "core_version": "v6.16.8",
        "verified_stages": set(),
        "quarantined_stages": set(),
        "navigation_stages": {stage.code for stage in activity.stages},
        "end_safety_margin": timedelta(minutes=15),
        "when_satisfied": "best_event",
        "configured_series": 0,
        "large_deficit_runs": 6,
        "medicine": 0,
        "medicine_expire_days": 2,
        "stone": 0,
    }
    defaults.update(overrides)
    return select_farming_plan(**defaults)  # type: ignore[arg-type]


def callback(label: str, body: dict[str, object]) -> str:
    return f"[INFO] Assistant::append_callback | {label} " + json.dumps(body)


def fight_drop(
    stage: str = "AT-6",
    *,
    stars: int = 3,
    taskid: int = 1,
    uuid: str = "fight-run",
) -> str:
    return callback(
        "SubTaskExtraInfo",
        {
            "what": "StageDrops",
            "details": {
                "stage": {"stageCode": stage, "stageId": f"id-{stage}"},
                "stars": stars,
                "drops": [],
            },
            "taskchain": "Fight",
            "taskid": taskid,
            "uuid": uuid,
        },
    )


def completed(*, taskid: int = 1, uuid: str = "fight-run") -> str:
    return callback(
        "TaskChainCompleted",
        {"taskchain": "Fight", "taskid": taskid, "uuid": uuid},
    )


def annihilation_drop(current: int, total: int, *, stars: int = 3) -> str:
    return callback(
        "SubTaskExtraInfo",
        {
            "what": "StageDrops",
            "details": {
                "stage": {"stageCode": "龙门市区", "stageId": "camp_03"},
                "stars": stars,
                "annihilation_weekly_process": [current, total],
            },
            "taskchain": "Fight",
            "taskid": 1,
            "uuid": "fight-run",
        },
    )


def weekly_state(current: int, total: int, status: str, *, stars: int = 3) -> dict[str, object]:
    return {
        "schema_version": 1,
        "client": "Official",
        "account": "main",
        "week_start_game_day": "2026-08-24",
        "week_start": "2026-08-23T20:00:00Z",
        "week_end": "2026-08-30T20:00:00Z",
        "status": status,
        "current": current,
        "total": total,
        "observed_at": "2026-08-24T00:00:00Z",
        "completed_at": "2026-08-24T00:00:00Z" if status == "complete" else None,
        "evidence": {
            "source": "maa-annihilation-log",
            "current": current,
            "total": total,
            "stars": stars,
            "complete": current == total,
            "completed_by": "TaskChainCompleted",
            "uuid": "client-run",
            "task_id": 1,
            "since_byte": 0,
            "log_device": None,
            "log_inode": None,
            "log_was_missing": True,
            "log_suffix_sha256": "a" * 64,
        },
    }


def jq_accepts(contract: Path, payload: dict[str, object]) -> bool:
    jq = shutil.which("jq")
    if jq is None:
        raise unittest.SkipTest("jq is a managed runtime dependency")
    completed_process = subprocess.run(
        [jq, "-e", "-f", str(contract), "-"],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed_process.returncode == 0


class _Fetched:
    def __init__(self, sha256: str) -> None:
        self.sha256 = sha256
        self.network_validated = True


class HighLevelRequirementTests(unittest.TestCase):
    def test_service_files_encode_the_unattended_safety_contract(self) -> None:
        validate_runtime_contracts(ROOT)

        daily = tomllib.loads((ROOT / "config/tasks/daily.toml").read_text())
        tasks = daily["tasks"]
        self.assertEqual(
            [task["type"] for task in tasks],
            ["Infrast", "Infrast", "Recruit", "Recruit", "Mall"],
        )
        shift_infrast, dorm_infrast = [
            task["params"] for task in tasks if task["type"] == "Infrast"
        ]
        self.assertEqual(
            shift_infrast["facility"],
            ["Mfg", "Trade", "Control", "Power", "Reception", "Office"],
        )
        self.assertTrue(shift_infrast["dorm_notstationed_enabled"])
        self.assertEqual(shift_infrast["drones"], "_NotUse")
        self.assertEqual(dorm_infrast["mode"], 10000)
        self.assertEqual(dorm_infrast["facility"], ["Dorm"])
        self.assertTrue(dorm_infrast["dorm_notstationed_enabled"])
        self.assertEqual(dorm_infrast["filename"], "protected-dorm.json")
        recruits = [task["params"] for task in tasks if task["type"] == "Recruit"]
        self.assertEqual(recruits[0]["confirm"], [5, 4, 3])
        self.assertEqual(recruits[0]["preserve_tags"], ["支援机械"])
        self.assertEqual(recruits[1]["confirm"], [5, 4, 3, 1])
        self.assertEqual(recruits[1]["first_tags"], ["支援机械"])
        self.assertEqual(recruits[1]["preserve_tags"], [])
        self.assertEqual(recruits[1]["recruitment_time"]["3"], 230)
        depot_task = tomllib.loads(
            (ROOT / "config/tasks/depot.toml").read_text()
        )
        self.assertTrue(depot_task["startup"])
        self.assertFalse(depot_task["closedown"])

        launcher = (ROOT / "scripts/run-daily.sh").read_text()
        self.assertEqual(launcher.count("\nrun_daily_routine\n"), 1)
        self.assertIn(
            'grep -Fc -- "EnterFacility Dorm #" "${log_file}"',
            launcher,
        )
        self.assertIn('grep -Fq -- "EnterFacility Training"', launcher)
        final_award = 'run_award_only "final service phase" "award-final"'
        self.assertEqual(launcher.count(final_award), 1)
        self.assertGreater(launcher.index(final_award), launcher.index("run_regular_fallback"))
        scheduled_workflow = launcher[
            launcher.index('info "daily-first mode: protecting the game day'):
        ]
        self.assertNotIn("pre_reset_slot", scheduled_workflow)
        self.assertIn('"${planner}" validate-service-readiness --value-only', launcher)
        self.assertNotIn('--profile "${MAA_HOST_PROFILE}" --dry-run', launcher)
        self.assertIn('[[ "${MAA_HOST_PROFILE}" == waydroid ]]', launcher)
        self.assertEqual(launcher.count(" supervisor-start --mode "), 1)
        self.assertEqual(launcher.count(" supervisor-finish --run-id "), 1)
        self.assertIn("llm_policy", (ROOT / "maa_planner/supervisor.py").read_text())
        farming_config = tomllib.loads((ROOT / "config/farming.toml").read_text())
        self.assertTrue(farming_config["supervisor"]["enabled"])
        self.assertTrue(farming_config["supervisor"]["required_on_exception"])
        self.assertTrue(farming_config["supervisor"]["recovery_enabled"])
        self.assertEqual(
            farming_config["supervisor"]["recovery_timeout_seconds"], 21600
        )
        self.assertIn("MAA_RECOVERY_ACTIVE", launcher)
        pre_reset_guard = launcher[
            launcher.index('if [[ "${dry_run}" != true && "${pre_reset_slot}" == true ]]'):
            launcher.index('\nmkdir -p -- "${project_root}/var/run"')
        ]
        self.assertIn('[[ "${MAA_RECOVERY_ACTIVE}" != true ]]', pre_reset_guard)
        self.assertIn("server_minute >= 145", pre_reset_guard)
        self.assertIn("02:25 cutoff", pre_reset_guard)
        self.assertIn(" supervisor-recover-run --run-id ", launcher)
        self.assertIn("--defer-to-recovery", launcher)
        recovery_scope = (ROOT / "docs/llm-recovery-scope.md").read_text()
        self.assertIn(
            "`Sandbox.full_access`", recovery_scope
        )
        self.assertIn("`ApprovalMode.deny_all`", recovery_scope)
        self.assertIn("is no Codex filesystem sandbox", recovery_scope)
        self.assertIn("正在获取更新", recovery_scope)
        self.assertIn("No saved proxy", recovery_scope)
        self.assertIn(
            "https://ak.hypergryph.com/downloads/android_lastest",
            recovery_scope,
        )
        self.assertIn("adb install --no-streaming -r", recovery_scope)
        self.assertIn("complete workflow through is the highest priority", recovery_scope)
        self.assertIn("./bin/zootd runtime-rollback", recovery_scope)
        self.assertIn("Operational repair and Git/PR protocol", recovery_scope)
        self.assertIn("choose a local repair branch", recovery_scope)
        self.assertNotIn("client-package-update", recovery_scope)

        proxy = tomllib.loads(
            (ROOT / "config/tasks/proxy-preflight.toml").read_text()
        )
        self.assertEqual(
            [task["type"] for task in proxy["tasks"]], ["Custom"] * 3
        )
        self.assertEqual(proxy["tasks"][1]["params"]["task_names"], ["1-7"])
        self.assertEqual(
            proxy["tasks"][2]["params"]["task_names"],
            ["StageQueue@CheckPrts"],
        )
        proxy_start = launcher.index("game_client_has_saved_proxy() {")
        proxy_end = launcher.index("\nrun_sanity_fight() {", proxy_start)
        proxy_function = launcher[proxy_start:proxy_end]
        self.assertEqual(proxy_function.count('run proxy-preflight'), 1)
        self.assertEqual(proxy_function.count('"${maa}"'), 1)
        self.assertIn('render_runtime_task proxy-preflight "${stage_code}"', proxy_function)
        self.assertIn('"${planner}" check-proxy', proxy_function)
        self.assertIn('--user-resource', proxy_function)
        self.assertNotIn("printf '%s\\n'", proxy_function)

        activity_start = launcher.index("run_planned_activity_candidates() {")
        activity_end = launcher.index(
            "\nrefresh_planner_sources_if_needed() {", activity_start
        )
        activity_function = launcher[activity_start:activity_end]
        self.assertIn("run_stage_with_proxy_retries", activity_function)
        self.assertNotIn("check-fight", activity_function)
        self.assertNotIn("quarantine-fight", activity_function)
        self.assertNotIn("pre_reset_slot", activity_function)

        fallback_start = launcher.index("run_regular_fallback() {")
        fallback_end = launcher.index("\nactivity_decision_is_safe() {", fallback_start)
        self.assertNotIn("pre_reset_slot", launcher[fallback_start:fallback_end])
        self.assertIn("farming_sanity_cleared", launcher[fallback_start:fallback_end])
        self.assertNotIn("any_fight_proof", launcher[fallback_start:fallback_end])

        final_fallback_start = launcher.rindex(
            'if [[ -z "${stage}" && "${farm_mode}" == auto &&\n'
            '      "${farming_contracts_ready}" == true &&'
        )
        final_fallback_end = launcher.index(
            '\nif [[ -n "${farming_evidence_log}"', final_fallback_start
        )
        final_fallback = launcher[final_fallback_start:final_fallback_end]
        self.assertIn('"${planner_helpers_ready}" == true', final_fallback)
        self.assertIn("farming_phase_result=degraded", final_fallback)
        self.assertNotIn("farming_phase_result=policy-resolved", final_fallback)

        source_code = (ROOT / "maa_planner/sources.py").read_text()
        source_config = (ROOT / "maa_planner/config.py").read_text()
        self.assertNotIn("fetch_official_bulletin_windows", source_code)
        self.assertNotIn("HTMLParser", source_code)
        self.assertNotIn("ak-webview.hypergryph.com", source_config)
        self.assertNotIn("sources.official", (ROOT / "config/farming.toml").read_text())

        self.assertNotIn("start_game_for_depot_with_retry", launcher)
        self.assertNotIn("startup Official", launcher)

        depot_start = launcher.index("scan_depot_inventory_once() {")
        depot_end = launcher.index("\nproxy_preflight_log_is_complete() {", depot_start)
        depot_function = launcher[depot_start:depot_end]
        self.assertEqual(
            depot_function.count('run depot --profile "${MAA_HOST_PROFILE}"'),
            1,
        )
        self.assertIn('if [[ "${depot_scan_attempted}" == true ]]', depot_function)
        self.assertIn("depot_scan_attempted=true", depot_function)
        self.assertIn('if [[ "${command_succeeded}" != true ]]', depot_function)
        self.assertIn("inventory_snapshot_ready=true", depot_function)
        self.assertEqual(depot_function.count('"${maa}"'), 1)

        daily_start = launcher.index("run_daily_routine() {")
        daily_end = launcher.index("\nannihilation_decision_is_safe() {", daily_start)
        daily_function = launcher[daily_start:daily_end]
        self.assertNotIn("daily_log_is_safe_to_retry", launcher)
        self.assertIn(
            "daily is reentrant; retrying the complete managed stage once",
            daily_function,
        )
        self.assertEqual(daily_function.count('run "${MAA_HOST_TASK}"'), 2)
        self.assertIn('render_runtime_task daily "${drone_mode}"', daily_function)
        self.assertNotIn("drone_input_index", launcher)
        self.assertNotIn("[tasks.params.drones]", (ROOT / "config/tasks/daily.toml").read_text())

        for task_name in ("proxy-preflight", "sanity-fight", "verify-fight"):
            task_text = (ROOT / f"config/tasks/{task_name}.toml").read_text()
            self.assertNotIn("[tasks.params.stage]", task_text)
            self.assertNotIn("alternatives =", task_text)
        self.assertEqual(launcher.count('render_runtime_task sanity-fight "${stage_code}"'), 1)
        self.assertEqual(launcher.count('render_runtime_task verify-fight "${stage_code}"'), 1)
        self.assertNotIn("printf '%s\\n' \"${stage_code}\" |", launcher)

        reuse_start = launcher.index("ensure_farming_inventory_snapshot() {")
        reuse_end = launcher.index("\nserver_minute_of_day_now() {", reuse_start)
        reuse_function = launcher[reuse_start:reuse_end]
        self.assertIn('if [[ "${inventory_snapshot_ready}" == true ]]', reuse_function)
        self.assertIn('if [[ "${depot_scan_attempted}" == true ]]', reuse_function)
        self.assertIn("no second scan", reuse_function)

        drone_start = launcher.index("prepare_daily_drone_policy() {")
        drone_end = launcher.index(
            "\nensure_farming_inventory_snapshot() {", drone_start
        )
        drone_function = launcher[drone_start:drone_end]
        self.assertNotIn("pre_reset_slot", drone_function)
        self.assertEqual(drone_function.count("scan_depot_inventory_once"), 1)
        self.assertIn("depot_scan_outcome=drone-target-unavailable", drone_function)
        self.assertNotIn("pre-reset-auxiliary-scan-skipped", launcher)
        self.assertNotIn("pre-reset-cutoff-after-fight-attempt", launcher)

        refresh_start = launcher.index("refresh_planner_sources_if_needed() {")
        refresh_end = launcher.index(
            "\nselect_daily_drone_policy_from_snapshot() {", refresh_start
        )
        refresh_function = launcher[refresh_start:refresh_end]
        self.assertEqual(refresh_function.count('"${planner}" sync-calendar'), 1)
        self.assertEqual(
            refresh_function.count('"${planner}" sync --skip-maa-hot-update'),
            1,
        )
        self.assertNotIn("pre_reset_slot", refresh_function)
        self.assertIn("planner_source_args=(--offline)", refresh_function)
        self.assertEqual(
            launcher.count('"${planner_source_args[@]}" --skip-maa-hot-update'),
            2,
        )

        updater = (ROOT / "scripts/update-maa-runtime.sh").read_text()
        wrapper = (ROOT / "bin/zootd-maa").read_text()
        host = (ROOT / "bin/zootd").read_text()
        self.assertIn("staging the latest stable MaaCore", updater)
        self.assertIn(
            '"${project_root}/config/infrast" "${root}/infrast"',
            updater,
        )
        self.assertIn("mv --exchange --no-copy --no-target-directory", updater)
        self.assertIn("MaaRuntime.previous", updater)
        self.assertIn("rollback_runtime", updater)
        self.assertIn('write_state promoting', updater)
        self.assertIn('"${planner}" runtime-fingerprint', updater)
        self.assertNotIn("printf '%s\\n' '1-7'", updater)
        self.assertIn("Direct live Core installation/update is disabled", wrapper)
        self.assertIn("Direct maa hot-update is disabled", wrapper)
        self.assertNotIn("--dry-run", host.split("usage() {", 1)[0])
        self.assertIn('"${planner}" validate-service-readiness', host)
        self.assertIn("runtime-rollback", host)

        sdk = (ROOT / "maa_planner/codex_sdk.py").read_text()
        recovery_adapter = (ROOT / "maa_planner/codex_recovery.py").read_text()
        self.assertIn("AsyncCodex", sdk)
        self.assertIn("output_schema=dict(request.output_schema)", sdk)
        self.assertIn("thread_resume", sdk)
        self.assertNotIn("subprocess", sdk)
        self.assertIn("ephemeral=False", recovery_adapter)
        self.assertFalse((ROOT / "maa_planner/codex_exec.py").exists())
        self.assertEqual(
            (ROOT / "requirements.txt").read_text().strip(),
            "openai-codex",
        )
        for adapter_name in (
            "zootd-codex-advisor",
            "zootd-codex-supervisor",
            "zootd-codex-recovery",
        ):
            adapter = (ROOT / "bin" / adapter_name).read_text()
            self.assertIn(".venv/bin/python", adapter)
            self.assertNotIn("codex exec", adapter)
        self.assertNotIn("MAA_CODEX_BIN", host)

        runtime_timer = (ROOT / "systemd/zootd-runtime-update.timer").read_text()
        pre_timer = (ROOT / "systemd/zootd-prereset.timer").read_text()
        post_timer = (ROOT / "systemd/zootd.timer").read_text()
        surface = (ROOT / "scripts/show-waydroid-scaled.sh").read_text()
        self.assertIn("OnCalendar=*-*-* 06:30:00 Asia/Shanghai", runtime_timer)
        self.assertIn("Persistent=false", runtime_timer)
        self.assertIn("OnCalendar=*-*-* 02:00:00 Asia/Shanghai", pre_timer)
        self.assertIn("OnCalendar=*-*-* 07:30:00 Asia/Shanghai", post_timer)
        self.assertIn("Persistent=true", post_timer)
        self.assertIn("--backend headless", surface)
        pre_service = (ROOT / "systemd/zootd-prereset.service").read_text()
        post_service = (ROOT / "systemd/zootd.service").read_text()
        self.assertIn("zootd run --pre-reset-slot", pre_service)
        self.assertIn("zootd run --post-reset-slot", post_service)
        farming = tomllib.loads((ROOT / "config/farming.toml").read_text())
        self.assertEqual(farming["annihilation"]["transaction_timeout_minutes"], 30)
        self.assertEqual(
            farming["inventory_equivalence"]["recipes_file"],
            "material-recipes.toml",
        )

    def test_runtime_task_values_are_rendered_without_prompts(self) -> None:
        cases = (
            ("daily", "Money", "drones"),
            ("proxy-preflight", "AT-6", "task_names"),
            ("sanity-fight", "AP-5", "stage"),
            ("verify-fight", "1-7", "stage"),
        )
        source_before = {
            task_name: (ROOT / f"config/tasks/{task_name}.toml").read_bytes()
            for task_name, _value, _parameter in cases
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            for task_name, value, parameter in cases:
                destination = output_root / f"{task_name}.toml"
                render_runtime_task(ROOT, task_name, value, destination)
                rendered = tomllib.loads(destination.read_text())
                index = 1 if task_name == "proxy-preflight" else 0
                expected = [value] if task_name == "proxy-preflight" else value
                self.assertEqual(rendered["tasks"][index]["params"][parameter], expected)
                self.assertNotIn("alternatives =", destination.read_text())
                self.assertNotIn("default_index =", destination.read_text())

            with self.assertRaises(RuntimeTaskError):
                render_runtime_task(
                    ROOT,
                    "sanity-fight",
                    '1-7"; stone = 999',
                    output_root / "unsafe.toml",
                )
            with self.assertRaises(RuntimeTaskError):
                render_runtime_task(
                    ROOT,
                    "daily",
                    "UseAllDrones",
                    output_root / "unsafe-daily.toml",
                )

        for task_name, expected in source_before.items():
            self.assertEqual(
                (ROOT / f"config/tasks/{task_name}.toml").read_bytes(), expected
            )


    def test_recovery_invocation_rejects_partial_or_wrong_slot_replays(self) -> None:
        launcher = ROOT / "scripts/run-daily.sh"
        base_env = {
            **os.environ,
            "MAA_RECOVERY_ACTIVE": "true",
            "MAA_RECOVERY_PARENT_RUN_ID": "20260905T000000.000000Z-12345678",
            "MAA_RECOVERY_ATTEMPT_ID": "a" * 32,
        }
        cases = (
            ("manual", ["--no-farm"], "complete automatic farming workflow"),
            ("manual", ["--dry-run"], "complete automatic farming workflow"),
            ("pre-reset", [], "does not match --pre-reset-slot"),
            ("post-reset", ["--pre-reset-slot"], "does not match --post-reset-slot"),
        )
        for slot, arguments, expected_error in cases:
            with self.subTest(slot=slot, arguments=arguments):
                completed = subprocess.run(
                    [str(launcher), *arguments],
                    cwd=ROOT,
                    env={**base_env, "MAA_RECOVERY_SLOT": slot},
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(expected_error, completed.stderr)

    def test_maa_activity_and_inventory_authorize_one_safe_fight(self) -> None:
        maa_payload = {
            "Official": {
                "sideStoryStage": {
                    "SSReopen-AT": {
                        "Activity": {
                            "TimeZone": 8,
                            "UtcStartTime": "2026/08/22 04:00:00",
                            "UtcExpireTime": "2026/09/01 03:59:59",
                            "StageName": "墟",
                            "Tip": "活动关卡开放中",
                        },
                        "MinimumRequired": "v6.15.1",
                        "Stages": [{"Value": "AT-6", "Drop": "30013"}],
                    }
                }
            }
        }
        activities = parse_maa_activities(maa_payload, "Official", "maa-sha")
        now = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
        efficiencies = build_yituliu_efficiencies(
            {
                "data": [
                    {
                        "stageCode": "AT-6",
                        "stageId": "act44side_06",
                        "apCost": 21,
                        "stageType": "ACT",
                    }
                ]
            },
            {
                "matrix": [
                    {
                        "stageId": "act44side_06",
                        "itemId": "30013",
                        "times": 300,
                        "quantity": 330,
                    }
                ]
            },
            {
                "data": [
                    {"itemId": "30013", "itemValue": 12.0},
                    {"itemId": "4001", "itemValue": 0.004},
                ]
            },
            activities[0].stages,
            minimum_samples=300,
            source_sha256="ytl-sha",
        )
        decision = select_farming_plan(
            now=now,
            activities=activities,
            efficiencies=efficiencies,
            inventory=InventorySnapshot(
                # White materials are excluded; 49 craftable blues earn 39 credit.
                items={"30013": 150, "30012": 245, "30011": 15},
                captured_at=now,
                complete=True,
            ),
            targets={"30013": StockTarget("30013", low=30, target=200)},
            blue_material_chains=BLUE_MATERIAL_CHAINS,
            core_version="v6.16.8",
            verified_stages=set(),
            quarantined_stages=set(),
            navigation_stages={"AT-6"},
            end_safety_margin=timedelta(minutes=15),
            when_satisfied="best_event",
            configured_series=0,
            large_deficit_runs=6,
            medicine=0,
            medicine_expire_days=2,
            stone=0,
        )
        self.assertEqual(decision.decision, "FIGHT")
        self.assertEqual(decision.selected_stage, "AT-6")
        self.assertEqual(decision.medicine, 0)
        self.assertEqual(decision.medicine_expire_days, 2)
        self.assertEqual(decision.stone, 0)
        self.assertEqual(decision.candidates[0].direct_inventory, 150)
        self.assertEqual(decision.candidates[0].craftable_equivalent, 39)
        self.assertEqual(decision.candidates[0].inventory, 189)
        self.assertEqual(decision.candidates[0].deficit, 11)
        self.assertTrue(jq_accepts(ROOT / "config/fight-decision.jq", decision.as_dict()))
        self.assertEqual(decision.series, 0)
        self.assertEqual(decision.evidence["execution_candidates"][0]["times_per_transaction"], 2147483647)
        batched = decision.as_dict()
        batched["evidence"]["execution_candidates"][0]["times_per_transaction"] = 3
        self.assertFalse(jq_accepts(ROOT / "config/fight-decision.jq", batched))

    def test_discounted_green_stock_prioritizes_sr8_and_ignores_white(self) -> None:
        activity = make_activity(stages=(
            ActivityStage("SR-8", "30043"),
            ActivityStage("SR-6", "31073"),
        ))
        for white in (None, 0, 233, 999999):
            with self.subTest(white=white):
                items = {"30043": 36, "30042": 602, "31073": 237}
                if white is not None:
                    items["30041"] = white
                decision = select_plan(
                    activity=activity,
                    inventory=InventorySnapshot(
                        items=items, captured_at=START, complete=True,
                    ),
                    targets={item: StockTarget(item, low=0, target=200)
                             for item in ("30043", "31073")},
                    efficiencies={
                        "SR-8": efficiency("SR-8", "30043", 27.0, 1.1145),
                        "SR-6": efficiency("SR-6", "31073", 32.4, 1.1316),
                    },
                )
                self.assertEqual(decision.selected_stage, "SR-8")
                iron = next(c for c in decision.candidates if c.stage_code == "SR-8")
                self.assertEqual(iron.inventory, 156)
                self.assertEqual(iron.deficit, 44)
                self.assertEqual(iron.craftable_equivalent, 120)
                self.assertEqual(iron.inventory_breakdown["craftable_t3"], 150)
                self.assertEqual(iron.inventory_breakdown["credited_craftable_t3"], 120)
                self.assertEqual(iron.inventory_breakdown["t2_from_t1"], 0)
                self.assertEqual(iron.inventory_breakdown["craftable_credit_percent"], 80)
                self.assertFalse(iron.inventory_breakdown["white_materials_included"])
                self.assertTrue(jq_accepts(ROOT / "config/fight-decision.jq", decision.as_dict()))

    def test_planner_fails_closed_or_uses_next_candidate_for_unsafe_inputs(self) -> None:
        activity = make_activity()
        self.assertEqual(
            select_plan(now=activity.end - timedelta(minutes=10)).decision,
            "NOOP",
        )
        self.assertEqual(select_plan(navigation_stages=set()).decision, "NOOP")
        self.assertEqual(
            select_plan(
                inventory=InventorySnapshot(
                    # Lower tiers cannot conceal an unobserved blue target.
                    items={"30053": 50, "30012": 999, "30011": 999},
                    captured_at=START,
                    complete=True,
                )
            ).decision,
            "NOOP",
        )

        quarantined = select_plan(
            quarantined_stages={(activity.instance_id, "AT-6")}
        )
        self.assertEqual(quarantined.decision, "FIGHT")
        self.assertEqual(quarantined.selected_stage, "AT-7")

        valid = select_plan().as_dict()
        unsafe = copy.deepcopy(valid)
        unsafe["evidence"]["execution_candidates"][0]["medicine"] = 1
        self.assertFalse(jq_accepts(ROOT / "config/fight-decision.jq", unsafe))
        agent_authorized = copy.deepcopy(valid)
        agent_authorized["evidence"]["execution_candidates"][0][
            "authorization"
        ] = "agent-approved"
        self.assertFalse(
            jq_accepts(ROOT / "config/fight-decision.jq", agent_authorized)
        )

    def test_annihilation_can_span_runs_without_gating_material_farming(self) -> None:
        now = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
        base = {
            "now": now,
            "activities": [],
            "client": "Official",
            "account": "main",
        }
        first = plan_annihilation(**base)
        partial = plan_annihilation(
            **base, state=weekly_state(730, 1800, "progress")
        )
        complete = plan_annihilation(
            **base, state=weekly_state(1800, 1800, "complete")
        )
        operator_state = operator_annihilation_confirmation(
            now=now,
            client="Official",
            account="main",
            expected_week_start_game_day="2026-08-24",
            reason="operator verified the in-game weekly cap",
            confirmation_id="b" * 32,
        )
        operator_complete = plan_annihilation(**base, state=operator_state)
        unstable = plan_annihilation(
            **base, state=weekly_state(365, 1800, "unstable", stars=2)
        )
        pre_reset_catch_up = plan_annihilation(
            now=datetime(2026, 8, 30, 18, 0, tzinfo=UTC),
            activities=[],
            client="Official",
            account="main",
        )
        self.assertEqual(first["decision"], "RUN")
        self.assertEqual(partial["decision"], "RUN")
        self.assertEqual(complete["decision"], "COMPLETE")
        self.assertEqual(operator_complete["decision"], "COMPLETE")
        self.assertEqual(
            operator_complete["reason"], "OPERATOR_WEEKLY_CAP_CONFIRMED"
        )
        self.assertEqual(unstable["decision"], "BLOCKED")
        self.assertEqual(pre_reset_catch_up["decision"], "RUN")
        self.assertEqual(
            pre_reset_catch_up["reason"], "WEEK_DEADLINE_CATCH_UP"
        )
        self.assertEqual(
            pre_reset_catch_up["execute_before"], "2026-08-30T20:00:00Z"
        )
        self.assertTrue(jq_accepts(ROOT / "config/annihilation-decision.jq", first))
        self.assertTrue(
            jq_accepts(
                ROOT / "config/annihilation-decision.jq", operator_complete
            )
        )
        self.assertIsNotNone(
            valid_annihilation_state(
                operator_state,
                client="Official",
                account="main",
                week=game_week(now),
            )
        )

        tampered_operator_state = copy.deepcopy(operator_state)
        tampered_operator_state["evidence"]["source"] = "maa-annihilation-log"
        self.assertIsNone(
            valid_annihilation_state(
                tampered_operator_state,
                client="Official",
                account="main",
                week=game_week(now),
            )
        )

        unsafe = copy.deepcopy(first)
        unsafe["times_per_transaction"] = 2
        self.assertFalse(
            jq_accepts(ROOT / "config/annihilation-decision.jq", unsafe)
        )
        launcher = (ROOT / "scripts/run-daily.sh").read_text()
        start = launcher.index("run_weekly_annihilation_if_due() {")
        end = launcher.index("\nrun_stage_with_proxy_retries() {", start)
        function = launcher[start:end]
        self.assertNotIn("return 1", function)
        self.assertTrue(function.rstrip().endswith("return 0\n}"))

    def test_only_fresh_completed_client_evidence_changes_capability(self) -> None:
        old_proof = "\n".join([fight_drop(), completed()]) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asst.log"
            path.write_text(old_proof, encoding="utf-8")
            metadata = path.stat()
            self.assertIsNone(
                extract_successful_fight(
                    _read_log_suffix(path, metadata.st_size), "AT-6"
                )
            )

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    fight_drop(taskid=2, uuid="fresh")
                    + "\n"
                    + completed(taskid=2, uuid="fresh")
                    + "\n"
                )
            suffix = _read_log_suffix(
                path,
                metadata.st_size,
                expected_device=metadata.st_dev,
                expected_inode=metadata.st_ino,
            )
            proof = extract_successful_fight(suffix, "AT-6")
            self.assertIsNotNone(proof)

            replacement = Path(directory) / "replacement.log"
            replacement.write_text(old_proof, encoding="utf-8")
            replacement.replace(path)
            with self.assertRaisesRegex(ValueError, "replaced or rotated"):
                _read_log_suffix(
                    path,
                    metadata.st_size,
                    expected_device=metadata.st_dev,
                    expected_inode=metadata.st_ino,
                )

        ledger = CapabilityLedger()
        key = CapabilityKey("Official", "main", "activity-instance", "AT-6")
        self.assertTrue(
            ledger.mark_verified_from_log(
                key, suffix, observed_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
            )
        )
        unstable = extract_unstable_fight(fight_drop(stars=2), "AT-6")
        self.assertIsNotNone(unstable)

        partial = extract_annihilation_progress(
            "\n".join([annihilation_drop(730, 1800), completed()])
        )
        full = extract_annihilation_progress(
            "\n".join([annihilation_drop(1800, 1800), completed()])
        )
        self.assertIsNotNone(partial)
        self.assertIsNotNone(full)
        assert partial is not None and full is not None
        self.assertFalse(partial.complete)
        self.assertTrue(full.complete)

    def test_depot_and_http_cache_never_invent_missing_or_tampered_input(self) -> None:
        depot = callback(
            "SubTaskExtraInfo",
            {
                "what": "DepotInfo",
                "details": {
                    "done": True,
                    "data": json.dumps({"3003": 149, "30013": 12}),
                },
                "taskchain": "Depot",
                "taskid": 1,
            },
        )
        snapshot = extract_depot_snapshot(depot)
        self.assertEqual(select_drone_mode(snapshot), ("PureGold", 149))
        empty_depot = callback(
            "SubTaskExtraInfo",
            {
                "what": "DepotInfo",
                "details": {"done": True, "data": "{}"},
                "taskchain": "Depot",
                "taskid": 1,
            },
        )
        with self.assertRaises(InventoryParseError):
            extract_depot_snapshot(empty_depot)
        self.assertEqual(
            select_drone_mode(
                InventorySnapshot(
                    items={"3003": 150}, captured_at=utc_now(), complete=True
                )
            ),
            ("Money", 150),
        )
        with self.assertRaises(InventoryValidationError):
            select_drone_mode(
                InventorySnapshot(
                    items={"4001": 1}, captured_at=utc_now(), complete=True
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = "https://example.com/source"
            request = b'{"profile":1}'
            body = b'{"ok":true}'
            atomic_write_bytes(root / "record.body", body)
            atomic_write_json(
                root / "record.meta.json",
                {
                    "schema_version": 1,
                    "url": url,
                    "final_url": url,
                    "fetched_at": isoformat(utc_now()),
                    "etag": None,
                    "last_modified": None,
                    "content_type": "application/json",
                    "sha256": sha256_bytes(body),
                    "size": len(body),
                    "method": "POST",
                    "request_sha256": sha256_bytes(request),
                },
            )
            cache = HttpCache(
                root, allowed_hosts={"example.com"}, network_enabled=False
            )
            fetched = cache.fetch(
                "record",
                url,
                accept="application/json",
                max_stale=timedelta(hours=1),
                method="POST",
                request_body=request,
            )
            self.assertEqual(fetched.body, body)
            with self.assertRaises(SourceError):
                cache.fetch(
                    "record",
                    url,
                    accept="application/json",
                    max_stale=timedelta(hours=1),
                    method="POST",
                    request_body=b'{"profile":2}',
                )
            atomic_write_bytes(root / "record.body", b"tampered")
            with self.assertRaisesRegex(SourceError, "checksum mismatch"):
                cache.fetch(
                    "record",
                    url,
                    accept="application/json",
                    max_stale=timedelta(hours=1),
                    method="POST",
                    request_body=request,
                )

    def test_llm_advisor_and_diagnostic_classifier_remain_read_only(self) -> None:
        evidence = json.dumps(
            {
                "schema_version": 1,
                "decision": "NOOP",
                "reason": "NO_ELIGIBLE_STAGE",
                "candidates": [
                    {
                        "stage_code": "AT-6",
                        "rejected_by": ["YITULIU_STAGE_UNAVAILABLE"],
                    }
                ],
                "evidence": {
                    "rejection_reasons": ["YITULIU_STAGE_UNAVAILABLE"]
                },
            }
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            seen: dict[str, object] = {}

            def valid_runner(request: CodexSDKRequest) -> str:
                seen["request"] = request
                output = {
                    "schema_version": 1,
                    "classification": "source_schema",
                    "confidence": 0.8,
                    "summary": "Upstream schema needs review.",
                    "mappings": [],
                    "evidence_refs": ["evidence.rejection_reasons"],
                }
                return json.dumps(output)

            result = json.loads(
                run_codex_advisor(
                    evidence,
                    environ={
                        "HOME": directory,
                        "PATH": directory,
                        "DISPLAY": ":0",
                        "MAA_SECRET": "must-not-leak",
                    },
                    sdk_runner=valid_runner,
                )
            )
            request = seen["request"]
            self.assertIsInstance(request, CodexSDKRequest)
            assert isinstance(request, CodexSDKRequest)
            self.assertTrue(request.ephemeral)
            self.assertEqual(request.approval_mode, "deny-all")
            self.assertEqual(request.sandbox, "read-only")
            self.assertIn("features.shell_tool=false", request.config_overrides)
            self.assertIn("tools.web_search=false", request.config_overrides)
            child_env = request.environment
            assert isinstance(child_env, dict)
            self.assertNotIn("DISPLAY", child_env)
            self.assertNotIn("MAA_SECRET", child_env)
            self.assertNotIn("authorization", result)

            fight = json.loads(evidence)
            fight["decision"] = "FIGHT"
            with self.assertRaises(CodexAdvisorError):
                run_codex_advisor(
                    json.dumps(fight).encode(),
                    environ={"HOME": directory},
                    sdk_runner=valid_runner,
                )

            def invented_runner(_request: CodexSDKRequest) -> str:
                output = {
                    "schema_version": 1,
                    "classification": "stage_mapping",
                    "confidence": 1,
                    "summary": "unsafe",
                    "mappings": [
                        {
                            "stage_code": "EVIL-9",
                            "yituliu_stage_id": "evil_09",
                            "reason": "invented",
                        }
                    ],
                    "evidence_refs": [],
                }
                return json.dumps(output)

            with self.assertRaisesRegex(
                CodexAdvisorError, "outside the deterministic"
            ):
                run_codex_advisor(
                    evidence,
                    environ={"HOME": directory},
                    sdk_runner=invented_runner,
                )

            supervisor_evidence = {
                "schema_version": 1,
                "kind": "exception-diagnosis",
                "run_id": "20260827T000000.000000Z-12345678",
                "process_status": 1,
                "expected_phases": ["daily", "cleanup"],
                "missing_phases": [],
                "unacceptable_phases": ["daily"],
                "start": {"mode": "full", "llm_policy": "exception-only"},
                "phase_results": [
                    {
                        "phase": "daily",
                        "result": "failed",
                        "outcome": "completion-proof-missing",
                    },
                    {
                        "phase": "cleanup",
                        "result": "succeeded",
                        "outcome": "resources-released",
                    },
                ],
                "chain_head_sha256": "0" * 64,
                "hard_safety_rules": [
                    "all managed stages, including daily, are reentrant"
                ],
            }

            def supervisor_runner(request: CodexSDKRequest) -> str:
                seen["supervisor_request"] = request
                output = {
                    "schema_version": 1,
                    "run_id": supervisor_evidence["run_id"],
                    "classification": "evidence",
                    "summary": "Daily lacks deterministic completion proof.",
                    "affected_phases": ["daily"],
                    "evidence_refs": ["phase_results[0]"],
                    "recommended_actions": ["Inspect the archived daily log."],
                    "safe_to_retry_whole_run": True,
                }
                return json.dumps(output)

            supervisor_result = json.loads(
                run_codex_supervisor(
                    json.dumps(supervisor_evidence).encode(),
                    environ={
                        "HOME": directory,
                        "PATH": directory,
                        "DISPLAY": ":0",
                        "MAA_SECRET": "must-not-leak",
                    },
                    sdk_runner=supervisor_runner,
                )
            )
            supervisor_request = seen["supervisor_request"]
            self.assertIsInstance(supervisor_request, CodexSDKRequest)
            assert isinstance(supervisor_request, CodexSDKRequest)
            self.assertEqual(supervisor_request.sandbox, "read-only")
            supervisor_env = supervisor_request.environment
            assert isinstance(supervisor_env, dict)
            self.assertNotIn("DISPLAY", supervisor_env)
            self.assertNotIn("MAA_SECRET", supervisor_env)
            self.assertTrue(supervisor_result["safe_to_retry_whole_run"])
            self.assertNotIn("authorization", supervisor_result)

            successful_evidence = copy.deepcopy(supervisor_evidence)
            successful_evidence["process_status"] = 0
            successful_evidence["unacceptable_phases"] = []
            with self.assertRaisesRegex(
                CodexSupervisorError, "refuses successful runs"
            ):
                run_codex_supervisor(
                    json.dumps(successful_evidence).encode(),
                    environ={"HOME": directory},
                    sdk_runner=supervisor_runner,
                )

            repo = Path(directory) / "history-repo"
            repo.mkdir()
            subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
            subprocess.run(
                ("git", "config", "user.name", "Test"), cwd=repo, check=True
            )
            subprocess.run(
                ("git", "config", "user.email", "test@example.invalid"),
                cwd=repo,
                check=True,
            )
            (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
            (repo / ".gitignore").write_text("/var/\n", encoding="utf-8")
            subprocess.run(
                ("git", "add", "baseline.txt", ".gitignore"), cwd=repo, check=True
            )
            subprocess.run(
                ("git", "commit", "-qm", "baseline"), cwd=repo, check=True
            )

            clean_run = start_run(repo, "dry-run", now=START)
            record_phase(
                repo,
                clean_run,
                phase="runtime-readiness",
                result="succeeded",
                outcome="receipt-accepted",
            )
            record_phase(
                repo,
                clean_run,
                phase="cleanup",
                result="succeeded",
                outcome="resources-released",
            )
            clean_finish = finish_run(
                repo,
                clean_run,
                process_status=0,
                supervisor_enabled=True,
                supervisor_required=True,
                command=("/definitely/not/invoked",),
                timeout_seconds=1,
            )
            self.assertEqual(clean_finish.status, "success")
            self.assertFalse(clean_finish.llm_invoked)
            self.assertEqual(len(load_run_events(repo, clean_run)), 4)

            adapter = Path(directory) / "exception-adapter.py"
            adapter.write_text(
                """import json, sys
evidence = json.load(sys.stdin)
print(json.dumps({
    'schema_version': 1,
    'run_id': evidence['run_id'],
    'classification': 'evidence',
    'summary': 'The runtime receipt was rejected.',
    'affected_phases': ['runtime-readiness'],
    'evidence_refs': ['unacceptable_phases'],
    'recommended_actions': ['Run the transactional runtime updater.'],
    'safe_to_retry_whole_run': False,
}))
""",
                encoding="utf-8",
            )
            failed_run = start_run(repo, "dry-run", now=START + timedelta(seconds=1))
            record_phase(
                repo,
                failed_run,
                phase="runtime-readiness",
                result="failed",
                outcome="receipt-rejected",
            )
            record_phase(
                repo,
                failed_run,
                phase="cleanup",
                result="succeeded",
                outcome="resources-released",
            )
            failed_finish = finish_run(
                repo,
                failed_run,
                process_status=1,
                supervisor_enabled=True,
                supervisor_required=True,
                command=(sys.executable, str(adapter)),
                timeout_seconds=5,
            )
            self.assertEqual(failed_finish.status, "failed")
            self.assertTrue(failed_finish.llm_invoked)
            self.assertIsNotNone(failed_finish.diagnosis)

            first_phase = repo / (
                f"var/state/supervisor/runs/{failed_run}/events/0001-phase-finished.json"
            )
            first_phase.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(SupervisorError, "hash chain"):
                load_run_events(repo, failed_run)


if __name__ == "__main__":
    unittest.main()
