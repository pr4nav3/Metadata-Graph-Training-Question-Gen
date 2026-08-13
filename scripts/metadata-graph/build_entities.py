#!/usr/bin/env python3
"""
Phase 4: Build Entity Nodes
============================
Two-tier entity extraction:
  1. Regex tier: Extract entities from document titles using pattern matching (88.8% coverage, instant)
  2. LLM tier: Extract entities from titles that regex missed (1,451 docs, batched via self-hosted LLM)

Merges with Vespa pan_ids (6,977 docs). Canonicalizes entity names and builds:
  - CanonicalEntity nodes
  - Document --mentions--> CanonicalEntity edges

Outputs:
  - graph/entities.jsonl          (CanonicalEntity nodes)
  - graph/entity_edges.jsonl       (Document--mentions-->CanonicalEntity edges)
- output/logs/entity_canonicalization_log.json  (raw → canonical mapping)
- output/logs/phase4_report.json       (build report)

Usage:
  python3 scripts/metadata-graph/build_entities.py
"""

import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
GRAPH_DIR = os.path.join(SCRIPT_DIR, "graph")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
LOG_DIR = os.path.join(OUTPUT_DIR, "logs")

LLM_ENDPOINT = os.environ.get(
    "METADATA_GRAPH_LLM_ENDPOINT",
    "https://grid.ai.juspay.net/v1/chat/completions",
)
LLM_API_KEY = (
    os.environ.get("METADATA_GRAPH_LLM_API_KEY")
    or os.environ.get("JUSPAY_API_KEY")
    or os.environ.get("LITELLM_API_KEY")
)
LLM_MODEL = os.environ.get("METADATA_GRAPH_LLM_MODEL", "glm-private")
LLM_BATCH_SIZE = 10
LLM_MAX_TOKENS = 8000
LLM_TIMEOUT = 120
LLM_PARALLEL_WORKERS = 8

PAN_RE = re.compile(r'PAN[:\s-]*([A-Z]{5}\d{4}[A-Z])')
RC_RE = re.compile(r'(?:RC\s*(?:No\.?)?|Recovery Certificate\s*(?:No\.?)?)\s*(\d+)\s*of\s*(\d{4})', re.I)
COMPANY_RE = re.compile(r'in the matter of\s+(.+?)(?:\s*[-–]\s*|\s*under\s*|\s*\[|\s*\(|\s*,\s*PAN)', re.I)
PERSON_RE = re.compile(r'against\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', re.I)
APPEAL_RE = re.compile(r'Appeal No\.\s*(\d+)\s*of\s*(\d{4})\s*filed by\s+(.+?)(?:\s*$)', re.I)
RECEIVED_FROM_RE = re.compile(
    r'(?:received from|application received from)\s+(.+?)'
    r'(?:\s+with\s+respect|\s+in\s+relation|\s+seeking|\s+under\s+|\s+for\s+|\s*$)',
    re.I,
)
SHOW_CAUSE_RE = re.compile(
    r'(?:Show Cause Notice|Notice)\s+dated\s+\d[\d./-]+\s+issued to\s+(?:Mr\.?\s+|Ms\.?\s+|Mrs\.?\s+|Shri\s+)?(.+?)(?:\s*$)',
    re.I,
)
COMPANY_SUFFIX_RE = re.compile(
    r'([A-Z][A-Za-z0-9&.\s]+?\s+(?:Limited|Ltd|Pvt\.?\s*Ltd|Private Limited|Inc\.?|Corporation|Corp\.?))',
    re.I,
)


def canonicalize_name(name):
    s = name.strip()
    s = re.sub(r'\s+', ' ', s)
    s = s.rstrip('.,;:')
    s = s.replace('Pvt. Ltd.', 'Pvt Ltd')
    s = s.replace('Pvt.Ltd.', 'Pvt Ltd')
    lower = s.lower()
    if lower in ('sebi', 'the sebi', 'security and exchange board of india',
                 'securities and exchange board of india'):
        return 'SEBI'
    return s


