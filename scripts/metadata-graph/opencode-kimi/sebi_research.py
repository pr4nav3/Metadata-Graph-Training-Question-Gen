#!/usr/bin/env python3
"""Shell-friendly SEBI graph/Vespa research CLI for OpenCode/Kimi agents."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
REPO_ROOT = METADATA_GRAPH_DIR.parent.parent
SOURCE_FILES_DIR = REPO_ROOT / "SEBI-14K-share" / "source-files" / "files"

from sebi_retrieval import CorpusSearch, DataUnavailable, DocumentStore, GraphStore, default_graph_db  # noqa: E402


DEFAULT_GRAPH_DB = default_graph_db(METADATA_GRAPH_DIR)
DEFAULT_CACHE_DB = METADATA_GRAPH_DIR / "output" / "hydration" / "doc_cache.sqlite"
DEFAULT_VESPA_URL = (
    os.environ.get("METADATA_KG_VESPA_QUERY_URL")
    or os.environ.get("VESPA_QUERY_URL")
    or "http://localhost:18081/search/"
)


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def truncate(value: Any, limit: int) -> str:
    text = normalize_ws(value)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def query_text(value: Any) -> str:
    if isinstance(value, list):
        return normalize_ws(" ".join(str(item) for item in value))
    return normalize_ws(value)


def chunk_id(doc_id: str, index: int) -> str:
    return f"{doc_id}#{index}"


def cache_status(cache_db: Path, doc_id: str) -> str:
    if not cache_db.exists():
        return "missing_cache_db"
    try:
        conn = sqlite3.connect(cache_db)
        row = conn.execute(
            "SELECT status FROM hydrated_docs WHERE vespa_doc_id = ?",
            (doc_id,),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return "cache_error"
    return str(row[0]) if row else "not_cached"


def chunk_view(chunk: dict[str, Any], max_chars: int) -> dict[str, Any]:
    doc_id = str(chunk.get("doc_id") or "")
    index = int(chunk.get("index"))
    return {
        "chunk_id": chunk_id(doc_id, index),
        "cite": chunk_id(doc_id, index),
        "doc_id": doc_id,
        "index": index,
        "pages": chunk.get("pages") or [],
        "labels": chunk.get("labels") or [],
        "text": truncate(chunk.get("text"), max_chars),
    }


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def render_document(doc: dict[str, Any]) -> str:
    parts = [
        f"doc_id: {doc.get('doc_id') or doc.get('vespaDocId') or ''}",
        f"graph_id: {doc.get('graph_id') or ''}",
        f"title: {doc.get('title') or doc.get('label') or ''}",
    ]
    date = doc.get("date")
    if date:
        parts.append(f"date: {date}")
    section = " / ".join(
        part
        for part in [
            str(doc.get("top_section") or doc.get("topSection") or ""),
            str(doc.get("section") or ""),
            str(doc.get("subsection") or ""),
        ]
        if part
    )
    if section:
        parts.append(f"section: {section}")
    url = doc.get("detail_url") or doc.get("detailUrl")
    if url:
        parts.append(f"url: {url}")
    return "\n".join(parts)


def render_chunks(chunks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in chunks:
        pages = ",".join(str(page) for page in item.get("pages") or [])
        labels = ", ".join(str(label) for label in item.get("labels") or [])
        suffix = []
        if pages:
            suffix.append(f"pages={pages}")
        if labels:
            suffix.append(f"labels={labels}")
        suffix_text = f" ({'; '.join(suffix)})" if suffix else ""
        lines.append(f"\n[{item['chunk_id']}]{suffix_text}")
        lines.append(str(item.get("text") or ""))
    return "\n".join(lines).strip()


class ResearchContext:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.graph = GraphStore(args.graph_db)
        self.documents = DocumentStore(
            args.cache_db,
            self.graph,
            args.vespa_url,
            allow_live_fetch=not args.no_live_fetch,
        )
        self.corpus = CorpusSearch(args.vespa_url)

    def close(self) -> None:
        self.documents.close()
        self.graph.close()

    def resolve_doc(self, identifier: str) -> dict[str, Any]:
        return self.graph.document(identifier)

    def resolve_doc_id(self, identifier: str) -> str:
        try:
            doc = self.resolve_doc(identifier)
        except DataUnavailable:
            value = normalize_ws(identifier)
            if value.startswith("clf-"):
                return value
            raise
        doc_id = normalize_ws(doc.get("vespaDocId"))
        if not doc_id:
            raise DataUnavailable(f"document has no Vespa doc_id: {identifier}")
        return doc_id


def output(args: argparse.Namespace, payload: dict[str, Any], text: str) -> None:
    if args.json:
        emit_json(payload)
    else:
        max_total = int(getattr(args, "max_total_chars", 0) or 0)
        if max_total > 0 and len(text) > max_total:
            text = (
                text[: max(0, max_total - 120)].rstrip()
                + "\n\n[output clipped by --max-total-chars; rerun with narrower query, lower limits, or a higher cap if needed]"
            )
        print(text)


def command_doc_meta(ctx: ResearchContext, args: argparse.Namespace) -> None:
    try:
        doc = ctx.resolve_doc(args.identifier)
        doc_id = normalize_ws(doc.get("vespaDocId"))
        metadata = ctx.documents.metadata(doc_id) if doc_id else doc
    except DataUnavailable:
        doc_id = ctx.resolve_doc_id(args.identifier)
        ctx.documents.fields(doc_id)
        doc = {}
        metadata = ctx.documents.metadata(doc_id)
    payload = {"ok": True, "document": metadata, "graph": doc}
    output(args, payload, render_document(metadata))


def command_doc_overview(ctx: ResearchContext, args: argparse.Namespace) -> None:
    doc_id = ctx.resolve_doc_id(args.doc_id)
    before = cache_status(args.cache_db, doc_id)
    overview = ctx.documents.overview(doc_id, args.max_snippets, args.max_chars)
    after = cache_status(args.cache_db, doc_id)
    chunks = [chunk_view(chunk, args.max_chars) for chunk in overview["chunks"]]
    payload = {
        "ok": True,
        "doc_id": doc_id,
        "cache_before": before,
        "cache_after": after,
        "document": overview["document"],
        "total_chunks": overview["total_chunks"],
        "sampling": overview["sampling"],
        "chunks": chunks,
    }
    text = "\n".join(
        [
            render_document(overview["document"]),
            f"cache_before: {before}",
            f"cache_after: {after}",
            f"total_chunks: {overview['total_chunks']}",
            f"sampling: {overview['sampling']}",
            render_chunks(chunks),
        ]
    )
    output(args, payload, text)


def command_search_doc(ctx: ResearchContext, args: argparse.Namespace) -> None:
    doc_id = ctx.resolve_doc_id(args.doc_id)
    query = query_text(args.query)
    before = cache_status(args.cache_db, doc_id)
    chunks = [
        chunk_view(chunk, args.max_chars)
        for chunk in ctx.documents.search(doc_id, query, args.limit, args.max_chars)
    ]
    after = cache_status(args.cache_db, doc_id)
    metadata = ctx.documents.metadata(doc_id)
    payload = {
        "ok": True,
        "doc_id": doc_id,
        "query": query,
        "cache_before": before,
        "cache_after": after,
        "document": metadata,
        "chunks": chunks,
    }
    text = "\n".join(
        [
            render_document(metadata),
            f"query: {query}",
            f"cache_before: {before}",
            f"cache_after: {after}",
            f"hits: {len(chunks)}",
            render_chunks(chunks),
        ]
    )
    output(args, payload, text)


def command_chunks(ctx: ResearchContext, args: argparse.Namespace) -> None:
    doc_id = ctx.resolve_doc_id(args.doc_id)
    before = cache_status(args.cache_db, doc_id)
    chunks = [
        chunk_view(chunk, args.max_chars)
        for chunk in ctx.documents.chunks(doc_id, args.start, args.limit, args.max_chars)
    ]
    after = cache_status(args.cache_db, doc_id)
    metadata = ctx.documents.metadata(doc_id)
    payload = {
        "ok": True,
        "doc_id": doc_id,
        "start": args.start,
        "limit": args.limit,
        "cache_before": before,
        "cache_after": after,
        "document": metadata,
        "chunks": chunks,
    }
    text = "\n".join(
        [
            render_document(metadata),
            f"range: start={args.start} limit={args.limit}",
            f"cache_before: {before}",
            f"cache_after: {after}",
            render_chunks(chunks),
        ]
    )
    output(args, payload, text)


def command_around(ctx: ResearchContext, args: argparse.Namespace) -> None:
    doc_id = ctx.resolve_doc_id(args.doc_id)
    start = max(0, args.chunk_index - args.before)
    limit = args.before + args.after + 1
    before = cache_status(args.cache_db, doc_id)
    chunks = [
        chunk_view(chunk, args.max_chars)
        for chunk in ctx.documents.chunks(doc_id, start, limit, args.max_chars)
    ]
    after = cache_status(args.cache_db, doc_id)
    metadata = ctx.documents.metadata(doc_id)
    payload = {
        "ok": True,
        "doc_id": doc_id,
        "center": args.chunk_index,
        "before": args.before,
        "after": args.after,
        "cache_before": before,
        "cache_after": after,
        "document": metadata,
        "chunks": chunks,
    }
    text = "\n".join(
        [
            render_document(metadata),
            f"around: center={args.chunk_index} before={args.before} after={args.after}",
            f"cache_before: {before}",
            f"cache_after: {after}",
            render_chunks(chunks),
        ]
    )
    output(args, payload, text)


SEARCH_STOPWORDS = {
    "about",
    "after",
    "against",
    "also",
    "among",
    "before",
    "between",
    "circular",
    "document",
    "documents",
    "from",
    "into",
    "latest",
    "matter",
    "sebi",
    "that",
    "their",
    "this",
    "under",
    "what",
    "when",
    "where",
    "which",
    "with",
}


def metadata_fallback_search(
    ctx: ResearchContext,
    *,
    query: str,
    semantic_query: str,
    limit: int,
    exclude_doc_ids: list[str],
    reason: str,
) -> dict[str, Any]:
    terms = [
        term
        for term in dict.fromkeys(re.findall(r"[a-z0-9]+", f"{query} {semantic_query}".lower()))
        if len(term) >= 3 and term not in SEARCH_STOPWORDS
    ]
    excluded = set(exclude_doc_ids)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    phrase = query.lower()
    for doc in ctx.graph.source_documents():
        doc_id = normalize_ws(doc.get("vespaDocId"))
        if not doc_id or doc_id in excluded:
            continue
        title = normalize_ws(doc.get("title"))
        section = " ".join(
            normalize_ws(doc.get(key))
            for key in ("topSection", "section", "subsection")
            if normalize_ws(doc.get(key))
        )
        haystack = f"{title} {section} {doc.get('date') or ''}".lower()
        score = 0.0
        if phrase and phrase in haystack:
            score += 8.0
        for term in terms:
            if term in haystack:
                score += 1.0
                if term in title.lower():
                    score += 2.0
        if score:
            scored.append((score, normalize_ws(doc.get("date")), doc))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    hits: list[dict[str, Any]] = []
    for score, _, doc in scored[: max(1, min(int(limit), 8))]:
        section = " / ".join(
            part
            for part in [
                normalize_ws(doc.get("topSection")),
                normalize_ws(doc.get("section")),
                normalize_ws(doc.get("subsection")),
            ]
            if part
        )
        hits.append(
            {
                "doc_id": normalize_ws(doc.get("vespaDocId")),
                "title": normalize_ws(doc.get("title")),
                "date": normalize_ws(doc.get("date")),
                "section": section,
                "score": round(score, 3),
                "chunks": [],
            }
        )
    return {
        "query": query,
        "semantic_query": semantic_query,
        "total_count": len(scored),
        "hits": hits,
        "fallback": "graph_metadata_only",
        "fallback_reason": reason,
    }


def command_search_corpus(ctx: ResearchContext, args: argparse.Namespace) -> None:
    query = query_text(args.query)
    semantic_query = query_text(args.semantic or "")
    try:
        result = ctx.corpus.search(
            query,
            semantic_query=semantic_query,
            limit=args.limit,
            exclude_doc_ids=args.exclude_doc_id or (),
        )
    except DataUnavailable as exc:
        if semantic_query:
            try:
                result = ctx.corpus.search(
                    query,
                    semantic_query="",
                    limit=args.limit,
                    exclude_doc_ids=args.exclude_doc_id or (),
                )
                result["semantic_query"] = semantic_query
                result["fallback"] = "semantic_to_lexical"
                result["fallback_reason"] = str(exc)
            except DataUnavailable as lexical_exc:
                result = metadata_fallback_search(
                    ctx,
                    query=query,
                    semantic_query=semantic_query,
                    limit=args.limit,
                    exclude_doc_ids=args.exclude_doc_id or [],
                    reason=f"{exc}; lexical retry failed: {lexical_exc}",
                )
        else:
            result = metadata_fallback_search(
                ctx,
                query=query,
                semantic_query=semantic_query,
                limit=args.limit,
                exclude_doc_ids=args.exclude_doc_id or [],
                reason=str(exc),
            )
    hits: list[dict[str, Any]] = []
    chunks_per_hit = max(0, min(int(getattr(args, "chunks_per_hit", 2) or 0), 4))
    brief = bool(getattr(args, "brief", False))
    for hit in result["hits"]:
        doc_id = str(hit.get("doc_id") or "")
        try:
            metadata = ctx.documents.metadata(doc_id)
        except DataUnavailable:
            metadata = {}
        section = " / ".join(
            part
            for part in [
                str(metadata.get("top_section") or ""),
                str(metadata.get("section") or ""),
                str(metadata.get("subsection") or ""),
            ]
            if part
        )
        chunks = [
            {
                "chunk_id": chunk_id(doc_id, int(chunk.get("index"))),
                "cite": chunk_id(doc_id, int(chunk.get("index"))),
                "doc_id": doc_id,
                "index": int(chunk.get("index")),
                "text": truncate(chunk.get("text"), args.max_chars),
            }
            for chunk in (hit.get("chunks") or [])[:chunks_per_hit]
            if str(chunk.get("index")).isdigit()
        ]
        hits.append(
            {
                **hit,
                "title": metadata.get("title") or hit.get("title") or "",
                "date": metadata.get("date") or hit.get("date") or "",
                "section": section,
                "chunks": chunks,
            }
        )
    payload = {"ok": True, **result, "hits": hits}
    lines = [
        f"query: {result['query']}",
        f"semantic_query: {result.get('semantic_query') or ''}",
        f"total_count: {result.get('total_count')}",
        f"hits: {len(hits)}",
    ]
    if result.get("fallback"):
        lines.append(f"fallback: {result['fallback']}")
        lines.append(f"fallback_reason: {result.get('fallback_reason') or ''}")
        if result["fallback"] == "semantic_to_lexical":
            lines.append("fallback_note: semantic corpus search was unavailable; lexical corpus hits are shown.")
        else:
            lines.append("fallback_note: corpus search was unavailable; use doc-overview/search-doc on promising doc IDs.")
    for i, hit in enumerate(hits, 1):
        lines.append(f"\n## hit {i}: {hit['doc_id']}")
        lines.append(f"title: {hit.get('title') or ''}")
        if hit.get("date"):
            lines.append(f"date: {hit['date']}")
        if hit.get("section"):
            lines.append(f"section: {hit['section']}")
        lines.append(f"score: {hit.get('score')}")
        if brief:
            for chunk in hit.get("chunks") or []:
                lines.append(f"[{chunk['chunk_id']}] {chunk.get('text') or ''}")
        else:
            lines.append(render_chunks(hit.get("chunks") or []))
    output(args, payload, "\n".join(lines).strip())


def command_graph_neighbors(ctx: ResearchContext, args: argparse.Namespace) -> None:
    result = ctx.graph.neighbors(
        args.identifier,
        max_bridges=args.max_bridges,
        max_documents=args.max_documents,
    )
    for doc in result.get("linked_documents") or []:
        doc["doc_id"] = doc.get("vespaDocId") or ""
    payload = {"ok": True, **result}
    lines = ["# Source", render_document(result["source"]), "\n# Bridges"]
    for bridge in result.get("bridges") or []:
        lines.append(
            f"- {bridge.get('node_id')}: {bridge.get('type')} {bridge.get('label')} "
            f"(edge={bridge.get('edge_type')}, degree={bridge.get('degree')})"
        )
    lines.append("\n# Linked Documents")
    for doc in result.get("linked_documents") or []:
        lines.append(
            f"- {doc.get('doc_id') or doc.get('node_id')}: {doc.get('label')} "
            f"date={doc.get('date') or ''} via={doc.get('via_label') or doc.get('edge_type') or ''}"
        )
    lines.append(f"\n{result.get('note') or ''}")
    output(args, payload, "\n".join(lines).strip())


def attachment_paths(ctx: ResearchContext, graph_id: str) -> list[dict[str, Any]]:
    rows = ctx.graph.conn.execute(
        """
        SELECT n.id, n.attrs_json
        FROM edges e
        JOIN nodes n ON n.id = e.target
        WHERE e.source = ?
          AND e.type = 'has_attachment'
          AND n.type IN ('Attachment', 'AttachmentFile')
        ORDER BY n.id
        """,
        (graph_id,),
    ).fetchall()
    paths: list[dict[str, Any]] = []
    for row in rows:
        attrs = json.loads(row["attrs_json"])
        local_path = normalize_ws(attrs.get("localPath"))
        absolute = SOURCE_FILES_DIR / local_path if local_path else None
        paths.append(
            {
                "attachment_id": row["id"],
                "label": attrs.get("label") or "",
                "local_path": local_path,
                "absolute_path": str(absolute) if absolute else "",
                "exists": bool(absolute and absolute.exists()),
                "sha256": attrs.get("sha256") or "",
                "size_bytes": attrs.get("sizeBytes"),
            }
        )
    return paths


def command_pdf_path(ctx: ResearchContext, args: argparse.Namespace) -> None:
    doc = ctx.resolve_doc(args.identifier)
    graph_id = str(doc.get("graph_id") or "")
    paths = attachment_paths(ctx, graph_id) if graph_id else []
    payload = {
        "ok": True,
        "document": doc,
        "attachments": paths,
        "storage_path": doc.get("storagePath") or "",
        "note": "Raw PDFs are fallback exploration only. Final records should cite Vespa chunk IDs.",
    }
    lines = [render_document(ctx.documents.metadata(doc.get("vespaDocId")) if doc.get("vespaDocId") else doc)]
    lines.append("\nRaw PDFs are fallback exploration only. Final records should cite Vespa chunk IDs.")
    for item in paths:
        lines.append(
            f"\n- {item['absolute_path']}\n  exists: {item['exists']}\n  label: {item['label']}"
        )
    if doc.get("storagePath"):
        lines.append(f"\nstoragePath: {doc['storagePath']}")
    output(args, payload, "\n".join(lines))


def add_common(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Emit JSON instead of compact text.",
    )
    parser.add_argument(
        "--graph-db",
        type=Path,
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_GRAPH_DB,
    )
    parser.add_argument(
        "--cache-db",
        type=Path,
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_CACHE_DB,
    )
    parser.add_argument(
        "--vespa-url",
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_VESPA_URL,
    )
    parser.add_argument(
        "--no-live-fetch",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=argparse.SUPPRESS if suppress_defaults else 1600,
    )
    parser.add_argument(
        "--max-total-chars",
        type=int,
        default=argparse.SUPPRESS if suppress_defaults else 0,
        help="Approximate maximum text output size. Use 0 to disable clipping.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doc-meta", help="Show graph/document metadata.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("identifier")
    p.set_defaults(func=command_doc_meta)

    p = sub.add_parser("doc-overview", help="Read evenly spaced document snippets.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("doc_id")
    p.add_argument("--max-snippets", type=int, default=10)
    p.set_defaults(func=command_doc_overview)

    p = sub.add_parser("search-doc", help="Search inside one document.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("doc_id")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", "--max", dest="limit", type=int, default=6)
    p.set_defaults(func=command_search_doc)

    p = sub.add_parser("chunks", help="Read a contiguous chunk range.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("doc_id")
    p.add_argument("start", type=int)
    p.add_argument("limit", type=int)
    p.set_defaults(func=command_chunks)

    p = sub.add_parser("around", help="Read chunks around a chunk index.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("doc_id")
    p.add_argument("chunk_index", type=int)
    p.add_argument("--before", type=int, default=2)
    p.add_argument("--after", type=int, default=2)
    p.set_defaults(func=command_around)

    p = sub.add_parser("search-corpus", help="Search the SEBI corpus.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("query", nargs="+")
    p.add_argument("--semantic", default="")
    p.add_argument("--limit", "--max", dest="limit", type=int, default=8)
    p.add_argument("--chunks-per-hit", type=int, default=2)
    p.add_argument("--exclude-doc-id", action="append", default=[])
    p.set_defaults(func=command_search_corpus, brief=False, max_total_chars=12000)

    p = sub.add_parser(
        "search-corpus-brief",
        help="Compact corpus search for broad discovery; fetch exact chunks after choosing leads.",
        allow_abbrev=False,
    )
    add_common(p, suppress_defaults=True)
    p.add_argument("query", nargs="+")
    p.add_argument("--semantic", default="")
    p.add_argument("--limit", "--max", dest="limit", type=int, default=8)
    p.add_argument("--chunks-per-hit", type=int, default=1)
    p.add_argument("--exclude-doc-id", action="append", default=[])
    p.set_defaults(
        func=command_search_corpus,
        brief=True,
        max_chars=400,
        max_total_chars=7000,
    )

    p = sub.add_parser("graph-neighbors", help="Inspect graph leads around a node/document.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("identifier")
    p.add_argument("--max-bridges", type=int, default=10)
    p.add_argument("--max-documents", type=int, default=12)
    p.set_defaults(func=command_graph_neighbors)

    p = sub.add_parser("pdf-path", help="Show raw PDF fallback path for a document.", allow_abbrev=False)
    add_common(p, suppress_defaults=True)
    p.add_argument("identifier")
    p.set_defaults(func=command_pdf_path)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ctx: Optional[ResearchContext] = None
    try:
        ctx = ResearchContext(args)
        args.func(ctx, args)
        return 0
    except (DataUnavailable, sqlite3.Error, ValueError, KeyError) as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if getattr(args, "json", False):
            emit_json(payload)
        else:
            print(f"ERROR: {payload['error']}", file=sys.stderr)
        return 2
    finally:
        if ctx:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
