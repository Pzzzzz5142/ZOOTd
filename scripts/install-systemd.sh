#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
login_user="$(id -un)"

install -d -- "${unit_dir}"
install -m 0644 -- "${project_root}/systemd/zootd.service" "${unit_dir}/zootd.service"
install -m 0644 -- "${project_root}/systemd/zootd.timer" "${unit_dir}/zootd.timer"
install -m 0644 -- "${project_root}/systemd/zootd-prereset.service" "${unit_dir}/zootd-prereset.service"
install -m 0644 -- "${project_root}/systemd/zootd-prereset.timer" "${unit_dir}/zootd-prereset.timer"
install -m 0644 -- "${project_root}/systemd/zootd-runtime-update.service" "${unit_dir}/zootd-runtime-update.service"
install -m 0644 -- "${project_root}/systemd/zootd-runtime-update.timer" "${unit_dir}/zootd-runtime-update.timer"
install -m 0644 -- "${project_root}/systemd/zootd-codex-update.service" "${unit_dir}/zootd-codex-update.service"
install -m 0644 -- "${project_root}/systemd/zootd-codex-update.timer" "${unit_dir}/zootd-codex-update.timer"
install -m 0644 -- "${project_root}/systemd/zootd-log-cleanup.service" "${unit_dir}/zootd-log-cleanup.service"
install -m 0644 -- "${project_root}/systemd/zootd-log-cleanup.timer" "${unit_dir}/zootd-log-cleanup.timer"
systemctl --user daemon-reload

printf 'Installed user units in %s\n' "${unit_dir}"

if [[ "${1:-}" == --enable ]]; then
    if [[ "$(loginctl show-user "${login_user}" -p Linger --value 2>/dev/null)" != yes ]]; then
        printf 'Enabling systemd user lingering for unattended timer execution.\n'
        loginctl enable-linger "${login_user}" || {
            printf 'Could not enable lingering. Run: sudo loginctl enable-linger %q\n' \
                "${login_user}" >&2
            exit 1
        }
    fi
    [[ "$(loginctl show-user "${login_user}" -p Linger --value 2>/dev/null)" == yes ]] || {
        printf 'User lingering is still disabled; refusing to claim unattended setup.\n' >&2
        exit 1
    }
    systemctl --user enable --now \
        zootd-log-cleanup.timer zootd-codex-update.timer zootd-runtime-update.timer \
        zootd-prereset.timer zootd.timer
    systemctl --user list-timers \
        zootd-log-cleanup.timer zootd-codex-update.timer zootd-runtime-update.timer \
        zootd-prereset.timer zootd.timer --no-pager
else
    printf 'Timers remain disabled. Enable them after a successful manual run with:\n'
    printf '  systemctl --user enable --now zootd-log-cleanup.timer zootd-codex-update.timer zootd-runtime-update.timer zootd-prereset.timer zootd.timer\n'
fi
