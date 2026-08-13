#!/usr/bin/env python3
"""Compact frontier ledger for OpenCode/Kimi SEBI discovery runs."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from run_identity import frontier_assigned_run_id, frontier_memory_id, normalize_ws


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
DEFAULT_LEDGER_PATH = OUTPUT_ROOT / "frontier_ledger.jsonl"
DEFAULT_LEDGER_VIEW_PATH = OUTPUT_ROOT / "frontier_ledger.md"
DEFAULT_EXPLORED_REGIONS_PATH = OUTPUT_ROOT / "previous_explored_regions.md"

ID_RE = re.compile(r"^frontier_(\d{4,})$")

ACTIVE_STATUSES = {"ready", "continue_generation", "assigned", "running", "needs_review"}
TERMINAL_STATUSES = {"saturated", "rejected", "duplicate"}
ALLOWED_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

FIELD_ORDER = [
    "frontier_id",
    "status",
    "title",
    "kind",
    "seed_doc_ids",
    "candidate_doc_ids",
    "scope_hint",
    "why_unexplored",
    "avoid",
    "source",
    "source_ids",
    "assigned_run_id",
    "memory_id",
    "outcome",
    "notes",
    "created_at",
    "updated_at",
]


def clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = normalize_ws(item)
        if text and text not in seen:
            cleaned.append(text)
            seen.add(text)
    return cleaned


def truncate(value: str, limit: int) -> str:
    value = normalize_ws(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def ordered_frontier(raw: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    status = normalize_ws(raw.get("status")) or "ready"
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid frontier status `{status}`")
    row = {
        "frontier_id": normalize_ws(raw.get("frontier_id")),
        "status": status,
        "title": normalize_ws(raw.get("title")),
        "kind": normalize_ws(raw.get("kind")) or "manual",
        "seed_doc_ids": clean_list(raw.get("seed_doc_ids")),
        "candidate_doc_ids": clean_list(raw.get("candidate_doc_ids")),
        "scope_hint": normalize_ws(raw.get("scope_hint")),
        "why_unexplored": normalize_ws(raw.get("why_unexplored")),
        "avoid": normalize_ws(raw.get("avoid")),
        "source": normalize_ws(raw.get("source")),
        "source_ids": clean_list(raw.get("source_ids")),
        "assigned_run_id": frontier_assigned_run_id(raw),
        "memory_id": frontier_memory_id(raw),
        "outcome": normalize_ws(raw.get("outcome")),
        "notes": normalize_ws(raw.get("notes")),
        "created_at": int(raw.get("created_at") or now),
        "updated_at": int(raw.get("updated_at") or now),
    }
    if not row["frontier_id"]:
        raise ValueError("frontier_id is required")
    if not row["title"]:
        raise ValueError(f"{row['frontier_id']}: title is required")
    if not row["scope_hint"]:
        raise ValueError(f"{row['frontier_id']}: scope_hint is required")
    return {key: row[key] for key in FIELD_ORDER}


def load_ledger(path: Path = DEFAULT_LEDGER_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = ordered_frontier(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            frontier_id = row["frontier_id"]
            if frontier_id in seen:
                raise ValueError(f"{path}:{line_number}: duplicate frontier_id `{frontier_id}`")
            seen.add(frontier_id)
            rows.append(row)
    return rows


def ledger_lock_path(path: Path = DEFAULT_LEDGER_PATH) -> Path:
    return path.with_name(f"{path.name}.lock")


@contextmanager
def locked_ledger(path: Path = DEFAULT_LEDGER_PATH) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_lock_path(path)
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def write_ledger(rows: Iterable[dict[str, Any]], path: Path = DEFAULT_LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [ordered_frontier(row) for row in rows]
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            for row in ordered:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def mutate_ledger(path: Path, mutator: Callable[[list[dict[str, Any]]], Any]) -> Any:
    with locked_ledger(path):
        rows = load_ledger(path)
        result = mutator(rows)
        write_ledger(rows, path)
        return result


def update_ledger_frontier(path: Path, frontier_id: str, **kwargs: Any) -> dict[str, Any]:
    def mutator(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return update_frontier(rows, frontier_id, **kwargs)

    return mutate_ledger(path, mutator)


def next_frontier_id(rows: Iterable[dict[str, Any]]) -> str:
    highest = 0
    for row in rows:
        match = ID_RE.fullmatch(normalize_ws(row.get("frontier_id")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"frontier_{highest + 1:04d}"


def find_frontier(rows: list[dict[str, Any]], frontier_id: str) -> dict[str, Any]:
    wanted = normalize_ws(frontier_id)
    for row in rows:
        if row["frontier_id"] == wanted:
            return row
    raise KeyError(f"frontier not found: {wanted}")


def upsert_frontier(rows: list[dict[str, Any]], frontier: dict[str, Any]) -> list[dict[str, Any]]:
    row = ordered_frontier(frontier)
    for index, existing in enumerate(rows):
        if existing["frontier_id"] == row["frontier_id"]:
            rows[index] = row
            return rows
    rows.append(row)
    return rows


def update_frontier(
    rows: list[dict[str, Any]],
    frontier_id: str,
    *,
    status: str | None = None,
    run_id: str = "",
    memory_id: str = "",
    clear_run: bool = False,
    clear_memory: bool = False,
    outcome: str = "",
    notes: str = "",
    clear_outcome: bool = False,
    clear_notes: bool = False,
) -> dict[str, Any]:
    row = dict(find_frontier(rows, frontier_id))
    if status:
        status = normalize_ws(status)
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"invalid frontier status `{status}`")
        row["status"] = status
    if clear_run:
        row["assigned_run_id"] = ""
    elif run_id:
        row["assigned_run_id"] = normalize_ws(run_id)
    if clear_memory:
        row["memory_id"] = ""
    elif memory_id:
        row["memory_id"] = normalize_ws(memory_id)
    if clear_outcome:
        row["outcome"] = ""
    elif outcome:
        row["outcome"] = normalize_ws(outcome)
    if clear_notes:
        row["notes"] = ""
    elif notes:
        row["notes"] = normalize_ws(notes)
    row["updated_at"] = int(time.time())
    upsert_frontier(rows, row)
    return row


def frontier_context(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "frontier_id": row["frontier_id"],
        "frontier_title": row["title"],
        "frontier_kind": row["kind"],
        "frontier_scope_hint": row["scope_hint"],
        "frontier_why_unexplored": row["why_unexplored"],
        "frontier_avoid": row["avoid"],
        "frontier_seed_doc_ids": row["seed_doc_ids"],
        "frontier_candidate_doc_ids": row["candidate_doc_ids"],
        "frontier_source": row["source"],
        "frontier_source_ids": row["source_ids"],
    }


def is_schedulable_frontier(row: dict[str, Any]) -> bool:
    status = normalize_ws(row.get("status"))
    if status == "continue_generation":
        return True
    if status != "ready":
        return False
    return not frontier_assigned_run_id(row) and not normalize_ws(row.get("outcome"))


def render_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# OpenCode/Kimi Frontier Ledger",
        "",
        "This is the compact Codex-curated backlog of evidence regions. It is not evidence for answers.",
        "Question-generation workers should receive one selected frontier through their run card, not this whole file.",
        "",
    ]
    status_order = ["ready", "continue_generation", "assigned", "running", "needs_review", "saturated", "rejected", "duplicate"]
    for status in status_order:
        group = [row for row in rows if row["status"] == status]
        if not group:
            continue
        lines.append(f"## {status}")
        for row in group:
            lines.append(f"- `{row['frontier_id']}` {row['title']} [{row['kind']}]")
            if row["seed_doc_ids"]:
                lines.append(f"  seeds: {', '.join(row['seed_doc_ids'])}")
            if row["scope_hint"]:
                lines.append(f"  scope: {truncate(row['scope_hint'], 220)}")
            if row["avoid"]:
                lines.append(f"  avoid: {truncate(row['avoid'], 180)}")
            if row["assigned_run_id"]:
                lines.append(f"  run_id: {row['assigned_run_id']}")
            if row["outcome"]:
                lines.append(f"  outcome: {truncate(row['outcome'], 220)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def list_preview(items: list[str], *, limit: int = 6) -> str:
    if not items:
        return ""
    shown = items[:limit]
    if len(items) > limit:
        shown.append(f"+{len(items) - limit} more")
    return ", ".join(shown)


def render_region_row(row: dict[str, Any]) -> list[str]:
    lines = [f"- `{row['frontier_id']}` [{row['status']}/{row['kind']}] {truncate(row['title'], 170)}"]
    doc_bits = []
    seeds = list_preview(row.get("seed_doc_ids") or [])
    candidates = list_preview(row.get("candidate_doc_ids") or [])
    if seeds:
        doc_bits.append(f"seeds: {seeds}")
    if candidates:
        doc_bits.append(f"candidates: {candidates}")
    if doc_bits:
        lines.append(f"  {'; '.join(doc_bits)}")
    if row.get("outcome"):
        lines.append(f"  outcome: {truncate(row['outcome'], 240)}")
    if row.get("avoid"):
        lines.append(f"  avoid: {truncate(row['avoid'], 220)}")
    link_bits = []
    if row.get("assigned_run_id"):
        link_bits.append(f"run_id={row['assigned_run_id']}")
    if row.get("memory_id"):
        link_bits.append(f"memory_id={row['memory_id']}")
    if link_bits:
        lines.append(f"  links: {', '.join(link_bits)}")
    return lines


def render_previous_explored_regions(rows: list[dict[str, Any]]) -> str:
    """Render compact explorer memory from the frontier ledger.

    This file is intentionally not a coverage-statistics report. It is a
    negative-memory digest for frontier discovery: what is already queued, in
    flight, closed, or known to be weak.
    """
    lines = [
        "# Previous Explored Regions",
        "",
        "This file is compact explorer memory generated from the frontier ledger.",
        "It intentionally contains no graph coverage statistics, density rankings,",
        "or topic-priority suggestions. Use it only to avoid repeating regions that",
        "are already queued, active, saturated, rejected, or duplicate.",
        "",
        "Do not use this as evidence for answers. Use the graph and corpus directly",
        "when deciding whether a new frontier is real and memory-dense.",
        "Scope hints are omitted here on purpose; use titles, seeds, outcomes,",
        "and avoid notes only as negative memory for where not to repeat.",
        "",
    ]
    sections = [
        (
            "Already Queued Or Active",
            ["ready", "continue_generation", "assigned", "running", "needs_review"],
            "Do not add another frontier for these regions unless the new frontier is materially narrower or different.",
        ),
        (
            "Already Explored Or Closed",
            ["saturated", "rejected", "duplicate"],
            "Avoid these regions unless new graph/corpus evidence creates a genuinely different memory target.",
        ),
    ]
    for title, statuses, note in sections:
        lines.extend([f"## {title}", "", note, ""])
        found_any = False
        for status in statuses:
            group = [row for row in rows if row["status"] == status]
            if not group:
                continue
            found_any = True
            lines.extend([f"### {status}", ""])
            for row in group:
                lines.extend(render_region_row(row))
            lines.append("")
        if not found_any:
            lines.extend(["- none", ""])
    return "\n".join(lines).rstrip() + "\n"


def print_rows(rows: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for row in rows:
        seeds = ",".join(row["seed_doc_ids"][:3])
        suffix = f" seeds={seeds}" if seeds else ""
        run = f" run_id={row['assigned_run_id']}" if row["assigned_run_id"] else ""
        print(f"{row['frontier_id']}\t{row['status']}\t{row['kind']}\t{row['title']}{suffix}{run}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List frontier rows.")
    list_parser.add_argument("--status", choices=sorted(ALLOWED_STATUSES))
    list_parser.add_argument("--kind")
    list_parser.add_argument("--limit", type=int, default=0)
    list_parser.add_argument("--json", action="store_true")

    show_parser = subparsers.add_parser("show", help="Show one frontier as JSON.")
    show_parser.add_argument("frontier_id")

    next_parser = subparsers.add_parser("next", help="Print the next schedulable frontier.")
    next_parser.add_argument("--json", action="store_true")

    add_parser = subparsers.add_parser("add", help="Add one frontier.")
    add_parser.add_argument("--frontier-id", default="")
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--kind", default="manual")
    add_parser.add_argument("--seed-doc-id", action="append", default=[])
    add_parser.add_argument("--candidate-doc-id", action="append", default=[])
    add_parser.add_argument("--scope-hint", required=True)
    add_parser.add_argument("--why-unexplored", default="")
    add_parser.add_argument("--avoid", default="")
    add_parser.add_argument("--source", default="codex")
    add_parser.add_argument("--source-id", action="append", default=[])
    add_parser.add_argument("--notes", default="")

    update_parser = subparsers.add_parser("update", help="Update frontier status/context.")
    update_parser.add_argument("frontier_id")
    update_parser.add_argument("--status", choices=sorted(ALLOWED_STATUSES))
    update_parser.add_argument("--run-id", default="")
    update_parser.add_argument("--memory-id", default="")
    update_parser.add_argument("--outcome", default="")
    update_parser.add_argument("--notes", default="")
    update_parser.add_argument("--clear-run", action="store_true")
    update_parser.add_argument("--clear-memory", action="store_true")
    update_parser.add_argument("--clear-outcome", action="store_true")
    update_parser.add_argument("--clear-notes", action="store_true")

    export_parser = subparsers.add_parser("export-md", help="Write a Markdown view of the ledger.")
    export_parser.add_argument("--output", type=Path, default=DEFAULT_LEDGER_VIEW_PATH)

    explored_parser = subparsers.add_parser(
        "export-explored-regions",
        help="Write compact explorer memory derived from all existing frontier rows.",
    )
    explored_parser.add_argument("--output", type=Path, default=DEFAULT_EXPLORED_REGIONS_PATH)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "add":
            def add_frontier(rows: list[dict[str, Any]]) -> str:
                frontier_id = normalize_ws(args.frontier_id) or next_frontier_id(rows)
                if any(row["frontier_id"] == frontier_id for row in rows):
                    raise ValueError(f"frontier already exists: {frontier_id}")
                frontier = {
                    "frontier_id": frontier_id,
                    "status": "ready",
                    "title": args.title,
                    "kind": args.kind,
                    "seed_doc_ids": args.seed_doc_id,
                    "candidate_doc_ids": args.candidate_doc_id,
                    "scope_hint": args.scope_hint,
                    "why_unexplored": args.why_unexplored,
                    "avoid": args.avoid,
                    "source": args.source,
                    "source_ids": args.source_id,
                    "notes": args.notes,
                }
                rows.append(ordered_frontier(frontier))
                return frontier_id

            print(mutate_ledger(args.ledger, add_frontier))
            return 0

        if args.command == "update":
            update_ledger_frontier(
                args.ledger,
                args.frontier_id,
                status=args.status,
                run_id=args.run_id,
                memory_id=args.memory_id,
                outcome=args.outcome,
                notes=args.notes,
                clear_run=args.clear_run,
                clear_memory=args.clear_memory,
                clear_outcome=args.clear_outcome,
                clear_notes=args.clear_notes,
            )
            print(args.frontier_id)
            return 0

        rows = load_ledger(args.ledger)

        if args.command == "list":
            selected = rows
            if args.status:
                selected = [row for row in selected if row["status"] == args.status]
            if args.kind:
                selected = [row for row in selected if row["kind"] == normalize_ws(args.kind)]
            if args.limit > 0:
                selected = selected[: args.limit]
            print_rows(selected, as_json=args.json)
            return 0

        if args.command == "show":
            print(json.dumps(find_frontier(rows, args.frontier_id), ensure_ascii=False, indent=2))
            return 0

        if args.command == "next":
            ready = [row for row in rows if is_schedulable_frontier(row)]
            if not ready:
                return 1
            print_rows([ready[0]], as_json=args.json)
            return 0

        if args.command == "export-md":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_markdown(rows), encoding="utf-8")
            print(str(args.output))
            return 0

        if args.command == "export-explored-regions":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_previous_explored_regions(rows), encoding="utf-8")
            print(str(args.output))
            return 0

    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
