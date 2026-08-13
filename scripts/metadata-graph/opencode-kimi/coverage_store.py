#!/usr/bin/env python3
"""Durable bookkeeping helpers for OpenCode/Kimi question-generation runs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from run_identity import card_memory_id, card_run_id, row_run_id


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
DEFAULT_COVERAGE_DB = OUTPUT_ROOT / "coverage.sqlite"


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def question_hash(question: str) -> str:
    normalized = normalize_ws(question).lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def connect(path: Path = DEFAULT_COVERAGE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the lean coverage schema for OpenCode research runs."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            pass_id TEXT NOT NULL,
            status TEXT NOT NULL,
            seed_doc_ids_json TEXT NOT NULL,
            output_path TEXT NOT NULL,
            summary_path TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS questions (
            question_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            question_hash TEXT NOT NULL,
            question_text TEXT NOT NULL,
            supporting_doc_ids_json TEXT NOT NULL,
            supporting_chunk_ids_json TEXT NOT NULL,
            manual_status TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_hash
            ON questions(question_hash);

        CREATE TABLE IF NOT EXISTS run_documents (
            doc_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            role TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (doc_id, run_id, role)
        );

        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            run_id TEXT,
            status TEXT NOT NULL,
            scope_hint TEXT,
            saturation_reason TEXT,
            output_path TEXT,
            summary_path TEXT,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_documents (
            memory_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            role TEXT NOT NULL,
            title TEXT,
            note TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (memory_id, doc_id, role)
        );

        CREATE TABLE IF NOT EXISTS future_memory_leads (
            lead_id TEXT PRIMARY KEY,
            source_run_id TEXT,
            source_memory_id TEXT,
            lead TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def supporting_docs(row: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    doc_ids: list[str] = []
    titles: dict[str, str] = {}
    for doc in row.get("supporting_docs") or []:
        if not isinstance(doc, dict):
            continue
        doc_id = normalize_ws(doc.get("doc_id"))
        if not doc_id:
            continue
        doc_ids.append(doc_id)
        title = normalize_ws(doc.get("title"))
        if title:
            titles[doc_id] = title
    return doc_ids, titles


def load_question_rows(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row["_source_file"] = str(path)
                row["_line_number"] = line_number
                rows.append(row)
    return rows


def upsert_run(conn: sqlite3.Connection, card: dict[str, Any], status: str = "assigned") -> None:
    now = int(time.time())
    run_id = card_run_id(card)
    memory_id = card_memory_id(card)
    seed_doc_ids = [normalize_ws(doc_id) for doc_id in card.get("seed_doc_ids") or [] if normalize_ws(doc_id)]
    conn.execute(
        """
        INSERT INTO runs
            (run_id, pass_id, status, seed_doc_ids_json, output_path, summary_path, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            pass_id=excluded.pass_id,
            status=excluded.status,
            seed_doc_ids_json=excluded.seed_doc_ids_json,
            output_path=excluded.output_path,
            summary_path=excluded.summary_path,
            updated_at=excluded.updated_at
        """,
        (
            run_id,
            normalize_ws(card.get("pass_id")),
            status,
            json_dumps(seed_doc_ids),
            normalize_ws(card.get("output_path")),
            normalize_ws(card.get("summary_path")),
            now,
        ),
    )
    if memory_id:
        conn.execute(
            """
            INSERT INTO memories
                (memory_id, run_id, status, scope_hint, output_path, summary_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                run_id=excluded.run_id,
                status=excluded.status,
                scope_hint=excluded.scope_hint,
                output_path=excluded.output_path,
                summary_path=excluded.summary_path,
                updated_at=excluded.updated_at
            """,
            (
                memory_id,
                run_id,
                "assigned",
                normalize_ws(card.get("scope_hint")),
                normalize_ws(card.get("output_path")),
                normalize_ws(card.get("summary_path")),
                now,
            ),
        )
    conn.commit()


def upsert_question(conn: sqlite3.Connection, row: dict[str, Any], memory_id: str = "") -> None:
    question_id = normalize_ws(row.get("id")) or f"{Path(row.get('_source_file', 'questions')).stem}:{row.get('_line_number', 0)}"
    run_id = row_run_id(row)
    memory_id = normalize_ws(memory_id)
    question = normalize_ws(row.get("question"))
    if not run_id or not question:
        return
    doc_ids, titles = supporting_docs(row)
    chunk_ids = [normalize_ws(chunk_id) for chunk_id in row.get("supporting_chunks") or [] if normalize_ws(chunk_id)]
    manual = row.get("manual_review") if isinstance(row.get("manual_review"), dict) else {}
    status = normalize_ws(manual.get("status")) or "pending"
    now = int(time.time())
    conn.execute(
        """
        INSERT OR REPLACE INTO questions
            (question_id, run_id, question_hash, question_text, supporting_doc_ids_json,
             supporting_chunk_ids_json, manual_status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (question_id, run_id, question_hash(question), question, json_dumps(doc_ids), json_dumps(chunk_ids), status, now),
    )
    for doc_id in doc_ids:
        conn.execute(
            """
            INSERT OR REPLACE INTO run_documents
                (doc_id, run_id, role, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (doc_id, run_id, "supporting", now),
        )
        if memory_id:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_documents
                    (memory_id, doc_id, role, title, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, doc_id, "supporting", titles.get(doc_id, ""), "", now),
            )
    conn.commit()


def sync_question_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    run_id: str = "",
    memory_id: str = "",
) -> int:
    rows = load_question_rows([path])
    run_id = normalize_ws(run_id)
    memory_id = normalize_ws(memory_id)
    count = 0
    for row in rows:
        if run_id and row_run_id(row) != run_id:
            continue
        upsert_question(conn, row, memory_id=memory_id)
        count += 1
    return count
