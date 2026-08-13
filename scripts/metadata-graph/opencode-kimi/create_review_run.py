#!/usr/bin/env python3
"""Create one Kimi review run card."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from coverage_store import normalize_ws
from run_identity import card_memory_id, card_run_id, frontier_memory_id


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
REPO_ROOT = METADATA_GRAPH_DIR.parent.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
PIPELINE_GOAL_PATH = SCRIPT_DIR / "handoff" / "PIPELINE_GOAL.md"


def repo_display(path: Path | str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return str(candidate)
    try:
        return str(candidate.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(candidate)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def question_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def write_prompt(card: dict[str, Any], prompt_path: Path) -> None:
    memory_lines: list[str] = []
    if normalize_ws(card.get("memory_path")):
        memory_lines = [f"- Same-memory context: {card['memory_path']}"]
    lines = [
        "# Frontier Question Review",
        "",
        "Read these files first:",
        f"- Pipeline goal: {card['pipeline_goal']}",
        f"- Review run card: {card['review_run_card_path']}",
        f"- Reviewer instructions: {card['reviewer_instructions']}",
        f"- Question taste guide: {card['question_taste_guide']}",
        f"- Original generation run card: {card['source_run_card_path']}",
        f"- Generated questions: {card['questions_path']}",
        "",
        "Use these supporting artifacts if they exist:",
        f"- Memory choice: {card['source_memory_choice_path']}",
        f"- Generator summary: {card['source_summary_path']}",
        f"- Generator prompt: {card['source_prompt_path']}",
        *memory_lines,
        "",
        "Run this mechanical check before judging taste:",
        f"`python3 {card['mechanical_check_cli']} {card['questions_path']}`",
        "",
        "Review contract:",
        "- Review this run as one memory region. Compare rows against each other.",
        "- Verify cited evidence with `sebi_research.py around` or `chunks` before accepting a row.",
        "- Do not rewrite questions and do not create new questions.",
        "- Use `accepted`, `rejected`, or `pending` only.",
        f"- Write decisions JSON to `{card['decisions_path']}`.",
        f"- Write a short review summary to `{card['review_summary_path']}`.",
        "- Do not export to CSV yourself; the pipeline exporter handles that after decisions are written.",
        "",
        "Decision JSON schema:",
        "```json",
        "{",
        f'  "run_id": "{card["run_id"]}",',
        '  "reviewed_by": "kimi_frontier_reviewer",',
        '  "decisions": [',
        "    {",
        '      "id": "question id",',
        '      "status": "accepted|rejected|pending",',
        '      "notes": "short evidence/taste reason",',
        '      "quality_scores": {',
        '        "evidence_support": 1,',
        '        "multi_doc_necessity": 1,',
        '        "human_shape": 1,',
        '        "difficulty": 1,',
        '        "novelty": 1',
        "      }",
        "    }",
        "  ]",
        "}",
        "```",
    ]
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-card", type=Path)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = normalize_ws(args.run_id)
    run_card_path = args.run_card or args.output_root / "run_cards" / f"{run_id}.json"
    if not run_card_path.exists():
        print(f"run card not found: {run_card_path}", file=sys.stderr)
        return 2

    source = load_json(run_card_path)
    source_run_id = card_run_id(source)
    if source_run_id != run_id:
        print(
            f"run id mismatch: requested={run_id} card={source_run_id}",
            file=sys.stderr,
        )
        return 2

    questions_path = Path(normalize_ws(source.get("output_path")))
    question_count = question_row_count(questions_path)
    if question_count == 0:
        print(f"no generated question rows to review: {questions_path}", file=sys.stderr)
        return 2

    review_run_cards_dir = args.output_root / "review_run_cards"
    review_prompts_dir = args.output_root / "review_prompts"
    decisions_dir = args.output_root / "review_decisions"
    review_summaries_dir = args.output_root / "review_summaries"
    for path in [review_run_cards_dir, review_prompts_dir, decisions_dir, review_summaries_dir]:
        path.mkdir(parents=True, exist_ok=True)

    review_run_card_path = review_run_cards_dir / f"{run_id}.json"
    prompt_path = review_prompts_dir / f"{run_id}.md"
    decisions_path = decisions_dir / f"{run_id}.json"
    review_summary_path = review_summaries_dir / f"{run_id}.md"

    if decisions_path.exists() and decisions_path.stat().st_size > 0 and not args.overwrite:
        print(f"review decisions already exist: {decisions_path}", file=sys.stderr)
        return 3

    memory_id = frontier_memory_id(source) or card_memory_id(source) or run_id
    memory_path = normalize_ws(source.get("memory_path"))
    if not memory_path and normalize_ws(source.get("frontier_id")):
        memory_path = str(args.output_root / "memory" / f"{memory_id}.jsonl")

    card = {
        "review_run_id": f"review_{run_id}",
        "run_id": run_id,
        "memory_id": memory_id,
        "frontier_id": normalize_ws(source.get("frontier_id")),
        "created_at": int(time.time()),
        "question_count": question_count,
        "source_run_card_path": repo_display(run_card_path),
        "source_prompt_path": normalize_ws(source.get("prompt_path")),
        "source_summary_path": normalize_ws(source.get("summary_path")),
        "source_memory_choice_path": normalize_ws(source.get("memory_choice_path")),
        "memory_path": memory_path,
        "pipeline_goal": repo_display(PIPELINE_GOAL_PATH),
        "questions_path": repo_display(questions_path),
        "decisions_path": repo_display(decisions_path),
        "review_summary_path": repo_display(review_summary_path),
        "prompt_path": repo_display(prompt_path),
        "review_prompt_path": repo_display(prompt_path),
        "review_run_card_path": repo_display(review_run_card_path),
        "reviewer_instructions": repo_display(SCRIPT_DIR / "REVIEWER_INSTRUCTIONS.md"),
        "question_taste_guide": repo_display(SCRIPT_DIR / "handoff" / "QUESTION_TASTE_GUIDE.md"),
        "research_cli": normalize_ws(source.get("research_cli")) or repo_display(SCRIPT_DIR / "sebi_research.py"),
        "mechanical_check_cli": repo_display(SCRIPT_DIR / "mechanical_check_outputs.py"),
        "export_cli": repo_display(SCRIPT_DIR / "review_and_export_questions.py"),
    }
    review_run_card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_prompt(card, prompt_path)
    print(str(review_run_card_path))
    print(str(prompt_path))
    print(str(decisions_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
