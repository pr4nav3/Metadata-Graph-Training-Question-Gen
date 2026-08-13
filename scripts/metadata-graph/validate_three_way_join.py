#!/usr/bin/env python3
"""
Three-Way Join Validation Script
=================================
Validates that we can join a SEBI document across all three data sources:
  1. scrape_state.db (SQLite)  -- documents + attachments
  2. Postgres (via docker exec)  -- collection_items with vespa_doc_id
  3. Vespa (HTTP API)            -- kb_items with metadata fields

The script picks 10 random resolved documents from scrape_state.db,
then tries to find their matching records in Postgres and Vespa.

Usage:
  python3 scripts/metadata-graph/validate_three_way_join.py

Outputs a human-readable join report to stdout.
"""

import json
import os
import random
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request

# ── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
SCRAPE_DB = os.path.join(PROJECT_ROOT, "SEBI-14K-share/source-files/files/scrape_state.db")
DEFAULT_VESPA_FEED_URL = (
    os.environ.get("METADATA_KG_VESPA_FEED_URL")
    or os.environ.get("VESPA_FEED_URL")
    or "http://localhost:18080"
)
VESPA_QUERY_URL = (
    os.environ.get("METADATA_KG_VESPA_QUERY_URL")
    or os.environ.get("VESPA_QUERY_URL")
    or "http://localhost:18081/search/"
)
VESPA_DOC_URL = DEFAULT_VESPA_FEED_URL.rstrip("/") + "/document/v1/namespace/kb_items/docid/{doc_id}"
POSTGRES_CONTAINER = (
    os.environ.get("METADATA_KG_POSTGRES_CONTAINER")
    or os.environ.get("XYNE_POSTGRES_CONTAINER")
    or "metadata-kg-xyne-db"
)
SAMPLE_SIZE = 10

# ── Helpers ─────────────────────────────────────────────────────────────────

