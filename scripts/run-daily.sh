#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
maa="${project_root}/bin/maa"
planner="${project_root}/bin/maa-planner"
scaled_ui="${project_root}/scripts/show-waydroid-scaled.sh"
host_config="${project_root}/config/host.env"
local_config="${project_root}/config/host.local.env"
decision_contract="${project_root}/config/fight-decision.jq"
annihilation_contract="${project_root}/config/annihilation-decision.jq"
annihilation_state="${project_root}/var/state/planner/annihilation.json"

if [[ -r "${host_config}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${host_config}"
    set +a
fi
if [[ -r "${local_config}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${local_config}"
    set +a
fi

# The unified safety validator inspects this exact directory. Do not allow an
# inherited environment variable to redirect tasks, runtime resources, or the
# evidence logs used by this launcher.
export MAA_CONFIG_DIR="${project_root}/config"
export MAA_DATA_DIR="${project_root}/var/data"
export MAA_CACHE_DIR="${MAA_DATA_DIR}/cache"
export MAA_STATE_DIR="${project_root}/var/state"

: "${MAA_HOST_PROFILE:=waydroid}"
: "${MAA_HOST_TASK:=daily}"
: "${MAA_FARM_MODE:=auto}"
: "${MAA_PURE_GOLD_DRONE_THRESHOLD:=150}"
# Completion is accepted only from the INFO callback markers emitted by maa-cli.
# Keep the unified launcher at INFO even if a local environment chose a quieter
# level; raw-run remains available for custom logging experiments.
MAA_LOG=info
export MAA_LOG

display_width=1280
display_height=720
official_package=com.hypergryph.arknights
network_test_url=https://ak.hypergryph.com
stage=""
farm_mode="${MAA_FARM_MODE}"
display_mode="${MAA_WAYDROID_DISPLAY_MODE:-auto}"
dry_run=false
check_device=false
e2e_award=false
verify_proxy=false
pre_reset_slot=false
post_reset_slot=false
pre_reset_fight_deadline_epoch=0
auto_farm_ready=true
planner_helpers_ready=true
farming_contracts_ready=true
annihilation_ready=true
annihilation_phase_outcome=not-applicable
session_started_by_launcher=false
waydroid_ui_pid=""
waydroid_serial=""
waydroid_ui_log=""
core_log="${MAA_STATE_DIR:-${project_root}/var/state}/debug/asst.log"
regular_fallback_stages=(AP-5 1-7)
activity_fight_completed=false
fight_attempted=false
server_timezone=Asia/Shanghai
daily_completed_game_day=""
daily_attempt_timeout=3h
drone_threshold="${MAA_PURE_GOLD_DRONE_THRESHOLD}"
drone_mode=_NotUse
drone_input_index=1
depot_scan_attempted=false
inventory_snapshot_ready=false
depot_scan_outcome=not-attempted
core_log_cursor_args=()
planner_source_args=()

info() {
    printf '[maa-daily] %s\n' "$*"
}

die() {
    printf '[maa-daily] error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

usage() {
    cat <<'EOF'
Usage: scripts/run-daily.sh [--farm auto|off] [--display-mode auto|desktop|headless] [--e2e-award] [--verify-proxy] [--daily-first] [--stage STAGE] [--dry-run] [--check-device]

Run the official-server daily routine in Waydroid at 1280x720.

Options:
  --farm MODE    Automatic event farming mode: auto (default) or off.
  --no-farm      Alias for --farm off.
  --display-mode Select the Waydroid surface backend. auto uses Hyprland when
                 available and otherwise runs unattended with Gamescope headless.
  --e2e-award    Run only the contract-locked ordinary Award claim as a light
                 end-to-end probe; no base, recruitment, shop, mail, or fight.
  --verify-proxy Compatibility audit mode: run each solver-selected candidate
                 at most once. Normal automatic mode already trusts the game
                 client as the ground truth for saved proxy play.
  --daily-first  Compatibility flag; daily-first is now the default order.
  --pre-reset-slot
  --post-reset-slot
                 Scheduler-only guarded slots; prefer --daily-first manually.
  --stage STAGE  Spend sanity on STAGE after the initial daily routine.
                 This is an explicit operator override; the planner cannot veto it.
                 All medicine expiring within two days is allowed; normal
                 medicine and Originite Prime remain disabled.
  --dry-run      Validate static contracts and the promoted generation receipt
                 without starting MaaCore, Waydroid, or the game.
  --check-device Validate Waydroid, ADB, 720p and networking without running MAA.
  -h, --help     Show this help.

Examples:
  ./scripts/run-daily.sh
  ./scripts/run-daily.sh --no-farm
  ./scripts/run-daily.sh --verify-proxy
  ./scripts/run-daily.sh --stage TO-8
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --farm)
            (( $# >= 2 )) || die "--farm requires auto or off"
            farm_mode="$2"
            shift 2
            ;;
        --no-farm)
            farm_mode=off
            shift
            ;;
        --display-mode)
            (( $# >= 2 )) || die "--display-mode requires auto, desktop, or headless"
            display_mode="$2"
            shift 2
            ;;
        --e2e-award)
            e2e_award=true
            farm_mode=off
            shift
            ;;
        --stage)
            (( $# >= 2 )) || die "--stage requires a value"
            stage="$2"
            shift 2
            ;;
        --verify-proxy)
            verify_proxy=true
            shift
            ;;
        --daily-first)
            shift
            ;;
        --pre-reset-slot)
            pre_reset_slot=true
            shift
            ;;
        --post-reset-slot)
            post_reset_slot=true
            shift
            ;;
        --dry-run)
            dry_run=true
            shift
            ;;
        --check-device)
            check_device=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

case "${farm_mode}" in
    auto|off)
        ;;
    *)
        die "invalid farming mode: ${farm_mode} (expected auto or off)"
        ;;
esac
case "${display_mode}" in
    auto|desktop|headless)
        ;;
    *)
        die "invalid display mode: ${display_mode} (expected auto, desktop, or headless)"
        ;;
esac
export MAA_WAYDROID_DISPLAY_MODE="${display_mode}"
if [[ ! "${drone_threshold}" =~ ^[0-9]{1,9}$ ]]; then
    die "invalid Pure Gold drone threshold: ${drone_threshold}"
fi
drone_threshold="$(( 10#${drone_threshold} ))"

if [[ "${verify_proxy}" == true && -n "${stage}" ]]; then
    die "--verify-proxy and --stage are mutually exclusive"
fi
if [[ "${e2e_award}" == true &&
      ( -n "${stage}" || "${verify_proxy}" == true ||
        "${pre_reset_slot}" == true || "${post_reset_slot}" == true ||
        "${check_device}" == true ) ]]; then
    die "--e2e-award cannot be combined with stage, proxy, scheduler-slot, or device-only modes"
fi
if [[ "${verify_proxy}" == true && "${farm_mode}" != auto ]]; then
    die "--verify-proxy requires --farm auto"
