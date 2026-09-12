#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
venv_dir="${project_root}/.venv"
mkdir -p -- "${project_root}/var/run"
exec 9>"${project_root}/var/run/zootd.lock"
exec 8>"${project_root}/var/run/codex-sdk.lock"
if ! flock -n 9 || ! flock -n 8; then
    printf 'MAA or Codex is active; skipping this SDK update.\n'
    exit 0
fi

if [[ ! -x "${venv_dir}/bin/python" ]]; then
    python3 -m venv "${venv_dir}"
fi
"${venv_dir}/bin/python" -m pip install --upgrade \
    --disable-pip-version-check --no-input --timeout 30 --retries 2 \
    --requirement "${project_root}/requirements.txt"
"${venv_dir}/bin/python" -m pip check
"${venv_dir}/bin/python" - <<'PY'
import importlib.metadata
import subprocess

from openai_codex import AsyncCodex, CodexConfig
from codex_cli_bin import bundled_codex_path

print(f"openai-codex SDK {importlib.metadata.version('openai-codex')}", flush=True)
subprocess.run([str(bundled_codex_path()), '--version'], check=True)
PY