def query_scrape_db(db_path, sql, params=()):
    """Run a query against the SQLite scrape_state.db."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def query_postgres(sql):
    """Run a query against Postgres via docker exec psql."""
    cmd = [
        "docker", "exec", POSTGRES_CONTAINER,
        "psql", "-U", "xyne", "-d", "xyne", "-t", "-A", "-F", "|", "-c", sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    lines = [line for line in result.stdout.strip().split("\n") if line]
    return lines


def query_vespa_search(yql):
    """Query Vespa search API."""
    url = VESPA_QUERY_URL + "?yql=" + urllib.parse.quote(yql)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_vespa_doc(doc_id):
    """Fetch metadata for a single document from Vespa search API (fast, no chunks)."""
    yql = (
        f'select docId, fileName, entity, document_id, document_date, '
        f'referenced_ids, pan_ids, entities_involved, ai_summary '
        f'from kb_items where docId contains "{doc_id}" limit 1'
    )
    data = query_vespa_search(yql)
    children = data.get("root", {}).get("children", [])
    if children:
        return {"fields": children[0].get("fields", {})}
    return {"error": "not found", "fields": {}}


def normalize_filename(name):
    """Normalize a filename for fuzzy matching."""
    # Lowercase, remove common prefixes/suffixes, collapse whitespace
    import re
    s = name.lower().strip()
    s = re.sub(r"\.(pdf|PDF)$", "", s)
    s = re.sub(r"[\s_]+", " ", s)
    s = re.sub(r"[^\w\s/-]", "", s)
    return s.strip()


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("THREE-WAY JOIN VALIDATION")
    print("scrape_state.db  ↔  Postgres collection_items  ↔  Vespa kb_items")
    print("=" * 80)
    print()

    # ── Step 1: Pick 10 random resolved documents from scrape_state.db ──
    docs = query_scrape_db(
        SCRAPE_DB,
        "SELECT id, top_section, section, subsection, date_iso, title, detail_url, status "
        "FROM documents WHERE status = 'resolved' ORDER BY RANDOM() LIMIT ?",
        (SAMPLE_SIZE,),
    )
    print(f"[1] Sampled {len(docs)} documents from scrape_state.db (status=resolved)")
    print()

    results = []

    for doc in docs:
        doc_id = doc["id"]
        title = doc["title"]
        print(f"─" * 80)
        print(f"Document #{doc_id}: {title[:80]}")
        print(f"  Taxonomy: {doc['top_section']} → {doc['section']} → {doc['subsection']}")
        print(f"  Date: {doc['date_iso']}")

        # ── Step 2: Get attachments for this doc from scrape_state.db ──
        attachments = query_scrape_db(
            SCRAPE_DB,
            "SELECT idx, label, original_filename, local_path, sha256, size_bytes, status "
            "FROM attachments WHERE doc_id = ? ORDER BY idx",
            (doc_id,),
        )
        print(f"  [scrape_state.db] Attachments: {len(attachments)}")
        for att in attachments:
            print(f"    #{att['idx']}: {att['original_filename'][:70]}")
            print(f"       local_path: {att['local_path'][:70] if att['local_path'] else 'NULL'}")

        # ── Step 3: Try to find matching collection_items in Postgres ──
        # Strategy: match by local_path basename → collection_items.name
        # The scrape_state.db stores SEBI's original filename (e.g., "1716959953149.pdf")
        # but the local_path contains the descriptive filename (e.g., "2024-05-29_kronox-lab-sciences-limited-rhp_81548.pdf")
        # which is what Postgres collection_items.name stores.
        join_results = []
        for att in attachments:
            local_path = att["local_path"] or ""
            fname = os.path.basename(local_path) if local_path else att["original_filename"]
            if not fname:
                continue

            # Try exact name match on the local_path basename
            escaped = fname.replace("'", "''")
            pg_rows = query_postgres(
                f"SELECT id, name, vespa_doc_id, collection_id, parent_id, storage_path "
                f"FROM collection_items WHERE name = '{escaped}' AND type = 'file' LIMIT 1"
            )

            if pg_rows:
                parts = pg_rows[0].split("|")
                ci_id, ci_name, vespa_doc_id, coll_id, parent_id, storage_path = parts
                join_results.append({
                    "attachment_idx": att["idx"],
                    "scrape_local_path": local_path,
                    "scrape_basename": fname,
                    "pg_collection_item_id": ci_id,
                    "pg_vespa_doc_id": vespa_doc_id,
                    "pg_collection_id": coll_id,
                    "pg_parent_id": parent_id,
                    "pg_storage_path": storage_path,
                    "join_method": "exact_basename",
                })
                print(f"  [Postgres] ✓ Match (exact basename): vespa_doc_id={vespa_doc_id}")
            else:
                # Fallback: try prefix LIKE on the first 30 chars of the basename
                like_pattern = fname[:40].replace("'", "''").replace("%", "\\%").replace("_", "\\_")
                pg_rows = query_postgres(
                    f"SELECT id, name, vespa_doc_id, collection_id, parent_id, storage_path "
                    f"FROM collection_items WHERE name LIKE '{like_pattern}%' AND type = 'file' LIMIT 1"
                )
                if pg_rows:
                    parts = pg_rows[0].split("|")
                    ci_id, ci_name, vespa_doc_id, coll_id, parent_id, storage_path = parts
                    join_results.append({
                        "attachment_idx": att["idx"],
                        "scrape_local_path": local_path,
                        "scrape_basename": fname,
                        "pg_collection_item_id": ci_id,
                        "pg_vespa_doc_id": vespa_doc_id,
                        "pg_collection_id": coll_id,
                        "pg_parent_id": parent_id,
                        "pg_storage_path": storage_path,
                        "join_method": "prefix_like",
                    })
                    print(f"  [Postgres] ✓ Match (prefix LIKE): vespa_doc_id={vespa_doc_id}")
                else:
                    join_results.append({
                        "attachment_idx": att["idx"],
                        "scrape_local_path": local_path,
                        "scrape_basename": fname,
                        "pg_collection_item_id": None,
                        "pg_vespa_doc_id": None,
                        "join_method": "NO_MATCH",
                    })
                    print(f"  [Postgres] ✗ No match for basename: {fname[:70]}")

        # ── Step 4: For each matched vespa_doc_id, fetch from Vespa ──
        vespa_results = []
        for jr in join_results:
            if not jr.get("pg_vespa_doc_id"):
                vespa_results.append({**jr, "vespa_doc": None})
                continue

            vespa_doc_id = jr["pg_vespa_doc_id"]
            vespa_data = fetch_vespa_doc(vespa_doc_id)

            if "error" in vespa_data:
                print(f"  [Vespa] ✗ Error fetching {vespa_doc_id}: {vespa_data['error']}")
                vespa_results.append({**jr, "vespa_doc": None, "vespa_error": vespa_data["error"]})
            else:
                fields = vespa_data.get("fields", {})
                vespa_results.append({
                    **jr,
                    "vespa_doc": {
                        "docId": fields.get("docId"),
                        "fileName": fields.get("fileName", "")[:80],
                        "entity": fields.get("entity"),
                        "document_id": fields.get("document_id"),
                        "document_date": fields.get("document_date"),
                        "referenced_ids": fields.get("referenced_ids"),
                        "pan_ids": fields.get("pan_ids"),
                        "entities_involved": fields.get("entities_involved"),
                        "ai_summary": bool(fields.get("ai_summary")),
                        "field_count": len(fields),
                    },
                })
                print(f"  [Vespa] ✓ Found: docId={fields.get('docId','?')[:40]}")
                print(f"    document_id={fields.get('document_id')}, referenced_ids={fields.get('referenced_ids')}")
                print(f"    document_date={fields.get('document_date')}, pan_ids={fields.get('pan_ids')}")
                ei = fields.get("entities_involved")
                ais = fields.get("ai_summary")
                print(f"    entities_involved={'POPULATED' if ei else 'EMPTY'}, ai_summary={'POPULATED' if ais else 'EMPTY'}")

        results.append({
            "scrape_doc_id": doc_id,
            "title": title,
            "taxonomy": f"{doc['top_section']} → {doc['section']} → {doc['subsection']}",
            "date": doc["date_iso"],
            "attachment_count": len(attachments),
            "joins": vespa_results,
        })
        print()

    # ── Summary ──
    print("=" * 80)
    print("JOIN SUMMARY")
    print("=" * 80)

    total_attachments = sum(r["attachment_count"] for r in results)
    total_joins = sum(len(r["joins"]) for r in results)
    successful_pg = sum(1 for r in results for j in r["joins"] if j.get("pg_vespa_doc_id"))
    successful_vespa = sum(1 for r in results for j in r["joins"] if j.get("vespa_doc"))
    no_match = sum(1 for r in results for j in r["joins"] if j.get("join_method") == "NO_MATCH")

    print(f"Documents sampled:      {len(results)}")
    print(f"Total attachments:      {total_attachments}")
    print(f"Postgres matches:       {successful_pg}/{total_joins} ({100*successful_pg/max(total_joins,1):.0f}%)")
    print(f"Vespa fetches:          {successful_vespa}/{total_joins} ({100*successful_vespa/max(total_joins,1):.0f}%)")
    print(f"No match in Postgres:   {no_match}")
    print()

    # Join method breakdown
    methods = {}
    for r in results:
        for j in r["joins"]:
            m = j.get("join_method", "UNKNOWN")
            methods[m] = methods.get(m, 0) + 1
    print("Join method breakdown:")
    for m, count in sorted(methods.items()):
        print(f"  {m}: {count}")
    print()

    # Vespa metadata field coverage in this sample
    if successful_vespa > 0:
        has_doc_id = sum(1 for r in results for j in r["joins"]
                        if j.get("vespa_doc") and j["vespa_doc"].get("document_id"))
        has_ref_ids = sum(1 for r in results for j in r["joins"]
                         if j.get("vespa_doc") and j["vespa_doc"].get("referenced_ids"))
        has_date = sum(1 for r in results for j in r["joins"]
                      if j.get("vespa_doc") and j["vespa_doc"].get("document_date"))
        has_pan = sum(1 for r in results for j in r["joins"]
                     if j.get("vespa_doc") and j["vespa_doc"].get("pan_ids"))
        has_ent = sum(1 for r in results for j in r["joins"]
                     if j.get("vespa_doc") and j["vespa_doc"].get("entities_involved"))
        has_ai = sum(1 for r in results for j in r["joins"]
                    if j.get("vespa_doc") and j["vespa_doc"].get("ai_summary"))

        print(f"Vespa metadata coverage (in {successful_vespa} matched docs):")
        print(f"  document_id:      {has_doc_id}/{successful_vespa}")
        print(f"  referenced_ids:   {has_ref_ids}/{successful_vespa}")
        print(f"  document_date:    {has_date}/{successful_vespa}")
        print(f"  pan_ids:          {has_pan}/{successful_vespa}")
        print(f"  entities_involved:{has_ent}/{successful_vespa}")
        print(f"  ai_summary:       {has_ai}/{successful_vespa}")

    print()
    print("VERDICT: ", end="")
    if successful_pg >= total_joins * 0.8 and successful_vespa >= total_joins * 0.8:
        print("✅ Three-way join is viable. Proceed to Phase 2.")
    elif successful_pg >= total_joins * 0.5:
        print("⚠️  Partial join success. Need fallback join strategy for unmatched records.")
    else:
        print("❌ Join failed. Need to investigate join keys before proceeding.")
    print()

    # Save detailed results to JSON
    output_path = os.path.join(SCRIPT_DIR, "three_way_join_report.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Detailed report saved to: {output_path}")


if __name__ == "__main__":
    main()