fi
if [[ "${verify_proxy}" == true && "${check_device}" == true ]]; then
    die "--verify-proxy and --check-device are mutually exclusive"
fi
if [[ "${stage}" == Annihilation ]]; then
    die "--stage Annihilation is reserved for the weekly planner and cannot bypass its one-transaction checks"
fi
if [[ "${pre_reset_slot}" == true && "${post_reset_slot}" == true ]]; then
    die "--pre-reset-slot and --post-reset-slot are mutually exclusive"
fi
if [[ "${pre_reset_slot}" == true ]]; then
    # Bound a stuck first attempt; retry remains allowed only before any
    # state-changing daily phase has started.
    daily_attempt_timeout=22m
fi

[[ "${MAA_HOST_TASK}" == daily ]] ||
    die "the unified launcher always runs task=daily; use maa-host raw-run for other tasks"
[[ "${MAA_HOST_PROFILE}" == waydroid ]] ||
    die "the unified launcher always uses the validated waydroid profile; use maa-host raw-run for other profiles"

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

waydroid_session_is_running() {
    timeout --signal=TERM --kill-after=2s 5s waydroid status 2>/dev/null |
        grep -Eq '^Session:[[:space:]]+RUNNING$'
}

cleanup() {
    local status=$?
    local attempt

    trap - EXIT INT TERM HUP

    if [[ -n "${waydroid_serial}" ]]; then
        timeout --signal=TERM --kill-after=2s 5s \
            adb disconnect "${waydroid_serial}" >/dev/null 2>&1 || true
    fi

    if [[ "${session_started_by_launcher}" == true ]] && waydroid_session_is_running; then
        info "stopping the Waydroid session started by this launcher"
        timeout --signal=TERM --kill-after=5s 15s waydroid session stop >/dev/null 2>&1 || true
    fi

    if [[ -n "${waydroid_ui_pid}" ]] && kill -0 "${waydroid_ui_pid}" 2>/dev/null; then
        for attempt in {1..20}; do
            kill -0 "${waydroid_ui_pid}" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "${waydroid_ui_pid}" 2>/dev/null; then
            kill -TERM "${waydroid_ui_pid}" 2>/dev/null || true
        fi
        for attempt in {1..20}; do
            kill -0 "${waydroid_ui_pid}" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "${waydroid_ui_pid}" 2>/dev/null; then
            kill -KILL "${waydroid_ui_pid}" 2>/dev/null || true
            for attempt in {1..8}; do
                kill -0 "${waydroid_ui_pid}" 2>/dev/null || break
                sleep 0.25
            done
        fi
        if ! kill -0 "${waydroid_ui_pid}" 2>/dev/null; then
            wait "${waydroid_ui_pid}" 2>/dev/null || true
        fi
    fi

    exit "${status}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

wait_for_waydroid() {
    local deadline=$(( SECONDS + 120 ))
    local address adb_state boot_completed status_output

    while (( SECONDS < deadline )); do
        if [[ "${session_started_by_launcher}" == true && -n "${waydroid_ui_pid}" ]] &&
           ! kill -0 "${waydroid_ui_pid}" 2>/dev/null; then
            return 2
        fi

        status_output="$(timeout --signal=TERM --kill-after=2s 5s waydroid status 2>/dev/null || true)"
        if grep -Eq '^Session:[[:space:]]+RUNNING$' <<<"${status_output}" &&
           grep -Eq '^Container:[[:space:]]+RUNNING$' <<<"${status_output}"; then
            address="$(awk '/^IP address:/ { print $3; exit }' <<<"${status_output}")"
            if [[ "${address}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
                waydroid_serial="${address}:5555"
                timeout --signal=TERM --kill-after=2s 8s \
                    waydroid adb connect >/dev/null 2>&1 || true
                adb_state="$(timeout --signal=TERM --kill-after=2s 5s adb devices 2>/dev/null |
                    awk -v serial="${waydroid_serial}" '$1 == serial { print $2; exit }')"
                case "${adb_state}" in
                    device)
                        boot_completed="$(timeout --signal=TERM --kill-after=2s 5s \
                            adb -s "${waydroid_serial}" shell getprop sys.boot_completed 2>/dev/null |
                            tr -d '\r')"
                        if [[ "${boot_completed}" == 1 ]]; then
                            return 0
                        fi
                        ;;
                    offline)
                        timeout --signal=TERM --kill-after=2s 5s \
                            adb disconnect "${waydroid_serial}" >/dev/null 2>&1 || true
                        ;;
                    unauthorized)
                        info "ADB authorization is waiting; accept the dialog in Waydroid"
                        ;;
                esac
            fi
        fi
        sleep 2
    done

    return 1
}

network_is_ready() {
    timeout --signal=TERM --kill-after=2s 12s adb -s "${waydroid_serial}" shell \
        "curl -kfsSI --connect-timeout 4 --max-time 8 '${network_test_url}' >/dev/null" \
        >/dev/null 2>&1
}

run_with_timeout() {
    local duration="$1"
    shift
    local status

    set +e
    timeout --signal=INT --kill-after=30s "${duration}" "$@" 8>&- 9>&-
    status=$?
    set -e

    case "${status}" in
        0)
            return 0
            ;;
        124)
            die "command timed out after ${duration}: $*"
            ;;
        *)
            die "command failed with status ${status}: $*"
            ;;
    esac
}

run_soft_with_timeout() {
    local duration="$1"
    shift
    local status

    set +e
    timeout --signal=INT --kill-after=30s "${duration}" "$@" 8>&- 9>&-
    status=$?
    set -e

    case "${status}" in
        0)
            return 0
            ;;
        124)
            info "optional command timed out after ${duration}: $*"
            ;;
        *)
            info "optional command failed with status ${status}: $*"
            ;;
    esac
    return "${status}"
}

farming_timeout_seconds() {
    local maximum_seconds="$1"
    local now_epoch remaining_seconds

    [[ "${maximum_seconds}" =~ ^[0-9]+$ ]] || return 1
    if (( pre_reset_fight_deadline_epoch <= 0 )); then
        printf '%s\n' "${maximum_seconds}"
        return 0
    fi
    now_epoch="$(date -u '+%s')"
    # timeout may need its 30-second kill grace. Stop the command five seconds
    # before the policy cutoff even if SIGINT is ignored.
    remaining_seconds=$(( pre_reset_fight_deadline_epoch - now_epoch - 35 ))
    if (( remaining_seconds < 60 )); then
        return 1
    fi
    if (( maximum_seconds < remaining_seconds )); then
        remaining_seconds="${maximum_seconds}"
    fi
    printf '%s\n' "${remaining_seconds}"
}

run_farming_soft_with_timeout() {
    local maximum_seconds="$1"
    local duration_seconds
    shift

    if ! duration_seconds="$(farming_timeout_seconds "${maximum_seconds}")"; then
        info "pre-reset farming window is closed; preserving time for final Award"
        return 1
    fi
    run_soft_with_timeout "${duration_seconds}s" "$@"
}

