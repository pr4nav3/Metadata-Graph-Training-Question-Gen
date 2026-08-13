#!/usr/bin/env python3
"""
Phase 3: Enrich Document Nodes from Vespa
=========================================
Visits all kb_items in Vespa, extracts the four populated metadata fields
(document_id, document_date, referenced_ids, pan_ids), updates the Document
nodes from Phase 2, and builds RegulationRef nodes + citation edges.

Outputs (updated in-place + new files):
  - graph/documents_enriched.jsonl  (Document nodes with Vespa metadata)
  - graph/regulation_refs.jsonl     (RegulationRef nodes)
  - graph/citation_edges.jsonl      (Document--cites-->RegulationRef + Document--cites-->Document)
  - output/logs/phase3_report.json        (build report)

Usage:
  python3 scripts/metadata-graph/enrich_from_vespa.py
"""

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
GRAPH_DIR = os.path.join(SCRIPT_DIR, "graph")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
LOG_DIR = os.path.join(OUTPUT_DIR, "logs")
DEFAULT_VESPA_FEED_URL = (
    os.environ.get("METADATA_KG_VESPA_FEED_URL")
    or os.environ.get("VESPA_FEED_URL")
    or "http://localhost:18080"
)
VESPA_VISIT_URL = (
    DEFAULT_VESPA_FEED_URL.rstrip("/")
    + "/document/v1/?cluster=my_content&namespace=namespace&documentType=kb_items"
    + "&wantedDocumentCount=400"
)

REG_REF_KINDS = [
    ("SEBI/HO", "sebi_ho"),
    ("CIR/", "circular"),
    ("WTM/", "warning"),
    ("AO/", "adjudication"),
    ("RC ", "recovery_certificate"),
    ("RC$", "recovery_certificate"),
    ("Recovery Certificate", "recovery_certificate"),
    ("Section ", "section_ref"),
    ("Order/", "order_ref"),
    ("QJA/", "order_ref"),
    ("NRO/", "order_ref"),
]


def classify_ref_id(ref_str):
    if not ref_str:
        return "other"
    upper = ref_str.strip()
    for prefix, kind in REG_REF_KINDS:
        if upper.startswith(prefix):
            return kind
    if "Act" in upper or "Regulations" in upper:
        return "act_or_regulation"
    return "other"


