# Pipeline State Machine

The pipeline is file-backed. Trust files and scripts over memory.

## Important Paths

- Ledger JSONL:
  `scripts/metadata-graph/output/opencode_kimi/frontier_ledger.jsonl`
- Human-readable ledger:
  `scripts/metadata-graph/output/opencode_kimi/frontier_ledger.md`
- Explorer previous-region memory:
  `scripts/metadata-graph/output/opencode_kimi/previous_explored_regions.md`
- Run cards:
  `scripts/metadata-graph/output/opencode_kimi/run_cards/`
- Prompts:
  `scripts/metadata-graph/output/opencode_kimi/prompts/`
- Uniqueness packets:
  `scripts/metadata-graph/output/opencode_kimi/uniqueness_packets/`
- Memory choices:
  `scripts/metadata-graph/output/opencode_kimi/memory_choices/`
- Memory files:
  `scripts/metadata-graph/output/opencode_kimi/memory/`
- Question JSONL:
  `scripts/metadata-graph/output/opencode_kimi/questions/`
- Review run cards:
  `scripts/metadata-graph/output/opencode_kimi/review_run_cards/`
- Review prompts:
  `scripts/metadata-graph/output/opencode_kimi/review_prompts/`
- Review decisions:
  `scripts/metadata-graph/output/opencode_kimi/review_decisions/`
- Review summaries:
  `scripts/metadata-graph/output/opencode_kimi/review_summaries/`
- Checkpoints:
  `scripts/metadata-graph/output/opencode_kimi/checkpoints/`
- Summaries:
  `scripts/metadata-graph/output/opencode_kimi/summaries/`
- Explorer run cards:
  `scripts/metadata-graph/output/opencode_kimi/explorer_run_cards/`
- Explorer prompts:
  `scripts/metadata-graph/output/opencode_kimi/explorer_prompts/`
- Explorer frontier candidates:
  `scripts/metadata-graph/output/opencode_kimi/frontier_candidates/`
- Frontier candidate validation reports:
  `scripts/metadata-graph/output/opencode_kimi/frontier_candidate_validations/`
- Batch events:
  `scripts/metadata-graph/output/opencode_kimi/batch_runs/<batch_id>/events.jsonl`
- Explorer batch events:
  `scripts/metadata-graph/output/opencode_kimi/explorer_batch_runs/<batch_id>/events.jsonl`
- Global supervisor snapshots/status:
  `scripts/metadata-graph/output/opencode_kimi/global_supervisor/`
- Export dataset:
  `questions/Kimi+Opencode_Questions.csv`

## Ledger Statuses

- `ready`: available for a run.
- `continue_generation`: productive memory region; schedule another small pass.
- `assigned`: run card exists; worker may not have started.
- `running`: worker started or partial output exists.
- `needs_review`: do not retry blindly; inspect.
- `saturated`: completed; do not schedule again.
- `rejected`: explored and not useful.
- `duplicate`: covered by existing practical question intent.

## Normal Flow

Explorer flow:

`OpenCode explorer -> frontier_candidates/*.jsonl -> deterministic validation -> ready ledger row | validation report with errors`

Question flow:

`ready | continue_generation -> assigned -> running -> saturated | continue_generation | rejected | duplicate | needs_review`

Question review is a separate gate after generation:

`generated pending rows -> question reviewer decisions -> exporter writes accepted rows`

The worker summary should contain:

`Frontier outcome: saturated | continue_generation | weak | duplicate | too_broad | no_multidoc_angle | needs_review - one sentence reason`

The sync script maps that line back to the ledger.

## Ambiguous State

If the ledger says `running` but no OS process exists, inspect the run
artifacts. If there is no output, no memory choice, no checkpoint, and no
summary, this is a stale/aborted run, not an active worker.

If a summary exists but no question JSONL exists, the frontier may still be
validly rejected or marked no-angle. Sync it rather than rerunning.

If question JSONL exists but summary is missing, keep `needs_review`; do not
cross out the frontier.

If question JSONL exists with `manual_review.status=pending`, do not export the
rows directly. Run a question review for that run or leave the rows pending.
