#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_port="${METADATA_KG_VESPA_CONFIG_PORT:-19072}"
target="${METADATA_KG_VESPA_CONFIG_URL:-http://localhost:${config_port}}"
dims="${METADATA_KG_EMBEDDING_DIMS:-1024}"
services_file="${METADATA_KG_VESPA_SERVICES_FILE:-services.demo.xml}"
stage_dir="$(mktemp -d "${TMPDIR:-/tmp}/metadata-kg-vespa-app.XXXXXX")"
archive="${stage_dir}.tar.gz"

cleanup() {
  rm -rf "$stage_dir" "$archive"
}
trap cleanup EXIT

cd "$repo_root"

echo "Staging Vespa app from $repo_root/server/platform/vespa"
rsync -a --exclude='._*' server/platform/vespa/ "$stage_dir/"

if [[ -f "$stage_dir/$services_file" ]]; then
  cp "$stage_dir/$services_file" "$stage_dir/services.xml"
fi

if [[ ! -f "$stage_dir/services.xml" ]]; then
  echo "Missing services.xml in staged Vespa app" >&2
  exit 1
fi

echo "Normalizing schema embedding dimensions to v[$dims]"
python3 - "$stage_dir/schemas" "$dims" <<'PY'
from pathlib import Path
import sys

schemas = Path(sys.argv[1])
dims = sys.argv[2]
for path in schemas.glob("*.sd"):
    text = path.read_text()
    text = text.replace("v[DIMS]", f"v[{dims}]")
    text = text.replace("v[384]", f"v[{dims}]")
    text = text.replace("v[768]", f"v[{dims}]")
    path.write_text(text)
PY

echo "Deploying Vespa app to $target"
if command -v vespa >/dev/null 2>&1; then
  vespa deploy --wait 960 --target "$target" "$stage_dir"
  vespa status --wait 75 --target "$target"
else
  COPYFILE_DISABLE=1 tar --exclude='._*' -czf "$archive" -C "$stage_dir" .
  session_id="$(
    curl -fsS \
      -H "Content-Type:application/x-gzip" \
      --data-binary "@$archive" \
      "$target/application/v2/tenant/default/session" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["session-id"])'
  )"
  curl -fsS -X PUT "$target/application/v2/tenant/default/session/${session_id}/prepared" >/dev/null
  curl -fsS -X PUT "$target/application/v2/tenant/default/session/${session_id}/active" >/dev/null
fi

echo "Vespa app deployed."