scan_depot_inventory_once() {
    local log_file="$1"
    local command_succeeded=false

    if [[ "${depot_scan_attempted}" == true ]]; then
        info "refusing a second Depot scan in the same launcher run"
        return 1
    fi

    depot_scan_attempted=true
    depot_scan_outcome=scan-failed
    if run_farming_soft_with_timeout 1800 "${maa}" --batch --log-file="${log_file}" \
        run depot --profile "${MAA_HOST_PROFILE}"; then
        command_succeeded=true
    fi

    if [[ "${command_succeeded}" == true ]]; then
        depot_scan_outcome=snapshot-invalid
    fi
    if ! run_farming_soft_with_timeout 60 "${planner}" inventory-from-log \
        --log "${log_file}"; then
        if [[ "${command_succeeded}" == true ]]; then
            info "Depot task completed but produced no valid inventory snapshot"
        else
            info "combined startup and Depot task failed before producing a valid snapshot"
        fi
        return 1
    fi

    depot_scan_outcome=ready
    inventory_snapshot_ready=true
}

proxy_preflight_log_is_complete() {
    local log_file="$1"

    [[ -s "${log_file}" ]] || return 1
    [[ "$(grep -Fc -- "Fight Start" "${log_file}")" -eq 1 ]] || return 1
    [[ "$(grep -Fc -- "Fight Completed" "${log_file}")" -eq 1 ]] || return 1
    [[ "$(grep -Fc -- "Custom Start" "${log_file}")" -eq 1 ]] || return 1
    [[ "$(grep -Fc -- "Custom Completed" "${log_file}")" -eq 1 ]] || return 1
    grep -Fq -- "AllTasksCompleted" "${log_file}" || return 1
    ! grep -Eq -- "(Fight|Custom) Error" "${log_file}"
}

game_client_has_saved_proxy() {
    local stage_code="$1"
    local log_prefix="$2"
    local preflight_log="${log_prefix}-proxy-preflight-${stage_code}.log"

    info "checking ${stage_code} navigation and saved proxy in one zero-battle MAA task"
    if ! printf '%s\n' "${stage_code}" |
       run_farming_soft_with_timeout 1080 "${maa}" \
           --log-file="${preflight_log}" run proxy-preflight \
           --profile "${MAA_HOST_PROFILE}"; then
        info "${stage_code} proxy preflight was unavailable"
        return 1
    fi
    if ! proxy_preflight_log_is_complete "${preflight_log}"; then
        info "${stage_code} proxy preflight returned without complete zero-battle and PRTS evidence"
        return 1
    fi

    info "the game client confirmed saved proxy play on ${stage_code}"
}

run_sanity_fight() {
    local stage_code="$1"
    local log_file="$2"
    local maximum_seconds="${3:-14400}"
    local duration_seconds

    info "running ${stage_code} with all medicine expiring within two days; normal medicine and Originite Prime remain disabled"
    if ! duration_seconds="$(farming_timeout_seconds "${maximum_seconds}")"; then
        info "pre-reset Fight window is closed; preserving time for final Award"
        return 1
    fi
    fight_attempted=true
    printf '%s\n' "${stage_code}" |
        run_soft_with_timeout "${duration_seconds}s" "${maa}" --log-file="${log_file}" \
            run sanity-fight --profile "${MAA_HOST_PROFILE}"
}

run_verify_fight() {
    local stage_code="$1"
    local log_file="$2"
    local duration_seconds

    info "verifying one ${stage_code} proxy result with native two-day medicine parameters"
    if ! duration_seconds="$(farming_timeout_seconds 5400)"; then
        info "pre-reset Fight window is closed; skipping proxy verification"
        return 1
    fi
    fight_attempted=true
    printf '%s\n' "${stage_code}" |
        run_soft_with_timeout "${duration_seconds}s" "${maa}" --log-file="${log_file}" \
            run verify-fight --profile "${MAA_HOST_PROFILE}"
}

capture_core_log_cursor() {
    local metadata device inode size

    core_log_cursor_args=()
    fight_core_offset=0
    if [[ ! -e "${core_log}" ]]; then
        core_log_cursor_args=(--log-was-missing)
        return 0
    fi
    [[ -f "${core_log}" ]] || return 1
    metadata="$(stat -Lc '%d %i %s' -- "${core_log}" 2>/dev/null)" || return 1
    read -r device inode size <<<"${metadata}"
    [[ "${device}" =~ ^[0-9]+$ && "${inode}" =~ ^[0-9]+$ && "${size}" =~ ^[0-9]+$ ]] || return 1
    fight_core_offset="${size}"
    core_log_cursor_args=(--log-device "${device}" --log-inode "${inode}")
}

active_activity_instance_for_stage() {
    local requested_stage="$1"
    local source_file="${project_root}/var/state/planner/latest-sources.json"
    local now_epoch

    [[ -r "${source_file}" ]] || return 1
    now_epoch="$(date -u '+%s')"
    jq -er --arg stage "${requested_stage}" --argjson now "${now_epoch}" '
        [
            .activities[]
            | select(.client == "Official")
            | select((.start | fromdateiso8601) <= $now and $now < (.end | fromdateiso8601))
            | select(any(.stages[]?; .code == $stage))
            | .instance_id
            | select(type == "string" and test("^[0-9a-f]{24}$"))
        ]
        | if length == 1 then .[0] else empty end
    ' "${source_file}"
}

