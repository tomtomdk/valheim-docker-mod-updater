#!/usr/bin/env bash
set -Eeuo pipefail

# Valheim Thunderstore mod updater for Docker/Compose servers.
# Server/container settings live in updater.settings.json.
# Mod package selections live in mods.json.

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS_FILE="${SETTINGS_FILE:-${SCRIPT_DIR}/updater.settings.json}"

json_setting() {
    local key="$1"
    local default_value="${2:-}"

    if [[ ! -f "${SETTINGS_FILE}" ]]; then
        printf '%s' "${default_value}"
        return 0
    fi

    python3 -c '
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
default = sys.argv[3]

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except FileNotFoundError:
    print(default, end="")
    raise SystemExit(0)

value = data
for part in key.split("."):
    if not isinstance(value, dict) or part not in value:
        print(default, end="")
        raise SystemExit(0)
    value = value[part]

if value is None:
    print(default, end="")
elif isinstance(value, bool):
    print("true" if value else "false", end="")
else:
    print(str(value), end="")
' "${SETTINGS_FILE}" "${key}" "${default_value}"
}

setting() {
    local env_name="$1"
    local json_key="$2"
    local default_value="${3:-}"
    local env_value="${!env_name-}"

    if [[ -n "${env_value}" ]]; then
        printf '%s' "${env_value}"
    else
        json_setting "${json_key}" "${default_value}"
    fi
}

DOCKER_MODE="$(setting DOCKER_MODE docker.mode "container")"
CONTAINER_NAME="$(setting CONTAINER_NAME docker.container_name "")"
COMPOSE_FILE="$(setting COMPOSE_FILE docker.compose_file "")"
COMPOSE_PROJECT_DIR="$(setting COMPOSE_PROJECT_DIR docker.compose_project_dir "")"
COMPOSE_SERVICE="$(setting COMPOSE_SERVICE docker.compose_service "")"
DOCKER_BIN="$(setting DOCKER_BIN docker.docker_bin "/usr/bin/docker")"
TARGET_ROOT="$(setting TARGET_ROOT valheim.target_root "")"
UPDATER_DIR="$(setting UPDATER_DIR updater.dir "${SCRIPT_DIR}")"
CONFIG="$(setting CONFIG updater.mods_config "${UPDATER_DIR}/mods.json")"
STATE_FILE="$(setting STATE_FILE updater.state_file "${UPDATER_DIR}/state.json")"
PY="$(setting PY updater.sync_script "${UPDATER_DIR}/thunderstore_sync.py")"
BACKUP_DIR="$(setting BACKUP_DIR backups.dir "${UPDATER_DIR}/backups/config-only")"
MAX_BACKUPS="$(setting MAX_BACKUPS backups.max_count "5")"
WAIT_SECONDS="${WAIT_SECONDS_OVERRIDE:-$(setting WAIT_SECONDS restart.wait_seconds "900")}"
STOP_TIMEOUT="$(setting STOP_TIMEOUT restart.stop_timeout_seconds "120")"
START_TIMEOUT="$(setting START_TIMEOUT restart.start_timeout_seconds "180")"
LOCK_FILE="$(setting LOCK_FILE updater.lock_file "/run/lock/valheim-docker-modupdater.lock")"

BEPINEX_DIR="${TARGET_ROOT}/BepInEx"
CONFIG_DIR="${BEPINEX_DIR}/config"

if [[ ${EUID} -ne 0 ]]; then
    if [[ -t 0 && -t 1 ]]; then
        exec sudo env SETTINGS_FILE="${SETTINGS_FILE}" WAIT_SECONDS_OVERRIDE="${WAIT_SECONDS}" "$0" "$@"
    fi

    echo "[ERROR] This script needs root privileges for Docker administration and file ownership." >&2
    echo "[ERROR] Run with sudo, or run it as root from cron/systemd." >&2
    exit 1
fi

require_value() {
    local name="$1"
    local value="$2"

    if [[ -z "${value}" ]]; then
        echo "[ERROR] Missing required setting: ${name}" >&2
        echo "[ERROR] Copy updater.settings.example.json to updater.settings.json and fill it in." >&2
        exit 2
    fi
}

human_time() {
    local seconds="$1"

    if (( seconds <= 0 )); then
        printf 'immediately'
    elif (( seconds < 60 )); then
        printf '%s seconds' "${seconds}"
    elif (( seconds % 60 == 0 )); then
        printf '%s minutes' "$((seconds / 60))"
    else
        printf '%sm %ss' "$((seconds / 60))" "$((seconds % 60))"
    fi
}

