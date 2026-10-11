"""Exercise the real shell/Python boundary without a device or private state."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from maa_planner.copilot_run import device_lock, execute


ROOT = Path(__file__).resolve().parents[1]


class CopilotEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'bin').mkdir()
        shutil.copy2(ROOT / 'bin/zootd', self.root / 'bin/zootd')
        shutil.copytree(ROOT / 'maa_planner', self.root / 'maa_planner',
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.commands = self.root / 'fake-bin'
        self.commands.mkdir()
        self.selected = self.root / 'selected-python'
        self.device_calls = self.root / 'device-calls'
        self.env = dict(os.environ, PYTHONPATH='', PYTHONNOUSERSITE='1',
                        PATH=str(self.commands) + os.pathsep + os.environ['PATH'],
                        MAA_CODEX_SDK_PYTHON=str(self.commands / 'sdk-only-python'))
        for name in ('adb', 'waydroid', 'sdk-only-python'):
            self.script(self.commands / name,
                        'printf "%s\\n" ' + shlex.quote(name) + ' >> '
                        + shlex.quote(str(self.device_calls)) + '\nexit 99\n')
        # A real ready-environment run stops at a synthetic dirty checkout.
        self.script(self.commands / 'git', 'printf " M synthetic.py\\n"\n')

    def script(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def python(self, path, label, *, missing_dependencies=False):
        self.script(path,
                    'printf "%s\\n" ' + shlex.quote(label) + ' >> '
                    + shlex.quote(str(self.selected)) + '\nexec '
                    + shlex.quote(sys.executable)
                    + (' -S' if missing_dependencies else '') + ' "$@"\n')

    def cli(self):
        result = subprocess.run([str(self.root / 'bin/zootd'), 'copilot-run', 'XX-1'],
                                cwd=self.root, env=self.env, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        audit = json.loads((Path(report['run_dir']) / 'result.json').read_text())
        self.assertEqual(audit['attempts'], [])
        self.assertNotIn('budget', audit)
        self.assertFalse(self.device_calls.exists())
        self.assertFalse((self.root / 'var/state/operator-box.json').exists())
        return result, audit

    def test_project_python_wins_over_path_and_sdk_override(self):
        self.python(self.root / '.venv/bin/python', 'project')
        self.script(self.commands / 'python3', 'exit 97\n')
        result, audit = self.cli()
        self.assertEqual(self.selected.read_text(), 'project\n')
        self.assertEqual(audit['failure_phase'], 'readiness', result.stderr)
        self.assertEqual(audit['python_environment']['executable'], sys.executable)
        self.assertIn('cv2', audit['python_environment']['dependencies'])

    def test_fallback_requires_actual_vision_imports(self):
        self.python(self.commands / 'python3', 'system')
        result, audit = self.cli()
        self.assertEqual(self.selected.read_text(), 'system\n')
        self.assertEqual(audit['failure_phase'], 'readiness', result.stderr)
        self.assertEqual(audit['python_environment']['executable'], sys.executable)

    def test_missing_dependencies_stop_before_device_or_budget(self):
        for project_exists in (True, False):
            with self.subTest(project_exists=project_exists):
                if self.selected.exists():
                    self.selected.unlink()
                path = (self.root / '.venv/bin/python' if project_exists
                        else self.commands / 'python3')
                if not project_exists:
                    (self.root / '.venv/bin/python').unlink()
                self.python(path, 'missing', missing_dependencies=True)
                result, audit = self.cli()
                self.assertEqual(audit['failure_phase'], 'python_environment')
                self.assertIn('cv2', audit['error'])
                self.assertIn('numpy', audit['error'])
                self.assertIn('./scripts/bootstrap.sh', result.stderr)
                self.assertEqual(audit['python_environment']['executable'], sys.executable)
                self.assertFalse(audit['python_environment']['ready'])
                self.assertEqual(list((self.root / 'var/run').iterdir()),
                                 [self.root / 'var/run/zootd.lock'])
                self.assertEqual(self.selected.read_text(), 'missing\n')

    def test_broken_project_binary_import_does_not_use_system_fallback(self):
        self.python(self.root / '.venv/bin/python', 'project')
        self.script(self.commands / 'python3', 'exit 97\n')
        # An installed extension can fail after a Python/shared-library upgrade.
        (self.root / 'cv2.py').write_text("raise OSError('synthetic ABI diagnostic')\n")
        result, audit = self.cli()
        self.assertEqual(self.selected.read_text(), 'project\n')
        self.assertEqual(audit['failure_phase'], 'python_environment')
        self.assertIn('cv2', audit['error'])
        self.assertNotIn('synthetic ABI diagnostic', result.stdout + result.stderr)

    def test_busy_environment_updater_prevents_dependency_probe(self):
        self.python(self.root / '.venv/bin/python', 'project')
        probe = self.root / 'dependency-probed'
        (self.root / 'cv2.py').write_text(
            'from pathlib import Path\n'
            + 'Path(' + repr(str(probe)) + ').touch()\n'
            + "raise OSError('synthetic upgrade in progress')\n")
        with device_lock(self.root):
            result, audit = self.cli()
        self.assertEqual(audit['failure_phase'], 'readiness', result.stderr)
        self.assertIn('holds the device lock', audit['error'])
        self.assertFalse(audit['python_environment']['ready'])
        self.assertFalse(probe.exists())

    def test_copilot_worker_uses_parent_even_if_default_environment_differs(self):
        # The real execute() creates a subprocess. Only its MaaCore module is
        # replaced with a harmless fixture that performs proof image operations.
        package = self.root / 'maa_planner'
        (package / 'copilot_core.py').write_text(
            "import cv2, json, numpy as np, sys\n"
            "from pathlib import Path\n"
            "encoded = cv2.imencode('.png', np.zeros((2,2,3), np.uint8))[1]\n"
            "image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)\n"
            "Path('parent-environment.json').write_text(json.dumps({"
            "'executable':sys.executable,'shape':image.shape}))\n")
        self.script(self.root / '.venv/bin/python', 'exit 96\n')
        run = self.root / 'attempt'
        run.mkdir()
        self.assertEqual(execute(self.root, run, 'synthetic', interpreter=sys.executable), 0)
        observed = json.loads((run / 'parent-environment.json').read_text())
        self.assertEqual(observed, {'executable': sys.executable, 'shape': [2, 2, 3]})
