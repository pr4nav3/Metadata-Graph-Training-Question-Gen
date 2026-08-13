# Migration Diff

This file records every intentional behavioral difference between the source
metadata-question pipeline and this standalone copy.

## Unchanged Safety Mechanisms

- `global_supervisor.py` retains the original non-blocking
  `global_supervisor.lock` around supervisor ticks.
- `run_frontier_batch.py` retains the original exclusive `review_export.lock`
  around dataset export.
- No additional worker, reviewer, or explorer lock was added.

## Portable Paths And Configuration

- Repository paths are derived from each script's location.
- Generated run cards, prompts, snapshots, and decisions store repo-relative
  paths instead of machine-specific paths.
- OpenCode commands quote paths and pass `--dir <repo-root>`, which is required
  because this repository's directory name contains spaces and may be nested
  under another Git repository.
- OpenCode temporary workspace names include a repository namespace to prevent
  collisions with another checkout.
- Vespa URLs, ports, and Docker container names come from environment variables.
- `run_with_server_env.sh` loads `server/.env.metadata-kg` after `server/.env`.
- Previously embedded LLM credentials in legacy graph utilities were replaced
  with environment variables so the repository can be pushed safely.

## Isolated Infrastructure

- `deployment/docker-compose.yml` is based on the source
  `deployment/docker-compose.dev.yml` Vespa, TEI, and Postgres services.
- Container names and host ports are namespaced so this stack cannot silently
  connect to the source repository's services.
- The source Vespa memory settings are unchanged: 8 GB container limit, 4 GB
  config-server maximum heap, and 2 GB config-proxy maximum heap.
- There is no `VESPA_IGNORE_NOT_ENOUGH_MEMORY` override.
- TEI downloads `intfloat/multilingual-e5-large-instruct` into a persistent
  Docker volume. Vespa still uses the same model, endpoint, and 1024 dimensions.

## Inherited Defensive Fixes

Two issues reproduced against the source code and copied historical data:

1. `run_frontier_batch.py` now treats a row as pending review only when it has a
   valid question ID. This prevents nested objects recovered from malformed,
   pretty-printed historical JSONL from becoming false review targets.
2. `global_supervisor.py` accepts extracted JSON only when its top-level
   `action` is in the existing executor allowlist. This prevents incidental JSON
   in model reasoning, such as `{"max_reviews": 1}`, from replacing the actual
   supervisor decision.

Regression tests cover both cases under
`scripts/metadata-graph/opencode-kimi/tests/`.

## Distribution Boundary

Git contains source, configuration, small reference datasets, and pipeline state.
The corpus and graph artifacts are manually downloaded. Hydration, Docker data,
logs, and transient locks are regenerated locally and ignored.

The separate eval-question-generation pipeline and the older
`hydrated-generation/` experiment are not dependencies of this metadata training
pipeline and are excluded from the standalone repository.
