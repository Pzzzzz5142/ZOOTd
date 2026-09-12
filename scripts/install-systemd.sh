#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
login_user="$(id -un)"

install -d -- "${unit_dir}"
install -m 0644 -- "${project_root}/systemd/maa-waydroid.service" "${unit_dir}/maa-waydroid.service"
install -m 0644 -- "${project_root}/systemd/maa-waydroid.timer" "${unit_dir}/maa-waydroid.timer"
install -m 0644 -- "${project_root}/systemd/maa-waydroid-prereset.service" "${unit_dir}/maa-waydroid-prereset.service"
install -m 0644 -- "${project_root}/systemd/maa-waydroid-prereset.timer" "${unit_dir}/maa-waydroid-prereset.timer"
install -m 0644 -- "${project_root}/systemd/maa-waydroid-runtime-update.service" "${unit_dir}/maa-waydroid-runtime-update.service"
install -m 0644 -- "${project_root}/systemd/maa-waydroid-runtime-update.timer" "${unit_dir}/maa-waydroid-runtime-update.timer"
install -m 0644 -- "${project_root}/systemd/maa-waydroid-codex-update.service" "${unit_dir}/maa-waydroid-codex-update.service"
install -m 0644 -- "${project_root}/systemd/maa-waydroid-codex-update.timer" "${unit_dir}/maa-waydroid-codex-update.timer"
if [[ -e "${unit_dir}/maa-waydroid-resource-update.timer" ||
      -e "${unit_dir}/maa-waydroid-resource-update.service" ]]; then
    systemctl --user disable --now maa-waydroid-resource-update.timer >/dev/null 2>&1 || true
    rm -f -- "${unit_dir}/maa-waydroid-resource-update.timer" \
        "${unit_dir}/maa-waydroid-resource-update.service"
fi
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
        maa-waydroid-codex-update.timer maa-waydroid-runtime-update.timer \
        maa-waydroid-prereset.timer maa-waydroid.timer
    systemctl --user list-timers \
        maa-waydroid-codex-update.timer maa-waydroid-runtime-update.timer \
        maa-waydroid-prereset.timer maa-waydroid.timer --no-pager
else
    printf 'Timers remain disabled. Enable them after a successful manual run with:\n'
    printf '  systemctl --user enable --now maa-waydroid-codex-update.timer maa-waydroid-runtime-update.timer maa-waydroid-prereset.timer maa-waydroid.timer\n'
fi
