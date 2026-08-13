#!/usr/bin/env python3
"""Create one frontier-memory OpenCode/Kimi run card."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
REPO_ROOT = METADATA_GRAPH_DIR.parent.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
PIPELINE_GOAL_PATH = SCRIPT_DIR / "handoff" / "PIPELINE_GOAL.md"

from build_uniqueness_packet import DEFAULT_QUESTION_PATTERNS, build_packet, resolve_patterns  # noqa: E402
from coverage_store import DEFAULT_COVERAGE_DB, connect, upsert_run  # noqa: E402
from frontier_ledger import DEFAULT_LEDGER_PATH, find_frontier, frontier_context, load_ledger, update_ledger_frontier  # noqa: E402
from run_identity import default_memory_id, frontier_memory_id, next_run_id, normalize_ws  # noqa: E402
from sebi_retrieval import DataUnavailable, GraphStore, default_graph_db  # noqa: E402


DEFAULT_GRAPH_DB = default_graph_db(METADATA_GRAPH_DIR)
DEFAULT_HUMAN_REFERENCE = "questions/SEBI_questions_answers.csv"
DEFAULT_SCOPE_HINT = (
    "Choose one under-covered multi-document SEBI memory region yourself using "
    "the graph, corpus search, and document chunks. No topic, section, "
    "issuer, matter, or document family is chosen for you."
)


def repo_display(path: Path | str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return str(candidate)
    try:
        return str(candidate.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(candidate)


def seed_doc_view(graph: GraphStore, identifier: str) -> dict[str, Any]:
    doc = graph.document(identifier)
    doc_id = normalize_ws(doc.get("vespaDocId"))
    if not doc_id:
        raise DataUnavailable(f"seed document has no Vespa doc_id: {identifier}")
    return {
        "doc_id": doc_id,
        "graph_id": doc.get("graph_id") or "",
        "title": doc.get("title") or "",
        "date": doc.get("date") or "",
        "top_section": doc.get("topSection") or "",
        "section": doc.get("section") or "",
        "subsection": doc.get("subsection") or "",
        "detail_url": doc.get("detailUrl") or "",
    }


def write_prompt(card: dict[str, Any], prompt_path: Path) -> None:
    run_card_path = card["run_card_path"]
    continuation_lines: list[str] = []
    if card.get("continuation_pass"):
        continuation_lines = [
            "Continuation run:",
            "- This is not a fresh frontier hunt. This is a small same-memory continuation pass.",
            "- Still follow the normal agent instructions below for QA style, evidence standards, output rows, and summaries.",
            "- Start by reading the run card, memory file, and uniqueness packet so you know what has already been asked.",
            "- Before broad searching, be clear about the specific new same-memory target you are pursuing.",
            "- Zero new questions is a valid successful outcome when the frontier is exhausted.",
            "- If you cannot name a concrete new same-memory target, stop and write the final summary with `Frontier outcome: saturated`, `duplicate`, or `no_multidoc_angle` as appropriate.",
            "- Do not keep searching just because adjacent documents exist. Adjacent but different regions belong in Future Leads, not this continuation pass.",
            "",
        ]
    frontier_lines: list[str] = []
    if normalize_ws(card.get("frontier_id")):
        frontier_lines = [
            "",
            "Frontier context:",
            f"- Start from frontier `{card['frontier_id']}`: {card.get('frontier_title', '')}",
            f"- Why this is open: {card.get('frontier_why_unexplored', '')}",
            f"- Avoid/reuse note: {card.get('frontier_avoid', '')}",
            "- The frontier is a starting evidence region, not a question type and not a hard document boundary.",
        ]
    memory_lines: list[str] = []
    if card.get("continuation_pass"):
        memory_lines = [
            "",
            "Continuation memory:",
            f"- Read `{card['memory_path']}` before writing questions.",
            "- It lists prior same-memory question intents. It is memory, not evidence.",
            "- Stay in this memory region and add only new memory-worthy questions.",
            "- Write at most 10 new rows in this continuation pass; stop earlier if no distinct memory target remains.",
        ]
    lines = [
        "# Frontier Memory OpenCode Run",
        "",
        *continuation_lines,
        "Read these files first:",
        f"- {card['pipeline_goal']}",
        f"- {run_card_path}",
        f"- {card['agent_instructions']}",
        f"- {card['uniqueness_packet_path']}",
        "",
        "Run contract:",
        "- Follow the agent instructions. Work on one general memory region only.",
        "- If no seed docs are supplied, choose the memory region yourself from graph/corpus leads.",
        "- `pass_id` is a batch label. `scope_hint`, if present, is only a soft starting note.",
        "- Generate useful, difficult, human, truly multi-document questions that teach distinct memory targets and improve corpus coverage.",
        "- Treat uniqueness as duplicate control, not the goal.",
        "- Do not target a fixed count and do not start a second unrelated memory region.",
        *frontier_lines,
        *memory_lines,
        "",
        "Artifact discipline:",
        "- Copy the exact artifact paths below. Do not reconstruct them from the current directory or another worktree.",
        "- Every question row must copy `run_id`, `memory_id`, and `frontier_id` from the run card when those fields are present.",
        f"- After choosing the memory region, write a small JSON object to `{card['memory_choice_path']}` with selected docs, rejected nearby leads, and why this is one memory region.",
        f"- After each accepted question, immediately append one JSON object to `{card['output_path']}`. Do not wait until the end.",
        f"- Keep a short progress note at `{card['checkpoint_path']}` if you have inspected several docs but are not ready to write a question.",
        f"- At the end, write `{card['summary_path']}` with saturation reason, uniqueness decisions, docs inspected, rejected leads, and future leads.",
        "- If this run has frontier context, include `Frontier outcome: saturated | continue_generation | weak | duplicate | too_broad | no_multidoc_angle | needs_review - one sentence reason` near the top of the summary.",
        "- Be conservative: if useful same-memory work remains, use `continue_generation`; use `needs_review` only for ambiguity or broken evidence.",
        "",
        "Retrieval and dedup reminders:",
        "- Use `search-corpus-brief` for broad discovery, then targeted `doc-overview`, `search-doc`, `around`, and `chunks` for evidence.",
        "- The uniqueness packet is memory, not evidence. Reuse docs/chunks only for different practical intents and answer shapes.",
        "- Similarity is fine when the question teaches a distinct memorization target; reject only when it adds no meaningful new target.",
        "- Add `question_type` and `difficulty_features` to each output row, but never force a type or ratio.",
        "",
    ]
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--pass-id", default="kimi_run_pilot_001")
    parser.add_argument("--memory-id", default="")
    parser.add_argument("--seed-doc-id", action="append", default=[])
    parser.add_argument("--frontier-id", default="")
    parser.add_argument("--frontier-ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--scope-hint", default=DEFAULT_SCOPE_HINT)
    parser.add_argument("--graph-db", type=Path, default=DEFAULT_GRAPH_DB)
    parser.add_argument("--coverage-db", type=Path, default=DEFAULT_COVERAGE_DB)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--human-reference", default=DEFAULT_HUMAN_REFERENCE)
    parser.add_argument("--question-path", action="append", default=None)
    parser.add_argument("--max-prior-questions", type=int, default=25)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_cards_dir = args.output_root / "run_cards"
    questions_dir = args.output_root / "questions"
    summaries_dir = args.output_root / "summaries"
    prompts_dir = args.output_root / "prompts"
    packets_dir = args.output_root / "uniqueness_packets"
    choices_dir = args.output_root / "memory_choices"
    checkpoints_dir = args.output_root / "checkpoints"
    for path in [run_cards_dir, questions_dir, summaries_dir, prompts_dir, packets_dir, choices_dir, checkpoints_dir]:
        path.mkdir(parents=True, exist_ok=True)

    frontier_rows: list[dict[str, Any]] = []
    frontier: dict[str, Any] | None = None
    if args.frontier_id:
        try:
            frontier_rows = load_ledger(args.frontier_ledger)
            frontier = find_frontier(frontier_rows, args.frontier_id)
        except (KeyError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if frontier["status"] not in {"ready", "continue_generation", "needs_review"} and not args.overwrite:
            print(
                f"frontier is not ready for a run: {frontier['frontier_id']} status={frontier['status']}",
                file=sys.stderr,
            )
            return 2

    run_id = args.run_id or next_run_id(run_cards_dir)
    continuation_pass = bool(frontier and frontier["status"] == "continue_generation")
    prior_memory_id = frontier_memory_id(frontier) if frontier else ""
    memory_id = args.memory_id or (prior_memory_id if continuation_pass and prior_memory_id else default_memory_id(args.frontier_id, run_id))
    run_card_path = run_cards_dir / f"{run_id}.json"
    if run_card_path.exists() and not args.overwrite:
        print(f"run card already exists: {run_card_path}", file=sys.stderr)
        return 2

    seed_identifiers: list[str] = []
    if frontier:
        seed_identifiers.extend(frontier.get("seed_doc_ids") or [])
    seed_identifiers.extend(args.seed_doc_id)
    seed_identifiers = list(dict.fromkeys(normalize_ws(identifier) for identifier in seed_identifiers if normalize_ws(identifier)))

    seed_docs: list[dict[str, Any]] = []
    if seed_identifiers:
        graph = GraphStore(args.graph_db)
        try:
            seed_docs = [seed_doc_view(graph, identifier) for identifier in seed_identifiers]
        finally:
            graph.close()

    scope_hint = args.scope_hint
    if frontier and args.scope_hint == DEFAULT_SCOPE_HINT:
        scope_hint = frontier["scope_hint"]

    output_path = questions_dir / f"{run_id}.jsonl"
    summary_path = summaries_dir / f"{run_id}.md"
    uniqueness_packet_path = packets_dir / f"{run_id}.md"
    memory_choice_path = choices_dir / f"{run_id}.json"
    checkpoint_path = checkpoints_dir / f"{run_id}.md"
    prompt_path = prompts_dir / f"{run_id}.md"
    memory_path = args.output_root / "memory" / f"{memory_id}.jsonl"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.touch(exist_ok=True)

    card = {
        "run_id": run_id,
        "run_type": "frontier_memory_research",
        "pass_id": args.pass_id,
        "created_at": int(time.time()),
        "memory_id": memory_id,
        "continuation_pass": continuation_pass,
        "scope_hint": scope_hint,
        "seed_doc_ids": [doc["doc_id"] for doc in seed_docs],
        "seed_docs": seed_docs,
        "human_reference": args.human_reference,
        "pipeline_goal": repo_display(PIPELINE_GOAL_PATH),
        "avoid_question_paths": args.question_path or DEFAULT_QUESTION_PATTERNS,
        "uniqueness_packet_path": repo_display(uniqueness_packet_path),
        "memory_path": repo_display(memory_path),
        "coverage_db": repo_display(args.coverage_db),
        "agent_instructions": repo_display(SCRIPT_DIR / "AGENT_INSTRUCTIONS.md"),
        "research_cli": repo_display(SCRIPT_DIR / "sebi_research.py"),
        "memory_choice_path": repo_display(memory_choice_path),
        "checkpoint_path": repo_display(checkpoint_path),
        "output_path": repo_display(output_path),
        "summary_path": repo_display(summary_path),
        "prompt_path": repo_display(prompt_path),
    }
    if frontier:
        card.update(frontier_context(frontier))
        card["frontier_ledger_path"] = repo_display(args.frontier_ledger)
    card["run_card_path"] = repo_display(run_card_path)
    run_card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    question_paths = resolve_patterns(card["avoid_question_paths"])
    conn = connect(args.coverage_db)
    try:
        upsert_run(conn, card, status="assigned")
        packet = build_packet(
            card,
            conn,
            question_paths,
            max_prior_questions=args.max_prior_questions,
        )
    finally:
        conn.close()
    uniqueness_packet_path.write_text(packet, encoding="utf-8")
    write_prompt(card, prompt_path)
    if frontier:
        update_ledger_frontier(
            args.frontier_ledger,
            frontier["frontier_id"],
            status="assigned",
            run_id=run_id,
            memory_id=memory_id,
        )
    print(str(run_card_path))
    print(str(prompt_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
