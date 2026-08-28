from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from maa_planner.codex_recovery import CodexRecoveryError, run_codex_recovery
from maa_planner.recovery import recover_failed_run, validate_recovery_report
from maa_planner.supervisor import (
    PHASES,
    describe_evidence_files,
    finish_run,
    load_run_events,
    record_phase,
    start_run,
)
from maa_planner.util import sha256_bytes


ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


class RecoveryTests(unittest.TestCase):
    def _repo(self, directory: str) -> Path:
        repo = Path(directory) / "recovery-repo"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs/llm-recovery-scope.md").write_bytes(
            (ROOT / "docs/llm-recovery-scope.md").read_bytes()
        )
        (repo / ".gitignore").write_text("/var/\n", encoding="utf-8")
        subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
        subprocess.run(("git", "config", "user.name", "Test"), cwd=repo, check=True)
        subprocess.run(
            ("git", "config", "user.email", "test@example.invalid"),
            cwd=repo,
            check=True,
        )
        subprocess.run(("git", "add", "."), cwd=repo, check=True)
        subprocess.run(("git", "commit", "-qm", "baseline"), cwd=repo, check=True)
        return repo

    def _failed_full_run(self, repo: Path, *, now: datetime = START) -> str:
        run_id = start_run(repo, "full", now=now)
        record_phase(
            repo,
            run_id,
            phase="runtime-readiness",
            result="succeeded",
            outcome="receipt-accepted",
            now=now + timedelta(seconds=1),
        )
        record_phase(
            repo,
            run_id,
            phase="device-readiness",
            result="succeeded",
            outcome="device-ready",
            now=now + timedelta(seconds=2),
        )
        record_phase(
            repo,
            run_id,
            phase="depot",
            result="failed",
            outcome="game-update-stalled",
            now=now + timedelta(seconds=3),
        )
        record_phase(
            repo,
            run_id,
            phase="cleanup",
            result="succeeded",
            outcome="resources-released",
            now=now + timedelta(seconds=4),
        )
        outcome = finish_run(
            repo,
            run_id,
            process_status=1,
            supervisor_enabled=True,
            supervisor_required=True,
            command=(),
            timeout_seconds=1,
            diagnose=False,
            now=now + timedelta(seconds=5),
        )
        final = json.loads(outcome.event_path.read_text(encoding="utf-8"))
        self.assertEqual(final["payload"]["llm"]["status"], "recovery-pending")
        return run_id

    def test_codex_recovery_keeps_shell_images_and_session_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_codex = Path(directory) / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            failed_run_id = "20260828T000000.000000Z-12345678"
            scope = (ROOT / "docs/llm-recovery-scope.md").read_bytes()
            evidence = {
                "schema_version": 1,
                "kind": "operational-recovery",
                "failed_run_id": failed_run_id,
                "screenshot_paths": [],
                "scope": {
                    "path": "docs/llm-recovery-scope.md",
                    "sha256": sha256_bytes(scope),
                    "version": 1,
                },
            }
            seen: dict[str, object] = {}

            def runner(
                argv: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                seen["argv"] = argv
                seen["env"] = kwargs["env"]
                seen["prompt"] = kwargs["input"]
                schema_path = Path(argv[argv.index("--output-schema") + 1])
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                self.assertIn("recovered", schema["properties"]["status"]["enum"])
                output = {
                    "schema_version": 1,
                    "failed_run_id": failed_run_id,
                    "status": "scope-blocked",
                    "classification": "scope",
                    "summary": "Interactive login is required.",
                    "actions_taken": ["Inspected the login screen."],
                    "verification": {
                        "command": "./bin/maa-host run",
                        "exit_status": None,
                        "successful_run_id": None,
                        "audit_path": None,
                    },
                    "scope_blocker": "manual-login",
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(output).encode(), b""
                )

            output = json.loads(
                run_codex_recovery(
                    json.dumps(evidence).encode(),
                    environ={
                        "HOME": directory,
                        "PATH": directory,
                        "DISPLAY": ":0",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus",
                        "OPENAI_API_KEY": "needed-by-codex-cli",
                        "MAA_SECRET": "must-not-leak",
                        "MAA_CODEX_BIN": str(fake_codex),
                        "MAA_CODEX_RECOVERY_TIMEOUT_SECONDS": "120",
                    },
                    runner=runner,
                )
            )
            argv = seen["argv"]
            self.assertIsInstance(argv, list)
            assert isinstance(argv, list)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "danger-full-access")
            disabled = {
                argv[index + 1]
                for index, value in enumerate(argv[:-1])
                if value == "--disable"
            }
            self.assertNotIn("shell_tool", disabled)
            self.assertNotIn("unified_exec", disabled)
            self.assertNotIn("view_image", disabled)
            self.assertIn('shell_environment_policy.inherit="all"', argv)
            child_env = seen["env"]
            self.assertIsInstance(child_env, dict)
            assert isinstance(child_env, dict)
            self.assertEqual(child_env["MAA_RECOVERY_ACTIVE"], "true")
            self.assertEqual(child_env["MAA_RECOVERY_PARENT_RUN_ID"], failed_run_id)
            self.assertEqual(child_env["DISPLAY"], ":0")
            self.assertIn("OPENAI_API_KEY", child_env)
            self.assertNotIn("MAA_SECRET", child_env)
            prompt = seen["prompt"]
            self.assertIsInstance(prompt, bytes)
            assert isinstance(prompt, bytes)
            self.assertIn(b"danger-full-access", prompt)
            self.assertEqual(output["scope_blocker"], "manual-login")

            evidence["scope"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(CodexRecoveryError, "scope identity"):
                run_codex_recovery(
                    json.dumps(evidence).encode(),
                    environ={
                        "HOME": directory,
                        "MAA_CODEX_BIN": str(fake_codex),
                    },
                    runner=runner,
                )

    def test_recovery_is_green_only_after_new_complete_full_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            failed_run_id = self._failed_full_run(repo)

            def adapter_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                successful_run_id = start_run(
                    repo, "full", now=START + timedelta(minutes=1)
                )
                for index, phase in enumerate(PHASES, start=1):
                    record_phase(
                        repo,
                        successful_run_id,
                        phase=phase,
                        result="succeeded",
                        outcome=f"{phase}-complete",
                        now=START + timedelta(minutes=1, seconds=index),
                    )
                successful = finish_run(
                    repo,
                    successful_run_id,
                    process_status=0,
                    supervisor_enabled=True,
                    supervisor_required=True,
                    command=(),
                    timeout_seconds=1,
                    now=START + timedelta(minutes=2),
                )
                output = {
                    "schema_version": 1,
                    "failed_run_id": failed_run_id,
                    "status": "recovered",
                    "classification": "network",
                    "summary": "Waydroid DNS recovered and the full retry passed.",
                    "actions_taken": [
                        "Compared host and Android DNS.",
                        "Waited through the game update dialog.",
                        "Ran the supplied full retry.",
                    ],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": 0,
                        "successful_run_id": successful_run_id,
                        "audit_path": str(successful.event_path.relative_to(repo)),
                    },
                    "scope_blocker": "none",
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(output).encode(), b""
                )

            outcome = recover_failed_run(
                repo,
                failed_run_id,
                slot="manual",
                command=("fake-recovery",),
                timeout_seconds=60,
                runner=adapter_runner,
            )
            self.assertEqual(outcome.status, "recovered")
            self.assertNotEqual(outcome.successful_run_id, failed_run_id)
            events = load_run_events(repo, failed_run_id)
            self.assertEqual(
                [event["event_type"] for event in events[-2:]],
                ["recovery-started", "recovery-finished"],
            )
            final = events[-1]["payload"]
            self.assertEqual(final["status"], "recovered")
            self.assertEqual(final["controller_verification"]["status"], "success")

    def test_scope_blocker_is_audited_but_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            failed_run_id = self._failed_full_run(
                repo, now=START + timedelta(hours=1)
            )

            def blocked_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                output = {
                    "schema_version": 1,
                    "failed_run_id": failed_run_id,
                    "status": "scope-blocked",
                    "classification": "scope",
                    "summary": "The account requires an interactive login.",
                    "actions_taken": ["Confirmed the login prompt with UI evidence."],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": None,
                        "successful_run_id": None,
                        "audit_path": None,
                    },
                    "scope_blocker": "manual-login",
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(output).encode(), b""
                )

            outcome = recover_failed_run(
                repo,
                failed_run_id,
                slot="post-reset",
                command=("fake-recovery",),
                timeout_seconds=60,
                runner=blocked_runner,
            )
            self.assertEqual(outcome.status, "scope-blocked")
            self.assertEqual(outcome.scope_blocker, "manual-login")
            self.assertIsNone(outcome.successful_run_id)
            final = load_run_events(repo, failed_run_id)[-1]["payload"]
            self.assertEqual(final["status"], "scope-blocked")
            self.assertEqual(
                final["controller_verification"]["status"], "scope-accepted"
            )

    def test_stateful_daily_parent_is_blocked_before_agent_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            now = START + timedelta(hours=2)
            failed_run_id = start_run(repo, "full", now=now)
            for index, phase in enumerate(
                ("runtime-readiness", "device-readiness", "depot"), start=1
            ):
                record_phase(
                    repo,
                    failed_run_id,
                    phase=phase,
                    result="succeeded",
                    outcome=f"{phase}-complete",
                    now=now + timedelta(seconds=index),
                )
            log_path = repo / "var/state/host/stateful-daily.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("Infrast Start\nconnection lost\n", encoding="utf-8")
            record_phase(
                repo,
                failed_run_id,
                phase="daily",
                result="failed",
                outcome="completion-proof-missing",
                evidence_files=describe_evidence_files(repo, [str(log_path)]),
                now=now + timedelta(seconds=4),
            )
            record_phase(
                repo,
                failed_run_id,
                phase="cleanup",
                result="succeeded",
                outcome="resources-released",
                now=now + timedelta(seconds=5),
            )
            finish_run(
                repo,
                failed_run_id,
                process_status=1,
                supervisor_enabled=True,
                supervisor_required=True,
                command=(),
                timeout_seconds=1,
                diagnose=False,
                now=now + timedelta(seconds=6),
            )
            invoked = False

            def must_not_run(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                nonlocal invoked
                invoked = True
                return subprocess.CompletedProcess(argv, 1, b"", b"")

            outcome = recover_failed_run(
                repo,
                failed_run_id,
                slot="post-reset",
                command=("must-not-run",),
                timeout_seconds=60,
                runner=must_not_run,
            )
            self.assertFalse(invoked)
            self.assertEqual(outcome.status, "scope-blocked")
            self.assertEqual(outcome.scope_blocker, "unsafe-stateful-replay")
            final = load_run_events(repo, failed_run_id)[-1]["payload"]
            self.assertFalse(final["agent"]["invoked"])
            self.assertEqual(
                final["controller_verification"]["scope_blocker"],
                "unsafe-stateful-replay",
            )

    def test_recovered_report_requires_controller_fields(self) -> None:
        failed_run_id = "20260828T000000.000000Z-12345678"
        with self.assertRaisesRegex(Exception, "complete success evidence"):
            validate_recovery_report(
                {
                    "schema_version": 1,
                    "failed_run_id": failed_run_id,
                    "status": "recovered",
                    "classification": "network",
                    "summary": "Looks fixed.",
                    "actions_taken": ["Inspected the failed audit."],
                    "verification": {
                        "command": "",
                        "exit_status": None,
                        "successful_run_id": None,
                        "audit_path": None,
                    },
                    "scope_blocker": "none",
                },
                failed_run_id=failed_run_id,
            )


if __name__ == "__main__":
    unittest.main()
