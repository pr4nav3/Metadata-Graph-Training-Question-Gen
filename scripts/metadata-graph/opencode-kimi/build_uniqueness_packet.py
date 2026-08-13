#!/usr/bin/env python3
"""Build a focused uniqueness packet for one Kimi frontier run."""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from coverage_store import DEFAULT_COVERAGE_DB, connect, load_question_rows, normalize_ws, supporting_docs
from run_identity import card_memory_id, card_run_id, row_run_id


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
REPO_ROOT = METADATA_GRAPH_DIR.parent.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"

DEFAULT_QUESTION_PATTERNS = [
    "questions/gold_multi_doc_questions_codex_verified_20260720.jsonl",
    "scripts/metadata-graph/output/opencode_kimi/questions/*.jsonl",
]

COMMON_TERMS = {
    "and",
    "the",
    "for",
    "with",
    "from",
    "this",
    "that",
    "sebi",
    "under",
    "question",
    "document",
    "documents",
    "one",
    "two",
    "three",
    "what",
    "when",
    "where",
    "which",
    "while",
    "would",
    "could",
    "should",
    "changed",
    "change",
    "changes",
    "compare",
    "elastic",
    "kimi",
    "opencode",
    "run",
    "smoke",
    "test",
    "remain",
    "within",
    "directly",
    "fresh",
    "generation",
    "minimum",
    "operational",
    "parallel",
    "pilot",
    "related",
    "relating",
    "requirement",
    "requirements",
    "review",
    "supervised",
    "true",
    "requiring",
    "needed",
    "understand",
    "material",
    "framework",
    "securities",
    "exchange",
    "stock",
    "board",
    "corporation",
    "corporations",
    "draft",
    "india",
    "legal",
    "circular",
    "circulars",
    "master",
    "guideline",
    "guidelines",
    "regulation",
    "regulations",
    "dated",
    "disclosure",
    "disclosures",
    "issuance",
    "issued",
    "issue",
    "issues",
    "investor",
    "investors",
    "investment",
}


