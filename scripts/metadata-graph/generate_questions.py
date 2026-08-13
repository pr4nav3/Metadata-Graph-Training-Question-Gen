#!/usr/bin/env python3
"""
GraphHopGenerator: Sample the SEBI metadata graph and generate multi-document questions.

Pipeline:
  1. Sample a seed node (degree-weighted by type)
  2. Collect 1-N hop neighborhood
  3. Assemble context block
  4. LLM generates 1-3 multi-document questions
  5. Validate (reject single-doc, trivial, duplicates)
  6. Write JSONL

Usage:
  python3 generate_questions.py                    # default config
  python3 generate_questions.py --target 500       # override target
  python3 generate_questions.py --entity-only       # entity seeds only
"""

import json
import os
import pickle
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import networkx as nx

OUTPUT_DIR = Path(__file__).parent / "output"
QUESTIONS_DIR = OUTPUT_DIR / "questions"

CONFIG = {
    "hop_depth": 2,
    "max_docs_per_neighborhood": 8,
    "seed_weights": {
        "CanonicalEntity": 0.30,
        "RegulationRef": 0.30,
        "Topic": 0.30,
        "Document": 0.10,
    },
    "entity_kind_weights": {
        "company": 1.0,
        "person": 1.0,
        "pan": 0.9,
        "regulator": 0.8,
        "recovery_certificate": 0.15,
        "other": 0.1,
        "regulation": 0.1,
    },
    "inverse_topic_weighting": True,
    "min_seed_degree": 3,
    "max_questions_per_neighborhood": 3,
    "llm_url": os.environ.get(
        "METADATA_GRAPH_LLM_ENDPOINT",
        "https://grid.ai.juspay.net/v1/chat/completions",
    ),
    "llm_model": os.environ.get("METADATA_GRAPH_LLM_MODEL", "private-large"),
    "llm_api_key": (
        os.environ.get("METADATA_GRAPH_LLM_API_KEY")
        or os.environ.get("JUSPAY_API_KEY")
        or os.environ.get("LITELLM_API_KEY")
    ),
    "llm_timeout": 300,
    "llm_workers": 2,
    "target_questions": 50,
}

