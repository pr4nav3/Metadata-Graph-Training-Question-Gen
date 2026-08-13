# Known Failures And Recovery

This file should grow whenever the pipeline reveals a new failure mode.

## Semantic Vespa HTTP 500

Symptom:

- lexical search works
- semantic search fails with HTTP 500
- error mentions `Embedding API call failed` and `tei-batch-proxy`

Known root cause:

- Vespa is configured to call `http://tei-batch-proxy:8080/v1/embeddings`
- `metadata-kg-tei-batch-proxy` is not running, not ready, or cannot load the embedding model

Response:

1. Run `python3 scripts/metadata-graph/opencode-kimi/search_health_check.py --json`.
2. If semantic fails, do not start question-generation workers.
3. Inspect `docker logs metadata-kg-tei-batch-proxy` and fix the embedding server or Vespa deployment first.

## No-Artifact Stall

Symptom:

- worker is alive for many minutes
- no `memory_choice`, `checkpoint`, `questions`, or `summary`
- logs show repeated searching or background delegation

Response:

1. Inspect stderr tail and batch events.
2. If it used background subagents, stop the worker and mark `needs_review`.
3. Do not retry immediately. Fix prompt/harness cause first.

## Stale Running Ledger Rows

Symptom:

- ledger status shows `running`
- no matching OS process exists
- run artifacts are absent or archived

Response:

1. Inspect the run card and batch event logs.
2. If no final artifacts exist, mark `needs_review` with a concrete note.
3. Export ledger markdown after repair.

## Subagent Leakage

Symptom:

- worker calls `call_omo_agent` or starts background exploration instead of
  writing its own artifacts

Response:

- For question-generation worker prompts, explicitly say:
  `Do not call background subagents. You are the worker.`

Supervisor agents may use their own judgment, but generation workers should not
delegate the core run.

## Duplicate Or Same-Intent Questions

Symptom:

- new question uses the same evidence and asks the same practical thing as a
  prior row with different wording

Response:

1. Trust the uniqueness packet as memory, not evidence.
2. Keep only the sharper practical intent.
3. Mark duplicates in review notes; do not export duplicates.

## Incomplete Reviewer Decisions

Symptom:

- a question JSONL still has pending rows
- the review decisions file exists but covers only some pending question IDs
- review/export repeats with `appended_rows=0` or otherwise makes no dataset
  progress

Known root cause:

- a reviewer produced a partial decisions file, or the exporter saw old
  decisions and skipped without completing the remaining pending rows

Response:

1. Do not manually edit question rows or decision files.
2. Choose `review_questions` for the affected pending run. The review runner
   checks decision coverage and can rerun review instead of re-exporting stale
   partial decisions.
3. If the same target still produces no progress after a rerun, choose a
   different legal action or `status_only` with the stuck run ID and evidence.

## OpenCode/LiteLLM Stream Timeout After Valid Rows

Symptom:

- worker stderr ends with `Error: The operation timed out`
- the isolated OpenCode log shows `stream error providerID=litellm modelID=kimi-latest`
- the question JSONL already contains valid rows, but the generator did not write
  the final summary

Known root cause:

- OpenCode is waiting on a LiteLLM/Kimi model stream, not a corpus search or
  shell command.
- The pipeline now writes a dedicated OpenCode config with a 20-minute provider
  timeout via `--opencode-provider-timeout-ms 1200000`.

Response:

1. Do not throw away valid rows.
2. Let `run_frontier_batch.py` salvage partial worker output: valid JSONL rows
   that pass the mechanical checker are synced, reviewed, and exported.
   If the worker left a truncated tail line, the runner preserves the raw file
   under `salvage_raw/`, drops only the invalid line, and checks the valid rows.
3. The frontier remains `needs_review` if the generator summary is missing,
   because the memory region may still have unexplored follow-up work.
4. If the partial rows fail mechanical checks, keep the frontier `needs_review`
   and inspect the JSONL before exporting anything.

## LiteLLM Model Access Or Hosting Changes

Symptom:

- worker stderr says the team is not allowed to access a model, or a previously
  hosted LiteLLM model has disappeared/reappeared
- manual `opencode run -m litellm/<model>` may work while the pipeline still
  tries the old model

Response:

1. Do not hand-edit or create ad hoc OpenCode config homes.
2. Switch every stage with one setting:
   `OPENCODE_PIPELINE_MODEL=litellm/private-large`.
3. For direct runner commands, also pass `--opencode-model litellm/private-large`
   and keep worker/reviewer commands on the `{opencode_model}` placeholder.
4. If generation and review intentionally use different models, set
   `OPENCODE_QUESTION_MODEL` and `OPENCODE_REVIEWER_MODEL`; the supervisor will
   pass the extra model into the generated config.
5. The generated config lives at
   `scripts/metadata-graph/output/opencode_kimi/opencode_config_home/opencode/opencode.json`
   and is regenerated by `run_frontier_batch.py`.