daily_log_is_complete() {
    local log_file="$1"
    local marker
    local -a required_markers=(
        "Mall Completed"
        "AllTasksCompleted"
    )

    [[ -s "${log_file}" ]] || return 1
    # Two isolated base task chains are required: ordinary facilities first,
    # then the unstationed-only Dorm phase. Exact counts keep a future config
    # merge from silently collapsing the training-room safety boundary.
    [[ "$(grep -Fc -- "Infrast Start" "${log_file}")" -eq 2 ]] || return 1
    [[ "$(grep -Fc -- "Infrast Completed" "${log_file}")" -eq 2 ]] || return 1
    [[ "$(grep -Fc -- "EnterFacility Dorm #" "${log_file}")" -eq 4 ]] || return 1
    ! grep -Fq -- "EnterFacility Training" "${log_file}" || return 1
    [[ "$(grep -Fc -- "Recruit Completed" "${log_file}")" -eq 2 ]] || return 1
    for marker in "${required_markers[@]}"; do
        grep -Fq -- "${marker}" "${log_file}" || return 1
    done
}

daily_log_is_safe_to_retry() {
    local log_file="$1"

    # A retry is permitted only when the first process produced a readable log
    # and no state-changing daily phase ever started. Once any base,
    # recruitment, or shop marker exists, replaying the monolithic daily would
    # repeat already-applied work and is therefore forbidden.
    [[ -s "${log_file}" ]] || return 1
    ! grep -Eq -- \
        "(Infrast|Recruit|Mall) (Start|Completed|Error|Stopped)|EnterFacility " \
        "${log_file}"
}

award_only_log_is_complete() {
    local log_file="$1"

    [[ -s "${log_file}" ]] || return 1
    grep -Fq -- "Award Completed" "${log_file}" || return 1
    grep -Fq -- "AllTasksCompleted" "${log_file}" || return 1
    ! grep -Eq -- "(Infrast|Recruit|Mall|Fight|Depot) (Start|Completed)" "${log_file}"
}

run_award_only() {
    local purpose="$1"
    local log_suffix="$2"
    local stamp log_file

    stamp="$(date '+%Y%m%d-%H%M%S-%N')"
    log_file="${project_root}/var/state/host/${stamp}-${log_suffix}.log"
    info "running the Award-only ${purpose}"
    info "MAA log: ${log_file}"
    run_with_timeout 30m "${maa}" --log-file="${log_file}" \
        run award-only --profile "${MAA_HOST_PROFILE}"
    award_only_log_is_complete "${log_file}" ||
        die "Award-only ${purpose} returned without isolated completion evidence; inspect ${log_file}"
    info "Award-only ${purpose} completed with no base, recruitment, shop, mail, or fight task"
}

server_game_day_now() {
    # The CN server changes game day at 04:00, not at civil midnight.
    TZ="${server_timezone}" date --date='4 hours ago' '+%F'
}

run_daily_routine() {
    local daily_stamp daily_log retry_log
    local started_game_day completed_game_day

    started_game_day="$(server_game_day_now)"
    info "running the daily routine for game day ${started_game_day} (04:00 reset)"
    daily_stamp="$(date '+%Y%m%d-%H%M%S-%N')"
    daily_log="${project_root}/var/state/host/${daily_stamp}-daily.log"
    info "MAA log: ${daily_log}"
    if printf '%s\n' "${drone_input_index}" |
       run_soft_with_timeout "${daily_attempt_timeout}" "${maa}" --log-file="${daily_log}" \
           run "${MAA_HOST_TASK}" --profile "${MAA_HOST_PROFILE}" &&
       daily_log_is_complete "${daily_log}"; then
        info "daily first attempt has complete protected task-chain evidence"
    else
        if ! daily_log_is_safe_to_retry "${daily_log}"; then
            die "daily lost completion proof after stateful work started; refusing to replay base, recruitment, or shop; inspect ${daily_log}"
        fi
        retry_log="${project_root}/var/state/host/${daily_stamp}-daily-retry.log"
        info "daily stopped before any stateful phase; retrying once is safe"
        info "MAA retry log: ${retry_log}"
        printf '%s\n' "${drone_input_index}" |
            run_with_timeout "${daily_attempt_timeout}" "${maa}" --log-file="${retry_log}" \
                run "${MAA_HOST_TASK}" --profile "${MAA_HOST_PROFILE}"
        daily_log_is_complete "${retry_log}" ||
            die "daily retry returned without complete task-chain evidence; inspect ${retry_log}"
    fi

    completed_game_day="$(server_game_day_now)"
    if [[ "${completed_game_day}" == "${started_game_day}" ]]; then
        daily_completed_game_day="${completed_game_day}"
        info "daily protected Infrast phases, Recruit, Mall and all task chains completed for game day ${completed_game_day}"
    else
        daily_completed_game_day=""
        info "daily completion crossed the 04:00 reset (${started_game_day} -> ${completed_game_day}); a fresh daily pass is required"
    fi
    # daily deliberately has closedown=false: all later maa-cli invocations
    # reuse the launcher-owned Waydroid session and the already running game.
}

annihilation_decision_is_safe() {
    local decision_file="$1"

    [[ -r "${annihilation_contract}" ]] || return 1
    jq -e -f "${annihilation_contract}" "${decision_file}" >/dev/null 2>&1
}

run_weekly_annihilation_if_due() {
    local stamp decision_file decision reason due_at
    local max_transactions transaction transaction_log fight_core_offset
    local previous_current observed_current observed_total observed_status observed_stars
    local command_succeeded planned_week planned_client planned_account
    local execute_before execute_before_epoch now_epoch remaining_seconds
    local transaction_timeout_seconds

    if [[ "${farm_mode}" != auto || "${annihilation_ready}" != true ]]; then
        return 0
    fi

    # Annihilation is a best-effort side phase, never a gate for ordinary
    # material farming. Only fresh client weekly-cap proof is persisted; an
    # already-empty ToDo entry and a navigation/recognition failure are both
    # unproven here, so either outcome stops this phase but not the main flow.
    annihilation_phase_outcome=planning
    stamp="$(date '+%Y%m%d-%H%M%S-%N')"
    decision_file="${project_root}/var/state/planner/annihilation-launcher-${stamp}.json"
    info "planning this game week's Annihilation from the activity calendar"
    if ! run_farming_soft_with_timeout 900 "${planner}" plan-annihilation \
        "${planner_source_args[@]}" --skip-maa-hot-update \
        --output "${decision_file}"; then
        annihilation_phase_outcome=planner-failed
        info "weekly Annihilation skipped because its deterministic planner failed"
        return 0
    fi
    if ! annihilation_decision_is_safe "${decision_file}"; then
        annihilation_phase_outcome=invalid-decision
        info "weekly Annihilation skipped because its decision failed launcher validation"
        return 0
    fi

    IFS=$'\t' read -r decision reason due_at < <(
        jq -r '[.decision, .reason, (.due_at // "completed")] | @tsv' \
            "${decision_file}"
    )
    case "${decision}" in
        COMPLETE)
            annihilation_phase_outcome=weekly-cap-confirmed
            info "weekly Annihilation already has fresh client weekly-cap proof"
            return 0
            ;;
        BLOCKED)
            annihilation_phase_outcome=blocked
            info "weekly Annihilation is blocked for this game week by an explicit client two-star result"
            return 0
            ;;
        MISSED)
            annihilation_phase_outcome=missed
            info "weekly Annihilation has less than one complete transaction window before this game week resets"
            return 0
            ;;
        WAIT)
            annihilation_phase_outcome=waiting
            info "weekly Annihilation WAIT (${reason}); planned time: ${due_at}"
            return 0
            ;;
        RUN)
            annihilation_phase_outcome=due
            info "weekly Annihilation is due (${reason})"
            ;;
        *)
            annihilation_phase_outcome=invalid-decision
            info "weekly Annihilation skipped because the decision is unknown: ${decision}"
            return 0
            ;;
    esac

    IFS=$'\t' read -r max_transactions transaction_timeout_seconds \
        planned_week planned_client planned_account execute_before < <(
        jq -r '[
            .max_transactions_per_run,
            .transaction_timeout_seconds,
            .week_start_game_day,
            .client,
            .account,
            .execute_before
        ] | @tsv' "${decision_file}"
    )
    execute_before_epoch="$(date -u -d "${execute_before}" '+%s' 2>/dev/null || printf '%s' 0)"
    if [[ ! "${execute_before_epoch}" =~ ^[0-9]+$ ]] || (( execute_before_epoch <= 0 )); then
        annihilation_phase_outcome=invalid-deadline
        info "weekly Annihilation skipped because its execution deadline is invalid"
        return 0
    fi
    if (( pre_reset_fight_deadline_epoch > 0 &&
          pre_reset_fight_deadline_epoch < execute_before_epoch )); then
        execute_before_epoch="${pre_reset_fight_deadline_epoch}"
        execute_before="03:25 ${server_timezone} pre-reset Fight cutoff"
    fi
    now_epoch="$(date -u '+%s')"
    remaining_seconds=$(( execute_before_epoch - now_epoch - 35 ))
    if (( remaining_seconds < transaction_timeout_seconds )); then
        annihilation_phase_outcome=deferred
        info "weekly Annihilation waits because a complete ${transaction_timeout_seconds}s transaction no longer fits before ${execute_before}"
        return 0
    fi

    previous_current=-1
    if [[ -r "${annihilation_state}" ]]; then
        previous_current="$(jq -r \
            --arg week "${planned_week}" \
            --arg client "${planned_client}" \
            --arg account "${planned_account}" '
            if .schema_version == 1 and .status == "progress"
               and .week_start_game_day == $week
               and .client == $client
               and .account == $account
               and (.current | type == "number" and . == floor and . >= 0)
            then .current else -1 end
        ' "${annihilation_state}" 2>/dev/null || printf '%s' -1)"
    fi

    for (( transaction = 1; transaction <= max_transactions; transaction++ )); do
        now_epoch="$(date -u '+%s')"
        remaining_seconds=$(( execute_before_epoch - now_epoch - 35 ))
        if (( remaining_seconds < transaction_timeout_seconds )); then
            info "weekly Annihilation stops because another complete ${transaction_timeout_seconds}s transaction does not fit before ${execute_before}"
            break
        fi
        stamp="$(date '+%Y%m%d-%H%M%S-%N')"
        transaction_log="${project_root}/var/state/host/${stamp}-annihilation-${transaction}.log"
        if ! capture_core_log_cursor; then
            annihilation_phase_outcome=evidence-unavailable
            info "weekly Annihilation skipped because a fresh MaaCore log cursor could not be captured"
            break
        fi
        command_succeeded=false
        fight_attempted=true
        info "weekly Annihilation transaction ${transaction}/${max_transactions}; client proxy state is re-checked"
        if run_soft_with_timeout "${transaction_timeout_seconds}s" "${maa}" --batch \
            --log-file="${transaction_log}" run annihilation \
            --profile "${MAA_HOST_PROFILE}"; then
            command_succeeded=true
        fi

        if ! run_soft_with_timeout 1m "${planner}" record-annihilation \
            --log "${core_log}" --since-byte "${fight_core_offset}" \
            "${core_log_cursor_args[@]}" \
            --week-start-game-day "${planned_week}"; then
            if [[ "${command_succeeded}" == true ]]; then
                annihilation_phase_outcome=weekly-state-unknown
                info "Annihilation returned without strong weekly-progress proof; it may already be complete, but this run will not guess or persist that state"
            else
                annihilation_phase_outcome=transaction-failed
                info "Annihilation transaction failed without client weekly-progress proof; its weekly state remains unknown"
            fi
            break
        fi

        IFS=$'\t' read -r observed_status observed_current observed_total \
            observed_stars < <(
            jq -r '[.status, .current, .total, .evidence.stars] | @tsv' \
                "${annihilation_state}"
        )
        if [[ "${observed_stars}" == 2 ]]; then
            annihilation_phase_outcome=blocked
            info "weekly progress was recorded, but the client reported an explicit two-star result; stopping further transactions"
        elif [[ "${observed_stars}" == 0 ]]; then
            info "weekly progress was recorded; star-template OCR was inconclusive"
        fi
        if [[ "${observed_status}" == complete ]]; then
            annihilation_phase_outcome=weekly-cap-confirmed
            info "weekly Annihilation reached the client-reported cap: ${observed_current}/${observed_total}"
            break
        fi
        if [[ "${observed_stars}" == 2 ]]; then
            break
        fi
        annihilation_phase_outcome=partial-progress
        info "weekly Annihilation client progress: ${observed_current}/${observed_total}"
        if (( observed_current <= previous_current )); then
            annihilation_phase_outcome=progress-stalled
            info "weekly progress did not advance; stopping transactions until the next launcher run"
            break
        fi
        previous_current="${observed_current}"
    done

    # Keep this explicit success return: every Annihilation outcome, including
    # unknown/failure, must hand control back to normal material farming.
    return 0
}

