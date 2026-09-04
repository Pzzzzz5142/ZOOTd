from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from maa_planner.runtime_receipt import RuntimeReceiptError, validate_runtime_receipt


class RuntimeUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _make_fake_maa(self, root: Path) -> None:
        fake = root / "bin/maa"
        shutil.copy2(
            self.project_root / "var/data/resource/item_index.json",
            root / "config/item-index.fixture.json",
        )
        self._write(
            fake,
            r'''#!/usr/bin/env bash
set -Eeuo pipefail

fake_project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

command_name=""
for argument in "$@"; do
    case "${argument}" in
        version|install|update|run|fight)
            command_name="${argument}"
            break
            ;;
    esac
done

case "${command_name}" in
    version)
        printf 'maa-cli vtest\nMaaCore %s\n' "$(<"${MAA_DATA_DIR}/core-version")"
        ;;
    install|update)
        rm -r -- "${MAA_DATA_DIR}/lib" "${MAA_DATA_DIR}/resource"
        mkdir -p -- "${MAA_DATA_DIR}/lib" "${MAA_DATA_DIR}/resource"
        mkdir -p -- "${MAA_CACHE_DIR}/resource/tasks"
        printf 'v2\n' >"${MAA_DATA_DIR}/core-version"
        printf 'candidate core\n' >"${MAA_DATA_DIR}/lib/libMaaCore.so"
        printf 'candidate base\n' >"${MAA_DATA_DIR}/resource/base"
        cp -- "${fake_project_root}/config/item-index.fixture.json" \
            "${MAA_DATA_DIR}/resource/item_index.json"
        printf '%s\n' '{"version":"v2","details":{"assets":[{"name":"MAA-v2-linux-x86_64.tar.gz"}]}}' >"${MAA_CACHE_DIR}/core-manifest-stable.json"
        printf 'core-etag\n' >"${MAA_CACHE_DIR}/core-manifest-stable.json.etag"
        printf 'archive\n' >"${MAA_CACHE_DIR}/MAA-v2-linux-x86_64.tar.gz"
        printf '{}\n' >"${MAA_CACHE_DIR}/StageActivityV2.json"
        printf 'activity-etag\n' >"${MAA_CACHE_DIR}/StageActivityV2.json.etag"
        printf '{}\n' >"${MAA_CACHE_DIR}/resource/tasks/tasks.json"
        printf 'tasks-etag\n' >"${MAA_CACHE_DIR}/resource/tasks/tasks.json.etag"
        ;;
    run|fight)
        [[ ! -e "${MAA_DATA_DIR}/MaaResource/bad" ]]
        [[ -s "${MAA_CACHE_DIR}/StageActivityV2.json" ]]
        [[ -s "${MAA_CACHE_DIR}/resource/tasks/tasks.json" ]]
        if [[ " $* " == *" run daily "* ]]; then
            [[ -s "${MAA_CONFIG_DIR}/infrast/protected-dorm.json" ]]
        fi
        ;;
    *)
        exit 2
        ;;
esac
''',
        )
        fake.chmod(0o755)

    def _make_resource_remote(self, root: Path) -> tuple[Path, str]:
        work = root / "resource-work"
        remote = root / "resource.git"
        work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=work, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=work,
            check=True,
        )
        self._write(work / "bad", "latest overlay is deliberately incompatible\n")
        subprocess.run(["git", "add", "bad"], cwd=work, check=True)
        subprocess.run(["git", "commit", "-qm", "candidate"], cwd=work, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remote)], check=True)
        return remote, commit

    def test_new_core_can_promote_with_previous_overlay_as_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "bin").mkdir()
            shutil.copy2(
                self.project_root / "scripts/update-maa-runtime.sh",
                root / "scripts/update-maa-runtime.sh",
            )
            (root / "scripts/update-maa-runtime.sh").chmod(0o755)
            shutil.copytree(self.project_root / "config", root / "config")
            shutil.copy2(self.project_root / "bin/maa-planner", root / "bin/maa-planner")
            (root / "bin/maa-planner").chmod(0o755)
            shutil.copytree(self.project_root / "maa_planner", root / "maa_planner")
            self._make_fake_maa(root)
            remote, candidate_commit = self._make_resource_remote(root)

            self._write(root / "var/data/core-version", "v1\n")
            self._write(root / "var/data/lib/libMaaCore.so", "live core\n")
            self._write(root / "var/data/resource/base", "live base\n")
            shutil.copy2(
                root / "config/item-index.fixture.json",
                root / "var/data/resource/item_index.json",
            )
            self._write(root / "var/data/MaaResource/live", "compatible overlay\n")
            self._write(root / "var/cache/maa-runtime/StageActivityV2.json", "{}\n")
            self._write(root / "var/cache/maa-runtime/StageActivityV2.json.etag", "old\n")
            self._write(root / "var/cache/maa-runtime/resource/tasks/tasks.json", "{}\n")
            self._write(
                root / "var/cache/maa-runtime/resource/tasks/tasks.json.etag", "old\n"
            )
            self._write(
                root / "var/state/runtime/maa-resource.json",
                json.dumps({"active_commit": "1" * 40}) + "\n",
            )

            env = os.environ.copy()
            env["MAA_RESOURCE_REMOTE_URL"] = str(remote)
            completed = subprocess.run(
                [str(root / "scripts/update-maa-runtime.sh")],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual((root / "var/data/core-version").read_text().strip(), "v2")
            self.assertTrue((root / "var/data/MaaResource/live").is_file())
            self.assertFalse((root / "var/data/MaaResource/bad").exists())
            self.assertEqual(
                (root / "var/cache/MaaRuntime.previous/core-version").read_text().strip(),
                "v1",
            )
            self.assertTrue((root / "var/cache/maa-runtime").is_symlink())
            self.assertEqual(
                (root / "var/cache/maa-runtime").resolve(),
                (root / "var/data/cache").resolve(),
            )
            state = json.loads(
                (root / "var/state/runtime/maa-resource.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["status"], "active")
            self.assertEqual(state["core"]["active_version"], "v2")
            self.assertEqual(state["resource"]["candidate_commit"], candidate_commit)
            self.assertEqual(state["resource"]["selected_source"], "live")
            self.assertEqual(state["hot_cache"]["selected_source"], "fresh")
            self.assertEqual(state["schema_version"], 3)
            self.assertEqual(state["generation"]["schema_version"], 1)
            self.assertIsInstance(state["transition"]["activated_at"], str)
            self.assertEqual(state["transition"]["previous_core_version"], "v1")
            self.assertEqual(
                state["transition"]["previous_resource_commit"], "1" * 40
            )
            self.assertEqual(validate_runtime_receipt(root), "sealed-schema-3")

            rolled_back = subprocess.run(
                [str(root / "scripts/update-maa-runtime.sh"), "--rollback"],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                rolled_back.returncode, 0, rolled_back.stdout + rolled_back.stderr
            )
            self.assertEqual((root / "var/data/core-version").read_text().strip(), "v1")
            self.assertEqual(
                (root / "var/cache/MaaRuntime.previous/core-version").read_text().strip(),
                "v2",
            )
            rollback_state = json.loads(
                (root / "var/state/runtime/maa-resource.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(rollback_state["status"], "active")
            self.assertEqual(rollback_state["core"]["active_version"], "v1")
            self.assertEqual(
                rollback_state["transition"]["previous_core_version"], "v2"
            )
            self.assertEqual(validate_runtime_receipt(root), "sealed-schema-3")

            proxy_config = root / "config/tasks/proxy-preflight.toml"
            self._write(
                proxy_config,
                proxy_config.read_text(encoding="utf-8") + "\n# unvalidated drift\n",
            )
            with self.assertRaises(RuntimeReceiptError):
                validate_runtime_receipt(root)


if __name__ == "__main__":
    unittest.main()
