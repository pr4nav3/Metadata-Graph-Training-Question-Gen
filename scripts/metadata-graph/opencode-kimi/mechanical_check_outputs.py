#!/usr/bin/env python3
"""Mechanical checks for OpenCode/Kimi SEBI question JSONL outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent

from sebi_retrieval import DataUnavailable, DocumentStore, GraphStore, default_graph_db  # noqa: E402


DEFAULT_GRAPH_DB = default_graph_db(METADATA_GRAPH_DIR)
DEFAULT_CACHE_DB = METADATA_GRAPH_DIR / "output" / "hydration" / "doc_cache.sqlite"
DEFAULT_VESPA_URL = (
    os.environ.get("METADATA_KG_VESPA_QUERY_URL")
    or os.environ.get("VESPA_QUERY_URL")
    or "http://localhost:18081/search/"
)
CHUNK_RE = re.compile(r"^([^#\s]+)#(\d+)$")


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_hash(value: str) -> str:
    return hashlib.sha256(normalize_ws(value).lower().encode("utf-8")).hexdigest()[:32]


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return rows, [f"output file does not exist: {path}"]
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"line {line_number}: row is not a JSON object")
            continue
        value["_line_number"] = line_number
        rows.append(value)
    return rows, errors


def doc_exists(graph: GraphStore, documents: DocumentStore, doc_id: str) -> bool:
    try:
        graph.document(doc_id)
        return True
    except DataUnavailable:
        pass
    try:
        documents.fields(doc_id)
        return True
    except DataUnavailable:
        return False


def check_chunk(documents: DocumentStore, doc_id: str, chunk_index: int) -> Optional[str]:
    try:
        documents.chunk(doc_id, chunk_index, max_chars=200)
    except DataUnavailable as exc:
        return str(exc)
    return None


def check_rows(
    rows: list[dict[str, Any]],
    graph: GraphStore,
    documents: DocumentStore,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    seen_questions: dict[str, str] = {}

    for row in rows:
        prefix = f"line {row.get('_line_number')}:"
        for field in ("id", "question", "answer", "supporting_docs", "supporting_chunks"):
            if field not in row:
                errors.append(f"{prefix} missing required field `{field}`")
        if "run_id" not in row:
            errors.append(f"{prefix} missing required field `run_id`")

        question_id = normalize_ws(row.get("id"))
        if question_id:
            if question_id in seen_ids:
                errors.append(f"{prefix} duplicate question id `{question_id}`")
            seen_ids.add(question_id)

        question = normalize_ws(row.get("question"))
        if question:
            qhash = stable_hash(question)
            if qhash in seen_questions:
                errors.append(
                    f"{prefix} duplicate exact question text with `{seen_questions[qhash]}`"
                )
            seen_questions[qhash] = question_id or f"line {row.get('_line_number')}"

        docs = row.get("supporting_docs")
        if not isinstance(docs, list):
            errors.append(f"{prefix} supporting_docs must be an array")
            docs = []
        doc_ids = [normalize_ws(doc.get("doc_id")) for doc in docs if isinstance(doc, dict)]
        doc_ids = [doc_id for doc_id in doc_ids if doc_id]
        if len(set(doc_ids)) < 2:
            errors.append(f"{prefix} expected at least two distinct supporting docs")
        for doc_id in sorted(set(doc_ids)):
            if not doc_exists(graph, documents, doc_id):
                errors.append(f"{prefix} supporting doc not found in graph/cache/Vespa: {doc_id}")

        chunks = row.get("supporting_chunks")
        if not isinstance(chunks, list):
            errors.append(f"{prefix} supporting_chunks must be an array")
            chunks = []
        chunk_values = [normalize_ws(item) for item in chunks if normalize_ws(item)]
        if len(set(chunk_values)) < 2:
            errors.append(f"{prefix} expected at least two distinct supporting chunks")
        for value in chunk_values:
            match = CHUNK_RE.match(value)
            if not match:
                errors.append(f"{prefix} invalid chunk id `{value}`")
                continue
            doc_id, raw_index = match.groups()
            if doc_ids and doc_id not in doc_ids:
                warnings.append(
                    f"{prefix} chunk `{value}` doc_id is not listed in supporting_docs"
                )
            chunk_error = check_chunk(documents, doc_id, int(raw_index))
            if chunk_error:
                errors.append(f"{prefix} chunk `{value}` unavailable: {chunk_error}")

        review = row.get("manual_review")
        if not isinstance(review, dict):
            errors.append(f"{prefix} manual_review must be an object")
        elif review.get("status") != "pending":
            errors.append(f"{prefix} manual_review.status must be `pending`")

        if normalize_ws(row.get("source")) != "opencode_kimi_research":
            warnings.append(f"{prefix} source should be `opencode_kimi_research`")

    return errors, warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("questions_jsonl", type=Path)
    parser.add_argument("--graph-db", type=Path, default=DEFAULT_GRAPH_DB)
    parser.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE_DB)
    parser.add_argument("--vespa-url", default=DEFAULT_VESPA_URL)
    parser.add_argument("--no-live-fetch", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, load_errors = load_jsonl(args.questions_jsonl)
    graph = GraphStore(args.graph_db)
    documents = DocumentStore(
        args.cache_db,
        graph,
        args.vespa_url,
        allow_live_fetch=not args.no_live_fetch,
    )
    try:
        errors, warnings = check_rows(rows, graph, documents)
    finally:
        documents.close()
        graph.close()
    errors = load_errors + errors
    result = {
        "ok": not errors,
        "path": str(args.questions_jsonl),
        "rows": len(rows),
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