docker_compose() {
    local -a args=(compose)

    if [[ -n "${COMPOSE_FILE}" ]]; then
        args+=(--file "${COMPOSE_FILE}")
    fi

    if [[ -n "${COMPOSE_PROJECT_DIR}" ]]; then
        args+=(--project-directory "${COMPOSE_PROJECT_DIR}")
    fi

    "${DOCKER_BIN}" "${args[@]}" "$@"
}

is_up() {
    case "${DOCKER_MODE}" in
        container)
            local running
            running="$(${DOCKER_BIN} inspect -f '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true)"
            [[ "${running}" == "true" ]]
            ;;
        compose)
            local cid running
            cid="$(docker_compose ps -q "${COMPOSE_SERVICE}" 2>/dev/null || true)"
            [[ -n "${cid}" ]] || return 1
            running="$(${DOCKER_BIN} inspect -f '{{.State.Running}}' "${cid}" 2>/dev/null || true)"
            [[ "${running}" == "true" ]]
            ;;
        *)
            echo "[ERROR] docker.mode must be 'container' or 'compose'." >&2
            return 2
            ;;
    esac
}

wait_for_down() {
    local elapsed=0

    while (( elapsed < STOP_TIMEOUT )); do
        if ! is_up; then
            return 0
        fi

        sleep 2
        elapsed=$((elapsed + 2))
    done

    return 1
}

wait_for_up() {
    local elapsed=0

    while (( elapsed < START_TIMEOUT )); do
        if is_up; then
            return 0
        fi

        sleep 2
        elapsed=$((elapsed + 2))
    done

    return 1
}

stop_server() {
    case "${DOCKER_MODE}" in
        container)
            echo "[INFO] Stopping Docker container: ${CONTAINER_NAME}"
            "${DOCKER_BIN}" stop "${CONTAINER_NAME}"
            ;;
        compose)
            echo "[INFO] Stopping Compose service: ${COMPOSE_SERVICE}"
            docker_compose stop "${COMPOSE_SERVICE}"
            ;;
    esac

    if ! wait_for_down; then
        echo "[ERROR] Server did not stop within ${STOP_TIMEOUT} seconds." >&2
        return 1
    fi

    echo "[OK] Server stopped."
}

start_server() {
    case "${DOCKER_MODE}" in
        container)
            echo "[INFO] Starting Docker container: ${CONTAINER_NAME}"
            "${DOCKER_BIN}" start "${CONTAINER_NAME}"
            ;;
        compose)
            echo "[INFO] Starting Compose service: ${COMPOSE_SERVICE}"
            docker_compose up -d "${COMPOSE_SERVICE}"
            ;;
    esac

    if ! wait_for_up; then
        echo "[ERROR] Server did not start within ${START_TIMEOUT} seconds." >&2
        return 1
    fi

    echo "[OK] Server is running."
}

ensure_directories() {
    install -d -m 0775 "${UPDATER_DIR}"
    install -d -m 0775 "${BACKUP_DIR}"

    if [[ ! -e "${STATE_FILE}" ]]; then
        touch "${STATE_FILE}"
    fi
}

prune_backups() {
    local keep="${1:-${MAX_BACKUPS}}"
    local -a backups=()
    local count i

    mapfile -t backups < <(
        find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'bepinex-config-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null |
        sort -nr |
        cut -d' ' -f2-
    )

    count=${#backups[@]}
    if (( count <= keep )); then
        return 0
    fi

    for ((i = keep; i < count; i++)); do
        echo "[INFO] Removing old config backup: ${backups[$i]}"
        rm -f -- "${backups[$i]}"
    done
}

make_config_backup() {
    local ts backup_file

    if [[ ! -d "${CONFIG_DIR}" ]]; then
        echo "[WARN] BepInEx config directory not found. Backup skipped."
        return 0
    fi

    if (( MAX_BACKUPS > 1 )); then
        prune_backups "$((MAX_BACKUPS - 1))"
    else
        prune_backups 0
    fi

    ts="$(date +%Y%m%d-%H%M%S)"
    backup_file="${BACKUP_DIR}/bepinex-config-${ts}.tar.gz"

    echo "[INFO] Backing up BepInEx/config to ${backup_file}"
    tar -czf "${backup_file}" -C "${BEPINEX_DIR}" config
    echo "[INFO] Backup size: $(du -h "${backup_file}" | awk '{print $1}')"

    prune_backups "${MAX_BACKUPS}"
}

require_value TARGET_ROOT "${TARGET_ROOT}"

case "${DOCKER_MODE}" in
    container)
        require_value CONTAINER_NAME "${CONTAINER_NAME}"
        ;;
    compose)
        require_value COMPOSE_SERVICE "${COMPOSE_SERVICE}"
        ;;
    *)
        echo "[ERROR] docker.mode must be 'container' or 'compose'." >&2
        exit 2
        ;;
esac

