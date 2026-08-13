#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${GLOBAL_SUPERVISOR_MODE:-once}"

ARGS=()

if [[ -n "${OPENCODE_PIPELINE_MODEL:-}" ]]; then
  ARGS+=("--opencode-model" "${OPENCODE_PIPELINE_MODEL}")
fi

if [[ "${GLOBAL_SUPERVISOR_SKIP_SEARCH_HEALTH:-0}" == "1" ]]; then
  ARGS+=("--skip-search-health")
fi

if [[ "${GLOBAL_SUPERVISOR_DRY_RUN:-0}" == "1" ]]; then
  ARGS+=("--dry-run")
fi

if [[ "${GLOBAL_SUPERVISOR_NO_WAKE:-0}" == "1" ]]; then
  ARGS+=("--no-wake")
fi

if [[ "${MODE}" == "loop" ]]; then
  if [[ -n "${GLOBAL_SUPERVISOR_INTERVAL_SECONDS:-}" ]]; then
    ARGS+=("--interval-seconds" "${GLOBAL_SUPERVISOR_INTERVAL_SECONDS}")
  fi

  if [[ -n "${GLOBAL_SUPERVISOR_DURATION_HOURS:-}" ]]; then
    ARGS+=("--duration-hours" "${GLOBAL_SUPERVISOR_DURATION_HOURS}")
  fi
fi

exec "${SCRIPT_DIR}/run_with_server_env.sh" \
  python3 "${SCRIPT_DIR}/global_supervisor.py" "${MODE}" "${ARGS[@]}" "$@"
