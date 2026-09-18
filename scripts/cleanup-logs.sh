#!/usr/bin/env bash
set -Eeuo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PYTHONPATH="${project_root}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m maa_planner.log_retention --project-root "${project_root}" "$@"
