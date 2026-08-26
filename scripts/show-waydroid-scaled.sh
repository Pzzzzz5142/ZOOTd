#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
display_width=1280
display_height=720
display_mode="${MAA_WAYDROID_DISPLAY_MODE:-auto}"
session_started=false
gamescope_pid=""
desktop_available=false

info() {
    printf '[waydroid-ui] %s\n' "$*"
}

die() {
    printf '[waydroid-ui] error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

import_desktop_environment() {
    local key value

    while IFS='=' read -r key value; do
        case "${key}" in
            DBUS_SESSION_BUS_ADDRESS|DISPLAY|HYPRLAND_INSTANCE_SIGNATURE|WAYLAND_DISPLAY|XDG_CURRENT_DESKTOP|XDG_RUNTIME_DIR|XDG_SESSION_TYPE)
                if [[ -n "${value}" ]]; then
                    printf -v "${key}" '%s' "${value}"
                    export "${key}"
                fi
                ;;
        esac
    done < <(systemctl --user show-environment 2>/dev/null || true)
}

detect_desktop() {
    local runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

    if [[ "${display_mode}" == headless ]]; then
        desktop_available=false
        unset DISPLAY HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY
        return 0
    fi

    if [[ -n "${WAYLAND_DISPLAY:-}" &&
          -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" &&
          -S "${runtime_dir}/${WAYLAND_DISPLAY}" &&
          -S "${runtime_dir}/hypr/${HYPRLAND_INSTANCE_SIGNATURE}/.socket.sock" ]]; then
        desktop_available=true
        return 0
    fi

    if [[ "${display_mode}" == desktop ]]; then
        die "desktop display mode was requested, but no live Hyprland session is available"
    fi

    # A lingering user manager remains alive while no graphical login exists.
    # Do not let stale variables accidentally select the nested backend.
    desktop_available=false
    unset DISPLAY HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY
}

waydroid_session_is_running() {
    timeout --signal=TERM --kill-after=2s 5s waydroid status 2>/dev/null |
        grep -Eq '^Session:[[:space:]]+RUNNING$'
}

cleanup() {
    local status=$?
    local attempt

    trap - EXIT INT TERM HUP

    if [[ "${session_started}" == true ]] && waydroid_session_is_running; then
        info "stopping the Waydroid session"
        timeout --signal=TERM --kill-after=5s 15s waydroid session stop >/dev/null 2>&1 || true
    fi

    if [[ -n "${gamescope_pid}" ]] && kill -0 "${gamescope_pid}" 2>/dev/null; then
        kill -TERM "${gamescope_pid}" 2>/dev/null || true
        for attempt in {1..20}; do
            kill -0 "${gamescope_pid}" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "${gamescope_pid}" 2>/dev/null; then
            kill -KILL "${gamescope_pid}" 2>/dev/null || true
        fi
    fi

    if [[ -n "${gamescope_pid}" ]]; then
        wait "${gamescope_pid}" 2>/dev/null || true
    fi

    exit "${status}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

configure_gamescope_window() {
    local deadline=$(( SECONDS + 15 ))
    local address=""

    while (( SECONDS < deadline )); do
        kill -0 "${gamescope_pid}" 2>/dev/null || return 2
        address="$(hyprctl clients -j 2>/dev/null | jq -r \
            --argjson pid "${gamescope_pid}" \
            '.[] | select(.pid == $pid) | .address' | head -n 1 || true)"
        if [[ "${address}" =~ ^0x[0-9a-fA-F]+$ ]]; then
            hyprctl dispatch \
                "hl.dsp.window.tag({ tag = '+maa-waydroid', window = 'address:${address}' })" \
                >/dev/null
            hyprctl dispatch \
                "hl.dsp.window.float({ action = 'on', window = 'address:${address}' })" \
                >/dev/null
            hyprctl dispatch \
                "hl.dsp.window.resize({ x = ${display_width}, y = ${display_height}, relative = false, window = 'address:${address}' })" \
                >/dev/null
            hyprctl dispatch \
                "hl.dsp.window.center({ window = 'address:${address}' })" \
                >/dev/null
            return 0
        fi
        sleep 0.25
    done

    return 1
}

focus_existing_scaled_window() {
    local address=""

    address="$(hyprctl clients -j 2>/dev/null | jq -r \
        '.[] | select(any(.tags[]?; . == "maa-waydroid")) | .address' |
        head -n 1 || true)"
    [[ "${address}" =~ ^0x[0-9a-fA-F]+$ ]] || return 1
    hyprctl dispatch "hl.dsp.focus({ window = 'address:${address}' })" >/dev/null
}

require_command gamescope
require_command flock
require_command systemctl
require_command timeout
require_command waydroid

case "${display_mode}" in
    auto|desktop|headless)
        ;;
    *)
        die "invalid MAA_WAYDROID_DISPLAY_MODE=${display_mode} (expected auto, desktop, or headless)"
        ;;
esac

import_desktop_environment
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
detect_desktop
if [[ "${desktop_available}" == true ]]; then
    require_command hyprctl
    require_command jq
fi

mkdir -p -- "${project_root}/var/run"
exec 7>"${project_root}/var/run/waydroid-scaled-ui.lock"
if ! flock -n 7; then
    if [[ "${desktop_available}" == true ]]; then
        for attempt in {1..60}; do
            if focus_existing_scaled_window; then
                info "focused the starting or running scaled Waydroid window"
                exit 0
            fi
            sleep 0.25
        done
        die "another scaled Waydroid launcher is active, but its window is unavailable"
    fi
    die "another headless scaled Waydroid launcher is already active"
fi

if waydroid_session_is_running; then
    current_display="$(timeout --signal=TERM --kill-after=2s 5s waydroid status 2>/dev/null |
        awk -F ':[[:space:]]*' '/^Wayland display:/ { print $2; exit }')"
    if [[ "${current_display}" == gamescope-* ]] && focus_existing_scaled_window; then
        info "focused the running scaled Waydroid window"
        exit 0
    fi
    die "a Waydroid session is already running on ${current_display:-an unknown display}; stop it before starting the scaled UI"
fi

session_started=true
if [[ "${desktop_available}" == true ]]; then
    info "starting a fixed ${display_width}x${display_height} Waydroid surface with host-side scaling"
    gamescope \
        --backend wayland \
        --expose-wayland \
        -w "${display_width}" -h "${display_height}" \
        -W "${display_width}" -H "${display_height}" \
        -S fit -F linear \
        --force-windows-fullscreen \
        -b -- waydroid show-full-ui 7>&- &
else
    info "no graphical login is active; starting a headless ${display_width}x${display_height} Waydroid surface"
    gamescope \
        --backend headless \
        --expose-wayland \
        -w "${display_width}" -h "${display_height}" \
        -W "${display_width}" -H "${display_height}" \
        -S fit -F linear \
        --force-windows-fullscreen \
        -- waydroid show-full-ui 7>&- &
fi
gamescope_pid=$!

if [[ "${desktop_available}" != true ]]; then
    info "headless Gamescope is starting; the caller will verify Android and ADB readiness"
elif configure_gamescope_window; then
    info "Waydroid is floating at ${display_width}x${display_height}; fullscreen scaling is ready"
else
    configure_status=$?
    if (( configure_status == 2 )); then
        die "Gamescope exited before its Hyprland window appeared"
    fi
    die "Gamescope did not create a Hyprland window within 15 seconds"
fi

set +e
wait "${gamescope_pid}"
gamescope_status=$?
set -e
gamescope_pid=""
exit "${gamescope_status}"
