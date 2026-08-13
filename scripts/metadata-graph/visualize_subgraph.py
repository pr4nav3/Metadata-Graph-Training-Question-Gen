#!/usr/bin/env python3
"""
Generate a self-contained interactive HTML visualization of a sampled subgraph.

Reads scripts/metadata-graph/output/sebi_metadata_graph.pkl and writes
scripts/metadata-graph/output/visualization/subgraph_sample.html.

This script is read-only w.r.t. existing graph artifacts and does not modify
any other code or output files.

Sampling strategy:
- Pick a representative Document node.
- Draw only the seed document + its directly connected neighbors.
- Keep all non-entity neighbors (folder, topic, taxonomy, attachment, regulation).
- Keep only the top-N most important entity neighbors.
- Drop the vast majority of entity-to-entity co-mention edges to avoid a hairball.
"""

import html
import json
import pickle
from collections import Counter
from pathlib import Path

import networkx as nx

SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = SCRIPT_DIR / "output"
PKL_PATH = OUTPUT_DIR / "sebi_metadata_graph.pkl"
VIZ_DIR = OUTPUT_DIR / "visualization"
HTML_PATH = VIZ_DIR / "subgraph_sample.html"

MAX_ENTITIES = 25
MAX_EXTRA_CO_MENTIONS = 20
TARGET_DEGREE_MIN = 12
TARGET_DEGREE_MAX = 80

NODE_TYPE_COLORS = {
    "Document": "#4C78A8",
    "Attachment": "#F58518",
    "TaxonomyNode": "#E45756",
    "CanonicalEntity": "#72B7B2",
    "RegulationRef": "#54A24B",
    "Topic": "#Eeca3b",
    "Folder": "#B279A2",
}
EDGE_TYPE_COLORS = {
    "instance_of": "#999999",
    "has_attachment": "#BBBBBB",
    "mentions": "#72B7B2",
    "cites": "#54A24B",
    "co_mentioned_with": "#B279A2",
    "belongs_to_folder": "#E45756",
    "child_of": "#888888",
    "governed_by": "#Eeca3b",
}
DEFAULT_NODE_COLOR = "#AAAAAA"
DEFAULT_EDGE_COLOR = "#CCCCCC"

NON_ENTITY_TYPES = {"Attachment", "TaxonomyNode", "RegulationRef", "Topic", "Folder"}