FEW_SHOT = [
    {
        "context": "SEED: [CanonicalEntity] Sumangal Industries Limited (company, 12 docs)\n\nDOCUMENTS (3):\n  1. \"Order against Sumangal Industries Ltd\" | Date: 2024-03-15 | Section: Enforcement/Orders of AO\n     Entities: Sumangal Industries Limited (company), Rajesh Kumar (person)\n     Cites: Section 28A of the SEBI Act, Section 11 of the SEBI Act\n  2. \"Recovery certificate against Sumangal Industries\" | Date: 2024-06-01 | Section: Enforcement/Recovery Proceedings\n     Entities: Sumangal Industries Limited (company)\n     Cites: Section 28A of the SEBI Act\n  3. \"Settlement order - Sumangal Industries\" | Date: 2024-09-15 | Section: Enforcement/Settlement Order\n     Entities: Sumangal Industries Limited (company)\n     Cites: Section 15J of the SEBI Act",
        "output": [
            {
                "question": "What is the timeline of SEBI enforcement actions against Sumangal Industries Limited?",
                "answer": "SEBI issued an Order against Sumangal Industries on March 15, 2024 (Orders of AO), followed by a Recovery Certificate on June 1, 2024 (Recovery Proceedings), and a Settlement Order on September 15, 2024 (Settlement Order). All three cite Section 28A of the SEBI Act.",
                "answer_type": "text",
                "difficulty": "medium",
                "supporting_doc_indices": [0, 1, 2],
                "golden_queries": ["Sumangal Industries SEBI order timeline", "Sumangal Industries recovery certificate", "Section 28A SEBI Act Sumangal"]
            }
        ]
    },
    {
        "context": "SEED: [Topic] Recovery Proceedings (6338 docs)\n\nDOCUMENTS (5):\n  1. \"Recovery proceedings under RC no 5305 against Ethan Constructions\" | Date: 2024-01-01 | Section: Enforcement/Recovery Proceedings\n  2. \"Recovery proceedings under RC no 5313 against Rahul Kamalkant Parasrampuria\" | Date: 2024-01-01 | Section: Enforcement/Recovery Proceedings\n  3. \"Notice of attachment in RC no 7243\" | Date: 2024-01-01 | Section: Enforcement/Recovery Proceedings\n  4. \"Completion order for RC no 7188\" | Date: 2024-01-01 | Section: Enforcement/Recovery Proceedings\n  5. \"Remittance order for RC no 7235\" | Date: 2024-01-01 | Section: Enforcement/Recovery Proceedings",
        "output": [
            {
                "question": "How many recovery proceedings were initiated on January 1, 2024, and what types of orders were issued?",
                "answer": "At least 5 recovery proceedings orders were issued on January 1, 2024, including proceedings against named entities (Ethan Constructions, Rahul Kamalkant Parasrampuria), notices of attachment, completion orders, and remittance orders under various RC numbers.",
                "answer_type": "list",
                "difficulty": "medium",
                "supporting_doc_indices": [0, 1, 2, 3, 4],
                "golden_queries": ["SEBI recovery proceedings January 2024", "RC 5305 5313 recovery", "recovery certificate attachment notice 2024"]
            }
        ]
    },
    {
        "context": "SEED: [RegulationRef] Section 28A of the SEBI Act (cited by 1383 docs)\n\nDOCUMENTS (4):\n  1. \"Order against SMS Techsoft\" | Date: 2024-02-15 | Section: Enforcement/Orders of AO\n     Entities: SMS Techsoft (company)\n     Cites: Section 28A of the SEBI Act, Section 11 of the SEBI Act\n  2. \"Recovery certificate against Safal Herbs\" | Date: 2024-03-20 | Section: Enforcement/Recovery Proceedings\n     Entities: Safal Herbs Limited (company)\n     Cites: Section 28A of the SEBI Act\n  3. \"Completion order for JMD Telefilms\" | Date: 2024-04-10 | Section: Enforcement/Recovery Proceedings\n     Entities: JMD Telefilms Industries Ltd (company)\n     Cites: Section 28A of the SEBI Act, Section 222 of the Income Tax Act\n  4. \"Quasi-judicial order against ISO at BSE\" | Date: 2024-05-05 | Section: Enforcement/Orders of ED or CGM\n     Entities: ISO at BSE (other)\n     Cites: Section 28A of the SEBI Act, Section 15HA of the SEBI Act",
        "output": [
            {
                "question": "Which SEBI enforcement orders cite Section 28A of the SEBI Act, and what other sections are cited alongside it?",
                "answer": "Section 28A is cited across multiple order types: an Order of AO against SMS Techsoft (also citing Section 11), a Recovery Certificate against Safal Herbs, a Completion Order for JMD Telefilms (also citing Section 222 of the Income Tax Act), and a Quasi-Judicial Order against ISO at BSE (also citing Section 15HA).",
                "answer_type": "list",
                "difficulty": "hard",
                "supporting_doc_indices": [0, 1, 2, 3],
                "golden_queries": ["Section 28A SEBI Act enforcement orders", "SEBI order Section 11 Section 28A", "Section 15HA Section 28A SEBI"]
            }
        ]
    },
    {
        "context": "SEED: [Document] DRHP Version 1 - ABC Chemicals Limited\n\nDOCUMENTS (3):\n  1. \"DRHP Version 1 - ABC Chemicals Limited\" | Date: 2025-01-10 | Section: Filings/Public Issues/Draft Offer Documents filed with SEBI\n     Topics: Draft Offer Documents filed with SEBI\n     Cites: ICDR Regulations 2018\n  2. \"DRHP Version 2 - ABC Chemicals Limited (revised filing)\" | Date: 2025-03-15 | Section: Filings/Public Issues/Draft Offer Documents filed with SEBI\n     Topics: Draft Offer Documents filed with SEBI\n     Cites: ICDR Regulations 2018\n  3. \"SEBI Circular on Risk Factor Disclosures\" | Date: 2024-06-01 | Section: Legal/Circulars\n     Topics: Circulars\n     Cites: ICDR Regulations 2018",
        "output": [
            {
                "question": "Compare Versions 1 and 2 of the ABC Chemicals DRHP. Did the company remove any risk factor without adequate explanation, and which SEBI circular governs such changes?",
                "answer": "Version 2 removes the risk factor relating to supplier concentration disclosed in Version 1 without explaining the rationale. The SEBI circular on Risk Factor Disclosures mandates that any material deletion or modification must be justified.",
                "answer_type": "boolean",
                "question_pattern": "join",
                "difficulty": "medium",
                "supporting_doc_indices": [0, 1, 2],
                "golden_queries": ["ABC Chemicals DRHP Version 1 Version 2 risk factor", "SEBI circular risk factor disclosure ICDR"]
            }
        ]
    },
    {
        "context": "SEED: [Topic] PMS Regulations\n\nDOCUMENTS (3):\n  1. \"SEBI (Portfolio Managers) Regulations, 2020\" | Date: 2020-10-01 | Section: Legal/Regulations\n     Topics: Regulations\n     Cites: SEBI Act, PMS Regulations\n  2. \"Master Circular for Portfolio Managers\" | Date: 2024-04-15 | Section: Legal/Master Circulars\n     Topics: Master Circulars\n     Cites: PMS Regulations\n  3. \"Clarification on investment in group company securities by Portfolio Managers\" | Date: 2023-08-22 | Section: Legal/Guidelines\n     Topics: Advisory or Guidance\n     Cites: PMS Regulations",
        "output": [
            {
                "question": "We are a Portfolio Manager. One of our group companies is launching a high-yield bond issue and we want to invest 20% of discretionary PMS client assets in these bonds. Is this permitted, and what prior consent or disclosure requirement applies under the PMS Regulations and Master Circular?",
                "answer": "No. The related-party investment cap for group-company securities is 15% of client assets and this is an absolute ceiling that client consent cannot raise. The Master Circular also requires prior written consent from each affected client and disclosure in the client agreement.",
                "answer_type": "text",
                "question_pattern": "apply",
                "difficulty": "hard",
                "supporting_doc_indices": [0, 1, 2],
                "golden_queries": ["PMS Regulations group company investment cap", "Portfolio Managers Master Circular prior consent related party"]
            }
        ]
    }
]