run_regular_fallback() {
    local stage_code fallback_stamp fallback_log fight_core_offset

    if [[ "${farming_contracts_ready}" != true ]]; then
        info "regular-stage fallback skipped because the sanity-spending contract is invalid"
        return 1
    fi
    if [[ "${planner_helpers_ready}" != true ]]; then
        info "regular-stage fallback skipped because fresh fight proof cannot be checked"
        return 1
    fi
    for stage_code in "${regular_fallback_stages[@]}"; do
        fallback_stamp="$(date '+%Y%m%d-%H%M%S-%N')"
        fallback_log="${project_root}/var/state/host/${fallback_stamp}-fallback-${stage_code}.log"
        if ! game_client_has_saved_proxy "${stage_code}" \
            "${project_root}/var/state/host/${fallback_stamp}-fallback"; then
            info "${stage_code} has no confirmed client proxy; trying the next fallback"
            continue
        fi
        if ! capture_core_log_cursor; then
            info "${stage_code} skipped because a fresh MaaCore log cursor could not be captured"
            continue
        fi
        info "regular fallback trying ${stage_code}"
        run_sanity_fight "${stage_code}" "${fallback_log}" || true
        if timeout --signal=TERM --kill-after=2s 1m "${planner}" check-fight \
            --log "${core_log}" --since-byte "${fight_core_offset}" \
            "${core_log_cursor_args[@]}" \
            --stage "${stage_code}" 8>&- 9>&- >/dev/null 2>&1; then
            info "regular fallback selected ${stage_code} and has fresh three-star proof"
            return 0
        fi
        info "${stage_code} was unavailable or produced no fresh proof; trying the next fallback"
        if [[ "${pre_reset_slot}" == true && "${fight_attempted}" == true ]]; then
            info "pre-reset Fight was attempted; returning immediately for final Award"
            break
        fi
    done
    return 1
}

activity_decision_is_safe() {
    local decision_file="$1"

    [[ -r "${decision_contract}" ]] || return 1
    jq -e -f "${decision_contract}" "${decision_file}" >/dev/null 2>&1
}