if ! [[ "${WAIT_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "[ERROR] restart.wait_seconds/WAIT_SECONDS must be a non-negative integer." >&2
    exit 2
fi

if ! [[ "${MAX_BACKUPS}" =~ ^[0-9]+$ ]]; then
    echo "[ERROR] backups.max_count/MAX_BACKUPS must be a non-negative integer." >&2
    exit 2
fi

if [[ ! -x "${DOCKER_BIN}" ]]; then
    echo "[ERROR] Docker CLI not found: ${DOCKER_BIN}" >&2
    exit 2
fi

if [[ ! -f "${CONFIG}" ]]; then
    echo "[ERROR] Missing mod config: ${CONFIG}" >&2
    exit 2
fi

if [[ ! -f "${PY}" ]]; then
    echo "[ERROR] Missing updater: ${PY}" >&2
    exit 2
fi

if [[ ! -d "${TARGET_ROOT}" ]]; then
    echo "[ERROR] Valheim target directory does not exist: ${TARGET_ROOT}" >&2
    exit 2
fi

if [[ ! -d "${BEPINEX_DIR}" ]]; then
    echo "[ERROR] BepInEx directory does not exist: ${BEPINEX_DIR}" >&2
    exit 2
fi

ensure_directories
mkdir -p "$(dirname "${LOCK_FILE}")"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[INFO] Another Valheim mod updater is already running."
    exit 0
fi

WAS_RUNNING=0
SERVER_STOPPED_BY_US=0

cleanup_on_exit() {
    local rc=$?
    trap - EXIT

    if (( rc != 0 && WAS_RUNNING == 1 && SERVER_STOPPED_BY_US == 1 )); then
        echo "[WARN] Updater failed after stopping the server. Attempting restart..."
        start_server || true
    fi

    exit "${rc}"
}

trap cleanup_on_exit EXIT

echo
echo "============================================================"
echo " Valheim Docker Thunderstore Mod Updater"
echo "============================================================"
echo "[INFO] Mode:     ${DOCKER_MODE}"
echo "[INFO] Target:   ${TARGET_ROOT}"
echo "[INFO] Settings: ${SETTINGS_FILE}"
echo "[INFO] Config:   ${CONFIG}"
echo "[INFO] Backups:  ${BACKUP_DIR}"
echo

if is_up; then
    WAS_RUNNING=1
    echo "[INFO] Valheim server is currently running."
else
    WAS_RUNNING=0
    echo "[WARN] Valheim server is currently not running."
fi

echo "[INFO] Checking Thunderstore for mod updates..."
restart_unix=$(( $(date +%s) + WAIT_SECONDS ))

set +e
"${PY}" --config "${CONFIG}" --target "${TARGET_ROOT}" --state "${STATE_FILE}" --check --notify-scheduled "${restart_unix}"
rc=$?
set -e

if [[ ${rc} -eq 0 ]]; then
    echo "[OK] No mod updates available. Nothing needs to be restarted."
    exit 0
fi

if [[ ${rc} -ne 10 ]]; then
    echo "[ERROR] Update check failed with exit code ${rc}. Server was not touched." >&2
    exit "${rc}"
fi

echo "[INFO] Mod updates are available."

if (( WAS_RUNNING == 1 && WAIT_SECONDS > 0 )); then
    echo "[INFO] Waiting $(human_time "${WAIT_SECONDS}") before restart."
    echo "[INFO] Restart time: $(date -d "@${restart_unix}")"
    sleep "${WAIT_SECONDS}"
elif (( WAS_RUNNING == 1 )); then
    echo "[INFO] Restart delay disabled."
else
    echo "[INFO] Server is already stopped. No restart delay is required."
fi

echo "[INFO] Re-checking updates before making changes..."
set +e
"${PY}" --config "${CONFIG}" --target "${TARGET_ROOT}" --state "${STATE_FILE}" --check
rc=$?
set -e

if [[ ${rc} -eq 0 ]]; then
    echo "[OK] Updates are no longer required. Restart cancelled."
    exit 0
fi

if [[ ${rc} -ne 10 ]]; then
    echo "[ERROR] Second update check failed with exit code ${rc}. Server was not stopped." >&2
    exit "${rc}"
fi

if (( WAS_RUNNING == 1 )); then
    stop_server
    SERVER_STOPPED_BY_US=1
fi

make_config_backup

echo "[INFO] Applying Thunderstore updates..."
"${PY}" --config "${CONFIG}" --target "${TARGET_ROOT}" --state "${STATE_FILE}" --notify
echo "[OK] Mod updates applied."

if (( WAS_RUNNING == 1 )); then
    start_server
    SERVER_STOPPED_BY_US=0
else
    echo "[INFO] Server was stopped before the updater ran. Leaving it stopped."
fi

echo
echo "============================================================"
echo "[OK] Valheim Docker mod update completed successfully."
echo "============================================================"