SYSTEM_PROMPT = """You are a SEBI compliance analyst expert. Given a neighborhood of connected documents, entities, and regulations from the SEBI corpus, generate multi-document questions that require reasoning across at least 2 documents to answer.

Style guidelines:
- Phrase questions as tasks a SEBI officer or compliance analyst would assign, not like exam questions.
- Prefer imperative/task-oriented openings such as: Compare, List, Cross-reference, Flag, Identify, Verify, Summarize, Track, Evaluate, Draw, Extract, Determine, Check — but do not limit yourself to these verbs.
- Questions may include 1-3 sub-parts or specify an output format (e.g., "in a table", "yes/no format", "chronologically").
- Encourage at least one question where the correct answer could be "no", "none", "not found", or "no evidence" — i.e., an absence/exception check.
- For apply-style questions, optionally frame them as a scenario or compliance check: "We are a Portfolio Manager..." or "A compliance officer needs to verify..."
- Generate a MIX of specificity: some questions can be tightly scoped to the exact documents/entities in the neighborhood, while others should ask broader analytical, comparative, or regulatory-pattern questions that generalize beyond the immediate seed.

Each question must:
- Require information from at least 2 of the listed documents
- Have a concise answer grounded in the provided context
- Refer to documents by their titles or dates in the answer, not by internal index labels like "doc2" or "Document 2"

Return a JSON array. Each element:
{
  "question": "the task-oriented question",
  "answer": "concise answer (1-3 sentences)",
  "answer_type": "entity|date|number|boolean|list|text",
  "question_pattern": "join|aggregate|apply",
  "difficulty": "easy|medium|hard",
  "supporting_doc_indices": [0-based indices into DOCUMENTS list],
  "golden_queries": ["search query 1", "query 2"]
}

Choose question_pattern based on the question:
- "join": connecting entities, citations, timelines, or comparisons across docs
- "aggregate": count, sum, list, or filtered collection across docs
- "apply": evaluate a scenario, rule, threshold, or compliance status

Return ONLY the JSON array. No other text."""


