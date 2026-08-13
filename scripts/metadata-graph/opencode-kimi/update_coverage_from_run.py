#!/usr/bin/env python3
"""Sync an OpenCode/Kimi run's artifacts into coverage.sqlite."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from coverage_store import DEFAULT_COVERAGE_DB, connect, normalize_ws, sync_question_file, upsert_run
from memory_store import DEFAULT_MEMORY_DIR, update_memory
from frontier_ledger import DEFAULT_LEDGER_PATH, update_ledger_frontier
from run_identity import card_memory_id, card_run_id, frontier_memory_id


DOC_ID_RE = re.compile(r"\bclf-[a-z0-9]+\b")
FUTURE_HEADING_RE = re.compile(r"^#+\s+future leads\b", re.IGNORECASE)
SATURATION_HEADING_RE = re.compile(r"^#+\s+saturation", re.IGNORECASE)
FRONTIER_OUTCOME_RE = re.compile(r"^frontier outcome\s*:\s*(.+)$", re.IGNORECASE)
FRONTIER_OUTCOME_STATUS = {
    "saturated": "saturated",
    "weak": "rejected",
    "duplicate": "duplicate",
    "too_broad": "rejected",
    "no_multidoc_angle": "rejected",
    "needs_review": "needs_review",
    "continue_generation": "continue_generation",
}


def load_run_card(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summary_sections(path: Path) -> tuple[list[str], str, str]:
    if not path.exists():
        return [], "", ""
    future_leads: list[str] = []
    saturation_lines: list[str] = []
    frontier_outcome = ""
    active = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        outcome_match = FRONTIER_OUTCOME_RE.match(stripped)
        if outcome_match:
            frontier_outcome = normalize_ws(outcome_match.group(1))
            continue
        if stripped.startswith("#"):
            if FUTURE_HEADING_RE.search(stripped):
                active = "future"
            elif SATURATION_HEADING_RE.search(stripped):
                active = "saturation"
            else:
                active = ""
            continue
        if active == "future" and (stripped.startswith("- ") or re.match(r"^\d+\.", stripped)):
            future_leads.append(stripped.lstrip("- ").strip())
        elif active == "saturation":
            saturation_lines.append(stripped)
    return future_leads, normalize_ws(" ".join(saturation_lines)), frontier_outcome


def frontier_status_from_outcome(frontier_outcome: str, summary_path: Path, output_path: Path) -> str:
    outcome_key = normalize_ws(frontier_outcome).split(" ", 1)[0].strip("`*:,.").lower()
    if outcome_key in FRONTIER_OUTCOME_STATUS:
        return FRONTIER_OUTCOME_STATUS[outcome_key]
    if summary_path.exists():
        return "needs_review"
    if output_path.exists():
        return "running"
    return "assigned"


def sync_summary(conn: sqlite3.Connection, card: dict[str, Any]) -> None:
    run_id = card_run_id(card)
    memory_id = card_memory_id(card) or run_id
    if not run_id:
        return
    summary_path = Path(normalize_ws(card.get("summary_path")))
    output_path = Path(normalize_ws(card.get("output_path")))
    future_leads, saturation_reason, _frontier_outcome = summary_sections(summary_path)
    now = int(time.time())
    if memory_id:
        if summary_path.exists():
            status = "summary_written"
        elif output_path.exists():
            status = "questions_written"
        else:
            status = "assigned"
        conn.execute(
            """
            INSERT INTO memories
                (memory_id, run_id, status, scope_hint, saturation_reason,
                 output_path, summary_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                status=excluded.status,
                saturation_reason=excluded.saturation_reason,
                output_path=excluded.output_path,
                summary_path=excluded.summary_path,
                updated_at=excluded.updated_at
            """,
            (
                memory_id,
                run_id,
                status,
                normalize_ws(card.get("scope_hint")),
                saturation_reason,
                normalize_ws(card.get("output_path")),
                normalize_ws(card.get("summary_path")),
                now,
            ),
        )
    if summary_path.exists():
        for doc_id in sorted(set(DOC_ID_RE.findall(summary_path.read_text(encoding="utf-8")))):
            conn.execute(
                """
                INSERT OR REPLACE INTO run_documents
                    (doc_id, run_id, role, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (doc_id, run_id, "mentioned_in_summary", now),
            )
            if memory_id:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memory_documents
                    (memory_id, doc_id, role, title, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (memory_id, doc_id, "mentioned_in_summary", "", "", now),
                )
    for index, lead in enumerate(future_leads, start=1):
        lead_id = f"{run_id}_future_{index:03d}"
        conn.execute(
            """
            INSERT OR REPLACE INTO future_memory_leads
                (lead_id, source_run_id, source_memory_id, lead, reason, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, run_id, memory_id, lead, "", "open", now),
        )
    conn.commit()


def sync_frontier_ledger(card: dict[str, Any], ledger_path: Path) -> None:
    frontier_id = normalize_ws(card.get("frontier_id"))
    if not frontier_id:
        return
    summary_path = Path(normalize_ws(card.get("summary_path")))
    output_path = Path(normalize_ws(card.get("output_path")))
    _future_leads, saturation_reason, frontier_outcome = summary_sections(summary_path)
    status = frontier_status_from_outcome(frontier_outcome, summary_path, output_path)
    outcome = frontier_outcome or saturation_reason
    update_ledger_frontier(
        ledger_path,
        frontier_id,
        status=status,
        run_id=card_run_id(card),
        memory_id=frontier_memory_id(card) or card_memory_id(card),
        outcome=outcome,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-card", type=Path, required=True)
    parser.add_argument("--coverage-db", type=Path, default=DEFAULT_COVERAGE_DB)
    parser.add_argument("--frontier-ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    card = load_run_card(args.run_card)
    conn = connect(args.coverage_db)
    try:
        output_path = Path(normalize_ws(card.get("output_path")))
        upsert_run(conn, card, status="questions_written" if output_path.exists() else "assigned")
        count = sync_question_file(
            conn,
            output_path,
            run_id=card_run_id(card),
            memory_id=card_memory_id(card),
        )
        sync_summary(conn, card)
    finally:
        conn.close()
    memory_id = frontier_memory_id(card) or card_memory_id(card)
    if memory_id:
        memory_value = normalize_ws(card.get("memory_path"))
        memory_path = Path(memory_value) if memory_value else DEFAULT_MEMORY_DIR / f"{memory_id}.jsonl"
        update_memory(memory_path, memory_id=memory_id, question_paths=[output_path])
    ledger_path = Path(normalize_ws(card.get("frontier_ledger_path")) or args.frontier_ledger)
    try:
        sync_frontier_ledger(card, ledger_path)
    except (KeyError, ValueError) as exc:
        print(f"frontier_ledger_warning={exc}", file=sys.stderr)
    print(f"synced_questions={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
