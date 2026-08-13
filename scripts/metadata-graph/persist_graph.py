#!/usr/bin/env python3
"""
Phase 6: Persist and Index the Graph

Loads all JSONL outputs from Phases 2-5 into:
1. NetworkX pickle (for in-memory graph walks)
2. SQLite with secondary indexes (for SQL queries and auditing)
3. logs/metadata_graph_build_report.json (final build report)

No Docker containers are touched. Reads graph layers from scripts/metadata-graph/graph/*.jsonl
and writes derived runtime artifacts under scripts/metadata-graph/output/.
"""

import json
import pickle
import sqlite3
import sys
import time
from pathlib import Path
from collections import Counter

import networkx as nx

GRAPH_DIR = Path(__file__).parent / "graph"
OUTPUT_DIR = Path(__file__).parent / "output"
LOG_DIR = OUTPUT_DIR / "logs"

NODE_FILES = [
    "documents_enriched.jsonl",
    "attachments.jsonl",
    "taxonomy.jsonl",
    "regulation_refs.jsonl",
    "entities.jsonl",
    "folders.jsonl",
    "topics.jsonl",
]

EDGE_FILES = [
    "edges.jsonl",
    "citation_edges.jsonl",
    "entity_edges.jsonl",
    "folder_edges.jsonl",
    "topic_edges.jsonl",
]

PHASE_REPORTS = [
    "logs/phase2_report.json",
    "logs/phase3_report.json",
    "logs/phase4_report.json",
    "logs/phase5_report.json",
]

INDEX_SPECS = [
    ("idx_nodes_type", "nodes(type)"),
    ("idx_nodes_name", "nodes(name)"),
    ("idx_edges_type", "edges(type)"),
    ("idx_edges_source", "edges(source)"),
    ("idx_edges_target", "edges(target)"),
    ("idx_nodes_date", "nodes(date)"),
    ("idx_nodes_section", "nodes(section)"),
    ("idx_nodes_kind", "nodes(kind)"),
    ("idx_nodes_canonical_id", "nodes(canonical_id)"),
]


def load_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            records.append(json.loads(line))
    return records


def normalize_nodes(nodes: list[dict]) -> list[dict]:
    for n in nodes:
        if "type" not in n:
            if "kind" in n and n.get("id", "").startswith("tax_"):
                n["type"] = "TaxonomyNode"
    return nodes


def build_networkx(nodes_by_id: dict, edges: list[dict]) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for nid, attrs in nodes_by_id.items():
        G.add_node(nid, **attrs)
    for edge in edges:
        G.add_edge(edge["source"], edge["target"], key=edge["type"], type=edge["type"])
    return G