def repo_display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def truncate(value: str, limit: int) -> str:
    value = normalize_ws(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def resolve_patterns(patterns: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_pattern in patterns:
        pattern = normalize_ws(raw_pattern)
        if not pattern:
            continue
        candidate = Path(pattern)
        search_pattern = str(candidate if candidate.is_absolute() else REPO_ROOT / candidate)
        for match in sorted(glob.glob(search_pattern)):
            path = Path(match)
            if path.is_file() and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def keyword_set(*values: Any) -> set[str]:
    text = " ".join(normalize_ws(value).lower() for value in values if normalize_ws(value))
    return {token for token in re.findall(r"[a-z0-9]{3,}", text) if token not in COMMON_TERMS}


def normalized_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [normalize_ws(item) for item in value if normalize_ws(item)]


def run_keywords(run_card: dict[str, Any]) -> set[str]:
    seed_titles = [doc.get("title") for doc in run_card.get("seed_docs") or [] if isinstance(doc, dict)]
    return keyword_set(
        run_card.get("scope_hint"),
        run_card.get("frontier_title"),
        run_card.get("frontier_scope_hint"),
        run_card.get("frontier_why_unexplored"),
        *seed_titles,
    )


def run_seed_doc_ids(run_card: dict[str, Any]) -> set[str]:
    return {normalize_ws(doc_id) for doc_id in run_card.get("seed_doc_ids") or [] if normalize_ws(doc_id)}


def prior_questions_from_files(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in load_question_rows(paths):
        question = normalize_ws(row.get("question"))
        if not question:
            continue
        doc_ids, titles = supporting_docs(row)
        rows.append(
            {
                "question_id": normalize_ws(row.get("id")) or f"{Path(row.get('_source_file', '')).stem}:{row.get('_line_number')}",
                "run_id": row_run_id(row),
                "question": question,
                "doc_ids": doc_ids,
                "doc_titles": titles,
                "chunk_ids": normalized_string_list(row.get("supporting_chunks")),
                "question_type": normalize_ws(row.get("question_type")),
                "difficulty_features": normalized_string_list(row.get("difficulty_features")),
                "source_file": repo_display(Path(row.get("_source_file", ""))),
                "source": normalize_ws(row.get("source")),
            }
        )
    return rows


def prior_questions_from_db(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT question_id, run_id, question_text, supporting_doc_ids_json,
               supporting_chunk_ids_json
        FROM questions
        ORDER BY updated_at DESC, question_id
        """
    ):
        try:
            doc_ids = json.loads(row["supporting_doc_ids_json"] or "[]")
        except json.JSONDecodeError:
            doc_ids = []
        try:
            chunk_ids = json.loads(row["supporting_chunk_ids_json"] or "[]")
        except json.JSONDecodeError:
            chunk_ids = []
        rows.append(
            {
                "question_id": normalize_ws(row["question_id"]),
                "run_id": normalize_ws(row["run_id"]),
                "question": normalize_ws(row["question_text"]),
                "doc_ids": [normalize_ws(doc_id) for doc_id in doc_ids if normalize_ws(doc_id)],
                "chunk_ids": [normalize_ws(chunk_id) for chunk_id in chunk_ids if normalize_ws(chunk_id)],
                "question_type": "",
                "difficulty_features": [],
                "doc_titles": {},
                "source_file": "coverage.sqlite",
                "source": "coverage_db",
            }
        )
    return rows


def merge_prior_questions(file_rows: list[dict[str, Any]], db_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in file_rows + db_rows:
        question_id = normalize_ws(row.get("question_id"))
        if not question_id:
            continue
        existing = by_id.get(question_id)
        if not existing:
            by_id[question_id] = row
            continue
        if row.get("doc_titles") and not existing.get("doc_titles"):
            existing["doc_titles"] = row["doc_titles"]
        if row.get("chunk_ids") and not existing.get("chunk_ids"):
            existing["chunk_ids"] = row["chunk_ids"]
        if row.get("question_type") and not existing.get("question_type"):
            existing["question_type"] = row["question_type"]
        if row.get("difficulty_features") and not existing.get("difficulty_features"):
            existing["difficulty_features"] = row["difficulty_features"]
        if row.get("source_file") != "coverage.sqlite":
            existing["source_file"] = row["source_file"]
    return list(by_id.values())


def question_relevance(row: dict[str, Any], seed_doc_ids: set[str], keys: set[str]) -> int:
    doc_ids = set(row.get("doc_ids") or [])
    doc_overlap = len(seed_doc_ids.intersection(doc_ids))
    titles = " ".join((row.get("doc_titles") or {}).values())
    row_keys = keyword_set(row.get("question"), titles, " ".join(doc_ids))
    overlap = len(keys.intersection(row_keys))
    return doc_overlap * 100 + overlap


def format_docs(row: dict[str, Any], max_docs: int = 5) -> str:
    doc_ids = row.get("doc_ids") or []
    titles = row.get("doc_titles") or {}
    parts: list[str] = []
    for doc_id in doc_ids[:max_docs]:
        title = normalize_ws(titles.get(doc_id))
        parts.append(f"{doc_id} ({truncate(title, 90)})" if title else doc_id)
    if len(doc_ids) > max_docs:
        parts.append(f"+{len(doc_ids) - max_docs} more")
    return "; ".join(parts) if parts else "none listed"


def format_chunks(row: dict[str, Any], max_chunks: int = 8) -> str:
    chunk_ids = row.get("chunk_ids") or []
    if not chunk_ids:
        return ""
    shown = [normalize_ws(chunk_id) for chunk_id in chunk_ids[:max_chunks]]
    if len(chunk_ids) > max_chunks:
        shown.append(f"+{len(chunk_ids) - max_chunks} more")
    return ", ".join(shown)


def selected_questions(
    prior: list[dict[str, Any]],
    run_card: dict[str, Any],
    max_prior_questions: int,
) -> tuple[list[dict[str, Any]], int]:
    seed_doc_ids = run_seed_doc_ids(run_card)
    keys = run_keywords(run_card)
    if seed_doc_ids or keys:
        scored = [(question_relevance(row, seed_doc_ids, keys), row) for row in prior]
        ordered = [
            row
            for score, row in sorted(scored, key=lambda item: (-item[0], normalize_ws(item[1].get("question_id"))))
            if score > 0
        ]
    else:
        ordered = sorted(
            prior,
            key=lambda row: (
                normalize_ws(row.get("source_file")),
                normalize_ws(row.get("question_id")),
            ),
        )
    if max_prior_questions <= 0:
        return ordered, 0
    return ordered[:max_prior_questions], max(0, len(ordered) - max_prior_questions)


def build_packet(
    run_card: dict[str, Any],
    conn: sqlite3.Connection,
    question_paths: list[Path],
    *,
    max_prior_questions: int,
) -> str:
    file_prior = prior_questions_from_files(question_paths)
    db_prior = prior_questions_from_db(conn)
    selected, omitted_questions = selected_questions(
        merge_prior_questions(file_prior, db_prior),
        run_card,
        max_prior_questions=max_prior_questions,
    )
    memory_counts = Counter(tuple(sorted(row.get("doc_ids") or [])) for row in selected if row.get("doc_ids"))

    lines: list[str] = []
    run_id = card_run_id(run_card) or "unknown_run"
    lines.append(f"# Focused Memory Guardrail Packet For {run_id}")
    lines.append("")
    lines.append("This is durable project memory for this one frontier run. It is not evidence.")
    lines.append("Use it to avoid repeated user intents, not to optimize for novelty. Reusing docs or a subgraph is allowed when the practical question teaches a distinct SEBI-document memory target.")
    lines.append("")
    lines.append("## Memory Frame")
    if normalize_ws(run_card.get("frontier_id")):
        lines.append(f"- frontier_id: {normalize_ws(run_card.get('frontier_id'))}")
        lines.append(f"- frontier_title: {normalize_ws(run_card.get('frontier_title')) or 'not set'}")
        if normalize_ws(run_card.get("frontier_why_unexplored")):
            lines.append(f"- frontier_why_unexplored: {normalize_ws(run_card.get('frontier_why_unexplored'))}")
    lines.append(f"- memory_id: {card_memory_id(run_card) or 'not set'}")
    lines.append(f"- scope_hint: {normalize_ws(run_card.get('scope_hint')) or 'not set'}")
    if normalize_ws(run_card.get("frontier_avoid")):
        lines.append(f"- frontier_avoid: {normalize_ws(run_card.get('frontier_avoid'))}")
    lines.append("")

    seed_docs = run_card.get("seed_docs") or []
    lines.append("## Seed Documents")
    if seed_docs:
        for doc in seed_docs:
            if not isinstance(doc, dict):
                continue
            title = truncate(normalize_ws(doc.get("title")), 180)
            section = " / ".join(
                part
                for part in [
                    normalize_ws(doc.get("top_section")),
                    normalize_ws(doc.get("section")),
                    normalize_ws(doc.get("subsection")),
                ]
                if part
            )
            suffix = f"; {section}" if section else ""
            lines.append(f"- {normalize_ws(doc.get('doc_id'))}: {title}{suffix}")
    else:
        for doc_id in sorted(run_seed_doc_ids(run_card)):
            lines.append(f"- {doc_id}")
        if not run_seed_doc_ids(run_card):
            lines.append("- none; Kimi may choose a coherent memory region from graph and corpus leads")
    lines.append("")

    lines.append("## Prior Question Intents")
    if selected:
        for row in selected:
            source = normalize_ws(row.get("source_file"))
            lines.append(f"- {normalize_ws(row.get('question_id'))} [{source}]")
            lines.append(f"  intent/question: {truncate(normalize_ws(row.get('question')), 300)}")
            lines.append(f"  docs: {format_docs(row)}")
            type_bits = []
            if normalize_ws(row.get("question_type")):
                type_bits.append(f"type={normalize_ws(row.get('question_type'))}")
            features = row.get("difficulty_features") or []
            if features:
                type_bits.append("features=" + ", ".join(features[:5]))
            if type_bits:
                lines.append(f"  labels: {'; '.join(type_bits)}")
            chunks = format_chunks(row)
            if chunks:
                lines.append(f"  chunks: {chunks}")
    else:
        lines.append("- none found")
    if omitted_questions:
        lines.append(f"- Omitted {omitted_questions} additional prior question intent(s). Adjust --max-prior-questions to test packet size.")
    lines.append("")

    lines.append("## Prior Doc-Memory Signals From Selected Intents")
    if memory_counts:
        for key, count in memory_counts.most_common(20):
            docs_text = ", ".join(key[:6])
            if len(key) > 6:
                docs_text += f", +{len(key) - 6} more"
            lines.append(f"- {count} selected prior question(s): {docs_text}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Candidate Labels To Use")
    lines.append("- new: different user need and answer shape from packet entries")
    lines.append("- adjacent_new: same docs/memory region, but a materially different evidence theme")
    lines.append("- duplicate: same practical user intent and answer shape")
    lines.append(
        "- thin_variant: technically different wording or detail, but the same practical user need and memorization target as another question"
    )
    lines.append("")
    return "\n".join(lines)


def load_run_card(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-card", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--coverage-db", type=Path, default=DEFAULT_COVERAGE_DB)
    parser.add_argument("--question-path", action="append", default=None)
    parser.add_argument("--max-prior-questions", type=int, default=25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_card = load_run_card(args.run_card)
    run_id = card_run_id(run_card) or args.run_card.stem
    output_path = args.output or OUTPUT_ROOT / "uniqueness_packets" / f"{run_id}.md"
    question_patterns = args.question_path or run_card.get("avoid_question_paths") or DEFAULT_QUESTION_PATTERNS
    question_paths = resolve_patterns(question_patterns)
    conn = connect(args.coverage_db)
    try:
        markdown = build_packet(
            run_card,
            conn,
            question_paths,
            max_prior_questions=args.max_prior_questions,
        )
    finally:
        conn.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
