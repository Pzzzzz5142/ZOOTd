#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_dir="${project_root}/.local/bin"
version=0.7.5
sdk_version=0.147.0
venv_dir="${project_root}/.venv"

case "$(uname -m)" in
    x86_64)
        target=x86_64-unknown-linux-gnu
        checksum=2f9ba534d061e1f657af266365da37a8a6ceec596dac6de65fc524af844a2292
        ;;
    aarch64|arm64)
        target=aarch64-unknown-linux-gnu
        checksum=cd229404f4ec2c6279af66d66bce2befd5b4a5cea39f2b7b35eff6830db73b4a
        ;;
    *)
        printf 'Unsupported architecture: %s\n' "$(uname -m)" >&2
        exit 1
        ;;
esac

missing_packages=()
command -v adb >/dev/null 2>&1 || missing_packages+=(android-tools)
command -v gamescope >/dev/null 2>&1 || missing_packages+=(gamescope)
command -v jq >/dev/null 2>&1 || missing_packages+=(jq)

if (( ${#missing_packages[@]} > 0 )); then
    if command -v pacman >/dev/null 2>&1; then
        printf 'Installing runtime dependencies with pacman: %s\n' "${missing_packages[*]}"
        sudo pacman -S --needed "${missing_packages[@]}"
    else
        printf 'Missing runtime commands; install adb, gamescope and jq first.\n' >&2
        exit 1
    fi
fi

require_command() {
    command -v "$1" >/dev/null 2>&1 || {
        printf 'Required command not found: %s\n' "$1" >&2
        exit 1
    }
}

require_command python3

if [[ -x "${install_dir}/maa" ]] && [[ "$("${install_dir}/maa" --version)" == "maa ${version}" ]]; then
    printf 'maa-cli %s is already installed.\n' "${version}"
else
    require_command curl
    require_command sha256sum
    require_command tar

    archive="maa_cli-v${version}-${target}.tar.gz"
    download_url="https://github.com/MaaAssistantArknights/maa-cli/releases/download/v${version}/${archive}"
    temp_dir="$(mktemp -d)"

    cleanup_temp() {
        if [[ -n "${temp_dir:-}" && -d "${temp_dir}" && "${temp_dir}" == /tmp/tmp.* ]]; then
            rm -r -- "${temp_dir}"
        fi
    }
    trap cleanup_temp EXIT

    printf 'Downloading maa-cli %s for %s...\n' "${version}" "${target}"
    curl --fail --location --output "${temp_dir}/${archive}" "${download_url}"
    printf '%s  %s\n' "${checksum}" "${temp_dir}/${archive}" | sha256sum --check --status
    tar -xzf "${temp_dir}/${archive}" -C "${temp_dir}"

    maa_source="$(find "${temp_dir}" -type f -name maa -print -quit)"
    [[ -n "${maa_source}" ]] || {
        printf 'maa binary not found in release archive.\n' >&2
        exit 1
    }

    install -d -- "${install_dir}"
    install -m 0755 -- "${maa_source}" "${install_dir}/maa"
    printf 'Installed %s\n' "${install_dir}/maa"
fi

if [[ ! -x "${venv_dir}/bin/python" ]]; then
    printf 'Creating the project-local Python environment.\n'
    python3 -m venv "${venv_dir}"
fi

if "${venv_dir}/bin/python" - "${sdk_version}" <<'PY'
import importlib.metadata
import sys

try:
    import openai_codex  # noqa: F401
    installed = importlib.metadata.version("openai-codex")
except (ImportError, importlib.metadata.PackageNotFoundError):
    raise SystemExit(1)
raise SystemExit(installed != sys.argv[1])
PY
then
    printf 'openai-codex SDK %s is already installed.\n' "${sdk_version}"
else
    "${venv_dir}/bin/python" -m pip install \
        --disable-pip-version-check \
        --requirement "${project_root}/requirements.txt"
    printf 'Installed openai-codex SDK %s in %s\n' \
        "${sdk_version}" "${venv_dir}"
fi