def create_sqlite(db_path: Path, nodes_by_id: dict, edges: list[dict]) -> dict:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    c.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT,
            date TEXT,
            section TEXT,
            kind TEXT,
            canonical_id TEXT,
            title TEXT,
            parent_id TEXT,
            attrs_json TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE edges (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            type TEXT NOT NULL,
            attrs_json TEXT
        )
    """)

    node_insert_count = 0
    for nid, attrs in nodes_by_id.items():
        row = {
            "id": nid,
            "type": attrs.get("type", ""),
            "name": attrs.get("name", ""),
            "date": attrs.get("date", ""),
            "section": attrs.get("section", ""),
            "kind": attrs.get("kind", ""),
            "canonical_id": attrs.get("canonicalId", ""),
            "title": attrs.get("title", ""),
            "parent_id": attrs.get("parentId", ""),
            "attrs_json": json.dumps(attrs, ensure_ascii=False),
        }
        c.execute(
            "INSERT INTO nodes (id, type, name, date, section, kind, canonical_id, title, parent_id, attrs_json) "
            "VALUES (:id, :type, :name, :date, :section, :kind, :canonical_id, :title, :parent_id, :attrs_json)",
            row,
        )
        node_insert_count += 1

    edge_insert_count = 0
    for edge in edges:
        c.execute(
            "INSERT INTO edges (source, target, type, attrs_json) VALUES (?, ?, ?, ?)",
            (edge["source"], edge["target"], edge["type"], json.dumps(edge, ensure_ascii=False)),
        )
        edge_insert_count += 1

    for idx_name, idx_def in INDEX_SPECS:
        try:
            c.execute(f"CREATE INDEX {idx_name} ON {idx_def}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    return {"nodes": node_insert_count, "edges": edge_insert_count}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Phase 6: Persist and Index the Graph")
    print("=" * 60)

    t0 = time.time()

    print("\n[1] Loading graph JSONL layers...")
    all_nodes = []
    node_counts = {}
    for fname in NODE_FILES:
        path = GRAPH_DIR / fname
        if not path.exists():
            print(f"    SKIP {fname} (not found)")
            continue
        records = load_jsonl(path)
        records = normalize_nodes(records)
        all_nodes.extend(records)
        node_counts[fname] = len(records)
        print(f"    {fname:40s} {len(records):>8d} nodes")

    all_edges = []
    edge_counts = {}
    for fname in EDGE_FILES:
        path = GRAPH_DIR / fname
        if not path.exists():
            print(f"    SKIP {fname} (not found)")
            continue
        records = load_jsonl(path)
        all_edges.extend(records)
        edge_counts[fname] = len(records)
        print(f"    {fname:40s} {len(records):>8d} edges")

    print(f"\n    Total: {len(all_nodes)} nodes, {len(all_edges)} edges")

    print("\n[2] Building node index by ID...")
    nodes_by_id = {}
    dup_ids = 0
    for node in all_nodes:
        nid = node.get("id")
        if nid is None:
            continue
        if nid in nodes_by_id:
            dup_ids += 1
        nodes_by_id[nid] = node
    print(f"    {len(nodes_by_id)} unique node IDs ({dup_ids} duplicates skipped)")

    print("\n[3] Building NetworkX graph...")
    G = build_networkx(nodes_by_id, all_edges)
    print(f"    {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    pickle_path = OUTPUT_DIR / "sebi_metadata_graph.pkl"
    with open(pickle_path, "wb") as f:
        pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
    pickle_size = pickle_path.stat().st_size / (1024 * 1024)
    print(f"    Wrote {pickle_path.name} ({pickle_size:.1f} MB)")

    print("\n[4] Building SQLite database...")
    sqlite_path = OUTPUT_DIR / "sebi_metadata_graph.sqlite"
    insert_counts = create_sqlite(sqlite_path, nodes_by_id, all_edges)
    sqlite_size = sqlite_path.stat().st_size / (1024 * 1024)
    print(f"    Wrote {sqlite_path.name} ({sqlite_size:.1f} MB)")
    print(f"    {insert_counts['nodes']} nodes, {insert_counts['edges']} edges inserted")

    print("\n[5] Verifying SQLite indexes...")
    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    indexes = [row[0] for row in c.fetchall()]
    print(f"    {len(indexes)} indexes: {indexes}")
    conn.close()

    print("\n[6] Loading phase reports...")
    phase_reports = {}
    for fname in PHASE_REPORTS:
        path = OUTPUT_DIR / fname
        if path.exists():
            with open(path) as f:
                phase_reports[fname.replace(".json", "")] = json.load(f)
            print(f"    {fname}")

    print("\n[7] Generating final build report...")
    node_type_counts = Counter(n.get("type", "?") for n in all_nodes)
    edge_type_counts = Counter(e.get("type", "?") for e in all_edges)

    report = {
        "phase": 6,
        "description": "Persist graph as NetworkX pickle + SQLite with secondary indexes",
        "build_time_sec": round(time.time() - t0, 1),
        "total_nodes": len(nodes_by_id),
        "total_edges": len(all_edges),
        "node_types": dict(node_type_counts),
        "edge_types": dict(edge_type_counts),
        "node_file_counts": node_counts,
        "edge_file_counts": edge_counts,
        "duplicate_node_ids": dup_ids,
        "output_files": {
            "pickle": {
                "path": "output/sebi_metadata_graph.pkl",
                "size_mb": round(pickle_size, 1),
                "node_count": G.number_of_nodes(),
                "edge_count": G.number_of_edges(),
            },
            "sqlite": {
                "path": "output/sebi_metadata_graph.sqlite",
                "size_mb": round(sqlite_size, 1),
                "node_count": insert_counts["nodes"],
                "edge_count": insert_counts["edges"],
                "indexes": indexes,
            },
        },
        "sqlite_indexes": [
            {"name": name, "definition": spec}
            for name, spec in INDEX_SPECS
        ],
        "phase_reports": {k: v for k, v in phase_reports.items()},
    }

    report_path = LOG_DIR / "metadata_graph_build_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"    Wrote {report_path.name}")

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print("Phase 6 Complete")
    print("=" * 60)
    print(f"  Total nodes:     {len(nodes_by_id)}")
    print(f"  Total edges:     {len(all_edges)}")
    print(f"  Pickle:          {pickle_path.name} ({pickle_size:.1f} MB)")
    print(f"  SQLite:          {sqlite_path.name} ({sqlite_size:.1f} MB)")
    print(f"  Indexes:         {len(indexes)}")
    print(f"  Build report:    {report_path.name}")
    print(f"  Elapsed:         {elapsed:.1f}s")


if __name__ == "__main__":
    main()
