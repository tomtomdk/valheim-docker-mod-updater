#!/usr/bin/env bash
set -Eeuo pipefail

# Run the Docker updater immediately, skipping the normal restart warning delay.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export WAIT_SECONDS_OVERRIDE=0
exec "${SCRIPT_DIR}/update_valheim_docker_mods.sh" "$@"
