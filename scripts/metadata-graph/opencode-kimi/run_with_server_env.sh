#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
env_file="$repo_root/server/.env"
defaults_file="$repo_root/server/.env.metadata-kg"

if [[ ! -f "$env_file" ]]; then
  echo "missing env file: $env_file" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
if [[ -f "$defaults_file" ]]; then
  # shellcheck disable=SC1090
  source "$defaults_file"
fi
set +a

export JUSPAY_API_KEY="${JUSPAY_API_KEY:-${LITELLM_API_KEY:-}}"

if [[ -z "${JUSPAY_API_KEY:-}" ]]; then
  echo "JUSPAY_API_KEY/LITELLM_API_KEY is not set in $env_file" >&2
  exit 2
fi

exec "$@"
