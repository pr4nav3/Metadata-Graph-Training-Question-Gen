#!/usr/bin/env python3
"""
Phase 5: Derive Relationship Edges

Builds:
1. Folder nodes from Postgres collection_items (type='folder')
2. Folder hierarchy edges (Folder --child_of--> Folder)
3. Document --belongs_to_folder--> Folder edges (from clFd)
4. Topic nodes derived from folder paths
5. Topic --governed_by--> Document edges

Co-mention edges were already built in Phase 4 (391,717 edges).

Inputs:
  - Postgres: collection_items (folders + files), collections
  - graph/documents_enriched.jsonl (12,915 docs with clFd field)

Outputs:
  - graph/folders.jsonl          — Folder nodes
  - graph/folder_edges.jsonl     — Folder hierarchy + Document→Folder edges
  - graph/topics.jsonl           — Topic nodes
  - graph/topic_edges.jsonl      — Topic→Document edges
  - output/logs/phase5_report.json     — Summary stats
"""

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
GRAPH_DIR = SCRIPT_DIR / "graph"
OUTPUT_DIR = SCRIPT_DIR / "output"
LOG_DIR = OUTPUT_DIR / "logs"
POSTGRES_CONTAINER = (
    os.environ.get("METADATA_KG_POSTGRES_CONTAINER")
    or os.environ.get("XYNE_POSTGRES_CONTAINER")
    or "metadata-kg-xyne-db"
)

def psql(query: str) -> list[list[str]]:
    """Run a psql query inside the configured Postgres container."""
    result = subprocess.run(
        ["docker", "exec", POSTGRES_CONTAINER, "psql", "-U", "xyne", "-d", "xyne",
         "-t", "-A", "-F", "\t", "-c", query],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"PSQL ERROR: {result.stderr}", file=sys.stderr)
        return []
    rows = []
    for line in result.stdout.strip().split("\n"):
        if line:
            rows.append(line.split("\t"))
    return rows

def fetch_folders() -> dict[str, dict]:
    """Fetch all folders from Postgres, return {folder_id: {name, parent_id, collection_id, collection_name}}."""
    rows = psql("""
        SELECT ci.id, ci.name, ci.parent_id, ci.collection_id, c.name as collection_name
        FROM collection_items ci
        JOIN collections c ON ci.collection_id = c.id
        WHERE ci.type = 'folder' AND c.deleted_at IS NULL
        AND c.name IN ('Enforcements','Filings','Legal','Media','Reports')
    """)
    folders = {}
    for row in rows:
        fid, name, parent_id, coll_id, coll_name = row
        folders[fid] = {
            "id": fid,
            "name": name,
            "parent_id": parent_id if parent_id else None,
            "collection_id": coll_id,
            "collection_name": coll_name,
        }
    return folders

def build_folder_paths(folders: dict[str, dict]) -> dict[str, str]:
    """Walk parent_id chain to build full path for each folder. Returns {folder_id: 'Collection/Parent/Child'}."""
    paths = {}
    def get_path(fid: str) -> str:
        if fid in paths:
            return paths[fid]
        f = folders.get(fid)
        if not f:
            return "?"
        coll = f["collection_name"]
        if f["parent_id"] and f["parent_id"] in folders:
            parent_path = get_path(f["parent_id"])
            full = f"{parent_path}/{f['name']}"
        else:
            full = f"{coll}/{f['name']}"
        paths[fid] = full
        return full
    
    for fid in folders:
        get_path(fid)
    return paths

def fetch_file_counts() -> dict[str, int]:
    """Count files per parent folder from Postgres."""
    rows = psql("""
        SELECT ci.parent_id::text, COUNT(*)
        FROM collection_items ci
        JOIN collections c ON ci.collection_id = c.id
        WHERE ci.type = 'file' AND c.deleted_at IS NULL
        AND c.name IN ('Enforcements','Filings','Legal','Media','Reports')
        GROUP BY ci.parent_id
    """)
    counts = {}
    for row in rows:
        if len(row) >= 2:
            parent_id, cnt = row[0], row[1]
            counts[parent_id] = int(cnt)
    return counts

def load_documents() -> list[dict]:
    """Load enriched documents from Phase 3 output."""
    docs = []
    with open(GRAPH_DIR / "documents_enriched.jsonl") as f:
        for line in f:
            docs.append(json.loads(line))
    return docs

