# Migration Plan

Goal: create a standalone copy of the metadata-knowledge-graph training question
generation pipeline without modifying the original xyne-search pipeline.

## Scope

- Copy the active Kimi/OpenCode pipeline code from `scripts/metadata-graph/opencode-kimi/`.
- Copy graph builder and audit utilities from `scripts/metadata-graph/`.
- Keep downloaded graph artifacts outside Git:
  - `scripts/metadata-graph/graph_v2/`
  - `scripts/metadata-graph/output_v2/`
- Copy pipeline state required for continuity:
  - `scripts/metadata-graph/output/opencode_kimi/`
  - `questions/`
- Document the manually downloaded `SEBI-14K-share/` package needed to restore
  Vespa, Postgres, and raw source files.
- Keep service configuration in Git and regenerate local service volumes.
- Regenerate the hydration cache on demand from Vespa; do not distribute it.

## Layout

The new folder keeps the same internal relative layout where that reduces risk:

```text
Metadata Knowledge Graph Training Question Gen/
  scripts/metadata-graph/...
  questions/...
  server/.env
  server/platform/vespa/...
  server/vespa-data/...  # generated locally, ignored
  server/xyne-data/...   # generated locally, ignored
  deployment/docker-compose.yml
```

## Portability Pass

- Rewrite copied absolute paths from the old repo root to repo-relative paths.
- Keep generated artifacts portable by using repo-relative paths, not a new
  machine-specific absolute path.
- Ignore secrets, downloaded artifacts, logs, caches, and mutable service data.
- Add env-driven defaults for Vespa URLs and Docker container names in the copy.
- Keep the original pipeline untouched.

## Verification

- Confirm no copied code or active state still references the old absolute repo path.
- Confirm the copied Python files compile.
- Run regression tests for inherited parser issues.
- Run lexical and semantic Vespa health checks.
- Run a supervisor-led generation and review/export acceptance sequence.

## Rollback

Delete only this new folder if the migration is not wanted. No original files are
edited by this migration.