def load_graph():
    with open(OUTPUT_DIR / "sebi_metadata_graph.pkl", "rb") as f:
        return pickle.load(f)


def compute_doc_degree(G, node_id):
    node_type = G.nodes[node_id].get("type")
    if node_type == "CanonicalEntity":
        return sum(1 for p in G.predecessors(node_id) if "mentions" in G[p][node_id])
    if node_type == "RegulationRef":
        return sum(1 for p in G.predecessors(node_id) if "cites" in G[p][node_id])
    if node_type == "Topic":
        return sum(1 for s in G.successors(node_id) if "governed_by" in G[node_id][s])
    if node_type == "Document":
        return G.degree(node_id)
    return 0


def build_seed_pool(G, config):
    active_types = {k for k, v in config["seed_weights"].items() if v > 0}
    pool = []
    for node_id, attrs in G.nodes(data=True):
        ntype = attrs.get("type")
        if ntype not in active_types:
            continue
        deg = compute_doc_degree(G, node_id)
        if deg < config["min_seed_degree"]:
            continue

        if ntype == "CanonicalEntity":
            name = attrs.get("name", "")
            if len(name) > 80:
                continue
            kind = attrs.get("kind", "other")
            kind_weight = config.get("entity_kind_weights", {}).get(kind, 0.5)
            weight = deg * kind_weight
        elif ntype == "Topic" and config.get("inverse_topic_weighting", False):
            weight = 1.0 / (deg ** 0.5)
        else:
            weight = float(deg)

        pool.append((node_id, ntype, weight))
    return pool


def sample_seed(pool, config):
    r = random.random()
    cum = 0.0
    target_type = None
    for stype, weight in config["seed_weights"].items():
        if weight == 0:
            continue
        cum += weight
        if r < cum:
            target_type = stype
            break
    if not target_type:
        target_type = list(config["seed_weights"].keys())[0]

    candidates = [(nid, w) for nid, ntype, w in pool if ntype == target_type]
    if not candidates:
        candidates = [(nid, w) for nid, _, w in pool]
    if not candidates:
        return None

    weights = [w for _, w in candidates]
    total = sum(weights)
    if total == 0:
        return random.choice(candidates)[0]
    r = random.uniform(0, total)
    cum = 0.0
    for nid, w in candidates:
        cum += w
        if r < cum:
            return nid
    return candidates[-1][0]


def collect_neighborhood(G, seed_id, hop_depth, max_docs):
    doc_nodes = set()
    if G.nodes[seed_id].get("type") == "Document":
        doc_nodes.add(seed_id)

    visited = {seed_id}
    frontier = [seed_id]
    for _ in range(hop_depth):
        next_frontier = []
        for node in frontier:
            for nbr in set(G.successors(node)) | set(G.predecessors(node)):
                if nbr not in visited:
                    visited.add(nbr)
                    next_frontier.append(nbr)
                    if G.nodes[nbr].get("type") == "Document":
                        doc_nodes.add(nbr)
        frontier = next_frontier
        if len(doc_nodes) >= max_docs:
            break

    doc_list = list(doc_nodes)[:max_docs]
    docs = []
    for doc_id in doc_list:
        d = G.nodes[doc_id]
        entities = []
        regs = []
        topics = []
        folders = []
        for nbr in G.successors(doc_id):
            nt = G.nodes[nbr].get("type")
            if nt == "CanonicalEntity":
                entities.append({"id": nbr, "name": G.nodes[nbr].get("name", ""), "kind": G.nodes[nbr].get("kind", "")})
            elif nt == "RegulationRef":
                regs.append({"id": nbr, "canonicalId": G.nodes[nbr].get("canonicalId", "")})
            elif nt == "Topic":
                topics.append(G.nodes[nbr].get("name", ""))
            elif nt == "Folder":
                folders.append(G.nodes[nbr].get("path", ""))
        docs.append({
            "id": doc_id,
            "title": d.get("title", ""),
            "date": d.get("date", ""),
            "topSection": d.get("topSection", ""),
            "section": d.get("section", ""),
            "vespaDocId": d.get("vespaDocId", ""),
            "documentId": d.get("documentId", ""),
            "entities": entities,
            "regs": regs,
            "topics": topics,
            "folders": folders,
        })

    return {"seed_id": seed_id, "seed_attrs": dict(G.nodes[seed_id]), "docs": docs}


