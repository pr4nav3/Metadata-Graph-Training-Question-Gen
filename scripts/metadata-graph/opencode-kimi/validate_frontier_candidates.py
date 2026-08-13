#!/usr/bin/env python3
"""Validate explorer frontier candidates and optionally promote valid rows."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from frontier_ledger import (
    DEFAULT_LEDGER_PATH,
    DEFAULT_LEDGER_VIEW_PATH,
    DEFAULT_EXPLORED_REGIONS_PATH,
    load_ledger,
    mutate_ledger,
    next_frontier_id,
    ordered_frontier,
    render_markdown,
    render_previous_explored_regions,
)
from run_identity import normalize_ws
from sebi_retrieval import DataUnavailable, DocumentStore, GraphStore, default_graph_db


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
DEFAULT_GRAPH_DB = default_graph_db(METADATA_GRAPH_DIR)
DEFAULT_CACHE_DB = METADATA_GRAPH_DIR / "output" / "hydration" / "doc_cache.sqlite"
DEFAULT_VESPA_URL = (
    os.environ.get("METADATA_KG_VESPA_QUERY_URL")
    or os.environ.get("VESPA_QUERY_URL")
    or "http://localhost:18081/search/"
)
CHUNK_RE = re.compile(r"^([^#\s]+)#(\d+)$")


def stable_text(value: Any) -> str:
    return normalize_ws(value).lower()


def clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = normalize_ws(item)
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def load_candidates(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            rows.append({"_candidate_file": str(path), "_line_number": 0, "_load_error": "file does not exist"})
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                rows.append({"_candidate_file": str(path), "_line_number": line_number, "_load_error": str(exc)})
                continue
            if not isinstance(value, dict):
                rows.append({"_candidate_file": str(path), "_line_number": line_number, "_load_error": "row is not an object"})
                continue
            value["_candidate_file"] = str(path)
            value["_line_number"] = line_number
            rows.append(value)
    return rows


def candidate_paths(args: argparse.Namespace) -> list[Path]:
    paths = [canonical_path(Path(item)) for item in args.candidate_file]
    candidate_dir = canonical_path(args.candidate_dir)
    if candidate_dir.exists():
        paths.extend(canonical_path(path) for path in sorted(candidate_dir.glob("*.jsonl")))
    return list(dict.fromkeys(paths))


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


def chunk_exists(documents: DocumentStore, chunk_id: str) -> bool:
    match = CHUNK_RE.fullmatch(chunk_id)
    if not match:
        return False
    doc_id, raw_index = match.groups()
    try:
        documents.chunk(doc_id, int(raw_index), max_chars=80)
        return True
    except DataUnavailable:
        return False


def validate_one(
    raw: dict[str, Any],
    *,
    graph: GraphStore,
    documents: DocumentStore,
    ledger_titles: set[str],
    ledger_seed_sets: set[tuple[str, ...]],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if raw.get("_load_error"):
        errors.append(f"load error: {raw['_load_error']}")

    title = normalize_ws(raw.get("title"))
    scope_hint = normalize_ws(raw.get("scope_hint"))
    why_unexplored = normalize_ws(raw.get("why_unexplored"))
    seed_doc_ids = clean_list(raw.get("seed_doc_ids"))
    candidate_doc_ids = clean_list(raw.get("candidate_doc_ids"))
    seed_set = tuple(sorted(seed_doc_ids))

    if not title:
        errors.append("title is required")
    elif stable_text(title) in ledger_titles:
        errors.append("title already exists in ledger")
    if not scope_hint:
        errors.append("scope_hint is required")
    if not why_unexplored:
        warnings.append("why_unexplored is empty")
    if len(seed_doc_ids) < 2 and not raw.get("single_doc_ok"):
        errors.append("expected at least two seed_doc_ids, or single_doc_ok=true for a special omnibus frontier")
    if len(seed_doc_ids) > 5:
        warnings.append("more than five seed docs; frontier may be too broad")
    if len(seed_set) >= 2 and seed_set in ledger_seed_sets:
        errors.append("exact seed_doc_ids set already exists in ledger")

    for doc_id in seed_doc_ids + candidate_doc_ids:
        if not doc_exists(graph, documents, doc_id):
            errors.append(f"doc_id not found in graph/cache: {doc_id}")

    role_doc_ids: set[str] = set()
    seed_roles = raw.get("seed_roles")
    if isinstance(seed_roles, list):
        for item in seed_roles:
            if not isinstance(item, dict):
                warnings.append("seed_roles contains non-object item")
                continue
            doc_id = normalize_ws(item.get("doc_id"))
            if doc_id:
                role_doc_ids.add(doc_id)
            for chunk_id in clean_list(item.get("verified_chunk_ids")):
                if not chunk_exists(documents, chunk_id):
                    errors.append(f"verified chunk not found: {chunk_id}")
    else:
        warnings.append("seed_roles missing or not an array")
    missing_roles = [doc_id for doc_id in seed_doc_ids if doc_id not in role_doc_ids]
    if missing_roles:
        warnings.append("seed_roles missing for: " + ", ".join(missing_roles[:5]))

    if re.search(r"\b\d{1,3}(?:,\d{2,3})*(?:\.\d+)?\s*(?:%|crore|lakh|rs\.?|₹)\b", scope_hint, re.I):
        warnings.append("scope_hint includes exact values; keep candidates directional where possible")
    if len(scope_hint) > 900:
        warnings.append("scope_hint is long; consider shortening before promotion")

    clean = {
        "title": title,
        "kind": normalize_ws(raw.get("kind")) or "exploratory",
        "seed_doc_ids": seed_doc_ids,
        "candidate_doc_ids": candidate_doc_ids,
        "scope_hint": scope_hint,
        "why_unexplored": why_unexplored,
        "avoid": normalize_ws(raw.get("avoid")),
        "source": normalize_ws(raw.get("source")) or "opencode_explorer",
        "source_ids": clean_list(raw.get("source_ids")),
        "notes": normalize_ws(raw.get("notes")),
    }
    return {
        "candidate_file": raw.get("_candidate_file"),
        "line_number": raw.get("_line_number"),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "candidate": clean,
        "promoted_frontier_id": "",
    }


def promote_valid(results: list[dict[str, Any]], ledger_path: Path) -> None:
    valid_results = [item for item in results if item["valid"]]
    if not valid_results:
        return

    def mutator(rows: list[dict[str, Any]]) -> None:
        title_seen = {stable_text(row.get("title")) for row in rows}
        seed_seen = {tuple(sorted(row.get("seed_doc_ids") or [])) for row in rows if len(row.get("seed_doc_ids") or []) >= 2}
        for item in valid_results:
            candidate = item["candidate"]
            seed_set = tuple(sorted(candidate.get("seed_doc_ids") or []))
            if stable_text(candidate.get("title")) in title_seen:
                item["valid"] = False
                item["errors"].append("became duplicate by title before promotion")
                continue
            if len(seed_set) >= 2 and seed_set in seed_seen:
                item["valid"] = False
                item["errors"].append("became duplicate by seed set before promotion")
                continue
            frontier_id = next_frontier_id(rows)
            source_ids = candidate.get("source_ids") or []
            source_ids = source_ids + [str(item.get("candidate_file") or "")]
            row = ordered_frontier({**candidate, "frontier_id": frontier_id, "status": "ready", "source_ids": source_ids})
            rows.append(row)
            title_seen.add(stable_text(row.get("title")))
            if len(seed_set) >= 2:
                seed_seen.add(seed_set)
            item["promoted_frontier_id"] = frontier_id

    mutate_ledger(ledger_path, mutator)


def write_views(ledger_path: Path, view_path: Path, explored_path: Path) -> None:
    rows = load_ledger(ledger_path)
    view_path.write_text(render_markdown(rows), encoding="utf-8")
    explored_path.write_text(render_previous_explored_regions(rows), encoding="utf-8")


def unique_failure_path(path: Path, failure_dir: Path) -> Path:
    failure_dir.mkdir(parents=True, exist_ok=True)
    target = failure_dir / path.name
    if not target.exists():
        return target
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = failure_dir / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    return failure_dir / f"{stem}.{int(time.time())}{suffix}"


def clean_handled_candidate_files(paths: list[Path], results: list[dict[str, Any]], failure_dir: Path) -> dict[str, Any]:
    by_file: dict[str, list[dict[str, Any]]] = {str(path): [] for path in paths}
    for item in results:
        candidate_file = normalize_ws(item.get("candidate_file"))
        if candidate_file:
            by_file.setdefault(candidate_file, []).append(item)

    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    cleanup_errors: list[str] = []
    for path in paths:
        key = str(path)
        if not path.exists():
            continue
        file_results = by_file.get(key, [])
        fully_handled = all(item.get("valid") and item.get("promoted_frontier_id") for item in file_results)
        if fully_handled:
            try:
                path.unlink()
                deleted.append(key)
            except OSError as exc:
                cleanup_errors.append(f"could not delete handled candidate file {key}: {exc}")
            continue
        try:
            target = unique_failure_path(path, failure_dir)
            path.replace(target)
            failed.append({"candidate_file": key, "moved_to": str(canonical_path(target))})
        except OSError as exc:
            cleanup_errors.append(f"could not move failed candidate file {key}: {exc}")

    return {
        "deleted_candidate_files": deleted,
        "failed_candidate_files": failed,
        "cleanup_errors": cleanup_errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-file", action="append", default=[])
    parser.add_argument("--candidate-dir", type=Path, default=OUTPUT_ROOT / "frontier_candidates")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--graph-db", type=Path, default=DEFAULT_GRAPH_DB)
    parser.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE_DB)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--fail-on-invalid", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = candidate_paths(args)
    output_root = canonical_path(args.output_root)
    report_path = canonical_path(args.report_path or output_root / "frontier_candidate_validations" / f"validation_{time.strftime('%Y%m%d_%H%M%S')}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    ledger_rows = load_ledger(args.ledger)
    ledger_titles = {stable_text(row.get("title")) for row in ledger_rows}
    ledger_seed_sets = {tuple(sorted(row.get("seed_doc_ids") or [])) for row in ledger_rows if len(row.get("seed_doc_ids") or []) >= 2}

    graph = GraphStore(args.graph_db)
    documents = DocumentStore(args.cache_db, graph, DEFAULT_VESPA_URL, allow_live_fetch=False)
    try:
        candidates = load_candidates(paths)
        results = [
            validate_one(
                candidate,
                graph=graph,
                documents=documents,
                ledger_titles=ledger_titles,
                ledger_seed_sets=ledger_seed_sets,
            )
            for candidate in candidates
        ]
    finally:
        documents.close()
        graph.close()

    if args.promote:
        promote_valid(results, args.ledger)
        write_views(args.ledger, DEFAULT_LEDGER_VIEW_PATH, DEFAULT_EXPLORED_REGIONS_PATH)

    summary = {
        "candidate_files": [str(path) for path in paths],
        "candidate_count": len(results),
        "promotion_attempted": bool(args.promote),
        "valid_count": sum(1 for item in results if item["valid"]),
        "promoted_count": sum(1 for item in results if item.get("promoted_frontier_id")),
        "report_path": str(report_path),
        "results": results,
    }
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.promote:
        summary.update(clean_handled_candidate_files(paths, results, output_root / "frontier_candidate_failures"))
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"candidate_count={summary['candidate_count']}")
        print(f"valid_count={summary['valid_count']}")
        print(f"promoted_count={summary['promoted_count']}")
        print(f"report={report_path}")
    if args.fail_on_invalid and summary["valid_count"] != summary["candidate_count"]:
        return 1
    if args.promote and (summary["valid_count"] != summary["candidate_count"] or summary.get("cleanup_errors")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
