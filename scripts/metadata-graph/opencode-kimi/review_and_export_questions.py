#!/usr/bin/env python3
"""Apply Codex review decisions and export accepted Kimi questions to CSV."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from memory_store import DEFAULT_MEMORY_DIR, update_memory
from coverage_store import DEFAULT_COVERAGE_DB, normalize_ws, question_hash
from run_identity import card_memory_id, frontier_memory_id, row_run_id


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
OUTPUT_ROOT = SCRIPT_DIR.parent / "output" / "opencode_kimi"
DEFAULT_DATASET = REPO_ROOT / "questions" / "Kimi+Opencode_Questions.csv"

CSV_FIELDS = [
    "id",
    "date",
    "asked_by",
    "pipeline",
    "context",
    "question",
    "answer_summary",
    "verification_status",
    "resources",
    "run_id",
    "memory_id",
    "frontier_id",
    "question_type",
    "difficulty_features",
    "supporting_doc_ids",
    "supporting_chunk_ids",
    "review_status",
    "review_notes",
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        row["_source_file"] = str(path)
        row["_line_number"] = line_number
        rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            clean = {key: value for key, value in row.items() if not key.startswith("_")}
            handle.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")


def normalize_decision_status(value: Any) -> str:
    raw = normalize_ws(value).lower()
    aliases = {
        "accept": "accepted",
        "accepted": "accepted",
        "reject": "rejected",
        "rejected": "rejected",
        "needs_human": "pending",
        "needs-human": "pending",
        "needs human": "pending",
        "human_needed": "pending",
        "human-needed": "pending",
        "human needed": "pending",
        "needs_review": "pending",
        "needs-review": "pending",
        "needs review": "pending",
        "human": "pending",
        "pending": "pending",
    }
    return aliases.get(raw, raw)


def normalize_decision_item(item: dict[str, Any], *, default_id: str = "", default_reviewed_by: str = "") -> dict[str, Any]:
    question_id = normalize_ws(item.get("id") or item.get("question_id") or default_id)
    status = normalize_decision_status(item.get("status") or item.get("decision"))
    notes = normalize_ws(item.get("notes") or item.get("reason"))
    normalized = dict(item)
    normalized["id"] = question_id
    normalized["status"] = status
    normalized["notes"] = notes
    if default_reviewed_by and not normalize_ws(normalized.get("reviewed_by")):
        normalized["reviewed_by"] = default_reviewed_by
    raw_decision = normalize_ws(item.get("decision"))
    if raw_decision and raw_decision.lower() != status:
        normalized["raw_decision"] = raw_decision
    return normalized


def decision_map(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    default_reviewed_by = ""
    if isinstance(raw, dict):
        default_reviewed_by = normalize_ws(raw.get("reviewed_by"))
        if isinstance(raw.get("decisions"), list):
            return {
                normalize_ws(item.get("id") or item.get("question_id")): normalize_decision_item(
                    item,
                    default_reviewed_by=default_reviewed_by,
                )
                for item in raw["decisions"]
                if isinstance(item, dict) and normalize_ws(item.get("id") or item.get("question_id"))
            }
    if isinstance(raw, list):
        return {
            normalize_ws(item.get("id") or item.get("question_id")): normalize_decision_item(item)
            for item in raw
            if isinstance(item, dict) and normalize_ws(item.get("id") or item.get("question_id"))
        }
    if isinstance(raw, dict):
        decisions: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if key in {"run_id", "reviewed_by", "review_run_id"}:
                continue
            if isinstance(value, dict):
                decisions[normalize_ws(key)] = normalize_decision_item(
                    value,
                    default_id=normalize_ws(key),
                    default_reviewed_by=default_reviewed_by,
                )
            elif isinstance(value, str):
                decisions[normalize_ws(key)] = normalize_decision_item(
                    {"status": value},
                    default_id=normalize_ws(key),
                    default_reviewed_by=default_reviewed_by,
                )
        return decisions
    return {}


def run_card(run_id: str, output_root: Path = OUTPUT_ROOT) -> dict[str, Any]:
    run_card_path = output_root / "run_cards" / f"{run_id}.json"
    if not run_card_path.exists():
        raise SystemExit(f"run card not found: {run_card_path}")
    return json.loads(run_card_path.read_text(encoding="utf-8"))


def question_paths_from_runs(run_ids: list[str], output_root: Path = OUTPUT_ROOT) -> list[Path]:
    paths: list[Path] = []
    for run_id in run_ids:
        card = run_card(run_id, output_root=output_root)
        paths.append(Path(card["output_path"]))
    return paths


def update_memories(run_ids: list[str], output_root: Path = OUTPUT_ROOT) -> None:
    for run_id in dict.fromkeys(normalize_ws(item) for item in run_ids if normalize_ws(item)):
        card = run_card(run_id, output_root=output_root)
        memory_id = frontier_memory_id(card) or card_memory_id(card)
        if not memory_id:
            continue
        output_path = Path(normalize_ws(card.get("output_path")))
        memory_value = normalize_ws(card.get("memory_path"))
        memory_path = Path(memory_value) if memory_value else DEFAULT_MEMORY_DIR / f"{memory_id}.jsonl"
        update_memory(memory_path, memory_id=memory_id, question_paths=[output_path])


def doc_ids(row: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for doc in row.get("supporting_docs") or []:
        if isinstance(doc, dict) and normalize_ws(doc.get("doc_id")):
            ids.append(normalize_ws(doc.get("doc_id")))
    return ids


def chunk_ids(row: dict[str, Any]) -> list[str]:
    return [normalize_ws(value) for value in row.get("supporting_chunks") or [] if normalize_ws(value)]


def verification_status(review: dict[str, Any]) -> str:
    reviewed_by = normalize_ws(review.get("reviewed_by")).lower()
    if "kimi" in reviewed_by:
        return "KIMI_REVIEW_ACCEPTED"
    if "codex" in reviewed_by:
        return "CODEX_ACCEPTED"
    return "REVIEW_ACCEPTED"


def csv_row(row: dict[str, Any], *, date: str) -> dict[str, str]:
    review = row.get("manual_review") if isinstance(row.get("manual_review"), dict) else {}
    docs = doc_ids(row)
    chunks = chunk_ids(row)
    run_id = row_run_id(row)
    memory_id = normalize_ws(row.get("memory_id"))
    return {
        "id": normalize_ws(row.get("id")),
        "date": date,
        "asked_by": "Kimi+OpenCode",
        "pipeline": "metadata knowledge graph",
        "context": run_id,
        "question": normalize_ws(row.get("question")),
        "answer_summary": normalize_ws(row.get("answer")),
        "verification_status": verification_status(review),
        "resources": json.dumps(docs, ensure_ascii=False),
        "run_id": run_id,
        "memory_id": memory_id,
        "frontier_id": normalize_ws(row.get("frontier_id")),
        "question_type": normalize_ws(row.get("question_type")),
        "difficulty_features": json.dumps(row.get("difficulty_features") or [], ensure_ascii=False),
        "supporting_doc_ids": json.dumps(docs, ensure_ascii=False),
        "supporting_chunk_ids": json.dumps(chunks, ensure_ascii=False),
        "review_status": normalize_ws(review.get("status")),
        "review_notes": normalize_ws(review.get("notes")),
    }


def existing_dataset_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return {normalize_ws(row.get("id")) for row in reader if normalize_ws(row.get("id"))}


def append_dataset(path: Path, rows: list[dict[str, str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids = existing_dataset_ids(path)
    new_rows = [row for row in rows if normalize_ws(row.get("id")) and normalize_ws(row.get("id")) not in existing_ids]
    write_header = not path.exists() or path.stat().st_size == 0
    fieldnames = CSV_FIELDS
    if path.exists() and path.stat().st_size > 0:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames:
                fieldnames = reader.fieldnames
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)
    return len(new_rows)


def update_coverage(conn: sqlite3.Connection, row: dict[str, Any], status: str) -> None:
    question_id = normalize_ws(row.get("id"))
    question = normalize_ws(row.get("question"))
    if not question_id or not question:
        return
    conn.execute(
        """
        UPDATE questions
        SET manual_status = ?, updated_at = ?
        WHERE question_id = ? OR question_hash = ?
        """,
        (status, int(time.time()), question_id, question_hash(question)),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question-jsonl", action="append", default=[])
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--decisions-json", type=Path)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--coverage-db", type=Path, default=DEFAULT_COVERAGE_DB)
    parser.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    parser.add_argument("--export-pending", action="store_true")
    parser.add_argument("--allow-partial-decisions", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = [Path(path) for path in args.question_jsonl]
    paths.extend(question_paths_from_runs(args.run_id, output_root=args.output_root))
    paths = list(dict.fromkeys(paths))
    if not paths:
        raise SystemExit("provide --question-jsonl or --run-id")

    decisions = decision_map(args.decisions_json)
    missing_decisions: list[str] = []
    if decisions and not args.allow_partial_decisions:
        for path in paths:
            for row in load_jsonl(path):
                qid = normalize_ws(row.get("id"))
                review = row.get("manual_review") if isinstance(row.get("manual_review"), dict) else {}
                status = normalize_ws(review.get("status")).lower() or "pending"
                if qid and status == "pending" and qid not in decisions:
                    missing_decisions.append(qid)
        if missing_decisions:
            preview = ", ".join(missing_decisions[:20])
            more = "" if len(missing_decisions) <= 20 else f" (+{len(missing_decisions) - 20} more)"
            raise SystemExit(f"decisions missing for pending row(s): {preview}{more}")

    accepted_rows: list[dict[str, str]] = []
    changed = 0

    conn = sqlite3.connect(args.coverage_db)
    try:
        for path in paths:
            rows = load_jsonl(path)
            for row in rows:
                qid = normalize_ws(row.get("id"))
                review = row.get("manual_review") if isinstance(row.get("manual_review"), dict) else {}
                if qid in decisions:
                    decision = decisions[qid]
                    status = normalize_ws(decision.get("status")).lower()
                    if status not in {"accepted", "rejected", "pending"}:
                        raise SystemExit(f"{qid}: invalid status {status}")
                    review = {
                        **review,
                        "status": status,
                        "notes": normalize_ws(decision.get("notes")),
                        "reviewed_by": normalize_ws(decision.get("reviewed_by")) or "Codex",
                        "reviewed_at": int(time.time()),
                    }
                    for extra_key in ("quality_scores", "scores", "flags", "raw_decision"):
                        if extra_key in decision:
                            review[extra_key] = decision[extra_key]
                    row["manual_review"] = review
                    update_coverage(conn, row, status)
                    changed += 1
                if normalize_ws(review.get("status")) == "accepted" or (
                    args.export_pending and normalize_ws(review.get("status")) == "pending"
                ):
                    accepted_rows.append(csv_row(row, date=args.date))
            if decisions and not args.dry_run:
                write_jsonl(path, rows)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    appended = 0 if args.dry_run else append_dataset(args.dataset, accepted_rows)
    if not args.dry_run and args.run_id:
        update_memories(args.run_id, output_root=args.output_root)
    print(f"reviewed_rows={changed}")
    print(f"accepted_rows_seen={len(accepted_rows)}")
    print(f"appended_rows={appended}")
    print(str(args.dataset))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