def assemble_context(nhd):
    seed = nhd["seed_attrs"]
    seed_type = seed.get("type", "?")
    seed_name = seed.get("name", seed.get("title", seed.get("canonicalId", nhd["seed_id"])))

    lines = [f"SEED: [{seed_type}] {seed_name}"]
    if seed_type == "CanonicalEntity":
        lines.append(f"  Kind: {seed.get('kind', '?')}")

    docs = nhd["docs"]
    if len(docs) < 2:
        return None

    lines.append(f"\nDOCUMENTS ({len(docs)}):")
    for i, doc in enumerate(docs):
        title = doc["title"][:80] if doc["title"] else "(untitled)"
        lines.append(f"  {i}. \"{title}\" | Date: {doc['date']} | Section: {doc['topSection']}/{doc['section']}")
        if doc["entities"]:
            ent_str = ", ".join(f"{e['name']} ({e['kind']})" for e in doc["entities"][:5])
            lines.append(f"     Entities: {ent_str}")
        if doc["regs"]:
            reg_str = ", ".join(r["canonicalId"] for r in doc["regs"][:5])
            lines.append(f"     Cites: {reg_str}")
        if doc["topics"]:
            lines.append(f"     Topics: {', '.join(doc['topics'])}")
        if doc["folders"]:
            lines.append(f"     Folders: {', '.join(doc['folders'])}")

    all_entities = {}
    for doc in docs:
        for e in doc["entities"]:
            if e["id"] not in all_entities:
                all_entities[e["id"]] = {**e, "count": 0}
            all_entities[e["id"]]["count"] += 1

    if all_entities:
        lines.append(f"\nENTITIES ({len(all_entities)}):")
        for e in sorted(all_entities.values(), key=lambda x: -x["count"])[:10]:
            lines.append(f"  - {e['name']} ({e['kind']}, {e['count']} docs)")

    all_regs = {}
    for doc in docs:
        for r in doc["regs"]:
            if r["id"] not in all_regs:
                all_regs[r["id"]] = {**r, "count": 0}
            all_regs[r["id"]]["count"] += 1

    if all_regs:
        lines.append(f"\nREGULATIONS ({len(all_regs)}):")
        for r in sorted(all_regs.values(), key=lambda x: -x["count"])[:10]:
            lines.append(f"  - {r['canonicalId']} ({r['count']} docs)")

    all_topics = set()
    for doc in docs:
        all_topics.update(doc["topics"])
    if all_topics:
        lines.append(f"\nTOPICS: {', '.join(sorted(all_topics))}")

    return "\n".join(lines)


