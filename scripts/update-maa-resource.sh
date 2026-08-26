#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Compatibility entry point. Runtime updates are now transactional across
# MaaCore, its bundled resource, the Git overlay, and the API hot cache.
exec "${project_root}/scripts/update-maa-runtime.sh" "$@"
