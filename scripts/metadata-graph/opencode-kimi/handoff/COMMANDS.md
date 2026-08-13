# Commands

Run commands from this standalone folder's root:

`Metadata Knowledge Graph Training Question Gen/`

## Environment

Use the wrapper so the `.env` API key is loaded correctly:

```bash
./scripts/metadata-graph/opencode-kimi/run_with_server_env.sh <command>
```

The wrapper reads `server/.env` and maps `LITELLM_API_KEY` to `JUSPAY_API_KEY`
when needed.

## Model Selection

Default model for every OpenCode stage is `litellm/kimi-latest`.

To switch the whole supervisor/generator/reviewer pipeline, set one variable:

```bash
OPENCODE_PIPELINE_MODEL=litellm/private-large
```

Bare model names are accepted by the scripts and treated as LiteLLM models, so
`private-large` becomes `litellm/private-large`. Use
`OPENCODE_QUESTION_MODEL` or `OPENCODE_REVIEWER_MODEL` only when generation and
review should intentionally use different models.

Do not hand-edit `output/opencode_kimi/opencode_config_home/opencode/opencode.json`.
`run_frontier_batch.py` regenerates that config with the selected LiteLLM model
and timeout.

## Health

```bash
python3 scripts/metadata-graph/opencode-kimi/search_health_check.py --json
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py status
```

## Explorer Candidate Refresh

Explorers do not write to the live ledger. They write candidates under
`scripts/metadata-graph/output/opencode_kimi/frontier_candidates/`, then the
validator promotes only candidates that pass basic deterministic checks.

```bash
python3 scripts/metadata-graph/opencode-kimi/run_explorer_batch.py prepare --count 2
```

Launch OpenCode explorers:

```bash
./scripts/metadata-graph/opencode-kimi/run_with_server_env.sh \
  python3 scripts/metadata-graph/opencode-kimi/run_explorer_batch.py run \
  --count 2 \
  --parallel 2 \
  --opencode-model litellm/private-large
```

Validate and promote candidates:

```bash
python3 scripts/metadata-graph/opencode-kimi/validate_frontier_candidates.py --promote
```

## Global Supervisor Snapshot

```bash
python3 scripts/metadata-graph/opencode-kimi/global_supervisor.py snapshot
```

This writes:

- `scripts/metadata-graph/output/opencode_kimi/global_supervisor/status.md`
- `scripts/metadata-graph/output/opencode_kimi/global_supervisor/snapshots/snapshot_*.json`

## Run Global Supervisor

The global supervisor is the only unattended control loop. It wakes OpenCode for
judgment, but the supervisor wakeup itself is run without `--auto`; deterministic
Python code writes and executes the decision.

One supervised tick:

```bash
OPENCODE_PIPELINE_MODEL=litellm/private-large \
./scripts/metadata-graph/opencode-kimi/global_supervisor_heartbeat.sh
```

Bounded 24-hour loop at 8-minute intervals:

```bash
GLOBAL_SUPERVISOR_MODE=loop \
GLOBAL_SUPERVISOR_INTERVAL_SECONDS=480 \
GLOBAL_SUPERVISOR_DURATION_HOURS=24 \
OPENCODE_PIPELINE_MODEL=litellm/private-large \
./scripts/metadata-graph/opencode-kimi/global_supervisor_heartbeat.sh
```

Default concurrency caps are:

- total spawned pipeline LLM agents: at most 6
- explorer agents: at most 3
- worker-side agents: at most 3, including question workers and reviewers
- ready-frontier buffer target/cap: 50

This default split intentionally favors simultaneous exploration and
generation/review over filling all 6 slots with one kind of work.

The supervisor must be able to inspect active processes. If process scanning is
blocked, it fails closed and will not start or validate agent work.

Use `GLOBAL_SUPERVISOR_DRY_RUN=1` to inspect the wake prompt and allowed command
without sending anything to OpenCode.

