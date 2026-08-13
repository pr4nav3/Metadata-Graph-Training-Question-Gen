# Metadata Knowledge Graph Training Question Gen

Standalone copy of the metadata-knowledge-graph training-question pipeline. The
original pipeline was not edited during this migration.

Start with [SETUP_INSTRUCTIONS.md](SETUP_INSTRUCTIONS.md). Every intentional
difference from the source copy is recorded in [MIGRATION_DIFF.md](MIGRATION_DIFF.md).

## What Is Included

- Kimi/OpenCode question pipeline: `scripts/metadata-graph/opencode-kimi/`
- Graph builders and audit utilities: `scripts/metadata-graph/*.py`
- Downloaded metadata graph artifacts: `scripts/metadata-graph/graph_v2/` and
  `scripts/metadata-graph/output_v2/`
- Pipeline state and generated rows: `scripts/metadata-graph/output/opencode_kimi/`
- Required question reference/export data under `questions/`
- Local runtime services: `deployment/docker-compose.yml`
- Downloaded SEBI source/Vespa package: `SEBI-14K-share/`
- Local, uncommitted environment file: `server/.env`
- Non-secret standalone defaults: `server/.env.metadata-kg`
- Vespa application config: `server/platform/vespa/`

The corpus, graph artifacts, hydration cache, Docker volumes, and logs are not
committed. The corpus and graph are downloaded manually; hydration and runtime
state are generated locally.

## Run From Repo Root

Use the wrapper so `server/.env` and `server/.env.metadata-kg` are loaded:

```bash
./scripts/metadata-graph/opencode-kimi/run_with_server_env.sh \
  python3 scripts/metadata-graph/opencode-kimi/search_health_check.py --json
```

## Verify Setup

After following the setup instructions, run:

```bash
./scripts/verify.sh
```

## Main Commands

```bash
python3 scripts/metadata-graph/opencode-kimi/global_supervisor.py snapshot
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py status
python3 scripts/metadata-graph/opencode-kimi/validate_frontier_candidates.py --json
```

For LLM-backed worker/reviewer runs, use the commands in
`scripts/metadata-graph/opencode-kimi/handoff/COMMANDS.md`.

## Portability Notes

Copied historical run cards and prompts use repo-relative paths. Newly generated
artifacts remain repo-relative when commands are run from this folder's root.

`server/.env` contains local credentials and is intentionally ignored.