run_planned_activity_candidates() {
    local decision_file="$1"
    local farm_stamp="$2"
    local stage_code item_id activity_instance
    local candidate_log fight_core_offset
    local reconciliation outcome recorded

    while IFS=$'\t' read -r stage_code item_id activity_instance; do
        if ! game_client_has_saved_proxy "${stage_code}" \
            "${project_root}/var/state/host/${farm_stamp}"; then
            info "local state is unchanged; trying the next activity candidate"
            continue
        fi
        candidate_log="${project_root}/var/state/host/${farm_stamp}-farm-${stage_code}.log"
        info "automatic farming trying ${stage_code} after the client proxy preflight"
        if ! capture_core_log_cursor; then
            info "${stage_code} skipped because a fresh MaaCore log cursor could not be captured"
            continue
        fi
        if [[ "${verify_proxy}" == true ]]; then
            run_verify_fight "${stage_code}" "${candidate_log}"
        else
            run_sanity_fight "${stage_code}" "${candidate_log}"
        fi || {
            info "${stage_code} was unavailable or the fight command failed; inspecting fresh client evidence"
        }

        reconciliation=""
        if reconciliation="$(timeout --signal=TERM --kill-after=2s 1m \
            "${planner}" reconcile-fight \
                --log "${core_log}" --since-byte "${fight_core_offset}" \
                "${core_log_cursor_args[@]}" \
                --stage "${stage_code}" --activity-instance "${activity_instance}" \
                8>&- 9>&- 2>/dev/null)" &&
           IFS=$'\t' read -r outcome recorded < <(
               jq -er '[.outcome, .recorded] | @tsv' <<<"${reconciliation}" 2>/dev/null
           ); then
            case "${outcome}" in
                verified)
                    activity_fight_completed=true
                    info "${stage_code} has fresh three-star completion proof"
                    if [[ "${recorded}" != true ]]; then
                        info "fresh fight proof was accepted, but audit bookkeeping failed"
                    fi
                    if [[ "${verify_proxy}" == true ]]; then
                        info "proxy verification succeeded; continuing the selected stage without a medicine-count stop"
                        if ! run_sanity_fight "${stage_code}" \
                            "${project_root}/var/state/host/${farm_stamp}-farm-${stage_code}-consume.log"; then
                            info "verified stage completed once, but its unlimited 48-hour medicine continuation stopped early"
                        fi
                    fi
                    return 0
                    ;;
                quarantined)
                    if [[ "${recorded}" == true ]]; then
                        info "${stage_code} produced an explicit non-three-star result and is quarantined for this activity"
                    else
                        info "${stage_code} produced an explicit non-three-star result, but quarantine bookkeeping failed"
                    fi
                    ;;
                unknown)
                    info "${stage_code} produced no stable proxy proof; local capability state is unchanged"
                    ;;
                *)
                    info "${stage_code} produced an invalid reconciliation result; local capability state is unchanged"
                    ;;
            esac
        else
            info "${stage_code} fight evidence could not be reconciled; local capability state is unchanged"
        fi
        if [[ "${pre_reset_slot}" == true && "${fight_attempted}" == true ]]; then
            info "pre-reset Fight was attempted; returning immediately for final Award"
            return 1
        fi
    done < <(
        jq -r '
            .evidence.execution_candidates[]
            | [
                .stage_code,
                .item_id,
                .activity_instance
            ]
            | @tsv
        ' "${decision_file}"
    )

    return 1
}

refresh_planner_sources_if_needed() {
    local refresh_succeeded=false

    if [[ "${check_device}" == true || "${planner_helpers_ready}" != true ]]; then
        return 0
    fi
    if [[ ! ( ( "${farm_mode}" == auto &&
                ( "${annihilation_ready}" == true ||
                  ( -z "${stage}" && "${auto_farm_ready}" == true ) ) ) ||
              ( -n "${stage}" && "${farming_contracts_ready}" == true ) ) ]]; then
        return 0
    fi

    # Refresh network sources exactly once. Every later planner in this
    # launcher run revalidates the resulting cache offline, so Annihilation and
    # material planning cannot each redownload the same MAA/official payloads.
    if [[ "${pre_reset_slot}" == true || -n "${stage}" ||
          "${auto_farm_ready}" != true ]]; then
        info "refreshing the shared activity calendar once; this path does not need a new efficiency download"
        if run_farming_soft_with_timeout 300 "${planner}" sync-calendar; then
            refresh_succeeded=true
        fi
    else
        info "refreshing all planner sources once for this launcher run"
        if run_soft_with_timeout 15m "${planner}" sync --skip-maa-hot-update; then
            refresh_succeeded=true
        fi
    fi

    # Do not immediately retry an unavailable endpoint from two downstream
    # planner processes. Fresh, checksum-validated cache may still work; stale
    # or incomplete cache deterministically fails closed to NOOP/fallback.
    planner_source_args=(--offline)
    if [[ "${refresh_succeeded}" != true ]]; then
        info "the one source refresh attempt failed; downstream planners will inspect cache offline without a network retry storm"
    fi
}

select_daily_drone_policy_from_snapshot() {
    local selected

    drone_mode=_NotUse
    drone_input_index=1
    if [[ ! -x "${planner}" ]]; then
        info "drone policy fallback: planner unavailable; drones disabled"
        return 1
    fi
    selected="$(timeout --signal=TERM --kill-after=2s 1m "${planner}" \
        select-drones --threshold "${drone_threshold}" --value-only 2>/dev/null || true)"
    case "${selected}" in
        PureGold)
            drone_mode=PureGold
            drone_input_index=2
            ;;
        Money)
            drone_mode=Money
            drone_input_index=3
            ;;
        *)
            info "drone policy fallback: fresh Pure Gold inventory is unavailable; drones disabled"
            return 1
            ;;
    esac
    info "drone policy selected ${drone_mode}: Pure Gold threshold is ${drone_threshold}"
}

prepare_daily_drone_policy() {
    local stamp depot_log

    if [[ "${farming_contracts_ready}" != true ]]; then
        drone_mode=_NotUse
        drone_input_index=1
        info "drone Depot skipped because its managed-task contract is invalid"
        return 0
    fi

    if [[ "${pre_reset_slot}" == true ]]; then
        drone_mode=_NotUse
        drone_input_index=1
        info "pre-reset skips only the auxiliary drone Depot; the complete old-game-day daily still runs"
        return 0
    fi

    stamp="$(date '+%Y%m%d-%H%M%S-%N')"
    depot_log="${project_root}/var/state/host/${stamp}-pre-daily-depot.log"
    info "starting the game and taking the run's single Depot snapshot in one MAA task chain"
    if ! scan_depot_inventory_once "${depot_log}"; then
        case "${depot_scan_outcome}" in
            scan-failed)
                info "drone policy fallback: the combined startup and Depot task failed; drones disabled"
                ;;
            snapshot-invalid)
                info "drone policy fallback: the run's inventory snapshot is invalid; drones disabled"
                ;;
            *)
                info "drone policy fallback: inventory is unavailable; drones disabled"
                ;;
        esac
    else
        select_daily_drone_policy_from_snapshot || true
    fi
}

