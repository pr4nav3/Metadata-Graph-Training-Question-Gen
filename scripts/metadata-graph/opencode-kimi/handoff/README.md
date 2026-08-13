# Kimi/OpenCode Handoff Pack

This directory is the durable memory for the pure OpenCode/Kimi version of the
SEBI metadata-graph question pipeline.

At each supervisor wakeup, read this file first, then the specific files that
match the situation. Do not load everything reflexively.

## Core Files

- `PIPELINE_GOAL.md`: the one-paragraph north star for why the pipeline exists:
  train SEBI-doc memory through hard, evidence-grounded QA coverage.
- `SUPERVISOR_RUNBOOK.md`: what the global Kimi/OpenCode supervisor is
  responsible for on each heartbeat-style wakeup.
- `PIPELINE_STATE_MACHINE.md`: ledger statuses, batch events, artifacts, and
  safe state transitions.
- `FRONTIER_DISCOVERY_PLAYBOOK.md`: how OpenCode explorers propose useful
  frontier candidates when the ledger needs replenishing.
- `../../output/opencode_kimi/previous_explored_regions.md`: generated compact
  memory of queued, active, saturated, rejected, and duplicate frontier regions.
  Regenerate it with `frontier_ledger.py export-explored-regions` before an
  explorer-led candidate refresh.
- `QUESTION_TASTE_GUIDE.md`: what makes generated questions good, human-shaped,
  difficult, and varied.
- `KNOWN_FAILURES_AND_RECOVERY.md`: observed failures and the preferred response
  when they recur.
- `COMMANDS.md`: exact commands and model/env conventions.
- `../REVIEWER_INSTRUCTIONS.md`: the compact role prompt for the per-run
  Kimi reviewer.

## Role Split

The supervisor is not the explorer, question generator, or reviewer. It wakes
up, checks infrastructure and pipeline state, asks OpenCode/Kimi for one bounded
JSON decision without `--auto`, then deterministic code writes that decision and
executes only whitelisted actions:
validate/promote frontier candidates, start a small explorer batch, start a
small question batch, run one review, sync, or write status. Explorer workers
write `frontier_candidates/*.jsonl`, not the live ledger. Question-generation
workers receive one run card and produce memory-region artifacts. Reviewer
workers receive one completed run and decide which generated rows are accepted,
rejected, or left pending for a human.

Hard cap: at most 6 spawned pipeline LLM agents total. Explorers may use at
most 3 slots, and worker-side agents may use at most 3 slots. Worker-side agents
include question-generation workers and reviewers. This split is deliberate:
the supervisor should keep exploration and question generation/review moving
side by side instead of letting one class of work consume the whole pool. Keep
the ready-frontier buffer around 50; do not grow it just to hoard supply.

Use intelligence, but keep state external. If a policy matters tomorrow, it
belongs in this directory or in a script, not only in a conversation.
