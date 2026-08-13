#!/usr/bin/env python3
"""
Phase 2: Build the Document Backbone from scrape_state.db
=========================================================
Reads all documents + attachments from scrape_state.db, cross-joins with
Postgres collection_items to attach vespa_doc_id / clId / clFd, and emits
Document nodes, Attachment nodes, TaxonomyNode hierarchy, and edges.

Outputs:
  - scripts/metadata-graph/graph/documents.jsonl   (Document nodes)
  - scripts/metadata-graph/graph/attachments.jsonl  (Attachment nodes)
  - scripts/metadata-graph/graph/taxonomy.jsonl     (TaxonomyNode nodes)
  - scripts/metadata-graph/graph/edges.jsonl        (all edges)
  - scripts/metadata-graph/output/logs/phase2_report.json (build report)

Usage:
  python3 scripts/metadata-graph/build_document_backbone.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
SCRAPE_DB = os.path.join(PROJECT_ROOT, "SEBI-14K-share/source-files/files/scrape_state.db")
GRAPH_DIR = os.path.join(SCRIPT_DIR, "graph")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
LOG_DIR = os.path.join(OUTPUT_DIR, "logs")
POSTGRES_CONTAINER = (
    os.environ.get("METADATA_KG_POSTGRES_CONTAINER")
    or os.environ.get("XYNE_POSTGRES_CONTAINER")
    or "metadata-kg-xyne-db"
)


def query_scrape_db(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def query_postgres_bulk(sql):
    cmd = [
        "docker", "exec", POSTGRES_CONTAINER,
        "psql", "-U", "xyne", "-d", "xyne", "-t", "-A", "-F", "\t", "-c", sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"Postgres query error: {result.stderr}", file=sys.stderr)
        return []
    lines = [line for line in result.stdout.strip().split("\n") if line]
    return lines


def build_pg_name_index():
    pg_rows = query_postgres_bulk(
        "SELECT name, id, vespa_doc_id, collection_id, parent_id, storage_path "
        "FROM collection_items WHERE type = 'file' AND name IS NOT NULL"
    )
    index = {}
    for line in pg_rows:
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        name, ci_id, vespa_doc_id, coll_id, parent_id, storage_path = parts[:6]
        index[name] = {
            "collection_item_id": ci_id,
            "vespa_doc_id": vespa_doc_id,
            "collection_id": coll_id,
            "parent_id": parent_id if parent_id else None,
            "storage_path": storage_path,
        }
    return index


def build_taxonomy_nodes(documents):
    taxonomy = {}
    node_id_counter = 0

    for doc in documents:
        top = doc["top_section"]
        section = doc["section"]
        subsection = doc["subsection"]

        top_key = f"ts::{top}"
        if top_key not in taxonomy:
            taxonomy[top_key] = {
                "id": f"tax_{node_id_counter}",
                "kind": "top_section",
                "name": top,
                "parentId": None,
            }
            node_id_counter += 1

        section_key = f"sec::{top}::{section}"
        if section_key not in taxonomy:
            taxonomy[section_key] = {
                "id": f"tax_{node_id_counter}",
                "kind": "section",
                "name": section,
                "parentId": taxonomy[top_key]["id"],
            }
            node_id_counter += 1

        if subsection:
            sub_key = f"sub::{top}::{section}::{subsection}"
            if sub_key not in taxonomy:
                taxonomy[sub_key] = {
                    "id": f"tax_{node_id_counter}",
                    "kind": "subsection",
                    "name": subsection,
                    "parentId": taxonomy[section_key]["id"],
                }
                node_id_counter += 1

    return list(taxonomy.values()), taxonomy


def get_taxonomy_ref(doc, taxonomy):
    top_key = f"ts::{doc['top_section']}"
    section_key = f"sec::{doc['top_section']}::{doc['section']}"
    sub_key = f"sub::{doc['top_section']}::{doc['section']}::{doc['subsection']}"

    if doc["subsection"] and sub_key in taxonomy:
        return taxonomy[sub_key]["id"]
    elif section_key in taxonomy:
        return taxonomy[section_key]["id"]
    elif top_key in taxonomy:
        return taxonomy[top_key]["id"]
    return None


def main():
    os.makedirs(GRAPH_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    start_time = time.time()

    print("=" * 70)
    print("PHASE 2: Build Document Backbone from scrape_state.db")
    print("=" * 70)

    # ── Step 1: Read all documents from scrape_state.db ──
    docs = query_scrape_db(
        SCRAPE_DB,
        "SELECT id, top_section, section, subsection, sid, ssid, smid, "
        "date_iso, title, detail_url, status "
        "FROM documents WHERE status = 'resolved' ORDER BY id"
    )
    print(f"\n[1] Loaded {len(docs)} resolved documents from scrape_state.db")

    # ── Step 2: Read all attachments from scrape_state.db ──
    attachments = query_scrape_db(
        SCRAPE_DB,
        "SELECT id, doc_id, idx, label, original_filename, local_path, "
        "size_bytes, sha256, status "
        "FROM attachments WHERE status = 'downloaded' ORDER BY doc_id, idx"
    )
    print(f"[2] Loaded {len(attachments)} downloaded attachments from scrape_state.db")

    # ── Step 3: Build Postgres name → collection_item index ──
    print("[3] Loading Postgres collection_items...")
    pg_index = build_pg_name_index()
    print(f"    Indexed {len(pg_index)} Postgres file items by name")

    # ── Step 4: Build taxonomy nodes ──
    print("[4] Building taxonomy nodes...")
    taxonomy_nodes, taxonomy_map = build_taxonomy_nodes(docs)
    print(f"    Created {len(taxonomy_nodes)} taxonomy nodes")

    # ── Step 5: Build Document nodes + Attachment nodes + edges ──
    print("[5] Building Document and Attachment nodes...")

    attachments_by_doc = defaultdict(list)
    for att in attachments:
        attachments_by_doc[att["doc_id"]].append(att)

    documents_out = []
    attachments_out = []
    edges_out = []

    pg_matched = 0
    pg_unmatched = 0
    multi_attachment_docs = 0

    for doc in docs:
        doc_id = doc["id"]
        doc_attachments = attachments_by_doc.get(doc_id, [])

        if len(doc_attachments) > 1:
            multi_attachment_docs += 1

        # Try to find a Postgres match using the first attachment's local_path basename
        pg_match = None
        for att in doc_attachments:
            local_path = att["local_path"] or ""
            basename = os.path.basename(local_path) if local_path else None
            if basename and basename in pg_index:
                pg_match = pg_index[basename]
                break

        if pg_match:
            pg_matched += 1
        else:
            pg_unmatched += 1

        tax_ref = get_taxonomy_ref(doc, taxonomy_map)

        doc_node = {
            "type": "Document",
            "id": f"doc_{doc_id}",
            "scrapeDbId": doc_id,
            "title": doc["title"],
            "date": doc["date_iso"],
            "topSection": doc["top_section"],
            "section": doc["section"],
            "subsection": doc["subsection"],
            "sid": doc["sid"],
            "ssid": doc["ssid"],
            "smid": doc["smid"],
            "status": doc["status"],
            "detailUrl": doc["detail_url"],
            # Postgres join fields (None if unmatched)
            "vespaDocId": pg_match["vespa_doc_id"] if pg_match else None,
            "clId": pg_match["collection_id"] if pg_match else None,
            "clFd": pg_match["parent_id"] if pg_match else None,
            "collectionItemId": pg_match["collection_item_id"] if pg_match else None,
            "storagePath": pg_match["storage_path"] if pg_match else None,
            # Vespa fields (to be filled in Phase 3)
            "documentId": None,
            "referencedIds": None,
            "documentDate": None,
            "panIds": None,
        }
        documents_out.append(doc_node)

        # Taxonomy edge
        if tax_ref:
            edges_out.append({
                "type": "instance_of",
                "source": f"doc_{doc_id}",
                "target": tax_ref,
            })

        # Attachment nodes + edges
        for att in doc_attachments:
            att_node = {
                "type": "Attachment",
                "id": f"att_{att['id']}",
                "docId": doc_id,
                "idx": att["idx"],
                "label": att["label"],
                "originalFilename": att["original_filename"],
                "localPath": att["local_path"],
                "sha256": att["sha256"],
                "sizeBytes": att["size_bytes"],
            }
            attachments_out.append(att_node)

            edges_out.append({
                "type": "has_attachment",
                "source": f"doc_{doc_id}",
                "target": f"att_{att['id']}",
            })

    print(f"    Documents: {len(documents_out)}")
    print(f"    Attachments: {len(attachments_out)}")
    print(f"    Edges: {len(edges_out)}")
    print(f"    Postgres matched: {pg_matched} ({100*pg_matched/len(documents_out):.1f}%)")
    print(f"    Postgres unmatched: {pg_unmatched} ({100*pg_unmatched/len(documents_out):.1f}%)")
    print(f"    Multi-attachment docs: {multi_attachment_docs}")

    # ── Step 6: Write output files ──
    print("\n[6] Writing output files...")

    docs_path = os.path.join(GRAPH_DIR, "documents.jsonl")
    with open(docs_path, "w") as f:
        for doc in documents_out:
            f.write(json.dumps(doc) + "\n")
    print(f"    {docs_path} ({len(documents_out)} nodes)")

    att_path = os.path.join(GRAPH_DIR, "attachments.jsonl")
    with open(att_path, "w") as f:
        for att in attachments_out:
            f.write(json.dumps(att) + "\n")
    print(f"    {att_path} ({len(attachments_out)} nodes)")

    tax_path = os.path.join(GRAPH_DIR, "taxonomy.jsonl")
    with open(tax_path, "w") as f:
        for node in taxonomy_nodes:
            f.write(json.dumps(node) + "\n")
    print(f"    {tax_path} ({len(taxonomy_nodes)} nodes)")

    edges_path = os.path.join(GRAPH_DIR, "edges.jsonl")
    with open(edges_path, "w") as f:
        for edge in edges_out:
            f.write(json.dumps(edge) + "\n")
    print(f"    {edges_path} ({len(edges_out)} edges)")

    # ── Step 7: Build report ──
    taxonomy_by_kind = defaultdict(int)
    for node in taxonomy_nodes:
        taxonomy_by_kind[node["kind"]] += 1

    edge_types = defaultdict(int)
    for edge in edges_out:
        edge_types[edge["type"]] += 1

    section_dist = defaultdict(int)
    for doc in documents_out:
        section_dist[doc["topSection"]] += 1

    report = {
        "phase": "Phase 2: Document Backbone",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - start_time, 2),
        "counts": {
            "documents": len(documents_out),
            "attachments": len(attachments_out),
            "taxonomy_nodes": len(taxonomy_nodes),
            "edges": len(edges_out),
        },
        "taxonomy_by_kind": dict(taxonomy_by_kind),
        "edge_types": dict(edge_types),
        "section_distribution": dict(section_dist),
        "postgres_join": {
            "matched": pg_matched,
            "unmatched": pg_unmatched,
            "match_rate": round(100 * pg_matched / len(documents_out), 2),
        },
        "multi_attachment_docs": multi_attachment_docs,
        "next_phase": "Phase 3: Enrich from Vespa — read document_id, referenced_ids, document_date, pan_ids for each vespaDocId",
    }

    report_path = os.path.join(LOG_DIR, "phase2_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n    {report_path}")
    print(f"\n{'=' * 70}")
    print(f"PHASE 2 COMPLETE in {report['duration_seconds']}s")
    print(f"  {len(documents_out)} Document nodes")
    print(f"  {len(attachments_out)} Attachment nodes")
    print(f"  {len(taxonomy_nodes)} TaxonomyNode nodes")
    print(f"  {len(edges_out)} edges")
    print(f"  Postgres join rate: {report['postgres_join']['match_rate']}%")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