ensure_farming_inventory_snapshot() {
    local depot_log="$1"

    if [[ "${inventory_snapshot_ready}" == true ]]; then
        info "reusing the run's pre-daily Depot snapshot for automatic farming; no second scan"
        return 0
    fi
    if [[ "${depot_scan_attempted}" == true ]]; then
        info "automatic farming skipped because the run's only Depot scan produced no valid snapshot"
        return 1
    fi

    info "no Depot scan has been attempted in this run; taking the single scan now"
    if scan_depot_inventory_once "${depot_log}"; then
        return 0
    fi
    case "${depot_scan_outcome}" in
        scan-failed)
            info "automatic farming skipped because the run's Depot scan failed"
            ;;
        snapshot-invalid)
            info "automatic farming skipped because no complete inventory snapshot was produced"
            ;;
        *)
            info "automatic farming skipped because inventory is unavailable"
            ;;
    esac
    return 1
}

server_minute_of_day_now() {
    local hour minute

    read -r hour minute < <(TZ="${server_timezone}" date '+%H %M')
    [[ "${hour}" =~ ^[0-9]{2}$ && "${minute}" =~ ^[0-9]{2}$ ]] ||
        die "cannot read the ${server_timezone} server clock"
    printf '%s\n' "$(( 10#${hour} * 60 + 10#${minute} ))"
}

stop_user_service_if_active() {
    local unit="$1"

    if systemctl --user is-active --quiet "${unit}"; then
        info "stopping conflicting scheduled slot: ${unit}"
        timeout --signal=TERM --kill-after=5s 6m \
            systemctl --user stop "${unit}" ||
            die "could not stop conflicting scheduled slot: ${unit}"
    fi
}

require_command adb
require_command flock
require_command grep
require_command systemctl
require_command timeout
require_command waydroid
[[ -x "${maa}" ]] || die "MAA wrapper is not executable: ${maa}"
[[ -x "${scaled_ui}" ]] || die "scaled Waydroid launcher is not executable: ${scaled_ui}"

if [[ "${dry_run}" != true && "${pre_reset_slot}" == true ]]; then
    server_minute="$(server_minute_of_day_now)"
    if (( server_minute < 180 || server_minute >= 185 )); then
        info "missed the 03:00-03:04 pre-reset start window; old-game-day work is not recoverable now"
        exit 0
    fi
    pre_reset_fight_deadline_epoch="$(
        TZ="${server_timezone}" date --date="$(TZ="${server_timezone}" date '+%F') 03:25:00" '+%s'
    )"
    stop_user_service_if_active maa-waydroid.service
elif [[ "${dry_run}" != true && "${post_reset_slot}" == true ]]; then
    server_minute="$(server_minute_of_day_now)"
    if (( server_minute >= 180 && server_minute < 240 )); then
        info "post-reset catch-up suppressed during the 03:00-04:00 old-game-day protection window"
        exit 0
    fi
    stop_user_service_if_active maa-waydroid-prereset.service
fi

mkdir -p -- "${project_root}/var/run" "${project_root}/var/state/host"
exec 9>"${project_root}/var/run/maa-daily.lock"
flock -n 9 || die "another one-click daily run is already active"
exec 8>"${project_root}/var/run/maa-host.lock"
flock -n 8 || die "another MAA run is already active"

if [[ "${check_device}" != true ]]; then
    [[ -x "${planner}" ]] || die "planner wrapper is not executable: ${planner}"
    info "checking the promoted runtime generation receipt without starting MaaCore"
    readiness_fields="$("${planner}" validate-service-readiness --value-only 8>&- 9>&-)" ||
        die "live runtime/config no longer matches a validated generation; run bin/maa-host runtime-update"
    IFS=$'\t' read -r receipt_mode farming_contracts_ready <<<"${readiness_fields}"
    [[ -n "${receipt_mode}" &&
       ( "${farming_contracts_ready}" == true || "${farming_contracts_ready}" == false ) ]] ||
        die "service readiness returned an invalid local result"
    info "runtime generation receipt accepted: ${receipt_mode}"
    if [[ "${farming_contracts_ready}" != true ]]; then
        auto_farm_ready=false
        annihilation_ready=false
        info "optional Depot, Annihilation and sanity Fight phases are disabled; daily and final Award remain enabled"
    fi
else
    info "device-only mode skips MAA runtime validation"
fi

if [[ "${check_device}" != true &&
      ( ( "${farm_mode}" == auto && -z "${stage}" ) || -n "${stage}" ) ]] &&
   { ! command -v jq >/dev/null 2>&1 || ! command -v stat >/dev/null 2>&1 ||
     [[ ! -x "${planner}" ]]; }; then
    planner_helpers_ready=false
    if [[ -n "${stage}" ]]; then
        info "planner helpers are unavailable; the explicit stage can run but cannot establish proxy proof"
    else
        auto_farm_ready=false
        info "planner helpers are unavailable; event farming will be skipped"
    fi
fi

if [[ "${check_device}" != true && "${farm_mode}" == auto ]]; then
    if [[ "${planner_helpers_ready}" != true ]] ||
       ! jq -n -f "${annihilation_contract}" >/dev/null 2>&1; then
        annihilation_ready=false
        info "Annihilation decision contract is invalid; weekly Annihilation will be skipped"
    fi
fi

if [[ "${check_device}" != true && "${farm_mode}" == auto && -z "${stage}" ]]; then
    if [[ "${planner_helpers_ready}" != true ]] ||
       ! jq -n -f "${decision_contract}" >/dev/null 2>&1; then
        auto_farm_ready=false
        info "activity decision contract is invalid; automatic farming will be skipped"
    fi
fi

if [[ "${dry_run}" == true ]]; then
    info "receipt validation completed; no MaaCore, Waydroid, or game process was started"
    exit 0
fi

import_desktop_environment
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
runtime_dir="${XDG_RUNTIME_DIR}"

if ! waydroid_session_is_running; then
    session_started_by_launcher=true
fi

waydroid_ui_log="${project_root}/var/state/host/waydroid-ui-$(date '+%Y%m%d-%H%M%S').log"
if [[ "${session_started_by_launcher}" == true ]]; then
    info "opening the scaled Waydroid UI (log: ${waydroid_ui_log})"
    "${scaled_ui}" 8>&- 9>&- >"${waydroid_ui_log}" 2>&1 &
else
    if [[ -n "${WAYLAND_DISPLAY:-}" && -S "${runtime_dir}/${WAYLAND_DISPLAY}" ]]; then
        info "reusing the running Waydroid session and opening its UI (log: ${waydroid_ui_log})"
        waydroid show-full-ui 8>&- 9>&- >"${waydroid_ui_log}" 2>&1 &
        waydroid_ui_pid=$!
    else
        # A session started by another managed run is already usable through
        # ADB. It needs neither a desktop surface nor a synthetic watcher.
        info "reusing the running headless Waydroid session"
        : >"${waydroid_ui_log}"
        waydroid_ui_pid=""
    fi
fi
if [[ "${session_started_by_launcher}" == true ]]; then
    waydroid_ui_pid=$!
fi

if wait_for_waydroid; then
    :
