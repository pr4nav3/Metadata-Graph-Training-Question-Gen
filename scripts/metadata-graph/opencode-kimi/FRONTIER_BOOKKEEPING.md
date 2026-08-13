# Frontier Bookkeeping

This pipeline has two separate jobs:

1. A model or human supervisor keeps a compact frontier ledger of underexplored
   evidence regions when the user explicitly asks for ledger refresh.
2. Kimi/OpenCode workers take one frontier or seedless run, explore one
   natural memory region, and generate only the strong multi-document questions the
   evidence naturally supports.

The frontier ledger is planning memory. It is not evidence and it is not a list
of desired question types.

Explorer-led ledger refresh uses one compact memory artifact derived from the
ledger:

- `scripts/metadata-graph/output/opencode_kimi/previous_explored_regions.md`

This artifact is for avoiding repeated regions only. It intentionally excludes
graph coverage statistics, density rankings, and topic-priority suggestions so
the explorer can spend its reasoning budget on graph/corpus exploration.

## Current Pipeline

1. `frontier_ledger.py` stores and updates the backlog at
   `scripts/metadata-graph/output/opencode_kimi/frontier_ledger.jsonl`.
2. `run_frontier_batch.py prepare` or
   `create_frontier_run.py --frontier-id <id>` copies one selected
   frontier into the run card, marks it `assigned`, and creates the
   prompt plus uniqueness packet.
3. The worker reads the run card, instructions, and uniqueness packet. It
   explores freely inside/around the assigned evidence region.
4. `mechanical_check_outputs.py` validates output JSONL shape, doc IDs, chunk
   IDs, exact duplicate question text, and minimum multi-doc structure.
5. `run_frontier_batch.py sync` or
   `update_coverage_from_run.py --run-card <run-card.json>` syncs questions,
   summary leads, and frontier outcome back into coverage/ledger state. Summary
   leads are clues for the next explicit ledger-refresh pass; they are not
   imported into the ready queue automatically.

## Ledger Fields

Each JSONL row is one frontier snapshot:

- `frontier_id`: stable id, usually `frontier_0001`.
- `status`: `ready`, `continue_generation`, `assigned`, `running`,
  `needs_review`, `saturated`, `rejected`, or `duplicate`.
- `title`: short human label.
- `kind`: where the frontier came from, such as `manual`, `exploratory`, or
  `graph_neighborhood`.
- `seed_doc_ids`: 0-4 useful starting documents. Workers may expand beyond
  these.
- `candidate_doc_ids`: optional nearby docs for context. This is not a hard
  boundary.
- `scope_hint`: concise directional hint explaining why the docs may be worth
  reading together. It is not evidence; avoid exact deltas, thresholds, legal
  conclusions, and final answers.
- `why_unexplored`: why the supervisor believes this region deserves a pass.
- `avoid`: specific prior-work warning, if known.
- `source` / `source_ids`: compact pointers to the explorer session or notes
  that produced the row.
- `assigned_run_id` / `memory_id`: run linkage and durable memory bucket.
- `outcome`: final one-line outcome after a worker reports back.
- `notes`: short Codex planning notes only.

Do not store full summaries, answers, chunks, long document dumps, or every
candidate thought in the ledger. Those stay in run artifacts and coverage DB.

## Frontier Status Meaning

- `ready`: available for a worker.
- `continue_generation`: same memory region should receive another small pass because
  concrete same-memory targets remain.
- `assigned`: run card created, worker may not have started.
- `running`: partial output exists but no final summary.
- `needs_review`: Codex should inspect before crossing out or rerunning.
- `saturated`: handled; do not schedule again unless a new lead appears.
- `rejected`: explored or reviewed and not useful as a frontier.
- `duplicate`: same practical region as existing completed work.

Only explicit worker outcomes should create terminal statuses. If a summary is
missing the `Frontier outcome:` line, the sync path marks the frontier
`needs_review` instead of crossing it out.

## Worker Contract

Workers must not force question types. The assigned frontier is an evidence
starting point only. The human set at `questions/SEBI_questions_answers.csv` is
for tone and difficulty patterns; generated questions should still vary
naturally by what the documents support.

Each final summary should include this exact line near the top:

`Frontier outcome: saturated | continue_generation | weak | duplicate | too_broad | no_multidoc_angle | needs_review - one sentence reason`

This line is the only structured outcome the ledger needs. Everything else can
remain concise prose.

If the worker handles one memory region but sees useful same-memory question targets
remaining, it should use `continue_generation` and name those targets in the
summary. If it sees a genuinely different region, it may mention that as a
concise future lead for the next explicit ledger-refresh pass. Do not use
`continue_generation` for a vague "maybe more exists"; use `saturated` when no
concrete same-memory target remains.