def load_graph(path: Path) -> nx.MultiDiGraph:
    print(f"Loading graph from {path} ...")
    with open(path, "rb") as f:
        G = pickle.load(f)
    print(f"  {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    return G


def choose_seed_document(G: nx.MultiDiGraph) -> str:
    """Pick a Document node with a moderately rich but not overwhelming neighborhood."""
    document_nodes = [
        n for n, attrs in G.nodes(data=True) if attrs.get("type") == "Document"
    ]
    print(f"Found {len(document_nodes):,} Document nodes")

    candidates = []
    for n in document_nodes:
        deg = G.degree(n)
        if TARGET_DEGREE_MIN <= deg <= TARGET_DEGREE_MAX:
            candidates.append(n)

    if not candidates:
        degrees = sorted([(n, G.degree(n)) for n in document_nodes], key=lambda x: x[1])
        mid = len(degrees) // 2
        candidates = [n for n, _ in degrees[mid - 50 : mid + 50]]
        print("No document in ideal degree range; falling back to median-degree set")

    # Prefer documents connected to a mix of types, especially entities + regulations.
    def richness_score(node: str) -> float:
        neigh_types = Counter(G.nodes[m].get("type", "Unknown") for m in G.neighbors(node))
        return (
            len(neigh_types) * 5
            + neigh_types.get("CanonicalEntity", 0) * 0.5
            + neigh_types.get("RegulationRef", 0) * 0.3
        )

    candidates.sort(key=richness_score, reverse=True)
    seed = candidates[0]
    neigh_types = Counter(G.nodes[m].get("type", "Unknown") for m in G.neighbors(seed))
    print(f"Selected seed {seed}: degree={G.degree(seed)}, types={dict(neigh_types)}")
    return seed


def sample_subgraph(G: nx.MultiDiGraph, seed: str) -> nx.MultiDiGraph:
    """
    Build a readable star-like subgraph around the seed document.

    - Keep all non-entity neighbors (folder, topic, taxonomy, attachment, regulation).
    - Keep only the top MAX_ENTITIES entity neighbors by degree.
    - Add a tiny sample of entity-to-entity co-mention edges among kept entities.
    - Suppress other neighbor-to-neighbor edges to avoid hairballs.
    """
    neighbors = list(G.neighbors(seed))
    print(f"Seed has {len(neighbors)} direct neighbors")

    by_type: dict[str, list[str]] = {}
    for n in neighbors:
        by_type.setdefault(G.nodes[n].get("type", "Unknown"), []).append(n)

    kept_nodes = {seed}

    # Keep every non-entity neighbor.
    for t in NON_ENTITY_TYPES:
        kept_nodes.update(by_type.get(t, []))

    # Keep only the top entities by degree (proxy for importance).
    entities = by_type.get("CanonicalEntity", [])
    entities.sort(key=lambda n: G.degree(n), reverse=True)
    kept_entities = set(entities[:MAX_ENTITIES])
    kept_nodes.update(kept_entities)

    print(
        f"Keeping {len(kept_nodes)} nodes: "
        f"{len(kept_entities)} entities + "
        f"{sum(len(by_type.get(t, [])) for t in NON_ENTITY_TYPES)} non-entity neighbors"
    )

    # Build edge set.
    kept_edges = []

    # 1. All edges from seed to kept neighbors.
    for u, v, key, attrs in G.out_edges(seed, data=True, keys=True):
        if v in kept_nodes:
            kept_edges.append((u, v, key, attrs))
    for u, v, key, attrs in G.in_edges(seed, data=True, keys=True):
        if u in kept_nodes:
            kept_edges.append((u, v, key, attrs))

    # 2. A small sample of co-mention edges among kept entities.
    co_mentions = []
    for u, v, key, attrs in G.edges(kept_entities, data=True, keys=True):
        if u in kept_entities and v in kept_entities:
            etype = attrs.get("type", key)
            if etype == "co_mentioned_with":
                co_mentions.append((u, v, key, attrs))

    # Sort by combined degree of the pair to keep the most "important" links.
    co_mentions.sort(key=lambda e: G.degree(e[0]) + G.degree(e[1]), reverse=True)
    kept_edges.extend(co_mentions[:MAX_EXTRA_CO_MENTIONS])

    # Construct a fresh MultiDiGraph with only the chosen nodes/edges.
    sub = nx.MultiDiGraph()
    for n in kept_nodes:
        sub.add_node(n, **G.nodes[n])
    for u, v, key, attrs in kept_edges:
        sub.add_edge(u, v, key=key, **attrs)

    print(f"Sampled subgraph: {sub.number_of_nodes()} nodes, {sub.number_of_edges()} edges")
    return sub


def node_label_and_title(attrs: dict) -> tuple[str, str]:
    ntype = attrs.get("type", "Unknown")
    if ntype == "Document":
        label = attrs.get("title") or attrs.get("name") or attrs.get("id", "")
    elif ntype == "CanonicalEntity":
        label = attrs.get("name") or attrs.get("id", "")
    elif ntype == "RegulationRef":
        label = attrs.get("canonicalId") or attrs.get("id", "")
    elif ntype in ("Topic", "Folder", "TaxonomyNode"):
        label = attrs.get("name") or attrs.get("id", "")
    elif ntype == "Attachment":
        label = attrs.get("label") or attrs.get("originalFilename") or attrs.get("id", "")
    else:
        label = attrs.get("name") or attrs.get("id", "")

    label = str(label).strip()
    if len(label) > 40:
        label = label[:37] + "..."

    tooltip_lines = [f"<b>{html.escape(ntype)}</b>", html.escape(str(attrs.get("id", "")))]
    for key in ("name", "title", "canonicalId", "path", "date", "kind", "collection"):
        val = attrs.get(key)
        if val:
            tooltip_lines.append(f"{key}: {html.escape(str(val))}")
    title = "<br>".join(tooltip_lines)
    return label, title


def build_vis_dataset(sub: nx.MultiDiGraph) -> tuple[list[dict], list[dict]]:
    nodes = []
    for n, attrs in sub.nodes(data=True):
        ntype = attrs.get("type", "Unknown")
        label, title = node_label_and_title(attrs)
        nodes.append(
            {
                "id": n,
                "label": label,
                "title": title,
                "group": ntype,
                "color": NODE_TYPE_COLORS.get(ntype, DEFAULT_NODE_COLOR),
            }
        )

    seen_edges = set()
    edges = []
    for u, v, key, attrs in sub.edges(data=True, keys=True):
        etype = attrs.get("type", key)
        edge_id = (u, v, etype)
        if edge_id in seen_edges:
            continue
        seen_edges.add(edge_id)
        edges.append(
            {
                "from": u,
                "to": v,
                "label": str(etype),
                "title": html.escape(str(etype)),
                "color": {"color": EDGE_TYPE_COLORS.get(str(etype), DEFAULT_EDGE_COLOR)},
                "arrows": "to",
            }
        )
    return nodes, edges


def render_html(nodes: list[dict], edges: list[dict], seed: str) -> str:
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    edges_json = json.dumps(edges, ensure_ascii=False)
    legend_items = "".join(
        f'<span style="display:inline-block;margin-right:12px;"><span style="display:inline-block;width:12px;height:12px;background:{color};margin-right:4px;border-radius:2px;"></span>{html.escape(ntype)}</span>'
        for ntype, color in NODE_TYPE_COLORS.items()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SEBI Metadata Graph — Sample Subgraph</title>
  <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
    #header {{ padding: 12px 16px; background: #f7f7f7; border-bottom: 1px solid #ddd; }}
    #header h1 {{ margin: 0 0 6px; font-size: 16px; }}
    #legend {{ font-size: 12px; line-height: 1.6; }}
    #container {{ width: 100vw; height: calc(100vh - 90px); background: #fafafa; }}
    #status {{ position: fixed; bottom: 12px; right: 12px; padding: 6px 10px; background: rgba(255,255,255,0.9); border: 1px solid #ddd; border-radius: 4px; font-size: 12px; color: #555; }}
  </style>
</head>
<body>
  <div id="header">
    <h1>SEBI Metadata Graph — Sample Subgraph (seed: {html.escape(seed)})</h1>
    <div id="legend">{legend_items}</div>
  </div>
  <div id="container"></div>
  <div id="status">Layout stabilizing...</div>
  <script type="text/javascript">
    const nodes = new vis.DataSet({nodes_json});
    const edges = new vis.DataSet({edges_json});
    const container = document.getElementById('container');
    const statusEl = document.getElementById('status');
    const data = {{ nodes: nodes, edges: edges }};
    const options = {{
      nodes: {{
        shape: 'dot',
        size: 14,
        font: {{ size: 12, face: 'sans-serif', strokeWidth: 3, strokeColor: '#ffffff' }},
        borderWidth: 1,
        shadow: true
      }},
      edges: {{
        width: 1.5,
        smooth: {{ type: 'continuous' }},
        font: {{ size: 9, align: 'middle', strokeWidth: 2, strokeColor: '#ffffff' }},
        arrows: {{ to: {{ scaleFactor: 0.5 }} }}
      }},
      physics: {{
        enabled: true,
        barnesHut: {{
          gravitationalConstant: -3000,
          centralGravity: 0.3,
          springLength: 95,
          springConstant: 0.04,
          damping: 0.09,
          avoidOverlap: 0.2
        }},
        stabilization: {{
          enabled: true,
          iterations: 2000,
          updateInterval: 50,
          onlyDynamicEdges: false,
          fit: true
        }},
        adaptiveTimestep: true,
        minVelocity: 0.75
      }},
      interaction: {{ hover: true, tooltipDelay: 100, hideEdgesOnDrag: true }},
      layout: {{ randomSeed: 42 }}
    }};
    const network = new vis.Network(container, data, options);

    network.on("stabilizationIterationsDone", function () {{
      statusEl.textContent = "Layout stable (physics frozen)";
      network.setOptions({{ physics: {{ enabled: false }} }});
    }});

    network.on("stabilizationProgress", function(params) {{
      statusEl.textContent = "Layout stabilizing... " + Math.round(params.iterations / params.total * 100) + "%";
    }});
  </script>
</body>
</html>
"""


def main():
    if not PKL_PATH.exists():
        print(f"ERROR: Graph pickle not found at {PKL_PATH}")
        print("Run persist_graph.py first to generate the graph.")
        return 1

    VIZ_DIR.mkdir(parents=True, exist_ok=True)
    G = load_graph(PKL_PATH)

    if G.number_of_nodes() == 0:
        print("Graph is empty; nothing to visualize.")
        return 1

    seed = choose_seed_document(G)
    sub = sample_subgraph(G, seed)
    nodes, edges = build_vis_dataset(sub)

    html_content = render_html(nodes, edges, seed)
    HTML_PATH.write_text(html_content, encoding="utf-8")
    print(f"Wrote {HTML_PATH} ({len(html_content):,} bytes)")
    print(f"  Nodes: {len(nodes)}, Edges: {len(edges)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
