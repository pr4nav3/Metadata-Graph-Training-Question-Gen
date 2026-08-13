#!/usr/bin/env bash
set -euo pipefail

server_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$server_dir/vespa-data" "$server_dir/vespa-logs"
sudo chown -R 1000:1000 "$server_dir/vespa-data" "$server_dir/vespa-logs"
sudo chmod -R 755 "$server_dir/vespa-data" "$server_dir/vespa-logs"
echo "Vespa bind-mount permissions are ready."