def main():
    os.makedirs(GRAPH_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    start_time = time.time()

    print("=" * 70)
    print("PHASE 3: Enrich Document Nodes from Vespa")
    print("=" * 70)

    # ── Step 1: Load Phase 2 Document nodes ──
    docs_path = os.path.join(GRAPH_DIR, "documents.jsonl")
    documents = {}
    with open(docs_path) as f:
        for line in f:
            doc = json.loads(line)
            documents[doc["id"]] = doc
    print(f"\n[1] Loaded {len(documents)} Document nodes from Phase 2")

    docs_with_vespa = {k: v for k, v in documents.items() if v.get("vespaDocId")}
    print(f"    {len(docs_with_vespa)} have vespaDocId (eligible for enrichment)")

    # ── Step 2: Visit Vespa and build docId → metadata map ──
    print("\n[2] Visiting Vespa kb_items...")
    vespa_metadata = {}
    cont = None
    batches = 0

    while True:
        url = VESPA_VISIT_URL
        if cont:
            url += f"&continuation={cont}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        docs = data.get("documents", [])
        for doc in docs:
            fields = doc.get("fields", {})
            doc_id = fields.get("docId")
            if not doc_id:
                continue
            vespa_metadata[doc_id] = {
                "document_id": fields.get("document_id"),
                "document_date": fields.get("document_date"),
                "referenced_ids": fields.get("referenced_ids"),
                "pan_ids": fields.get("pan_ids"),
            }

        batches += 1
        cont = data.get("continuation")
        if not cont or not docs:
            break
        if batches % 10 == 0:
            print(f"    Batch {batches}: {len(vespa_metadata)} docs visited ({time.time()-start_time:.1f}s)")

    print(f"    Visited {len(vespa_metadata)} Vespa docs in {batches} batches ({time.time()-start_time:.1f}s)")

    # ── Step 3: Update Document nodes with Vespa metadata ──
    print("\n[3] Enriching Document nodes...")
    enriched = 0
    vespa_miss = 0
    field_coverage = defaultdict(int)

    for doc_id, doc in documents.items():
        vespa_id = doc.get("vespaDocId")
        if not vespa_id:
            continue

        meta = vespa_metadata.get(vespa_id)
        if not meta:
            vespa_miss += 1
            continue

        enriched += 1
        doc["documentId"] = meta["document_id"]
        doc["documentDate"] = meta["document_date"]
        doc["referencedIds"] = meta["referenced_ids"]
        doc["panIds"] = meta["pan_ids"]

        if meta["document_id"]:
            field_coverage["document_id"] += 1
        if meta["document_date"]:
            field_coverage["document_date"] += 1
        if meta["referenced_ids"]:
            field_coverage["referenced_ids"] += 1
        if meta["pan_ids"]:
            field_coverage["pan_ids"] += 1

    print(f"    Enriched: {enriched}")
    print(f"    Vespa miss (docId not found): {vespa_miss}")
    print(f"    Field coverage (of enriched):")
    for field in ["document_id", "document_date", "referenced_ids", "pan_ids"]:
        count = field_coverage[field]
        print(f"      {field}: {count}/{enriched} ({100*count/enriched:.1f}%)")

    # ── Step 4: Build RegulationRef nodes + citation edges ──
    print("\n[4] Building RegulationRef nodes and citation edges...")
    reg_refs = {}
    reg_ref_edges = []
    doc_to_doc_edges = []

    # Build a map of document_id → Document node for inter-doc citation
    doc_id_to_node = {}
    for doc in documents.values():
        did = doc.get("documentId")
        if did:
            doc_id_to_node[str(did)] = doc["id"]

    for doc in documents.values():
        doc_node_id = doc["id"]
        refs = doc.get("referencedIds")
        if not refs:
            continue

        for ref in refs:
            if not ref or not ref.strip():
                continue
            ref = ref.strip()

            # Create or get RegulationRef node
            ref_key = f"ref::{ref}"
            if ref_key not in reg_refs:
                reg_refs[ref_key] = {
                    "type": "RegulationRef",
                    "id": f"regref_{len(reg_refs)}",
                    "canonicalId": ref,
                    "kind": classify_ref_id(ref),
                }

            # Document --cites--> RegulationRef
            reg_ref_edges.append({
                "type": "cites",
                "source": doc_node_id,
                "target": reg_refs[ref_key]["id"],
            })

            # Check if this ref matches another Document's document_id
            ref_str = str(ref)
            if ref_str in doc_id_to_node:
                target_doc = doc_id_to_node[ref_str]
                if target_doc != doc_node_id:
                    doc_to_doc_edges.append({
                        "type": "cites_document",
                        "source": doc_node_id,
                        "target": target_doc,
                    })

    print(f"    RegulationRef nodes: {len(reg_refs)}")
    print(f"    Document--cites-->RegulationRef edges: {len(reg_ref_edges)}")
    print(f"    Document--cites-->Document edges: {len(doc_to_doc_edges)}")

    # ── Step 5: Write output files ──
    print("\n[5] Writing output files...")

    enriched_path = os.path.join(GRAPH_DIR, "documents_enriched.jsonl")
    with open(enriched_path, "w") as f:
        for doc in documents.values():
            f.write(json.dumps(doc) + "\n")
    print(f"    {enriched_path} ({len(documents)} nodes)")

    regref_path = os.path.join(GRAPH_DIR, "regulation_refs.jsonl")
    with open(regref_path, "w") as f:
        for ref in reg_refs.values():
            f.write(json.dumps(ref) + "\n")
    print(f"    {regref_path} ({len(reg_refs)} nodes)")

    citation_path = os.path.join(GRAPH_DIR, "citation_edges.jsonl")
    with open(citation_path, "w") as f:
        for edge in reg_ref_edges + doc_to_doc_edges:
            f.write(json.dumps(edge) + "\n")
    print(f"    {citation_path} ({len(reg_ref_edges) + len(doc_to_doc_edges)} edges)")

    # ── Step 6: Build report ──
    reg_kind_dist = defaultdict(int)
    for ref in reg_refs.values():
        reg_kind_dist[ref["kind"]] += 1

    report = {
        "phase": "Phase 3: Enrich from Vespa",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - start_time, 2),
        "vespa_visit": {
            "total_docs_visited": len(vespa_metadata),
            "batches": batches,
        },
        "enrichment": {
            "documents_eligible": len(docs_with_vespa),
            "documents_enriched": enriched,
            "vespa_miss": vespa_miss,
            "field_coverage": {
                field: {
                    "count": field_coverage[field],
                    "percentage": round(100 * field_coverage[field] / max(enriched, 1), 1),
                }
                for field in ["document_id", "document_date", "referenced_ids", "pan_ids"]
            },
        },
        "regulation_refs": {
            "total_nodes": len(reg_refs),
            "by_kind": dict(reg_kind_dist),
        },
        "citation_edges": {
            "doc_to_regref": len(reg_ref_edges),
            "doc_to_doc": len(doc_to_doc_edges),
            "total": len(reg_ref_edges) + len(doc_to_doc_edges),
        },
        "next_phase": "Phase 4: Build Entity Nodes from title regex + pan_ids",
    }

    report_path = os.path.join(LOG_DIR, "phase3_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n    {report_path}")

    print(f"\n{'=' * 70}")
    print(f"PHASE 3 COMPLETE in {report['duration_seconds']}s")
    print(f"  {enriched} documents enriched with Vespa metadata")
    print(f"  {len(reg_refs)} RegulationRef nodes")
    print(f"  {len(reg_ref_edges)} Document→RegulationRef citation edges")
    print(f"  {len(doc_to_doc_edges)} Document→Document citation edges")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
