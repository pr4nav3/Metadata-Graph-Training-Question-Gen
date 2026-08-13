#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

required_files=(
  "server/.env"
  "SEBI-14K-share/SEBI-14K-MANIFEST.txt"
  "SEBI-14K-share/data/vespa-prebuilt/vespa-var.tar.gz"
  "SEBI-14K-share/source-files/files/scrape_state.db"
  "scripts/metadata-graph/output_v2/sebi_metadata_graph_v2.sqlite"
  "questions/Kimi+Opencode_Questions.csv"
  "questions/SEBI_questions_answers.csv"
)

for path in "${required_files[@]}"; do
  if [[ ! -f "$path" ]]; then
    echo "FAIL missing required file: $path" >&2
    exit 2
  fi
done

for command in docker python3 opencode rg; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "FAIL missing command: $command" >&2
    exit 2
  fi
done

python3 - <<'PY'
import sqlite3
from pathlib import Path

path = Path("scripts/metadata-graph/output_v2/sebi_metadata_graph_v2.sqlite")
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if not {"nodes", "edges"}.issubset(tables):
    raise SystemExit("FAIL graph database is missing nodes/edges tables")
nodes = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
conn.close()
if nodes <= 0 or edges <= 0:
    raise SystemExit("FAIL graph database is empty")
print(f"PASS graph database: nodes={nodes} edges={edges}")
PY

if command -v rg >/dev/null 2>&1; then
  if rg -n '/Users/[^/]+/|xyne-search' scripts/metadata-graph \
    --glob '*.py' --glob '*.sh' --glob '!**/output/**' --glob '!**/output_v2/**' \
    --glob '!**/graph/**' --glob '!**/graph_v2/**'; then
    echo "FAIL machine-specific path remains in active metadata code" >&2
    exit 2
  fi
fi

rg -q 'global_supervisor\.lock' scripts/metadata-graph/opencode-kimi/global_supervisor.py
rg -q 'review_export\.lock' scripts/metadata-graph/opencode-kimi/run_frontier_batch.py
echo "PASS original supervisor and export locks are present"

python3 -m unittest discover \
  -s scripts/metadata-graph/opencode-kimi/tests \
  -p 'test_*.py'

docker compose --env-file server/.env.metadata-kg \
  -f deployment/docker-compose.yml config -q
echo "PASS Docker Compose configuration"

./scripts/metadata-graph/opencode-kimi/run_with_server_env.sh \
  python3 scripts/metadata-graph/opencode-kimi/search_health_check.py --json

./scripts/metadata-graph/opencode-kimi/run_with_server_env.sh \
  python3 scripts/metadata-graph/opencode-kimi/global_supervisor.py snapshot \
  --opencode-model "${OPENCODE_PIPELINE_MODEL:-litellm/private-large}"

echo "PASS standalone metadata pipeline verification"