else
    wait_status=$?
    if (( wait_status == 2 )); then
        die "Waydroid UI exited during startup; inspect ${waydroid_ui_log}"
    fi
    if timeout --signal=TERM --kill-after=2s 5s adb devices 2>/dev/null |
       awk -v serial="${waydroid_serial}" '$1 == serial && $2 == "unauthorized" { found = 1 } END { exit !found }'; then
        die "ADB is unauthorized; accept the debugging dialog in Waydroid and retry"
    fi
    die "Waydroid did not become ready within 120 seconds; inspect ${waydroid_ui_log}"
fi

info "ADB is ready: ${waydroid_serial}"
timeout --signal=TERM --kill-after=2s 10s waydroid prop set persist.waydroid.width "${display_width}"
timeout --signal=TERM --kill-after=2s 10s waydroid prop set persist.waydroid.height "${display_height}"
timeout --signal=TERM --kill-after=2s 10s waydroid prop set persist.waydroid.multi_windows false
timeout --signal=TERM --kill-after=2s 10s \
    adb -s "${waydroid_serial}" shell wm size "${display_width}x${display_height}"

size_output="$(timeout --signal=TERM --kill-after=2s 10s \
    adb -s "${waydroid_serial}" shell wm size | tr -d '\r')"
if ! grep -Eq "(Override|Physical) size: ${display_width}x${display_height}$" <<<"${size_output}"; then
    die "failed to set Waydroid resolution to ${display_width}x${display_height}: ${size_output}"
fi
info "Waydroid resolution: ${display_width}x${display_height}"

package_path="$(timeout --signal=TERM --kill-after=2s 10s \
    adb -s "${waydroid_serial}" shell pm path "${official_package}" 2>/dev/null |
    tr -d '\r' || true)"
grep -Fq 'package:' <<<"${package_path}" ||
    die "official Arknights package is not installed: ${official_package}"

network_is_ready ||
    die "Waydroid cannot reach ${network_test_url}; run scripts/install-network-fix.sh once"
info "Waydroid network is ready"

if [[ "${check_device}" == true ]]; then
    info "device checks passed"
    exit 0
fi

if [[ "${e2e_award}" == true ]]; then
    run_award_only "end-to-end probe" "e2e-award"
    exit 0
fi

info "daily-first mode: protecting the game day that ends at 04:00 ${server_timezone}"
prepare_daily_drone_policy
run_daily_routine
refresh_planner_sources_if_needed

# Weekly Annihilation consumes sanity, so it is always planned after the
# protected daily pass and before material farming.
run_weekly_annihilation_if_due

if [[ -z "${stage}" && "${farm_mode}" == auto ]]; then
    info "weekly Annihilation phase outcome: ${annihilation_phase_outcome}; continuing to normal material farming"
fi

if [[ -n "${stage}" && "${farming_contracts_ready}" == true ]]; then
    fight_stamp="$(date '+%Y%m%d-%H%M%S')"
    info "spending sanity on ${stage}; all medicine expiring within two days is enabled"
    if ! game_client_has_saved_proxy "${stage}" \
        "${project_root}/var/state/host/${fight_stamp}-manual"; then
        info "explicit stage farming skipped because the game client did not confirm saved proxy play"
    else
        if ! capture_core_log_cursor; then
            info "explicit stage farming skipped because a fresh MaaCore log cursor could not be captured"
        else
            if ! run_sanity_fight "${stage}" \
            "${project_root}/var/state/host/${fight_stamp}-fight.log"; then
                info "explicit stage farming failed; continuing with final Award"
            elif [[ "${planner_helpers_ready}" == true ]]; then
                manual_activity_instance="$(active_activity_instance_for_stage "${stage}" 2>/dev/null || true)"
                if [[ "${manual_activity_instance}" =~ ^[0-9a-f]{24}$ ]]; then
                    if ! run_soft_with_timeout 1m "${planner}" record-fight \
                        --log "${core_log}" --since-byte "${fight_core_offset}" \
                        "${core_log_cursor_args[@]}" \
                        --stage "${stage}" --activity-instance "${manual_activity_instance}"; then
                        info "manual fight completed, but its proxy capability could not be recorded"
                    fi
                else
                    info "manual fight completed; no unique active activity instance was found to record"
                fi
            fi
        fi
    fi
elif [[ -z "${stage}" && "${farm_mode}" == auto && "${auto_farm_ready}" == true ]]; then
    farm_stamp="$(date '+%Y%m%d-%H%M%S')"
    depot_log="${project_root}/var/state/host/${farm_stamp}-depot.log"
    decision_file="${project_root}/var/state/planner/launcher-${farm_stamp}.json"

    if ! ensure_farming_inventory_snapshot "${depot_log}"; then
        :
    elif ! run_farming_soft_with_timeout 300 "${planner}" plan \
        "${planner_source_args[@]}" --skip-maa-hot-update \
        --output "${decision_file}"; then
        info "automatic farming skipped because the planner failed"
    elif [[ ! -s "${decision_file}" ]]; then
        info "automatic farming skipped because the planner wrote no decision"
    else
        decision_fields="$(jq -er '[
            (.schema_version | select(type == "number") | tostring),
            (.decision | select(type == "string")),
            (.reason // "unspecified" | select(type == "string"))
        ] | @tsv' "${decision_file}" 2>/dev/null || true)"
        IFS=$'\t' read -r decision_schema planner_decision planner_reason \
            <<<"${decision_fields}"
        if [[ "${decision_schema}" != 1 ]]; then
            info "automatic farming skipped because the planner decision schema is invalid"
        elif [[ "${planner_decision}" != FIGHT ]]; then
            info "automatic farming NOOP: ${planner_reason}"
        elif ! activity_decision_is_safe "${decision_file}"; then
            info "automatic farming skipped because the FIGHT decision failed launcher validation"
        else
            info "automatic farming decision: ${decision_file}"
            run_planned_activity_candidates "${decision_file}" "${farm_stamp}" || true
        fi
    fi
fi

if [[ "${pre_reset_slot}" == true && "${fight_attempted}" == true ]]; then
    info "pre-reset Fight was attempted; skipping fallback and proceeding to final Award"
elif [[ -z "${stage}" && "${farm_mode}" == auto &&
        "${activity_fight_completed}" != true ]]; then
    info "no activity-stage fight was completed; entering AP-5 -> 1-7 fallback"
    if ! run_regular_fallback; then
        info "regular-stage fallback produced no fresh fight proof; continuing with final Award"
    fi
fi

current_game_day="$(server_game_day_now)"
if [[ "${daily_completed_game_day}" != "${current_game_day}" ]]; then
    die "daily maintenance crossed the 04:00 game-day boundary; refusing to rerun base, recruitment, or shop in the same service"
fi

# Ordinary task rewards are intentionally the final MAA phase. It is isolated
# from daily.toml so post-fight reconciliation can never re-enter the base.
award_started_game_day="${current_game_day}"
run_award_only "final service phase" "award-final"
award_completed_game_day="$(server_game_day_now)"
[[ "${award_completed_game_day}" == "${award_started_game_day}" ]] ||
    die "Award-only final phase crossed the 04:00 game-day boundary"