def extract_regex_entities(title, top_section, section):
    entities = []

    if PAN_RE.search(title):
        for pan in PAN_RE.findall(title):
            entities.append({"name": pan, "kind": "pan", "raw": pan})

    if RC_RE.search(title):
        for rc_num, rc_year in RC_RE.findall(title):
            entities.append({
                "name": f"RC {rc_num} of {rc_year}",
                "kind": "recovery_certificate",
                "raw": f"Recovery Certificate No. {rc_num} of {rc_year}",
            })

    m = COMPANY_RE.search(title)
    if m:
        raw = m.group(1).strip().rstrip('.,;:')
        if len(raw) > 3:
            entities.append({"name": canonicalize_name(raw), "kind": "company", "raw": raw})

    m = PERSON_RE.search(title)
    if m:
        raw = m.group(1).strip()
        if len(raw) > 3 and raw.lower() not in ('the', 'a', 'an'):
            entities.append({"name": raw, "kind": "person", "raw": raw})

    m = APPEAL_RE.search(title)
    if m:
        raw = m.group(3).strip()
        if len(raw) > 3:
            entities.append({"name": raw, "kind": "person", "raw": raw})

    m = RECEIVED_FROM_RE.search(title)
    if m:
        raw = m.group(1).strip().rstrip('.,;:')
        if len(raw) > 3:
            kind = "company" if re.search(r'(?:Limited|Ltd|Pvt|Private)', raw, re.I) else "company"
            entities.append({"name": canonicalize_name(raw), "kind": kind, "raw": raw})

    m = SHOW_CAUSE_RE.search(title)
    if m:
        raw = m.group(1).strip()
        if len(raw) > 3:
            entities.append({"name": raw, "kind": "person", "raw": raw})

    if top_section == "Filings":
        for m in COMPANY_SUFFIX_RE.finditer(title):
            raw = m.group(1).strip()
            if len(raw) > 3:
                entities.append({"name": canonicalize_name(raw), "kind": "company", "raw": raw})
                break
        else:
            if len(title) > 3 and not title.startswith("20"):
                entities.append({"name": canonicalize_name(title), "kind": "company", "raw": title})

    if top_section == "Legal" and "Regulations" in section:
        if len(title) > 5:
            entities.append({"name": canonicalize_name(title), "kind": "regulation", "raw": title})

    for m in COMPANY_SUFFIX_RE.finditer(title):
        raw = m.group(1).strip()
        if len(raw) > 5:
            entities.append({"name": canonicalize_name(raw), "kind": "company", "raw": raw})

    seen = set()
    deduped = []
    for e in entities:
        key = (e["name"].lower(), e["kind"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    return deduped


def extract_llm_entities(titles):
    if not titles:
        return {}
    if not LLM_API_KEY:
        raise RuntimeError(
            "Missing LLM credential: set METADATA_GRAPH_LLM_API_KEY, "
            "JUSPAY_API_KEY, or LITELLM_API_KEY"
        )
    prompt = (
        "Extract named entities from each SEBI document title below. "
        "Extract: companies, persons, organizations, regulatory bodies. "
        "Skip generic terms like 'SEBI', 'Annual Report', 'Master Circular', "
        "'Consultation Paper', 'Notice', 'Order'. "
        "Return ONLY a JSON object (no markdown fences) mapping title index "
        'to entity array. Empty array if no entities found.\n\n'
        '{"0": [{"name": "...", "kind": "company|person|regulator|other"}], ...}\n\n'
        "Titles:\n"
    )
    for i, t in enumerate(titles):
        prompt += f"{i}: {t}\n"

    data = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": LLM_MAX_TOKENS,
    }).encode()

    req = urllib.request.Request(
        LLM_ENDPOINT,
        data=data,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
        content = result["choices"][0]["message"].get("content", "")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)
        return json.loads(content)
    except Exception as e:
        print(f"    LLM error: {e}", file=sys.stderr)
        return {}