To start this from an OpenCode chat, ask that chat to run the bounded loop
command above from repo root and keep it running. The instruction can be this
literal:

```text
From the `Metadata Knowledge Graph Training Question Gen/` folder, run the
global Kimi/OpenCode supervisor for 24 hours at 8-minute intervals using
GLOBAL_SUPERVISOR_MODE=loop GLOBAL_SUPERVISOR_INTERVAL_SECONDS=480
GLOBAL_SUPERVISOR_DURATION_HOURS=24 OPENCODE_PIPELINE_MODEL=litellm/private-large
./scripts/metadata-graph/opencode-kimi/global_supervisor_heartbeat.sh.
Do not edit code or repair the pipeline; just run the supervisor command and
report the status path.
```

To stop it when it is running in the foreground, interrupt that terminal with
Ctrl-C or ask the same OpenCode chat to stop the running command. If it was
started in the background, find the `global_supervisor.py loop` process and kill
that PID deliberately; do not kill worker or explorer processes unless you are
intentionally aborting active work.

## Start A Conservative Question Batch

```bash
./scripts/metadata-graph/opencode-kimi/run_with_server_env.sh \
  python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py run \
  --max-runs 1 \
  --parallel 1 \
  --poll-seconds 20 \
  --worker-timeout-seconds 1500 \
  --opencode-model litellm/private-large \
  --opencode-provider-timeout-ms 1200000 \
  --worker-command 'env XDG_CONFIG_HOME="{opencode_config_home}" XDG_DATA_HOME=/private/tmp/opencode-worker-{opencode_workspace_slug}-{opencode_model_slug}-{run_id} opencode run --dir "{cwd}" --agent judge -m {opencode_model} --auto --title {run_id} "Read and execute the run prompt at {prompt_path}. Follow the run card and agent instructions exactly. Write the required artifacts to the paths in the run card. Do not modify pipeline code. Do not call background subagents."'
```

When the same wakeup is allowed to review/export, add:

```bash
--reviewer-timeout-seconds 1500 \
--reviewer-command 'env XDG_CONFIG_HOME="{opencode_config_home}" XDG_DATA_HOME=/private/tmp/opencode-reviewer-{opencode_workspace_slug}-{opencode_model_slug}-{run_id} opencode run --dir "{cwd}" --agent judge -m {opencode_model} --auto --title review-{run_id} "Read and execute the review prompt at {prompt_path}. Write decisions to {decisions_path} and the review summary to {review_summary_path}. Do not generate new questions."'
```

## Sync

```bash
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py sync
```

## Export Accepted Questions

```bash
python3 scripts/metadata-graph/opencode-kimi/review_and_export_questions.py \
  --run-id <run_id> \
  --decisions-json <decisions.json>
```

Every exported row in `questions/Kimi+Opencode_Questions.csv` must include
`pipeline=metadata knowledge graph`.

## Run One Question Review

```bash
./scripts/metadata-graph/opencode-kimi/run_with_server_env.sh \
  python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py review \
  --run-id <run_id> \
  --opencode-model litellm/private-large \
  --reviewer-timeout-seconds 1500 \
  --opencode-provider-timeout-ms 1200000 \
  --reviewer-command 'env XDG_CONFIG_HOME="{opencode_config_home}" XDG_DATA_HOME=/private/tmp/opencode-reviewer-{opencode_workspace_slug}-{opencode_model_slug}-{run_id} opencode run --dir "{cwd}" --agent judge -m {opencode_model} --auto --title review-{run_id} "Read and execute the review prompt at {prompt_path}. Write decisions to {decisions_path} and the review summary to {review_summary_path}. Do not generate new questions."'
```

Omit `--run-id` and keep `--max-reviews 1` to let the script select the
oldest Kimi run with pending generated question rows. Add
`--include-non-kimi-reviews` only when you intentionally want to review old Opus
artifacts too.
