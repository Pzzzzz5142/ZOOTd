#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
maa="${project_root}/bin/maa"
planner="${project_root}/bin/maa-planner"
data_dir="${project_root}/var/data"
runtime_cache_dir="${data_dir}/cache"
control_cache_dir="${project_root}/var/cache"
legacy_runtime_cache="${control_cache_dir}/maa-runtime"
core_installer_cache="${control_cache_dir}/maa-core-installer"
previous_runtime="${control_cache_dir}/MaaRuntime.previous"
state_dir="${project_root}/var/state/runtime"
state_file="${state_dir}/maa-resource.json"
mirror="${control_cache_dir}/MaaResource.git"
remote_url="${MAA_RESOURCE_REMOTE_URL:-https://github.com/MaaAssistantArknights/MaaResource.git}"
remote_branch="${MAA_RESOURCE_REMOTE_BRANCH:-main}"
requested_commit=""
candidate_root=""
lock_dir="${project_root}/var/run"

info() {
    printf '[maa-runtime] %s\n' "$*"
}

die() {
    printf '[maa-runtime] error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

usage() {
    cat <<'EOF'
Usage: scripts/update-maa-runtime.sh [--ref FULL_COMMIT]

Stage the latest stable MaaCore, its bundled base resource, MaaResource, and
the maa-cli API hot cache as one candidate. Promote the complete runtime only
after every managed task passes a dry-run. --ref selects one MaaResource
commit for repair or compatibility auditing while Core still follows stable.
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --ref)
            (( $# >= 2 )) || die "--ref requires a full commit hash"
            requested_commit="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

if [[ -n "${requested_commit}" && ! "${requested_commit}" =~ ^[0-9a-f]{40}$ ]]; then
    die "--ref must be a full lowercase Git commit hash"
fi
if [[ ! "${remote_branch}" =~ ^[A-Za-z0-9._/-]+$ ]] ||
   [[ "${remote_branch}" == -* ]] || [[ "${remote_branch}" == *..* ]]; then
    die "unsafe MaaResource branch name: ${remote_branch}"
fi

cleanup_candidate() {
    if [[ -n "${candidate_root}" && -d "${candidate_root}" &&
          "${candidate_root}" == "${control_cache_dir}"/maa-runtime-candidate.* ]]; then
        rm -r -- "${candidate_root}"
    fi
}
trap cleanup_candidate EXIT

remove_candidate_tree() {
    local path="$1"

    [[ "${path}" == "${candidate_root}"/* && "${path}" != "${candidate_root}" ]] ||
        die "refusing to remove a path outside the runtime candidate: ${path}"
    if [[ -e "${path}" || -L "${path}" ]]; then
        [[ -d "${path}" && ! -L "${path}" ]] ||
            die "refusing to replace an unsafe candidate path: ${path}"
        rm -r -- "${path}"
    fi
}

cache_is_complete() {
    local cache="$1"

    [[ -s "${cache}/StageActivityV2.json" &&
       -s "${cache}/StageActivityV2.json.etag" &&
       -s "${cache}/resource/tasks/tasks.json" &&
       -s "${cache}/resource/tasks/tasks.json.etag" ]]
}

core_version_at() {
    local runtime_data="$1"
    local runtime_cache="$2"
    local runtime_config="$3"
    local output

    output="$(MAA_DATA_DIR="${runtime_data}" \
        MAA_CACHE_DIR="${runtime_cache}" \
        MAA_CONFIG_DIR="${runtime_config}" \
        "${maa}" version 2>/dev/null)" || return 1
    awk '/^MaaCore / { print $2; found = 1 } END { exit !found }' <<<"${output}"
}

active_resource_commit() {
    jq -r '.active_commit // empty' "${state_file}" 2>/dev/null || true
}

write_state() {
    local status="$1"
    local candidate_commit="$2"
    local active_commit="$3"
    local candidate_core="$4"
    local active_core="$5"
    local resource_source="$6"
    local cache_source="$7"
    local reason="$8"
    local temporary generation_json="null"
    local hot_tasks_sha256=""
    local activity_sha256=""

    if [[ "${status}" == active || "${status}" == kept-previous ]]; then
        generation_json="$("${planner}" runtime-fingerprint)" ||
            die "validated runtime could not be sealed into a generation receipt"
        hot_tasks_sha256="$(jq -er '.hot_cache.tasks_sha256' <<<"${generation_json}")"
        activity_sha256="$(jq -er '.hot_cache.activity_sha256' <<<"${generation_json}")"
    fi

    mkdir -p -- "${state_dir}"
    temporary="$(mktemp "${state_dir}/.maa-runtime.XXXXXX")"
    jq -n \
        --arg status "${status}" \
        --arg checked_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        --arg remote "${remote_url}" \
        --arg branch "${remote_branch}" \
        --arg candidate_commit "${candidate_commit}" \
        --arg active_commit "${active_commit}" \
        --arg candidate_core "${candidate_core}" \
        --arg active_core "${active_core}" \
        --arg resource_source "${resource_source}" \
        --arg cache_source "${cache_source}" \
        --arg hot_tasks_sha256 "${hot_tasks_sha256}" \
        --arg activity_sha256 "${activity_sha256}" \
        --argjson generation "${generation_json}" \
        --arg reason "${reason}" '
        {
            schema_version: 3,
            status: $status,
            checked_at: $checked_at,
            core: {
                channel: "stable",
                candidate_version: ($candidate_core | select(length > 0) // null),
                active_version: ($active_core | select(length > 0) // null)
            },
            resource: {
                remote: $remote,
                branch: $branch,
                candidate_commit: ($candidate_commit | select(length > 0) // null),
                active_commit: ($active_commit | select(length > 0) // null),
                selected_source: ($resource_source | select(length > 0) // null)
            },
            hot_cache: {
                selected_source: ($cache_source | select(length > 0) // null),
                tasks_sha256: ($hot_tasks_sha256 | select(length > 0) // null),
                activity_sha256: ($activity_sha256 | select(length > 0) // null)
            },
            candidate_commit: ($candidate_commit | select(length > 0) // null),
            active_commit: ($active_commit | select(length > 0) // null),
            generation: $generation,
            reason: $reason,
            validation: {
                mode: "candidate MaaCore dry-run",
                tasks: [
                    "daily", "award-only", "annihilation", "depot",
                    "proxy-preflight", "sanity-fight", "verify-fight",
                    "material-recipe-item-index"
                ]
            }
        }
    ' >"${temporary}"
    mv -- "${temporary}" "${state_file}"
}

validate_runtime_at() {
    local candidate_data="$1"
    local candidate_cache="$2"
    local candidate_config="$3"
    local item_index task_name

    for task_name in daily award-only annihilation depot; do
        if ! MAA_DATA_DIR="${candidate_data}" \
            MAA_CACHE_DIR="${candidate_cache}" \
            MAA_CONFIG_DIR="${candidate_config}" \
            "${maa}" --batch \
            --log-file=/dev/null run "${task_name}" \
            --profile waydroid --dry-run >/dev/null; then
            info "runtime validation failed for task=${task_name}"
            return 1
        fi
    done
    if ! printf '%s\n' '1-7' | MAA_DATA_DIR="${candidate_data}" \
        MAA_CACHE_DIR="${candidate_cache}" \
        MAA_CONFIG_DIR="${candidate_config}" \
        "${maa}" \
        --log-file=/dev/null run proxy-preflight --profile waydroid \
        --dry-run >/dev/null; then
        info "runtime validation failed for task=proxy-preflight"
        return 1
    fi
    if ! printf '%s\n' '1-7' | MAA_DATA_DIR="${candidate_data}" \
        MAA_CACHE_DIR="${candidate_cache}" \
        MAA_CONFIG_DIR="${candidate_config}" \
        "${maa}" \
        --log-file=/dev/null run sanity-fight --profile waydroid \
        --dry-run >/dev/null; then
        info "runtime validation failed for task=sanity-fight"
        return 1
    fi
    if ! printf '%s\n' '1-7' | MAA_DATA_DIR="${candidate_data}" \
        MAA_CACHE_DIR="${candidate_cache}" \
        MAA_CONFIG_DIR="${candidate_config}" \
        "${maa}" \
        --log-file=/dev/null run verify-fight --profile waydroid \
        --dry-run >/dev/null; then
        info "runtime validation failed for task=verify-fight"
        return 1
    fi
    item_index="${candidate_data}/resource/item_index.json"
    if [[ -f "${candidate_data}/MaaResource/resource/item_index.json" ]]; then
        item_index="${candidate_data}/MaaResource/resource/item_index.json"
    fi
    if ! "${planner}" validate-material-recipes \
        --item-index "${item_index}" >/dev/null; then
        info "runtime validation failed for blue-material recipe identity"
        return 1
    fi
}

make_config_view() {
    local root="$1"

    mkdir -p -- "${root}"
    cp -- "${project_root}/config/cli.toml" "${root}/cli.toml"
    sed -i \
        's/^check_interval = 3153600000$/check_interval = 3600/' \
        "${root}/cli.toml"
    grep -Fxq 'check_interval = 3600' "${root}/cli.toml" ||
        die "failed to create the candidate hot-update policy"
    ln -s -- "${project_root}/config/profiles" "${root}/profiles"
    ln -s -- "${project_root}/config/tasks" "${root}/tasks"
    # maa-cli resolves an Infrast filename relative to CONFIG_DIR/infrast.
    # Keep the protected Dorm schedule in the isolated candidate view so a
    # compatible runtime is not rejected merely because that local policy file
    # was absent from the staging directory.
    ln -s -- "${project_root}/config/infrast" "${root}/infrast"
}

migrate_live_cache() {
    [[ -d "${data_dir}" && ! -L "${data_dir}" ]] || return 0

    if [[ ! -e "${runtime_cache_dir}" ]]; then
        if [[ -d "${legacy_runtime_cache}" && ! -L "${legacy_runtime_cache}" ]]; then
            info "moving the validated API cache into the atomic runtime"
            mv -- "${legacy_runtime_cache}" "${runtime_cache_dir}"
        elif [[ -L "${legacy_runtime_cache}" ]]; then
            mkdir -p -- "${runtime_cache_dir}"
        else
            mkdir -p -- "${runtime_cache_dir}"
        fi
    fi
    [[ -d "${runtime_cache_dir}" && ! -L "${runtime_cache_dir}" ]] ||
        die "runtime cache must be a real directory inside var/data"

    if [[ -L "${legacy_runtime_cache}" ]]; then
        [[ "$(readlink -f -- "${legacy_runtime_cache}")" == "$(readlink -f -- "${runtime_cache_dir}")" ]] ||
            die "legacy runtime-cache symlink points outside the managed runtime"
    elif [[ -e "${legacy_runtime_cache}" ]]; then
        die "both the atomic and legacy runtime caches exist; refusing to merge"
    else
        ln -s -- ../data/cache "${legacy_runtime_cache}"
    fi
}

seed_core_installer_cache() {
    local destination="$1"
    local source file
    local -a files

    mkdir -p -- "${destination}"
    shopt -s nullglob
    if compgen -G "${core_installer_cache}/core-manifest-*.json" >/dev/null; then
        source="${core_installer_cache}"
    else
        # One-time migration from the cache layout used before runtime
        # generations were introduced.
        source="${control_cache_dir}"
    fi
    if [[ -d "${source}" ]]; then
        files=(
            "${source}"/core-manifest-*.json
            "${source}"/core-manifest-*.json.etag
            "${source}"/MAA-v*-linux-*.tar.gz
        )
        for file in "${files[@]}"; do
            [[ -f "${file}" && ! -L "${file}" ]] || continue
            cp -a -- "${file}" "${destination}/"
        done
    fi
    shopt -u nullglob
}

preserve_and_strip_core_installer_cache() {
    local source="$1"
    local file manifest expected_archive asset_suffix
    local -a files installer_files old_files

    mkdir -p -- "${core_installer_cache}"
    [[ -d "${core_installer_cache}" && ! -L "${core_installer_cache}" ]] ||
        die "Core installer cache must be a real managed directory"
    manifest="${source}/core-manifest-stable.json"
    [[ -s "${manifest}" ]] || die "stable Core manifest is missing from the candidate"
    case "$(uname -m)" in
        x86_64)
            asset_suffix=-linux-x86_64.tar.gz
            ;;
        aarch64|arm64)
            asset_suffix=-linux-aarch64.tar.gz
            ;;
        *)
            die "unsupported Core architecture: $(uname -m)"
            ;;
    esac
    expected_archive="$(jq -r --arg suffix "${asset_suffix}" '
        [.details.assets[].name | select(endswith($suffix))][0] // empty
    ' "${manifest}")"
    [[ "${expected_archive}" == MAA-v*-linux-*.tar.gz ]] ||
        die "stable Core manifest has no asset for this Linux architecture"

    shopt -s nullglob
    old_files=(
        "${core_installer_cache}"/core-manifest-*.json
        "${core_installer_cache}"/core-manifest-*.json.etag
        "${core_installer_cache}"/MAA-v*-linux-*.tar.gz
    )
    for file in "${old_files[@]}"; do
        [[ -f "${file}" && ! -L "${file}" ]] ||
            die "refusing to replace an unsafe Core installer cache entry"
        rm -- "${file}"
    done
    files=("${manifest}" "${manifest}.etag")
    if [[ -f "${source}/${expected_archive}" ]]; then
        files+=("${source}/${expected_archive}")
    fi
    for file in "${files[@]}"; do
        [[ -f "${file}" && ! -L "${file}" ]] || continue
        cp -a -- "${file}" "${core_installer_cache}/"
    done
    installer_files=(
        "${source}"/core-manifest-*.json
        "${source}"/core-manifest-*.json.etag
        "${source}"/MAA-v*-linux-*.tar.gz
    )
    for file in "${installer_files[@]}"; do
        [[ -f "${file}" && ! -L "${file}" ]] ||
            die "refusing to strip an unsafe candidate cache entry"
        rm -- "${file}"
    done
    shopt -u nullglob
}

prepare_overlay() {
    local source="$1"
    local destination="${candidate_root}/data/MaaResource"

    remove_candidate_tree "${destination}"
    case "${source}" in
        latest)
            cp -a -- "${candidate_root}/snapshots/MaaResource.latest" \
                "${destination}"
            ;;
        live)
            [[ -d "${candidate_root}/snapshots/MaaResource.live" ]] || return 1
            cp -a -- "${candidate_root}/snapshots/MaaResource.live" \
                "${destination}"
            ;;
        base)
            ;;
        *)
            die "unknown overlay source: ${source}"
            ;;
    esac
}

prepare_hot_cache() {
    local source="$1"
    local destination="${candidate_root}/data/cache"

    remove_candidate_tree "${destination}"
    case "${source}" in
        fresh)
            cp -a -- "${candidate_root}/snapshots/cache.fresh" "${destination}"
            ;;
        live)
            [[ -d "${candidate_root}/snapshots/cache.live" ]] || return 1
            cp -a -- "${candidate_root}/snapshots/cache.live" "${destination}"
            ;;
        *)
            die "unknown hot-cache source: ${source}"
            ;;
    esac
}

try_combination() {
    local overlay_source="$1"
    local cache_source="$2"
    local validation_config="${candidate_root}/config"

    prepare_overlay "${overlay_source}" || return 1
    prepare_hot_cache "${cache_source}" || return 1
    if [[ "${cache_source}" == live ]]; then
        validation_config="${project_root}/config"
    fi
    info "validating stable Core with overlay=${overlay_source} cache=${cache_source}"
    validate_runtime_at "${candidate_root}/data" \
        "${candidate_root}/data/cache" "${validation_config}"
}

keep_live_or_fail() {
    local reason="$1"
    local candidate_commit="$2"
    local candidate_core="$3"
    local active_commit="$4"
    local active_core="$5"

    if [[ -d "${data_dir}/lib" && -d "${data_dir}/resource" ]] &&
       validate_runtime_at "${data_dir}" "${runtime_cache_dir}" \
           "${project_root}/config"; then
        write_state kept-previous "${candidate_commit}" "${active_commit}" \
            "${candidate_core}" "${active_core}" live live "${reason}"
        info "candidate rejected; the complete live runtime remains active"
        exit 0
    fi
    write_state failed "${candidate_commit}" "${active_commit}" \
        "${candidate_core}" "${active_core}" "" "" \
        "${reason}; the live runtime also failed validation"
    die "candidate and live runtime both failed validation"
}

require_command cp
require_command diff
require_command flock
require_command git
require_command jq
require_command mv
require_command python3
require_command tar
mv --help | grep -Fq -- '--exchange' ||
    die "GNU mv with atomic --exchange support is required"
[[ -x "${maa}" ]] || die "MAA wrapper is not executable: ${maa}"
[[ -x "${planner}" ]] || die "planner wrapper is not executable: ${planner}"

mkdir -p -- "${control_cache_dir}" "${lock_dir}"
exec 9>"${lock_dir}/maa-host.lock"
flock -n 9 || die "another MAA run is already active"

migrate_live_cache
active_commit="$(active_resource_commit)"
active_core=""
if [[ -d "${data_dir}/lib" ]]; then
    active_core="$(core_version_at "${data_dir}" "${runtime_cache_dir}" \
        "${project_root}/config" || true)"
fi

if ! git --git-dir="${mirror}" rev-parse --is-bare-repository \
    >/dev/null 2>&1; then
    info "initializing the MaaResource staging mirror"
    if ! git clone --mirror -- "${remote_url}" "${mirror}" >/dev/null; then
        keep_live_or_fail "could not initialize the MaaResource mirror" \
            "" "" "${active_commit}" "${active_core}"
    fi
fi
git --git-dir="${mirror}" remote set-url origin "${remote_url}"

if [[ -z "${requested_commit}" ]]; then
    info "fetching an isolated MaaResource candidate"
    if ! git --git-dir="${mirror}" fetch --prune origin \
        "+refs/heads/${remote_branch}:refs/remotes/origin/${remote_branch}" \
        >/dev/null; then
        keep_live_or_fail "could not fetch the MaaResource candidate" \
            "" "" "${active_commit}" "${active_core}"
    fi
    candidate_commit="$(git --git-dir="${mirror}" rev-parse \
        "refs/remotes/origin/${remote_branch}^{commit}")"
else
    candidate_commit="$(git --git-dir="${mirror}" rev-parse \
        "${requested_commit}^{commit}" 2>/dev/null)" ||
        die "requested MaaResource commit is unavailable in the staging mirror"
fi

candidate_root="$(mktemp -d "${control_cache_dir}/maa-runtime-candidate.XXXXXX")"
mkdir -p -- "${candidate_root}/data" "${candidate_root}/snapshots"
make_config_view "${candidate_root}/config"

if [[ -d "${data_dir}" && ! -L "${data_dir}" ]]; then
    info "creating a copy-on-write candidate from the active runtime"
    cp -a --reflink=auto -- "${data_dir}/." "${candidate_root}/data/"
    if [[ -d "${data_dir}/MaaResource" && ! -L "${data_dir}/MaaResource" ]]; then
        cp -a --reflink=auto -- "${data_dir}/MaaResource" \
            "${candidate_root}/snapshots/MaaResource.live"
    fi
    if cache_is_complete "${runtime_cache_dir}"; then
        cp -a --reflink=auto -- "${runtime_cache_dir}" \
            "${candidate_root}/snapshots/cache.live"
    fi
fi

remove_candidate_tree "${candidate_root}/data/cache"
mkdir -p -- "${candidate_root}/data/cache"
seed_core_installer_cache "${candidate_root}/data/cache"

info "staging the latest stable MaaCore and its bundled base resource"
if [[ -f "${candidate_root}/data/lib/libMaaCore.so" ]]; then
    core_command=(update stable)
else
    core_command=(install stable --force)
fi
if ! MAA_DATA_DIR="${candidate_root}/data" \
    MAA_CACHE_DIR="${candidate_root}/data/cache" \
    MAA_CONFIG_DIR="${candidate_root}/config" \
    "${maa}" --batch "${core_command[@]}"; then
    keep_live_or_fail "stable Core candidate could not be staged" \
        "${candidate_commit}" "" "${active_commit}" "${active_core}"
fi
candidate_core="$(core_version_at "${candidate_root}/data" \
    "${candidate_root}/data/cache" "${candidate_root}/config" || true)"
[[ -n "${candidate_core}" ]] ||
    keep_live_or_fail "staged Core has no readable version" \
        "${candidate_commit}" "" "${active_commit}" "${active_core}"

preserve_and_strip_core_installer_cache "${candidate_root}/data/cache"
cache_is_complete "${candidate_root}/data/cache" ||
    keep_live_or_fail "candidate API hot cache is incomplete" \
        "${candidate_commit}" "${candidate_core}" \
        "${active_commit}" "${active_core}"
cp -a --reflink=auto -- "${candidate_root}/data/cache" \
    "${candidate_root}/snapshots/cache.fresh"

mkdir -p -- "${candidate_root}/snapshots/MaaResource.latest"
if ! git --git-dir="${mirror}" archive "${candidate_commit}" |
    tar -x -C "${candidate_root}/snapshots/MaaResource.latest"; then
    keep_live_or_fail "could not materialize the MaaResource candidate" \
        "${candidate_commit}" "${candidate_core}" \
        "${active_commit}" "${active_core}"
fi

selected_overlay=""
selected_cache=""
for combination in \
    latest:fresh live:fresh base:fresh \
    latest:live live:live base:live; do
    overlay_source="${combination%%:*}"
    cache_source="${combination##*:}"
    if try_combination "${overlay_source}" "${cache_source}"; then
        selected_overlay="${overlay_source}"
        selected_cache="${cache_source}"
        break
    fi
done

if [[ -z "${selected_overlay}" ]]; then
    keep_live_or_fail \
        "no latest stable Core/resource/cache combination passed validation" \
        "${candidate_commit}" "${candidate_core}" \
        "${active_commit}" "${active_core}"
fi

case "${selected_overlay}" in
    latest) selected_commit="${candidate_commit}" ;;
    live) selected_commit="${active_commit}" ;;
    base) selected_commit="" ;;
esac

if [[ -d "${data_dir}" ]] &&
   diff -qr -- "${candidate_root}/data" "${data_dir}" >/dev/null; then
    write_state kept-previous "${candidate_commit}" "${selected_commit}" \
        "${candidate_core}" "${active_core}" \
        "${selected_overlay}" "${selected_cache}" \
        "latest stable Core/runtime inputs were checked; live is already the selected runtime"
    info "live runtime is already the validated selection"
    exit 0
fi

# Invalidate the old receipt before changing the live directory. The global
# lock keeps services from observing the brief transition; after a crash they
# will see status=promoting and refuse to trust an unsealed generation.
write_state promoting "${candidate_commit}" "${active_commit}" \
    "${candidate_core}" "${active_core}" \
    "${selected_overlay}" "${selected_cache}" \
    "validated candidate is awaiting atomic promotion and post-promotion proof"

if [[ -e "${previous_runtime}" || -L "${previous_runtime}" ]]; then
    [[ -d "${previous_runtime}" && ! -L "${previous_runtime}" ]] ||
        die "refusing to replace an unsafe previous-runtime path"
    rm -r -- "${previous_runtime}"
fi

had_live=false
if [[ -d "${data_dir}" && ! -L "${data_dir}" ]]; then
    had_live=true
    info "atomically promoting Core ${candidate_core} and its validated resources"
    mv --exchange --no-copy --no-target-directory -- \
        "${candidate_root}/data" "${data_dir}"
    mv -- "${candidate_root}/data" "${previous_runtime}"
else
    info "promoting the first validated runtime"
    mv -- "${candidate_root}/data" "${data_dir}"
fi
migrate_live_cache

if ! validate_runtime_at "${data_dir}" "${runtime_cache_dir}" \
    "${project_root}/config"; then
    if [[ "${had_live}" == true && -d "${previous_runtime}" ]]; then
        mv --exchange --no-copy --no-target-directory -- \
            "${data_dir}" "${previous_runtime}"
        mv -- "${previous_runtime}" "${candidate_root}/rejected-after-promotion"
        migrate_live_cache
    else
        mv -- "${data_dir}" "${candidate_root}/rejected-after-promotion"
    fi
    restored_core="$(core_version_at "${data_dir}" "${runtime_cache_dir}" \
        "${project_root}/config" || true)"
    if [[ "${had_live}" == true ]] &&
       validate_runtime_at "${data_dir}" "${runtime_cache_dir}" \
           "${project_root}/config"; then
        write_state kept-previous "${candidate_commit}" "${active_commit}" \
            "${candidate_core}" "${restored_core}" live live \
            "post-promotion validation failed; previous runtime was atomically restored and revalidated"
        info "previous runtime was restored and resealed for normal services"
    else
        write_state rolled-back "${candidate_commit}" "${active_commit}" \
            "${candidate_core}" "${restored_core}" live live \
            "post-promotion validation failed; no restored runtime passed revalidation"
    fi
    die "post-promotion validation failed; restored the previous runtime"
fi

write_state active "${candidate_commit}" "${selected_commit}" \
    "${candidate_core}" "${candidate_core}" \
    "${selected_overlay}" "${selected_cache}" \
    "stable Core and selected resources passed every managed-task contract"
info "active runtime: Core ${candidate_core}, overlay=${selected_overlay}, cache=${selected_cache}"