def main():
    os.makedirs(GRAPH_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    start_time = time.time()

    print("=" * 70)
    print("PHASE 4: Build Entity Nodes")
    print("=" * 70)

    # ── Step 1: Load enriched documents ──
    docs_path = os.path.join(GRAPH_DIR, "documents_enriched.jsonl")
    documents = []
    with open(docs_path) as f:
        for line in f:
            documents.append(json.loads(line))
    print(f"\n[1] Loaded {len(documents)} enriched Document nodes")

    # ── Step 2: Regex extraction on all titles ──
    print("\n[2] Running regex entity extraction on all titles...")
    regex_entities_by_doc = {}
    no_match_docs = []

    for doc in documents:
        title = doc["title"]
        ts = doc["topSection"]
        sec = doc["section"]
        entities = extract_regex_entities(title, ts, sec)

        if entities:
            regex_entities_by_doc[doc["id"]] = entities
        else:
            no_match_docs.append(doc)

    print(f"    Regex matched: {len(regex_entities_by_doc)} docs ({100*len(regex_entities_by_doc)/len(documents):.1f}%)")
    print(f"    No regex match: {len(no_match_docs)} docs (will try LLM)")

    # ── Step 3: LLM extraction on no-match titles (parallel batches) ──
    print(f"\n[3] Running LLM entity extraction on {len(no_match_docs)} no-match docs...")
    llm_entities_by_doc = {}
    llm_batches = (len(no_match_docs) + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE

    def process_llm_batch(batch_idx, batch_docs):
        titles = [d["title"] for d in batch_docs]
        result = extract_llm_entities(titles)
        batch_entities = {}
        for j, doc in enumerate(batch_docs):
            key = str(j)
            if key in result and result[key]:
                batch_entities[doc["id"]] = result[key]
        return batch_idx, batch_entities

    batches = []
    for i in range(0, len(no_match_docs), LLM_BATCH_SIZE):
        batches.append((i // LLM_BATCH_SIZE, no_match_docs[i:i + LLM_BATCH_SIZE]))

    completed = 0
    with ThreadPoolExecutor(max_workers=LLM_PARALLEL_WORKERS) as executor:
        futures = {executor.submit(process_llm_batch, idx, batch): idx for idx, batch in batches}
        for future in as_completed(futures):
            batch_idx, batch_entities = future.result()
            llm_entities_by_doc.update(batch_entities)
            completed += 1
            if completed % 20 == 0 or completed == llm_batches:
                print(f"    Completed {completed}/{llm_batches} batches: {len(llm_entities_by_doc)} entities ({time.time()-start_time:.1f}s)")

    print(f"    LLM matched: {len(llm_entities_by_doc)} docs")
    print(f"    Still no entities: {len(no_match_docs) - len(llm_entities_by_doc)} docs")

    # ── Step 4: Merge pan_ids from Vespa ──
    print("\n[4] Merging pan_ids from Vespa...")
    pan_count = 0
    for doc in documents:
        pan_ids = doc.get("panIds")
        if not pan_ids:
            continue
        doc_id = doc["id"]
        existing = regex_entities_by_doc.get(doc_id, []) + llm_entities_by_doc.get(doc_id, [])

        for pan in pan_ids:
            existing.append({"name": pan, "kind": "pan", "raw": pan})
            pan_count += 1

        all_entities = {e["name"].lower(): e for e in existing}
        all_entities = list(all_entities.values())
        if doc_id in regex_entities_by_doc:
            regex_entities_by_doc[doc_id] = all_entities
        else:
            regex_entities_by_doc[doc_id] = all_entities

    print(f"    Added {pan_count} PAN entities from Vespa")

    # ── Step 5: Canonicalize entities and build nodes ──
    print("\n[5] Canonicalizing entities and building nodes...")
    entity_registry = {}
    entity_log = {}
    mention_edges = []
    entity_id_counter = 0

    for doc in documents:
        doc_id = doc["id"]
        entities = regex_entities_by_doc.get(doc_id, [])
        if not entities:
            continue

        for e in entities:
            raw_name = e.get("raw", e.get("name", ""))
            canonical = canonicalize_name(e["name"])
            if len(canonical) < 2:
                continue

            log_key = raw_name.lower() if raw_name else canonical.lower()
            if log_key not in entity_log:
                entity_log[log_key] = canonical

            canon_key = canonical.lower()
            if canon_key not in entity_registry:
                entity_registry[canon_key] = {
                    "type": "CanonicalEntity",
                    "id": f"ent_{entity_id_counter}",
                    "name": canonical,
                    "kind": e["kind"],
                    "aliases": set(),
                }
                entity_id_counter += 1

            if raw_name and raw_name != canonical and raw_name.lower() != canon_key:
                entity_registry[canon_key]["aliases"].add(raw_name)

            mention_edges.append({
                "type": "mentions",
                "source": doc_id,
                "target": entity_registry[canon_key]["id"],
            })

    entity_nodes = []
    for ent in entity_registry.values():
        node = dict(ent)
        node["aliases"] = sorted(list(node["aliases"])) if node["aliases"] else []
        entity_nodes.append(node)

    print(f"    CanonicalEntity nodes: {len(entity_nodes)}")
    print(f"    Document--mentions-->Entity edges: {len(mention_edges)}")

    docs_with_entities = set()
    for edge in mention_edges:
        docs_with_entities.add(edge["source"])
    print(f"    Docs with at least one entity: {len(docs_with_entities)}/{len(documents)} ({100*len(docs_with_entities)/len(documents):.1f}%)")

    # ── Step 6: Build co-mention edges ──
    print("\n[6] Building co-mention edges...")
    doc_entities = defaultdict(list)
    for edge in mention_edges:
        doc_entities[edge["source"]].append(edge["target"])

    co_mention_edges = []
    co_mention_seen = set()
    for doc_id, ent_ids in doc_entities.items():
        if len(ent_ids) < 2:
            continue
        for i in range(len(ent_ids)):
            for j in range(i + 1, len(ent_ids)):
                pair = tuple(sorted([ent_ids[i], ent_ids[j]]))
                if pair not in co_mention_seen:
                    co_mention_seen.add(pair)
                    co_mention_edges.append({
                        "type": "co_mentioned_with",
                        "source": ent_ids[i],
                        "target": ent_ids[j],
                        "weight": 1,
                    })

    print(f"    Co-mention edges: {len(co_mention_edges)}")

    # ── Step 7: Write output files ──
    print("\n[7] Writing output files...")

    ent_path = os.path.join(GRAPH_DIR, "entities.jsonl")
    with open(ent_path, "w") as f:
        for node in entity_nodes:
            f.write(json.dumps(node) + "\n")
    print(f"    {ent_path} ({len(entity_nodes)} nodes)")

    edge_path = os.path.join(GRAPH_DIR, "entity_edges.jsonl")
    with open(edge_path, "w") as f:
        for edge in mention_edges + co_mention_edges:
            f.write(json.dumps(edge) + "\n")
    print(f"    {edge_path} ({len(mention_edges) + len(co_mention_edges)} edges)")

    log_path = os.path.join(LOG_DIR, "entity_canonicalization_log.json")
    with open(log_path, "w") as f:
        json.dump(entity_log, f, indent=2)
    print(f"    {log_path} ({len(entity_log)} mappings)")

    # ── Step 8: Build report ──
    kind_dist = defaultdict(int)
    for ent in entity_nodes:
        kind_dist[ent["kind"]] += 1

    report = {
        "phase": "Phase 4: Build Entity Nodes",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - start_time, 2),
        "extraction": {
            "regex_matched": len(regex_entities_by_doc) - len(llm_entities_by_doc),
            "llm_matched": len(llm_entities_by_doc),
            "pan_ids_merged": pan_count,
            "no_entities": len(documents) - len(docs_with_entities),
        },
        "entities": {
            "total_nodes": len(entity_nodes),
            "by_kind": dict(kind_dist),
            "docs_with_entities": len(docs_with_entities),
            "coverage_percentage": round(100 * len(docs_with_entities) / len(documents), 1),
        },
        "edges": {
            "mentions": len(mention_edges),
            "co_mentions": len(co_mention_edges),
        },
        "llm_config": {
            "model": LLM_MODEL,
            "batch_size": LLM_BATCH_SIZE,
            "batches": llm_batches,
        },
        "next_phase": "Phase 5: Derive Relationship Edges (folder hierarchy, topic tags)",
    }

    report_path = os.path.join(LOG_DIR, "phase4_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n    {report_path}")

    print(f"\n{'=' * 70}")
    print(f"PHASE 4 COMPLETE in {report['duration_seconds']}s")
    print(f"  {len(entity_nodes)} CanonicalEntity nodes")
    print(f"  {len(mention_edges)} Document→Entity mention edges")
    print(f"  {len(co_mention_edges)} co-mention edges")
    print(f"  Entity coverage: {report['entities']['coverage_percentage']}% of documents")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