## Unattended Batch Flow

The supervisor should populate enough `ready` rows for a batch only when the
user asks for ledger refresh, then stop. The batch runner can drain those rows
without the supervisor staying alive:

```bash
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py prepare --max-runs 20
```

This creates run cards and marks those frontiers `assigned`. If workers are
started outside the runner, call sync later:

```bash
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py sync
```

If a stable worker command is available, the runner can launch workers itself:

```bash
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py run \
  --max-runs 20 \
  --parallel 5 \
  --worker-command 'opencode run -m litellm/claude-opus-4-6 "Read and execute the run prompt at {prompt_path}"'
```

The worker command is a template. Available placeholders are:

- `{run_id}`
- `{run_card_path}`
- `{memory_id}`
- `{frontier_id}`
- `{prompt_path}`
- `{output_path}`
- `{summary_path}`
- `{cwd}`

The runner logs batch events under
`scripts/metadata-graph/output/opencode_kimi/batch_runs/<batch_id>/`.

## Useful Commands

```bash
python3 scripts/metadata-graph/opencode-kimi/frontier_ledger.py list --status ready
python3 scripts/metadata-graph/opencode-kimi/frontier_ledger.py next --json
python3 scripts/metadata-graph/opencode-kimi/frontier_ledger.py export-md
python3 scripts/metadata-graph/opencode-kimi/frontier_ledger.py export-explored-regions
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py status
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py prepare --max-runs 5
python3 scripts/metadata-graph/opencode-kimi/run_frontier_batch.py sync
python3 scripts/metadata-graph/opencode-kimi/create_frontier_run.py --frontier-id frontier_0001
python3 scripts/metadata-graph/opencode-kimi/update_coverage_from_run.py --run-card scripts/metadata-graph/output/opencode_kimi/run_cards/<run_id>.json
```

Add frontiers after model-guided review of the graph, corpus, and generated
artifacts:

```bash
python3 scripts/metadata-graph/opencode-kimi/frontier_ledger.py add \
  --title "Short evidence-region label" \
  --kind manual \
  --seed-doc-id clf-example \
  --scope-hint "Explore this concrete evidence neighborhood; candidate axes: ..." \
  --why-unexplored "No prior accepted questions cover this practical region." \
  --avoid "Do not repeat the nearby completed memory region on ..."
```

For an explorer refresh, first regenerate the previous-region memory, then have
Codex/OpenCode read `FRONTIER_DISCOVERY_PLAYBOOK.md` and
`previous_explored_regions.md`. Do not give the explorer coverage-stat snapshots
unless the user asks for stats-informed exploration.

## OpenCode/Kimi Migration Note

Codex access is temporary, so this pipeline is being transferred to a pure
OpenCode/Kimi operating model. The durable handoff lives in
`scripts/metadata-graph/opencode-kimi/handoff/`.

Target shape:

1. An external heartbeat (`launchd`, cron, or a bounded loop) periodically runs
   `global_supervisor.py once` or `global_supervisor.py loop`.
2. That script writes a deterministic snapshot of search health, active
   explorers/workers, recent batch events, ledger counts, frontier candidate
   files, artifacts, pending questions, and dataset state.
3. The heartbeat then wakes an OpenCode supervisor session with the snapshot and
   handoff index. The model writes one decision JSON; deterministic code
   executes only whitelisted actions.
4. The Kimi/OpenCode supervisor decides whether to wait, validate/promote
   frontier candidates, start explorers, diagnose failures, sync completed
   artifacts, mark ambiguous frontiers for review, run one question review, or
   start a small new question-generation batch with the selected
   `OPENCODE_PIPELINE_MODEL` value.
5. A per-run Kimi reviewer can run after a generation run completes.
   It reviews one run's generated rows as a set, writes structured
   decisions, and lets the exporter append only accepted rows to
   `questions/Kimi+Opencode_Questions.csv`.
6. Kimi/OpenCode workers now use a dedicated OpenCode config generated by
   `run_frontier_batch.py` for the selected `litellm/<model>`, with the LiteLLM
   API key read from the repo `.env` wrapper and a 20-minute provider stream
   timeout. If a worker times out after writing valid question rows, the runner
   should salvage mechanically valid partial rows, route them through
   review/export, and leave the frontier `needs_review` when the final summary
   is missing.
7. Explorer refresh is candidate-first: Kimi should use
   `FRONTIER_DISCOVERY_PLAYBOOK.md` and the graph/corpus tools to write
   `frontier_candidates/*.jsonl`. Batch run/sync does not auto-import summary
   bullets as ready frontiers, and explorers do not mutate the live ledger.

Keep this section updated as new failure modes or operating rules appear. Do not
expect any model to remember project policy across days unless the policy is in
this handoff pack or encoded in scripts.
