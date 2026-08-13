#!/usr/bin/env python3
"""Compact per-memory context for continuation question-generation passes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coverage_store import load_question_rows, normalize_ws, supporting_docs
from run_identity import row_run_id


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
DEFAULT_MEMORY_DIR = OUTPUT_ROOT / "memory"


def truncate(value: Any, limit: int) -> str:
    text = normalize_ws(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def fallback_doc_ids(row: dict[str, Any]) -> list[str]:
    doc_ids, _titles = supporting_docs(row)
    if doc_ids:
        return doc_ids
    raw = row.get("supporting_doc_ids")
    if isinstance(raw, list):
        return [normalize_ws(item) for item in raw if normalize_ws(item)]
    return []


def compact_row(row: dict[str, Any], *, memory_id: str) -> dict[str, Any] | None:
    question_id = normalize_ws(row.get("id"))
    question = normalize_ws(row.get("question"))
    if not question_id or not question:
        return None
    memory_id = normalize_ws(memory_id)
    review = row.get("manual_review") if isinstance(row.get("manual_review"), dict) else {}
    chunks = [normalize_ws(item) for item in row.get("supporting_chunks") or [] if normalize_ws(item)]
    return {
        "question_id": question_id,
        "run_id": row_run_id(row),
        "memory_id": memory_id,
        "review_status": normalize_ws(review.get("status")) or "pending",
        "question_type": normalize_ws(row.get("question_type")),
        "question": truncate(question, 500),
        "answer_focus": truncate(row.get("answer") or row.get("answer_summary"), 240),
        "supporting_doc_ids": fallback_doc_ids(row)[:8],
        "supporting_chunk_ids": chunks[:12],
    }


def read_memory(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and normalize_ws(value.get("question_id")):
                rows.append(value)
    return rows


def write_memory(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        rows,
        key=lambda row: (
            normalize_ws(row.get("run_id")),
            normalize_ws(row.get("question_id")),
        ),
    )
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def update_memory(memory_path: Path, *, memory_id: str, question_paths: list[Path]) -> int:
    memory_id = normalize_ws(memory_id)
    if not memory_id:
        return 0
    by_id = {normalize_ws(row.get("question_id")): row for row in read_memory(memory_path)}
    for row in load_question_rows(question_paths):
        compact = compact_row(row, memory_id=memory_id)
        if compact:
            by_id[compact["question_id"]] = compact
    write_memory(memory_path, list(by_id.values()))
    return len(by_id)
