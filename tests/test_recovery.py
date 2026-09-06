from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from maa_planner.codex_recovery import CodexRecoveryError, run_codex_recovery
from maa_planner.codex_sdk import CodexSDKRequest, run_sdk_request
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
    @staticmethod
    def _no_code_repair(base_head: str) -> dict[str, object]:
        return {
            "status": "not-needed",
            "base_head": base_head,
            "branch": None,
            "commit": None,
            "applied_to_runtime": False,
            "pull_request_url": None,
            "pull_request_error": None,
            "validation_commands": [],
        }

    def _repo(self, directory: str) -> Path:
        repo = Path(directory) / "recovery-repo"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs/llm-recovery-scope.md").write_bytes(
            (ROOT / "docs/llm-recovery-scope.md").read_bytes()
        )
        (repo / "config/tasks").mkdir(parents=True)
        (repo / "config/tasks/proxy-preflight.toml").write_text(
            "client_type = \"Official\"\n", encoding="utf-8"
        )
        (repo / "scripts").mkdir()
        (repo / "scripts/run-daily.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
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

    def _successful_full_run(
        self,
        repo: Path,
        *,
        now: datetime,
        recovery_evidence: dict[str, object] | None = None,
    ) -> str:
        if recovery_evidence is None:
            run_id = start_run(repo, "full", now=now)
        else:
            attempt = recovery_evidence["recovery_attempt"]
            assert isinstance(attempt, dict)
            with patch.dict(
                os.environ,
                {
                    "MAA_RECOVERY_ACTIVE": "true",
                    "MAA_RECOVERY_PARENT_RUN_ID": str(
                        recovery_evidence["failed_run_id"]
                    ),
                    "MAA_RECOVERY_ATTEMPT_ID": str(attempt["attempt_id"]),
                    "MAA_RECOVERY_SLOT": str(attempt["slot"]),
                },
                clear=False,
            ):
                run_id = start_run(repo, "full", now=now)
        for index, phase in enumerate(PHASES, start=1):
            record_phase(
                repo,
                run_id,
                phase=phase,
                result="succeeded",
                outcome=f"{phase}-complete",
                now=now + timedelta(seconds=index),
            )
        outcome = finish_run(
            repo,
            run_id,
            process_status=0,
            supervisor_enabled=True,
            supervisor_required=True,
            command=(),
            timeout_seconds=1,
            now=now + timedelta(minutes=1),
        )
        self.assertEqual(outcome.status, "success")
        return run_id

    @staticmethod
    def _write_runtime_receipt(repo: Path, *, core: str, checked_at: str) -> None:
        path = repo / "var/state/runtime/maa-resource.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "status": "active",
                    "checked_at": checked_at,
                    "core": {
                        "channel": "stable",
                        "candidate_version": core,
                        "active_version": core,
                    },
                    "resource": {"active_commit": core.removeprefix("v") * 40},
                    "generation": {
                        "runtime_tree_sha256": core.removeprefix("v") * 64,
                        "managed_config_sha256": "a" * 64,
                        "core_library_sha256": core.removeprefix("v") * 64,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_codex_recovery_is_unsandboxed_and_keeps_session_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            failed_run_id = "20260828T000000.000000Z-12345678"
            scope = (ROOT / "docs/llm-recovery-scope.md").read_bytes()
            base_head = "1" * 40
            evidence = {
                "schema_version": 2,
                "kind": "operational-recovery",
                "failed_run_id": failed_run_id,
                "recovery_attempt": {
                    "attempt_id": "a" * 32,
                    "slot": "manual",
                },
                "screenshot_paths": [],
                "controller_repository": {"head": base_head},
                "agent_session": {
                    "ephemeral": False,
                    "thread_state_path": (
                        f"var/state/recovery/{failed_run_id}/codex-thread.json"
                    ),
                },
                "scope": {
                    "path": "docs/llm-recovery-scope.md",
                    "sha256": sha256_bytes(scope),
                    "version": 8,
                },
            }
            seen: dict[str, object] = {}

            def sdk_runner(request: CodexSDKRequest) -> str:
                seen["request"] = request
                schema = request.output_schema
                self.assertIn("recovered", schema["properties"]["status"]["enum"])
                self.assertNotIn(
                    "client-package-update",
                    schema["properties"]["scope_blocker"]["enum"],
                )
                output = {
                    "schema_version": 2,
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
                    "code_repair": self._no_code_repair(base_head),
                    "scope_blocker": "manual-login",
                }
                return json.dumps(output)

            output = json.loads(
                run_codex_recovery(
                    json.dumps(evidence).encode(),
                    environ={
                        "HOME": directory,
                        "PATH": directory,
                        "DISPLAY": ":0",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus",
                        "OPENAI_API_KEY": "needed-by-codex-sdk",
                        "MAA_SECRET": "must-not-leak",
                        "MAA_CODEX_RECOVERY_TIMEOUT_SECONDS": "120",
                    },
                    sdk_runner=sdk_runner,
                )
            )
            request = seen["request"]
            self.assertIsInstance(request, CodexSDKRequest)
            assert isinstance(request, CodexSDKRequest)
            self.assertFalse(request.ephemeral)
            self.assertIsNone(request.resume_thread_id)
            self.assertEqual(
                request.thread_id_path,
                ROOT / f"var/state/recovery/{failed_run_id}/codex-thread.json",
            )
            self.assertEqual(request.approval_mode, "deny-all")
            self.assertEqual(request.sandbox, "full-access")
            disabled = {
                value.removeprefix("features.").removesuffix("=false")
                for value in request.config_overrides
                if value.startswith("features.") and value.endswith("=false")
            }
            self.assertNotIn("shell_tool", disabled)
            self.assertNotIn("unified_exec", disabled)
            self.assertNotIn("view_image", disabled)
            self.assertIn(
                'shell_environment_policy.inherit="all"',
                request.config_overrides,
            )
            child_env = request.environment
            self.assertIsInstance(child_env, dict)
            assert isinstance(child_env, dict)
            self.assertEqual(child_env["MAA_RECOVERY_ACTIVE"], "true")
            self.assertEqual(child_env["MAA_RECOVERY_PARENT_RUN_ID"], failed_run_id)
            self.assertEqual(child_env["MAA_RECOVERY_ATTEMPT_ID"], "a" * 32)
            self.assertEqual(child_env["MAA_RECOVERY_SLOT"], "manual")
            self.assertEqual(
                request.thread_state_context,
                {
                    "failed_run_id": failed_run_id,
                    "attempt_id": "a" * 32,
                    "base_head": base_head,
                    "scope_sha256": sha256_bytes(scope),
                },
            )
            self.assertEqual(child_env["DISPLAY"], ":0")
            self.assertIn("OPENAI_API_KEY", child_env)
            self.assertNotIn("MAA_SECRET", child_env)
            prompt = request.prompt
            self.assertIsInstance(prompt, str)
            self.assertIn("completely unsandboxed shell", prompt)
            self.assertIn("client APK update", prompt)
            self.assertIn(
                "Every managed stage, including daily, is reentrant", prompt
            )
            self.assertEqual(output["scope_blocker"], "manual-login")

            evidence["scope"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(CodexRecoveryError, "scope identity"):
                run_codex_recovery(
                    json.dumps(evidence).encode(),
                    environ={"HOME": directory},
                    sdk_runner=sdk_runner,
                )

    def test_recovery_is_green_only_after_new_complete_full_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            failed_run_id = self._failed_full_run(repo)

            def adapter_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                successful_run_id = self._successful_full_run(
                    repo,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    recovery_evidence=evidence,
                )
                successful_events = load_run_events(repo, successful_run_id)
                audit_path = (
                    repo
                    / "var/state/supervisor/runs"
                    / successful_run_id
                    / "events"
                    / f"{len(successful_events) - 1:04d}-run-finished.json"
                )
                output = {
                    "schema_version": 2,
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
                        "audit_path": str(audit_path.relative_to(repo)),
                    },
                    "code_repair": self._no_code_repair(
                        evidence["controller_repository"]["head"]
                    ),
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

    def test_incident_includes_runtime_upgrade_and_recent_phase_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            self._write_runtime_receipt(
                repo, core="v1", checked_at="2026-08-27T23:30:00Z"
            )
            successful_run_id = self._successful_full_run(repo, now=START)
            self._write_runtime_receipt(
                repo, core="v2", checked_at="2026-08-28T00:30:00Z"
            )
            failed_run_id = self._failed_full_run(
                repo, now=START + timedelta(hours=1)
            )

            def blocked_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                self.assertEqual(
                    evidence["recent_successful_full_run"]["run_id"],
                    successful_run_id,
                )
                comparison = evidence["runtime_context"]["comparison"]
                self.assertIn("core_version", comparison["changed_fields"])
                self.assertEqual(
                    comparison["identities"]["recent_success"]["core_version"],
                    "v1",
                )
                self.assertEqual(
                    comparison["identities"]["failed_run_start"]["core_version"],
                    "v2",
                )
                depot = next(
                    item
                    for item in evidence["phase_comparison"]
                    if item["phase"] == "depot"
                )
                self.assertEqual(depot["recent_success"]["result"], "succeeded")
                self.assertEqual(depot["failed_run"]["result"], "failed")
                output = {
                    "schema_version": 2,
                    "failed_run_id": failed_run_id,
                    "status": "scope-blocked",
                    "classification": "scope",
                    "summary": "Interactive login is required.",
                    "actions_taken": ["Inspected enriched incident evidence."],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": None,
                        "successful_run_id": None,
                        "audit_path": None,
                    },
                    "code_repair": self._no_code_repair(
                        evidence["controller_repository"]["head"]
                    ),
                    "scope_blocker": "manual-login",
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
                runner=blocked_runner,
            )
            self.assertEqual(outcome.status, "scope-blocked")

    def test_recovery_accepts_agent_selected_branch_and_pull_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            subprocess.run(
                (
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/example/recovery-repo.git",
                ),
                cwd=repo,
                check=True,
            )
            failed_run_id = self._failed_full_run(repo)

            def repair_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                branch = "fix/agent-selected"
                subprocess.run(("git", "switch", "-c", branch), cwd=repo, check=True)
                task_path = repo / "config/tasks/proxy-preflight.toml"
                task_path.write_text(
                    task_path.read_text(encoding="utf-8") + "# repaired\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    ("git", "add", "config/tasks/proxy-preflight.toml"),
                    cwd=repo,
                    check=True,
                )
                subprocess.run(
                    ("git", "commit", "-qm", "fix: recover workflow"),
                    cwd=repo,
                    check=True,
                )
                commit = subprocess.run(
                    ("git", "rev-parse", "HEAD"),
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                successful_run_id = self._successful_full_run(
                    repo,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    recovery_evidence=evidence,
                )
                successful_events = load_run_events(repo, successful_run_id)
                audit_path = (
                    repo
                    / "var/state/supervisor/runs"
                    / successful_run_id
                    / "events"
                    / f"{len(successful_events) - 1:04d}-run-finished.json"
                )
                output = {
                    "schema_version": 2,
                    "failed_run_id": failed_run_id,
                    "status": "recovered",
                    "classification": "configuration",
                    "summary": "The committed repair passed a complete full run.",
                    "actions_taken": ["Committed and verified a repair branch."],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": 0,
                        "successful_run_id": successful_run_id,
                        "audit_path": str(audit_path.relative_to(repo)),
                    },
                    "code_repair": {
                        "status": "pr-opened",
                        "base_head": evidence["controller_repository"]["head"],
                        "branch": branch,
                        "commit": commit,
                        "applied_to_runtime": True,
                        "pull_request_url": (
                            "https://github.com/example/recovery-repo/pull/7"
                        ),
                        "pull_request_error": None,
                        "validation_commands": ["python -m unittest"],
                    },
                    "scope_blocker": "none",
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(output).encode(), b""
                )

            def verify_pull_request(
                _root: Path,
                url: str,
                branch: str,
                commit: str,
                base_branch: str,
            ) -> dict[str, object]:
                self.assertEqual(url, "https://github.com/example/recovery-repo/pull/7")
                self.assertEqual(branch, "fix/agent-selected")
                self.assertRegex(commit, r"^[0-9a-f]{40}$")
                self.assertEqual(base_branch, "master")
                return {"status": "verified"}

            outcome = recover_failed_run(
                repo,
                failed_run_id,
                slot="manual",
                command=("fake-recovery",),
                timeout_seconds=60,
                runner=repair_runner,
                pull_request_verifier=verify_pull_request,
            )
            self.assertEqual(outcome.status, "recovered")
            self.assertEqual(outcome.repair_branch, "fix/agent-selected")
            self.assertEqual(
                outcome.pull_request_url,
                "https://github.com/example/recovery-repo/pull/7",
            )
            self.assertEqual(
                load_run_events(repo, failed_run_id)[-1]["payload"][
                    "controller_verification"
                ]["code_repair"]["applied_to_runtime"],
                True,
            )

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
                    "schema_version": 2,
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
                    "code_repair": self._no_code_repair(
                        evidence["controller_repository"]["head"]
                    ),
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

    def test_adapter_transport_failure_retries_with_continuation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            failed_run_id = self._failed_full_run(repo)
            attempts: list[dict[str, object]] = []
            attempt_ids: list[str] = []

            def flaky_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                attempts.append(evidence["adapter_attempt"])
                attempt_ids.append(evidence["recovery_attempt"]["attempt_id"])
                if len(attempts) == 1:
                    return subprocess.CompletedProcess(argv, 1, b"", b"Bad Request")
                output = {
                    "schema_version": 2,
                    "failed_run_id": failed_run_id,
                    "status": "scope-blocked",
                    "classification": "scope",
                    "summary": "Interactive login is required.",
                    "actions_taken": ["Resumed recovery after the transport error."],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": None,
                        "successful_run_id": None,
                        "audit_path": None,
                    },
                    "code_repair": self._no_code_repair(
                        evidence["controller_repository"]["head"]
                    ),
                    "scope_blocker": "manual-login",
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
                runner=flaky_runner,
            )
            self.assertEqual(outcome.status, "scope-blocked")
            self.assertEqual([item["number"] for item in attempts], [1, 2])
            self.assertEqual(len(set(attempt_ids)), 1)
            self.assertEqual(attempts[0]["prior_errors"], [])
            self.assertIn("Bad Request", attempts[1]["prior_errors"][0])
            final = load_run_events(repo, failed_run_id)[-1]["payload"]
            self.assertEqual(len(final["agent"]["attempt_errors"]), 1)

    def test_historical_success_cannot_masquerade_as_this_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            historical_run_id = self._successful_full_run(repo, now=START)
            failed_run_id = self._failed_full_run(
                repo, now=START + timedelta(hours=1)
            )

            def replay_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                historical_events = load_run_events(repo, historical_run_id)
                historical_audit = (
                    repo
                    / "var/state/supervisor/runs"
                    / historical_run_id
                    / "events"
                    / f"{len(historical_events) - 1:04d}-run-finished.json"
                )
                (repo / "var/state/supervisor/latest-run.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "run_id": historical_run_id,
                            "status": "success",
                            "llm_invoked": False,
                            "event_sha256": historical_events[-1]["event_sha256"],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                output = {
                    "schema_version": 2,
                    "failed_run_id": failed_run_id,
                    "status": "recovered",
                    "classification": "runtime",
                    "summary": "Claimed a prior successful run.",
                    "actions_taken": ["Repointed the latest-run index."],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": 0,
                        "successful_run_id": historical_run_id,
                        "audit_path": str(historical_audit.relative_to(repo)),
                    },
                    "code_repair": self._no_code_repair(
                        evidence["controller_repository"]["head"]
                    ),
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
                runner=replay_runner,
            )
            self.assertEqual(outcome.status, "failed")
            final = load_run_events(repo, failed_run_id)[-1]["payload"]
            self.assertIn(
                "not linked to this recovery attempt",
                final["controller_verification"]["error"],
            )

    def test_applied_source_repair_cannot_rewrite_audit_producers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(directory)
            failed_run_id = self._failed_full_run(repo)

            def repair_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                evidence = json.loads(kwargs["input"])
                branch = "fix/unsafe-launcher"
                subprocess.run(("git", "switch", "-c", branch), cwd=repo, check=True)
                launcher = repo / "scripts/run-daily.sh"
                launcher.write_text(
                    "#!/usr/bin/env bash\n# fabricate accepted phases\nexit 0\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    ("git", "add", "scripts/run-daily.sh"), cwd=repo, check=True
                )
                subprocess.run(
                    ("git", "commit", "-qm", "fix: unsafe launcher"),
                    cwd=repo,
                    check=True,
                )
                commit = subprocess.run(
                    ("git", "rev-parse", "HEAD"),
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                successful_run_id = self._successful_full_run(
                    repo,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                    recovery_evidence=evidence,
                )
                successful_events = load_run_events(repo, successful_run_id)
                audit_path = (
                    repo
                    / "var/state/supervisor/runs"
                    / successful_run_id
                    / "events"
                    / f"{len(successful_events) - 1:04d}-run-finished.json"
                )
                output = {
                    "schema_version": 2,
                    "failed_run_id": failed_run_id,
                    "status": "recovered",
                    "classification": "configuration",
                    "summary": "Applied a launcher change.",
                    "actions_taken": ["Changed the audit-producing launcher."],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": 0,
                        "successful_run_id": successful_run_id,
                        "audit_path": str(audit_path.relative_to(repo)),
                    },
                    "code_repair": {
                        "status": "pr-failed",
                        "base_head": evidence["controller_repository"]["head"],
                        "branch": branch,
                        "commit": commit,
                        "applied_to_runtime": True,
                        "pull_request_url": None,
                        "pull_request_error": "offline test remote",
                        "validation_commands": ["python -m unittest"],
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
                runner=repair_runner,
            )
            self.assertEqual(outcome.status, "failed")
            final = load_run_events(repo, failed_run_id)[-1]["payload"]
            self.assertIn(
                "declarative task files",
                final["controller_verification"]["error"],
            )

    def test_sdk_resume_path_calls_thread_resume_and_rewrites_bound_state(self) -> None:
        calls: dict[str, object] = {}

        class FakeThread:
            id = "thread-resumed"

            async def run(self, _turn_input: object, **_: object) -> object:
                return types.SimpleNamespace(final_response="{}")

        class FakeAsyncCodex:
            def __init__(self, _config: object) -> None:
                pass

            async def __aenter__(self) -> "FakeAsyncCodex":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def thread_resume(self, thread_id: str, **_: object) -> FakeThread:
                calls["resumed"] = thread_id
                return FakeThread()

            async def thread_start(self, **_: object) -> FakeThread:
                raise AssertionError("thread_start must not be called")

        fake_sdk = types.SimpleNamespace(
            ApprovalMode=types.SimpleNamespace(deny_all="deny-all"),
            AsyncCodex=FakeAsyncCodex,
            CodexConfig=lambda **kwargs: kwargs,
            LocalImageInput=lambda value: value,
            Sandbox=types.SimpleNamespace(
                read_only="read-only",
                workspace_write="workspace-write",
                full_access="full-access",
            ),
            TextInput=lambda value: value,
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            state_path = workspace / "state/thread.json"
            context = {
                "failed_run_id": "20260828T000000.000000Z-12345678",
                "attempt_id": "b" * 32,
                "base_head": "c" * 40,
                "scope_sha256": "d" * 64,
            }
            request = CodexSDKRequest(
                prompt="resume",
                output_schema={"type": "object"},
                cwd=workspace,
                sandbox="full-access",
                images=(),
                model=None,
                timeout_seconds=10,
                environment={"HOME": directory},
                config_overrides=(),
                ephemeral=False,
                resume_thread_id="thread-original",
                thread_id_path=state_path,
                thread_state_context=context,
            )
            with patch.dict(sys.modules, {"openai_codex": fake_sdk}):
                self.assertEqual(run_sdk_request(request), "{}")
            self.assertEqual(calls["resumed"], "thread-original")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 2)
            self.assertEqual(saved["thread_id"], "thread-resumed")
            self.assertEqual(saved["context"], context)

    def test_stateful_daily_parent_still_invokes_reentrant_recovery(self) -> None:
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

            def blocked_runner(
                argv: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                nonlocal invoked
                invoked = True
                evidence = json.loads(kwargs["input"])
                self.assertEqual(
                    evidence["reentrancy_policy"],
                    {
                        "all_managed_stages": True,
                        "daily": True,
                        "whole_run_replay": True,
                    },
                )
                self.assertEqual(
                    evidence["client_update_policy"]["official_apk_url"],
                    "https://ak.hypergryph.com/downloads/android_lastest",
                )
                self.assertTrue(
                    evidence["client_update_policy"]["preserve_app_data"]
                )
                self.assertEqual(
                    evidence["client_update_policy"]["install_argv_prefix"],
                    ["adb", "install", "--no-streaming", "-r"],
                )
                self.assertFalse(
                    evidence["client_update_policy"]["allow_uninstall"]
                )
                output = {
                    "schema_version": 2,
                    "failed_run_id": failed_run_id,
                    "status": "scope-blocked",
                    "classification": "scope",
                    "summary": "Interactive login is required after replay.",
                    "actions_taken": [
                        "Accepted daily as reentrant and inspected the login screen."
                    ],
                    "verification": {
                        "command": evidence["retry_command"],
                        "exit_status": None,
                        "successful_run_id": None,
                        "audit_path": None,
                    },
                    "code_repair": self._no_code_repair(
                        evidence["controller_repository"]["head"]
                    ),
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
            self.assertTrue(invoked)
            self.assertEqual(outcome.status, "scope-blocked")
            self.assertEqual(outcome.scope_blocker, "manual-login")
            final = load_run_events(repo, failed_run_id)[-1]["payload"]
            self.assertTrue(final["agent"]["invoked"])
            self.assertEqual(
                final["controller_verification"]["status"],
                "scope-accepted",
            )

    def test_recovered_report_requires_controller_fields(self) -> None:
        failed_run_id = "20260828T000000.000000Z-12345678"
        with self.assertRaisesRegex(Exception, "complete success evidence"):
            validate_recovery_report(
                {
                    "schema_version": 2,
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
                    "code_repair": self._no_code_repair("0" * 40),
                    "scope_blocker": "none",
                },
                failed_run_id=failed_run_id,
            )


if __name__ == "__main__":
    unittest.main()
