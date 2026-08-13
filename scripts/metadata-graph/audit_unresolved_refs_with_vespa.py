#!/usr/bin/env python3
"""Probe unresolved graph references against Vespa.

This is intentionally audit-only. A Vespa hit means "this document contains the
identifier", not "this document owns the identifier", so the script only marks
high-confidence owner candidates when the exact identifier appears in identity
fields or opening chunks with a compatible document type.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any

from build_metadata_graph_v2 import NON_DOCUMENT_REFERENCE_KINDS, normalize_ws


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GRAPH_DB = SCRIPT_DIR / "output_v2" / "sebi_metadata_graph_v2.sqlite"
DEFAULT_OUT_JSONL = SCRIPT_DIR / "output_v2" / "logs" / "unresolved_refs_vespa_probe.jsonl"
DEFAULT_VESPA_URL = (
    os.environ.get("METADATA_KG_VESPA_QUERY_URL")
    or os.environ.get("VESPA_QUERY_URL")
    or "http://localhost:18081/search/"
)
DOC_LIKE_KINDS = {
    "adjudication_order_id",
    "circular_id",
    "order_id",
    "quasi_judicial_order_id",
    "recovery_certificate",
    "reference_text",
    "sebi_identifier",
    "whole_time_member_order_id",
}


def compact(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def identifier_compacts(kind: str, canonical: str) -> set[str]:
    values = {compact(canonical)}
    rc = re.search(r"\bRC\s+(\d+)\s+OF\s+(\d{4})\b", canonical, re.I)
    if kind == "recovery_certificate" and rc:
        number, year = rc.groups()
        values.add(compact(f"Recovery Certificate No. {number} of {year}"))
        values.add(compact(f"Certificate No. {number} of {year}"))
        values.add(compact(f"RC{number} of {year}"))
    return {value for value in values if value}


def contains_identifier(value: Any, needles: set[str]) -> bool:
    haystack = compact(value)
    return bool(haystack and any(needle in haystack for needle in needles))


def contains_identifier_near_front(value: Any, needles: set[str], max_chars: int = 350) -> bool:
    text = str(value or "")[:max_chars]
    return contains_identifier(text, needles)


def chunk_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("chunk") or value.get("text") or "")
    return ""


def hit_chunks(fields: dict[str, Any]) -> list[dict[str, Any]]:
    match_cells = (
        ((fields.get("matchfeatures") or {}).get("chunk_scores") or {}).get("cells") or {}
    )
    ranked_indices = sorted(
        (
            (float(score), int(index))
            for index, score in match_cells.items()
            if str(index).isdigit() and isinstance(score, (int, float))
        ),
        reverse=True,
    )
    indices = [index for _, index in ranked_indices[:3]]

    summary = fields.get("top_chunks_summary")
    position_cells = (
        ((fields.get("summaryfeatures") or {}).get("best_summary_chunks") or {}).get("cells") or {}
    )
    positions = sorted(int(index) for index in position_cells if str(index).isdigit())
    if isinstance(summary, list) and positions:
        by_index = {index: summary[pos] for pos, index in enumerate(positions) if pos < len(summary)}
        use_indices = indices or positions[:3]
        return [
            {"index": index, "text": normalize_ws(chunk_text(by_index[index]))[:700]}
            for index in use_indices
            if index in by_index
        ]

    raw_summary = fields.get("chunks_summary")
    raw_positions = fields.get("chunks_pos_summary")
    if not isinstance(raw_summary, list):
        return []
    if isinstance(raw_positions, list):
        by_index = {
            int(index): raw_summary[pos]
            for pos, index in enumerate(raw_positions)
            if pos < len(raw_summary) and isinstance(index, int)
        }
    else:
        by_index = {index: value for index, value in enumerate(raw_summary)}
    use_indices = indices or list(by_index)[:3]
    return [
        {"index": index, "text": normalize_ws(chunk_text(by_index[index]))[:700]}
        for index in use_indices
        if index in by_index
    ]


def compatible_owner_kind(kind: str, title: str, file_name: str) -> bool:
    text = f"{title} {file_name}".lower()
    if kind == "circular_id":
        return "circular" in text or "/circulars/" in text
    if kind in {"order_id", "whole_time_member_order_id", "quasi_judicial_order_id", "adjudication_order_id"}:
        # Recovery documents often mention an originating enforcement order in
        # the first page. That is citation evidence, not owner identity evidence.
        if "recovery proceedings" in text:
            return False
        return "/orders/" in text or "orders of " in text or "adjudication order" in text
    if kind == "recovery_certificate":
        return "recovery" in text or "certificate" in text
    if kind == "sebi_identifier":
        return any(token in text for token in ("circular", "order", "master circular", "letter"))
    return False


def classify_hit(kind: str, canonical: str, fields: dict[str, Any]) -> dict[str, Any]:
    needles = identifier_compacts(kind, canonical)
    title = normalize_ws(fields.get("title") or "")
    file_name = normalize_ws(fields.get("fileName") or "")
    document_id = normalize_ws(fields.get("document_id") or "")
    referenced_ids = fields.get("referenced_ids") or []
    chunks = hit_chunks(fields)

    title_match = contains_identifier(title, needles)
    file_match = contains_identifier(file_name, needles)
    document_id_match = contains_identifier(document_id, needles)
    referenced_id_match = any(contains_identifier(value, needles) for value in referenced_ids)
    chunk_matches = []
    for chunk in chunks:
        if not contains_identifier(chunk.get("text"), needles):
            continue
        chunk_matches.append(
            {
                "index": chunk["index"],
                "opening": int(chunk["index"]) <= 2,
                "front": contains_identifier_near_front(chunk.get("text"), needles),
            }
        )
    first_chunk_match = any(int(item["index"]) == 0 for item in chunk_matches)
    first_chunk_front_match = any(int(item["index"]) == 0 and item.get("front") for item in chunk_matches)
    early_chunk_match = any(item["opening"] for item in chunk_matches)
    late_chunk_match = any(not item["opening"] for item in chunk_matches)
    compatible = compatible_owner_kind(kind, title, file_name)
    strict_first_chunk_kind = kind in {
        "adjudication_order_id",
        "circular_id",
        "order_id",
        "quasi_judicial_order_id",
        "sebi_identifier",
        "whole_time_member_order_id",
    }
    if kind in {"circular_id", "sebi_identifier"}:
        chunk_owner_match = first_chunk_front_match
    else:
        chunk_owner_match = first_chunk_match if strict_first_chunk_kind else early_chunk_match
    master_circular_context = bool(
        re.search(r"(?:^|[^A-Za-z0-9])master[-_\s]+circular(?:$|[^A-Za-z0-9])", f"{title} {file_name}", re.I)
    )
    if kind in {"circular_id", "sebi_identifier"} and master_circular_context and not (
        title_match or file_match or document_id_match
    ):
        chunk_owner_match = False
    owner_candidate = bool(
        compatible
        and (title_match or file_match or document_id_match or chunk_owner_match)
    )
    if owner_candidate:
        reason = "identity_field_or_opening_chunk_exact_match"
    elif referenced_id_match and not chunk_matches:
        reason = "matched_referenced_ids_only"
    elif late_chunk_match:
        reason = "matched_late_body_reference"
    elif title_match or file_match or document_id_match or early_chunk_match:
        reason = "exact_match_but_incompatible_document_type"
    else:
        reason = "no_exact_identifier_in_returned_fields"

    return {
        "ownerCandidate": owner_candidate,
        "reason": reason,
        "signals": {
            "titleMatch": title_match,
            "fileNameMatch": file_match,
            "documentIdMatch": document_id_match,
            "firstChunkMatch": first_chunk_match,
            "firstChunkFrontMatch": first_chunk_front_match,
            "referencedIdsMatch": referenced_id_match,
            "earlyChunkMatch": early_chunk_match,
            "lateChunkMatch": late_chunk_match,
            "compatibleKind": compatible,
            "chunkMatches": chunk_matches,
        },
        "chunks": chunks,
    }


def vespa_search(url: str, query: str, hits: int) -> dict[str, Any]:
    body = {
        "yql": "select * from kb_items where userInput(@query)",
        "query": query[:300],
        "hits": max(1, min(int(hits), 20)),
        "timeout": "30s",
        "ranking.profile": "default_native_dynamic_chunks_file_v6_rsf_vec",
        "input.query(alpha)": 0.0,
        "presentation.summary": "lean",
        "input.query(summary_chunks)": 3,
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


def load_vespa_doc_to_graph_doc(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT json_extract(v.attrs_json, '$.docId') AS vespa_doc_id,
               d.id AS graph_doc_id
        FROM edges ha
        JOIN nodes d ON d.id = ha.source
        JOIN nodes a ON a.id = ha.target
        JOIN edges vi ON vi.source = a.id AND vi.type = 'vespa_item'
        JOIN nodes v ON v.id = vi.target
        WHERE ha.type = 'has_attachment'
          AND d.type = 'Document'
          AND v.type = 'VespaItem'
        """
    ).fetchall()
    return {
        str(row["vespa_doc_id"]): str(row["graph_doc_id"])
        for row in rows
        if row["vespa_doc_id"] and row["graph_doc_id"]
    }


