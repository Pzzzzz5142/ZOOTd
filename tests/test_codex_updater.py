import fcntl
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CodexUpdaterTests(unittest.TestCase):
    def test_busy_run_or_recovery_prevents_environment_mutation(self):
        for lock_name in ("zootd.lock", "codex-sdk.lock"):
            with self.subTest(lock=lock_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "scripts").mkdir()
                (root / "var/run").mkdir(parents=True)
                script = root / "scripts/update-codex-sdk.sh"
                shutil.copy2(ROOT / "scripts/update-codex-sdk.sh", script)
                with (root / "var/run" / lock_name).open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_SH)
                    result = subprocess.run(
                        [str(script)], capture_output=True, text=True, timeout=5
                    )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("skipping", result.stdout)
                self.assertFalse((root / ".venv").exists())

    def test_failed_install_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / ".venv/bin").mkdir(parents=True)
            script = root / "scripts/update-codex-sdk.sh"
            shutil.copy2(ROOT / "scripts/update-codex-sdk.sh", script)
            python = root / ".venv/bin/python"
            python.write_text("#!/bin/sh\nexit 23\n")
            python.chmod(0o755)
            result = subprocess.run(
                [str(script)], capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 23)
            self.assertNotIn("openai-codex SDK", result.stdout)