def derive_topic_name(folder_path: str) -> str:
    """Derive a topic name from a folder path.
    
    Examples:
      Enforcements/Orders/Settlement Order → Settlement Order
      Legal/Regulations → Regulations
      Filings/Public Issues → Public Issues
    """
    # Use the leaf folder name as the topic
    parts = folder_path.split("/")
    return parts[-1] if parts else folder_path

def main():
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Phase 5: Derive Relationship Edges")
    print("=" * 60)
    
    # --- 1. Fetch folders from Postgres ---
    print("\n[1] Fetching folders from Postgres...")
    folders = fetch_folders()
    print(f"    Fetched {len(folders)} folders")
    
    folder_paths = build_folder_paths(folders)
    file_counts = fetch_file_counts()
    
    # Print folder hierarchy summary
    depth_counts = defaultdict(int)
    for fid, path in folder_paths.items():
        depth = len(path.split("/"))
        depth_counts[depth] += 1
    print(f"    Folder depths: {dict(depth_counts)}")
    
    # --- 2. Emit Folder nodes ---
    print("\n[2] Writing Folder nodes...")
    folder_nodes = []
    for fid, f in folders.items():
        path = folder_paths[fid]
        file_count = file_counts.get(fid, 0)
        folder_nodes.append({
            "type": "Folder",
            "id": f"folder_{fid[:8]}",
            "pgId": fid,
            "name": f["name"],
            "path": path,
            "collection": f["collection_name"],
            "parentId": f["parent_id"],
            "fileCount": file_count,
        })
    
    with open(GRAPH_DIR / "folders.jsonl", "w") as out:
        for node in folder_nodes:
            out.write(json.dumps(node, ensure_ascii=False) + "\n")
    print(f"    Wrote {len(folder_nodes)} Folder nodes → folders.jsonl")
    
    # --- 3. Emit folder hierarchy edges (Folder --child_of--> Folder) ---
    print("\n[3] Writing folder hierarchy edges...")
    hierarchy_edges = []
    for fid, f in folders.items():
        if f["parent_id"] and f["parent_id"] in folders:
            hierarchy_edges.append({
                "type": "child_of",
                "source": f"folder_{fid[:8]}",
                "target": f"folder_{f['parent_id'][:8]}",
            })
    print(f"    {len(hierarchy_edges)} Folder --child_of--> Folder edges")
    
    # --- 4. Load documents and emit Document --belongs_to_folder--> Folder edges ---
    print("\n[4] Loading documents and building folder membership edges...")
    docs = load_documents()
    print(f"    Loaded {len(docs)} documents")
    
    folder_membership_edges = []
    docs_with_folder = 0
    docs_without_folder = 0
    folder_doc_counts = defaultdict(int)
    
    for doc in docs:
        clfd = doc.get("clFd")
        if clfd and clfd in folders:
            folder_membership_edges.append({
                "type": "belongs_to_folder",
                "source": doc["id"],
                "target": f"folder_{clfd[:8]}",
            })
            docs_with_folder += 1
            folder_doc_counts[clfd] += 1
        else:
            docs_without_folder += 1
    
    print(f"    {docs_with_folder} docs with folder ({docs_with_folder*100/len(docs):.1f}%)")
    print(f"    {docs_without_folder} docs without folder ({docs_without_folder*100/len(docs):.1f}%)")
    
    # --- 5. Emit all folder-related edges ---
    all_folder_edges = hierarchy_edges + folder_membership_edges
    with open(GRAPH_DIR / "folder_edges.jsonl", "w") as out:
        for edge in all_folder_edges:
            out.write(json.dumps(edge, ensure_ascii=False) + "\n")
    print(f"\n    Wrote {len(all_folder_edges)} folder edges → folder_edges.jsonl")
    print(f"      ({len(hierarchy_edges)} child_of + {len(folder_membership_edges)} belongs_to_folder)")
    
    # --- 6. Derive Topic nodes and Topic --governed_by--> Document edges ---
    print("\n[5] Deriving Topic nodes and governed_by edges...")
    
    topic_map = {}  # topic_name → Topic node
    topic_edges = []
    docs_with_topic = 0
    
    for doc in docs:
        clfd = doc.get("clFd")
        if clfd and clfd in folders:
            folder_path = folder_paths[clfd]
            topic_name = derive_topic_name(folder_path)
            
            if topic_name not in topic_map:
                topic_map[topic_name] = {
                    "type": "Topic",
                    "id": f"topic_{len(topic_map)}",
                    "name": topic_name,
                    "sourceFolder": folder_path,
                }
            
            topic_edges.append({
                "type": "governed_by",
                "source": topic_map[topic_name]["id"],
                "target": doc["id"],
            })
            docs_with_topic += 1
    
    topic_nodes = list(topic_map.values())
    
    with open(GRAPH_DIR / "topics.jsonl", "w") as out:
        for node in topic_nodes:
            out.write(json.dumps(node, ensure_ascii=False) + "\n")
    print(f"    Wrote {len(topic_nodes)} Topic nodes → topics.jsonl")
    
    with open(GRAPH_DIR / "topic_edges.jsonl", "w") as out:
        for edge in topic_edges:
            out.write(json.dumps(edge, ensure_ascii=False) + "\n")
    print(f"    Wrote {len(topic_edges)} Topic --governed_by--> Document edges → topic_edges.jsonl")
    
    # --- 7. Summary report ---
    print("\n[6] Generating phase5 report...")
    
    # Top 10 folders by doc count
    top_folders = sorted(folder_doc_counts.items(), key=lambda x: -x[1])[:10]
    top_folder_summary = [
        {"folder": folder_paths.get(fid, "?"), "folder_id": fid[:8], "doc_count": cnt}
        for fid, cnt in top_folders
    ]
    
    topic_by_id = {node["id"]: node for node in topic_nodes}
    topic_doc_counts = defaultdict(int)
    for edge in topic_edges:
        topic_doc_counts[edge["source"]] += 1
    top_topics = sorted(topic_doc_counts.items(), key=lambda x: -x[1])[:10]
    topic_summary = [
        {"topic": topic_by_id[tid]["name"], "topic_id": tid, "doc_count": cnt, "source_folder": topic_by_id[tid]["sourceFolder"]}
        for tid, cnt in top_topics
    ]
    
    report = {
        "phase": 5,
        "description": "Derive relationship edges: folder hierarchy, folder membership, topic derivation",
        "inputs": {
            "documents_enriched": len(docs),
            "postgres_folders": len(folders),
        },
        "folder_nodes": {
            "total": len(folder_nodes),
            "by_collection": dict(defaultdict(lambda: 0, {
                coll: sum(1 for f in folder_nodes if f["collection"] == coll)
                for coll in set(f["collection"] for f in folder_nodes)
            })),
            "hierarchy_edges": len(hierarchy_edges),
            "root_folders": sum(1 for f in folder_nodes if not f["parentId"]),
            "depth_distribution": dict(depth_counts),
        },
        "folder_membership": {
            "docs_with_folder": docs_with_folder,
            "docs_without_folder": docs_without_folder,
            "coverage_pct": round(docs_with_folder * 100 / len(docs), 1),
            "belongs_to_folder_edges": len(folder_membership_edges),
            "folders_with_docs": len(folder_doc_counts),
            "folders_without_docs": len(folders) - len(folder_doc_counts),
            "top_10_folders_by_doc_count": top_folder_summary,
        },
        "topics": {
            "total_topics": len(topic_nodes),
            "governed_by_edges": len(topic_edges),
            "docs_with_topic": docs_with_topic,
            "docs_without_topic": len(docs) - docs_with_topic,
            "topic_coverage_pct": round(docs_with_topic * 100 / len(docs), 1),
            "top_10_topics_by_doc_count": topic_summary,
        },
        "output_files": [
            "graph/folders.jsonl",
            "graph/folder_edges.jsonl",
            "graph/topics.jsonl",
            "graph/topic_edges.jsonl",
        ],
    }
    
    with open(LOG_DIR / "phase5_report.json", "w") as out:
        json.dump(report, out, indent=2, ensure_ascii=False)
    
    print("\n" + "=" * 60)
    print("Phase 5 Complete")
    print("=" * 60)
    print(f"  Folder nodes:           {len(folder_nodes)}")
    print(f"  Folder hierarchy edges: {len(hierarchy_edges)}")
    print(f"  belongs_to_folder edges: {len(folder_membership_edges)}")
    print(f"  Topic nodes:            {len(topic_nodes)}")
    print(f"  governed_by edges:     {len(topic_edges)}")
    print(f"  Folder coverage:       {docs_with_folder}/{len(docs)} ({docs_with_folder*100/len(docs):.1f}%)")
    print(f"  Topic coverage:        {docs_with_topic}/{len(docs)} ({docs_with_topic*100/len(docs):.1f}%)")
    print(f"  Report: output/logs/phase5_report.json")

if __name__ == "__main__":
    main()