def call_llm(context, config):
    few_shot_text = ""
    for i, ex in enumerate(FEW_SHOT):
        few_shot_text += f"\n\nEXAMPLE {i+1}:\nContext:\n{ex['context']}\n\nOutput:\n{json.dumps(ex['output'], indent=2)}"

    user_msg = f"""Given the following neighborhood of connected documents from the SEBI corpus, generate 1-{config['max_questions_per_neighborhood']} multi-document questions.

{few_shot_text}

NOW GENERATE QUESTIONS FOR THIS NEIGHBORHOOD:

{context}

Return ONLY a JSON array of questions. Use the same format as the examples above."""

    payload = {
        "model": config["llm_model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
        "max_tokens": 2000,
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                config["llm_url"],
                headers={"Authorization": f"Bearer {config['llm_api_key']}", "Content-Type": "application/json"},
                json=payload,
                timeout=config["llm_timeout"],
            )
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return parse_llm_response(text)
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            if attempt < 2:
                time.sleep(1)
            else:
                print(f"  LLM error: {e}")
                return []
    return []


def parse_llm_response(text):
    if not text:
        return []
    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "questions" in parsed:
            return parsed["questions"]
        return [parsed] if isinstance(parsed, dict) else []
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return []


def validate(q):
    if not isinstance(q, dict):
        return False, "not a dict"
    question = q.get("question") or ""
    answer = q.get("answer") or ""
    if not question.strip():
        return False, "empty question"
    if not answer.strip():
        return False, "empty answer"
    if len(question) < 20:
        return False, "question too short"
    if len(answer) < 5:
        return False, "answer too short"
    indices = q.get("supporting_doc_indices") or []
    if len(indices) < 2:
        return False, "fewer than 2 supporting docs"
    answer_lower = answer.lower().strip()
    if answer_lower in ("yes", "no", "true", "false", "none", "n/a"):
        return False, "trivial answer"
    return True, None


def infer_question_pattern(question, answer):
    q = question.lower()
    a = answer.lower()
    aggregate_markers = [
        "how many", "how much", "count", "sum", "total", "list", "which all",
        "enumerate", "what are the", "how many distinct", "how many offenders",
    ]
    apply_markers = [
        "can we", "is this permitted", "is this allowed", "comply", "compliance",
        "violate", "violation", "threshold", "prior consent", "requirement",
        "what is the liability", "does this satisfy", "evaluate", "checklist",
    ]
    if any(m in q for m in aggregate_markers):
        return "aggregate"
    if any(m in q for m in apply_markers):
        return "apply"
    return "join"


def enrich_output(q, nhd):
    docs = nhd["docs"]
    indices = q.get("supporting_doc_indices") or []
    supporting_docs = []
    for idx in indices:
        if 0 <= idx < len(docs):
            doc = docs[idx]
            supporting_docs.append({
                "docId": doc["id"],
                "vespaDocId": doc["vespaDocId"],
                "sebiDocId": str(doc.get("documentId", "")),
                "role": "primary" if idx == indices[0] else "secondary",
            })

    seed = nhd["seed_attrs"]
    seed_type = seed.get("type", "")
    seed_name = seed.get("name", seed.get("title", seed.get("canonicalId", nhd["seed_id"])))

    if seed_type == "CanonicalEntity":
        hop_desc = f"{nhd['seed_id']} --mentioned_in--> {', '.join(d['docId'] for d in supporting_docs)}"
    elif seed_type == "RegulationRef":
        hop_desc = f"{nhd['seed_id']} --cited_by--> {', '.join(d['docId'] for d in supporting_docs)}"
    elif seed_type == "Topic":
        hop_desc = f"{nhd['seed_id']} --governs--> {', '.join(d['docId'] for d in supporting_docs)}"
    else:
        hop_desc = f"{nhd['seed_id']} --> {', '.join(d['docId'] for d in supporting_docs)}"

    pattern = (q.get("question_pattern") or "").strip().lower()
    if pattern not in ("join", "aggregate", "apply"):
        pattern = infer_question_pattern(q["question"], q["answer"])

    return {
        "question": q["question"],
        "answer": q["answer"],
        "answer_type": (q.get("answer_type") or "text"),
        "question_pattern": pattern,
        "difficulty": (q.get("difficulty") or "medium"),
        "seed_node": {"id": nhd["seed_id"], "type": seed_type, "name": seed_name},
        "hop_depth": CONFIG["hop_depth"],
        "supporting_docs": supporting_docs,
        "reasoning_path": hop_desc,
        "golden_queries": q.get("golden_queries") or [],
    }


def dominant_topic(nhd):
    if nhd["seed_attrs"].get("type") == "Topic":
        return nhd["seed_attrs"].get("name", "")
    topic_counts = defaultdict(int)
    for doc in nhd["docs"]:
        for t in doc.get("topics", []):
            topic_counts[t] += 1
    if topic_counts:
        return max(topic_counts.items(), key=lambda x: x[1])[0]
    return ""


def process_one(G, seed_id, config):
    nhd = collect_neighborhood(G, seed_id, config["hop_depth"], config["max_docs_per_neighborhood"])
    if len(nhd["docs"]) < 2:
        return []
    context = assemble_context(nhd)
    if not context:
        return []
    raw_questions = call_llm(context, config) or []
    results = []
    for q in raw_questions:
        valid, reason = validate(q)
        if valid:
            enriched = enrich_output(q, nhd)
            enriched["_dominant_topic"] = dominant_topic(nhd)
            results.append(enriched)
    return results


def main():
    args = sys.argv[1:]
    entity_only = "--entity-only" in args
    target_override = None
    for i, a in enumerate(args):
        if a == "--target" and i + 1 < len(args):
            target_override = int(args[i + 1])

    if entity_only:
        CONFIG["seed_weights"] = {"CanonicalEntity": 1.0}
    if target_override:
        CONFIG["target_questions"] = target_override
    if not CONFIG["llm_api_key"]:
        raise SystemExit(
            "Missing LLM credential: set METADATA_GRAPH_LLM_API_KEY, "
            "JUSPAY_API_KEY, or LITELLM_API_KEY"
        )

    print("=" * 60)
    print("GraphHopGenerator")
    print("=" * 60)
    print(f"  Hop depth:     {CONFIG['hop_depth']}")
    print(f"  Seed weights:   {CONFIG['seed_weights']}")
    print(f"  Target:         {CONFIG['target_questions']} questions")
    print(f"  LLM:            {CONFIG['llm_model']} ({CONFIG['llm_workers']} workers)")

    print("\n[1] Loading graph...")
    G = load_graph()
    print(f"    {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    print("\n[2] Building seed pool...")
    pool = build_seed_pool(G, CONFIG)
    type_counts = defaultdict(int)
    for _, ntype, deg in pool:
        type_counts[ntype] += 1
    print(f"    {len(pool)} seeds: {dict(type_counts)}")

    print(f"\n[3] Generating questions (target: {CONFIG['target_questions']})...")
    QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    output_file = QUESTIONS_DIR / f"questions_{int(time.time())}.jsonl"

    all_questions = []
    batch_num = 0
    recent_topics = []
    diversity_window = 8

    with open(output_file, "w") as out:
        while len(all_questions) < CONFIG["target_questions"]:
            batch_size = min(CONFIG["llm_workers"], CONFIG["target_questions"] - len(all_questions) + 2)
            batch_seeds = []
            for _ in range(batch_size):
                seed = sample_seed(pool, CONFIG)
                if seed:
                    batch_seeds.append(seed)
            if not batch_seeds:
                print("    No seeds available.")
                break

            batch_num += 1
            t0 = time.time()
            batch_results = []

            with ThreadPoolExecutor(max_workers=CONFIG["llm_workers"]) as executor:
                futures = {executor.submit(process_one, G, sid, CONFIG): sid for sid in batch_seeds}
                for future in as_completed(futures):
                    try:
                        questions = future.result()
                        batch_results.extend(questions)
                    except Exception as e:
                        print(f"    Worker error: {e}")

            seen = {q["question"].lower() for q in all_questions}
            accepted_this_batch = 0
            for q in batch_results:
                if q["question"].lower() in seen:
                    continue
                topic = q.pop("_dominant_topic", "")
                if topic and topic in recent_topics:
                    continue
                if topic:
                    recent_topics.append(topic)
                    if len(recent_topics) > diversity_window:
                        recent_topics.pop(0)
                seen.add(q["question"].lower())
                out.write(json.dumps(q, ensure_ascii=False) + "\n")
                out.flush()
                all_questions.append(q)
                accepted_this_batch += 1

            elapsed = time.time() - t0
            print(f"    Batch {batch_num}: {accepted_this_batch} accepted / {len(batch_results)} generated, {len(all_questions)} total ({elapsed:.1f}s)")

            if not batch_results:
                print("    No questions in this batch, trying different seeds...")

    print(f"\n{'=' * 60}")
    print(f"Done: {len(all_questions)} questions → {output_file}")
    print(f"{'=' * 60}")

    if all_questions:
        print("\nSample questions:")
        for q in all_questions[:3]:
            print(f"\n  Q: {q['question'][:100]}")
            print(f"  A: {q['answer'][:100]}")
            print(f"  Type: {q['answer_type']} | Difficulty: {q['difficulty']} | Docs: {len(q['supporting_docs'])}")

    difficulty_counts = defaultdict(int)
    pattern_counts = defaultdict(int)
    for q in all_questions:
        difficulty_counts[q["difficulty"]] += 1
        pattern_counts[q.get("question_pattern", "")] += 1
    print(f"\nDifficulty: {dict(difficulty_counts)}")
    print(f"Patterns: {dict(pattern_counts)}")


if __name__ == "__main__":
    main()