def unresolved_identifiers(
    conn: sqlite3.Connection,
    kinds: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    excluded_kinds = set(NON_DOCUMENT_REFERENCE_KINDS)
    if kinds:
        kind_filter = sorted(kinds)
    else:
        kind_filter = sorted(DOC_LIKE_KINDS - excluded_kinds)
    placeholders = ",".join("?" for _ in kind_filter)
    rows = conn.execute(
        f"""
        WITH src AS (
            SELECT e.source AS vespa_node, e.target AS ident, e.attrs_json AS edge_attrs
            FROM edges e
            JOIN nodes s ON s.id = e.source
            WHERE e.type = 'references_identifier'
              AND s.type = 'VespaItem'
        ),
        srcdoc AS (
            SELECT vi.target AS vespa_node, ha.source AS doc_node
            FROM edges vi
            JOIN edges ha ON ha.target = vi.source AND ha.type = 'has_attachment'
            WHERE vi.type = 'vespa_item'
        ),
        targets AS (
            SELECT hi.target AS ident, hi.source AS target_doc
            FROM edges hi
            JOIN nodes ns ON ns.id = hi.source
            WHERE hi.type = 'has_identifier'
              AND ns.type = 'Document'
        ),
        unresolved AS (
            SELECT src.ident, src.vespa_node, srcdoc.doc_node, src.edge_attrs
            FROM src
            LEFT JOIN srcdoc ON srcdoc.vespa_node = src.vespa_node
            LEFT JOIN targets
              ON targets.ident = src.ident
             AND (srcdoc.doc_node IS NULL OR targets.target_doc <> srcdoc.doc_node)
            WHERE targets.target_doc IS NULL
        )
        SELECT ni.id AS identifier_id,
               ni.kind AS kind,
               ni.name AS canonical,
               count(*) AS occurrence_count,
               count(DISTINCT unresolved.doc_node) AS source_doc_count,
               count(DISTINCT unresolved.vespa_node) AS source_vespa_count,
               json_group_array(DISTINCT json_extract(unresolved.edge_attrs, '$.raw')) AS raw_examples
        FROM unresolved
        JOIN nodes ni ON ni.id = unresolved.ident
        WHERE ni.type = 'Identifier'
          AND ni.kind IN ({placeholders})
        GROUP BY ni.id, ni.kind, ni.name
        ORDER BY occurrence_count DESC, ni.kind, ni.name
        LIMIT ?
        """,
        (*kind_filter, max(1, int(limit))),
    ).fetchall()
    return [dict(row) for row in rows]


def source_docs_for_identifier(conn: sqlite3.Connection, identifier_id: str, limit: int = 20) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT ha.source AS doc_node
        FROM edges e
        JOIN nodes s ON s.id = e.source AND s.type = 'VespaItem'
        JOIN edges vi ON vi.target = e.source AND vi.type = 'vespa_item'
        JOIN edges ha ON ha.target = vi.source AND ha.type = 'has_attachment'
        WHERE e.type = 'references_identifier'
          AND e.target = ?
          AND ha.source IS NOT NULL
        LIMIT ?
        """,
        (identifier_id, max(1, int(limit))),
    ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def probe_identifier(
    conn: sqlite3.Connection,
    vespa_doc_to_graph_doc: dict[str, str],
    row: dict[str, Any],
    *,
    vespa_url: str,
    hits: int,
) -> dict[str, Any]:
    canonical = normalize_ws(row["canonical"])
    source_docs = set(source_docs_for_identifier(conn, row["identifier_id"]))
    try:
        data = vespa_search(vespa_url, canonical, hits)
        error = ""
    except Exception as exc:
        data = {}
        error = f"{type(exc).__name__}: {exc}"

    result = {
        "identifierId": row["identifier_id"],
        "kind": row["kind"],
        "canonical": canonical,
        "occurrenceCount": int(row["occurrence_count"]),
        "sourceDocCount": int(row["source_doc_count"]),
        "sourceVespaCount": int(row["source_vespa_count"]),
        "rawExamples": json.loads(row.get("raw_examples") or "[]")[:5],
        "vespaTotalCount": data.get("root", {}).get("fields", {}).get("totalCount"),
        "acceptedOwnerCandidates": [],
        "rejectedHits": [],
        "error": error,
    }
    for child in data.get("root", {}).get("children", []) or []:
        fields = child.get("fields") or {}
        doc_id = normalize_ws(fields.get("docId") or "")
        graph_doc_id = vespa_doc_to_graph_doc.get(doc_id, "")
        classification = classify_hit(row["kind"], canonical, fields)
        hit = {
            "docId": doc_id,
            "graphDocId": graph_doc_id,
            "isKnownSourceDoc": bool(graph_doc_id and graph_doc_id in source_docs),
            "title": normalize_ws(fields.get("title") or fields.get("fileName") or doc_id),
            "fileName": normalize_ws(fields.get("fileName") or ""),
            "score": float(child.get("relevance") or 0.0),
            **classification,
        }
        if classification["ownerCandidate"] and graph_doc_id:
            result["acceptedOwnerCandidates"].append(hit)
        else:
            result["rejectedHits"].append(hit)
    result["acceptedOwnerCandidateCount"] = len(result["acceptedOwnerCandidates"])
    result["rejectedHitCount"] = len(result["rejectedHits"])
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-db", type=Path, default=DEFAULT_GRAPH_DB)
    parser.add_argument("--vespa-url", default=DEFAULT_VESPA_URL)
    parser.add_argument("--out-jsonl", type=Path, default=DEFAULT_OUT_JSONL)
    parser.add_argument("--limit-identifiers", type=int, default=50)
    parser.add_argument("--hits", type=int, default=8)
    parser.add_argument(
        "--kind",
        action="append",
        choices=sorted(DOC_LIKE_KINDS),
        help="Restrict to one or more identifier kinds.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.graph_db.exists():
        raise SystemExit(f"graph DB does not exist: {args.graph_db}")
    args.out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.graph_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        vespa_doc_to_graph_doc = load_vespa_doc_to_graph_doc(conn)
        rows = unresolved_identifiers(conn, set(args.kind or []), args.limit_identifiers)
        results = [
            probe_identifier(
                conn,
                vespa_doc_to_graph_doc,
                row,
                vespa_url=args.vespa_url,
                hits=args.hits,
            )
            for row in rows
        ]
    finally:
        conn.close()

    with args.out_jsonl.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    accepted = sum(item["acceptedOwnerCandidateCount"] for item in results)
    with_errors = sum(1 for item in results if item.get("error"))
    summary = {
        "ok": with_errors == 0,
        "graph_db": str(args.graph_db),
        "out_jsonl": str(args.out_jsonl),
        "identifier_count": len(results),
        "accepted_owner_candidates": accepted,
        "identifiers_with_owner_candidate": sum(
            1 for item in results if item["acceptedOwnerCandidateCount"] > 0
        ),
        "identifiers_with_errors": with_errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if with_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
