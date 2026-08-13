#!/usr/bin/env python3
"""Graph, document, and corpus-search adapters for OpenCode SEBI research."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "in", "information", "into", "is", "it", "of", "on", "or", "that", "the",
    "their", "this", "to", "under", "was", "were", "what", "when", "where",
    "which", "with",
}


class DataUnavailable(ValueError):
    """Raised when required graph or document data cannot be loaded."""


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def default_graph_db(metadata_graph_dir: Path) -> Path:
    candidates = [
        metadata_graph_dir / "output_v2" / "sebi_metadata_graph_v2.sqlite",
        metadata_graph_dir / "output" / "sebi_metadata_graph.sqlite",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def clean_text(value: Any, max_chars: int = 1800) -> str:
    text = re.sub(r"^\[Page \d+(?:-\d+)?\]\s*", "", str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = normalize_ws(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def coerce_chunks(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    chunks: list[str] = []
    for item in raw:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            chunks.append(str(item.get("chunk") or item.get("text") or ""))
        else:
            chunks.append("")
    return chunks


def chunk_metadata(raw: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(raw, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for fallback, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        value = item.get("chunk_index", fallback)
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        result[index] = item
    return result


def chunk_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("chunk") or value.get("text") or "")
    return ""


class GraphStore:
    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise DataUnavailable(f"metadata graph not found: {path}")
        self.path = path
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.documents_by_graph_id: dict[str, dict[str, Any]] = {}
        self.documents_by_vespa_id: dict[str, dict[str, Any]] = {}
        self._load_documents()

    def close(self) -> None:
        self.conn.close()

    def _load_documents(self) -> None:
        rows = self.conn.execute(
            "SELECT id, attrs_json FROM nodes WHERE type = 'Document'"
        ).fetchall()
        for row in rows:
            attrs = json.loads(row["attrs_json"])
            attrs["graph_id"] = row["id"]
            self.documents_by_graph_id[row["id"]] = attrs
            vespa_id = normalize_ws(attrs.get("vespaDocId"))
            if vespa_id:
                self.documents_by_vespa_id[vespa_id] = attrs
        self._load_v2_attachment_documents()

    def _load_v2_attachment_documents(self) -> None:
        """Map v2 AttachmentFile/VespaItem records back to their parent document.

        Graph v2 intentionally keeps scrape-page documents and individual PDF
        attachments separate. The Kimi/OpenCode tooling works in Vespa doc IDs,
        so this adapter exposes a document-like view for each attachment-level
        Vespa item without requiring the graph itself to collapse those files
        into a single parent-document `vespaDocId`.
        """
        try:
            rows = self.conn.execute(
                """
                SELECT
                    d.id AS doc_graph_id,
                    d.attrs_json AS doc_attrs_json,
                    a.id AS attachment_graph_id,
                    a.attrs_json AS attachment_attrs_json,
                    v.attrs_json AS vespa_attrs_json,
                    e.attrs_json AS edge_attrs_json
                FROM edges ha
                JOIN nodes d ON d.id = ha.source
                JOIN nodes a ON a.id = ha.target
                JOIN edges e ON e.source = a.id AND e.type = 'vespa_item'
                JOIN nodes v ON v.id = e.target
                WHERE ha.type = 'has_attachment'
                  AND d.type = 'Document'
                  AND a.type IN ('Attachment', 'AttachmentFile')
                  AND v.type = 'VespaItem'
                """
            ).fetchall()
        except sqlite3.Error:
            return

        for row in rows:
            doc_attrs = json.loads(row["doc_attrs_json"])
            attachment_attrs = json.loads(row["attachment_attrs_json"])
            vespa_attrs = json.loads(row["vespa_attrs_json"])
            edge_attrs = json.loads(row["edge_attrs_json"])
            vespa_id = normalize_ws(vespa_attrs.get("docId") or attachment_attrs.get("selectedVespaDocId"))
            if not vespa_id:
                continue
            doc_attrs["graph_id"] = row["doc_graph_id"]
            parent = self.documents_by_graph_id.setdefault(row["doc_graph_id"], doc_attrs)
            parent_ids = list(parent.get("vespaDocIds") or [])
            if vespa_id not in parent_ids:
                parent_ids.append(vespa_id)
            parent["vespaDocIds"] = parent_ids
            if not normalize_ws(parent.get("primaryVespaDocId")) or edge_attrs.get("selected"):
                parent["primaryVespaDocId"] = vespa_id
                parent["vespaDocId"] = parent["primaryVespaDocId"]
            elif not normalize_ws(parent.get("vespaDocId")):
                parent["vespaDocId"] = parent["primaryVespaDocId"]
            view = {
                **doc_attrs,
                "graph_id": row["doc_graph_id"],
                "attachment_graph_id": row["attachment_graph_id"],
                "attachmentId": attachment_attrs.get("attachmentId"),
                "attachmentIdx": attachment_attrs.get("idx"),
                "attachmentLabel": attachment_attrs.get("label") or "",
                "attachmentLocalPath": attachment_attrs.get("localPath") or "",
                "attachmentBasename": attachment_attrs.get("basename") or "",
                "vespaDocId": vespa_id,
                "documentId": vespa_attrs.get("documentId") or doc_attrs.get("documentId") or "",
                "selectedVespaItem": bool(edge_attrs.get("selected")),
            }
            existing = self.documents_by_vespa_id.get(vespa_id)
            if not existing or view["selectedVespaItem"]:
                self.documents_by_vespa_id[vespa_id] = view

    def document(self, identifier: str) -> dict[str, Any]:
        doc = self.documents_by_graph_id.get(identifier) or self.documents_by_vespa_id.get(identifier)
        if not doc:
            raise DataUnavailable(f"graph document not found: {identifier}")
        return dict(doc)

    def source_documents(self) -> list[dict[str, Any]]:
        docs: dict[str, dict[str, Any]] = {}
        for doc in self.documents_by_graph_id.values():
            vespa_id = normalize_ws(doc.get("vespaDocId"))
            if vespa_id:
                docs[vespa_id] = dict(doc)
        for vespa_id, doc in self.documents_by_vespa_id.items():
            docs[vespa_id] = dict(doc)
        return list(docs.values())

    def node(self, node_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT id, type, attrs_json FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if not row:
            raise DataUnavailable(f"graph node not found: {node_id}")
        attrs = json.loads(row["attrs_json"])
        return self._node_view(row["id"], row["type"], attrs)

    @staticmethod
    def _node_view(node_id: str, node_type: str, attrs: dict[str, Any]) -> dict[str, Any]:
        label = normalize_ws(
            attrs.get("name")
            or attrs.get("title")
            or attrs.get("canonicalId")
            or attrs.get("canonical")
            or attrs.get("path")
            or node_id
        )
        view: dict[str, Any] = {"node_id": node_id, "type": node_type, "label": label}
        for key in ("kind", "date", "topSection", "section", "subsection", "vespaDocId"):
            value = attrs.get(key)
            if value not in (None, "", []):
                view[key] = value
        return view

    def _decorate_document_view(self, view: dict[str, Any], graph_id: str) -> dict[str, Any]:
        doc = self.documents_by_graph_id.get(graph_id)
        view["graph_id"] = graph_id
        if not doc:
            return view
        for key in ("vespaDocId", "primaryVespaDocId", "vespaDocIds", "date", "topSection", "section", "subsection"):
            value = doc.get(key)
            if value not in (None, "", []):
                view[key] = value
        return view

    def neighbors(
        self,
        identifier: str,
        *,
        max_bridges: int = 12,
        max_documents: int = 20,
    ) -> dict[str, Any]:
        try:
            source_doc = self.document(identifier)
            node_id = str(source_doc["graph_id"])
        except DataUnavailable:
            node_id = identifier
            source_doc = None
        source = self.node(node_id)
        if source_doc:
            source = self._decorate_document_view(source, node_id)
        rows = self.conn.execute(
            """
            SELECT e.source, e.target, e.type,
                   n.id AS neighbor_id, n.type AS neighbor_type, n.attrs_json
            FROM edges e
            JOIN nodes n ON n.id = CASE WHEN e.source = ? THEN e.target ELSE e.source END
            WHERE e.source = ? OR e.target = ?
            """,
            (node_id, node_id, node_id),
        ).fetchall()

        direct_docs: list[dict[str, Any]] = []
        bridges: list[dict[str, Any]] = []
        for row in rows:
            attrs = json.loads(row["attrs_json"])
            view = self._node_view(row["neighbor_id"], row["neighbor_type"], attrs)
            view["edge_type"] = row["type"]
            if row["neighbor_type"] == "Document":
                view = self._decorate_document_view(view, row["neighbor_id"])
                if row["neighbor_id"] != node_id:
                    direct_docs.append(view)
                continue
            degree = self.conn.execute(
                "SELECT count(*) FROM edges WHERE source = ? OR target = ?",
                (row["neighbor_id"], row["neighbor_id"]),
            ).fetchone()[0]
            view["degree"] = int(degree)
            bridges.append(view)

        bridges.sort(key=lambda item: (item.get("degree", 0), item.get("type", ""), item.get("label", "")))
        bridges = bridges[: max(1, max_bridges)]

        linked_docs: list[dict[str, Any]] = []
        seen = {node_id}
        for bridge in bridges:
            candidate_rows = self.conn.execute(
                """
                SELECT n.id, n.type, n.attrs_json, e.type AS edge_type
                FROM edges e
                JOIN nodes n ON n.id = CASE WHEN e.source = ? THEN e.target ELSE e.source END
                WHERE (e.source = ? OR e.target = ?) AND n.type = 'Document'
                ORDER BY n.date DESC, n.title
                LIMIT ?
                """,
                (
                    bridge["node_id"],
                    bridge["node_id"],
                    bridge["node_id"],
                    max_documents,
                ),
            ).fetchall()
            for row in candidate_rows:
                if row["id"] in seen:
                    continue
                attrs = json.loads(row["attrs_json"])
                view = self._node_view(row["id"], row["type"], attrs)
                view = self._decorate_document_view(view, row["id"])
                view["via_node_id"] = bridge["node_id"]
                view["via_label"] = bridge["label"]
                view["via_type"] = bridge["type"]
                view["edge_type"] = row["edge_type"]
                linked_docs.append(view)
                seen.add(row["id"])
                if len(linked_docs) >= max_documents:
                    break
            if len(linked_docs) >= max_documents:
                break

        for item in direct_docs:
            if item["node_id"] not in seen and len(linked_docs) < max_documents:
                linked_docs.append(item)
                seen.add(item["node_id"])
        return {
            "source": source,
            "bridges": bridges,
            "linked_documents": linked_docs,
            "note": "High-degree bridges may be broad. A graph link is a lead, not evidence.",
        }


class DocumentStore:
    def __init__(
        self,
        cache_path: Path,
        graph: GraphStore,
        vespa_query_url: str,
        *,
        allow_live_fetch: bool = True,
    ) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path = cache_path
        self.conn = sqlite3.connect(cache_path)
        self.graph = graph
        self.vespa_query_url = vespa_query_url.rstrip("/") + "/"
        self.allow_live_fetch = allow_live_fetch
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hydrated_docs (
                vespa_doc_id TEXT PRIMARY KEY,
                fetched_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                fields_json TEXT,
                error TEXT
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def cached_ids(self) -> set[str]:
        return {
            str(row[0])
            for row in self.conn.execute(
                "SELECT vespa_doc_id FROM hydrated_docs WHERE status = 'ok'"
            )
        }

    def fields(self, doc_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT status, fields_json, error FROM hydrated_docs WHERE vespa_doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row and row[0] == "ok" and row[1]:
            fields = json.loads(row[1])
            if coerce_chunks(fields.get("chunks_summary")):
                return fields
        if not self.allow_live_fetch:
            raise DataUnavailable(f"document is not available in cache: {doc_id}")
        result = self._fetch_live(doc_id)
        self._write_cache(doc_id, result.get("status", "error"), result.get("fields") or {}, result.get("error"))
        if result.get("status") != "ok":
            raise DataUnavailable(f"Vespa could not hydrate {doc_id}: {result.get('status')}")
        return result["fields"]

    def _fetch_live(self, doc_id: str) -> dict[str, Any]:
        safe_id = doc_id.replace("\\", "\\\\").replace('"', '\\"')
        yql = (
            "select docId,title,fileName,document_id,document_date,referenced_ids,pan_ids,"
            f"chunks_summary,chunks_map from kb_items where docId contains \"{safe_id}\" limit 1"
        )
        url = self.vespa_query_url + "?yql=" + urllib.parse.quote(yql) + "&hits=1"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            children = data.get("root", {}).get("children", [])
            if not children:
                return {"status": "no_hit", "fields": {}}
            fields = children[0].get("fields") or {}
            if normalize_ws(fields.get("docId")) != doc_id:
                return {
                    "status": "error",
                    "fields": {},
                    "error": "Vespa lookup returned a different document ID",
                }
            return {"status": "ok", "fields": fields}
        except Exception as exc:
            return {"status": "error", "fields": {}, "error": f"{type(exc).__name__}: {exc}"}

    def _write_cache(
        self,
        doc_id: str,
        status: str,
        fields: dict[str, Any],
        error: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO hydrated_docs
                (vespa_doc_id, fetched_at, status, fields_json, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (doc_id, int(time.time()), status, json.dumps(fields, ensure_ascii=False), error),
        )
        self.conn.commit()

    def metadata(self, doc_id: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            graph_doc = self.graph.document(doc_id)
        except DataUnavailable:
            graph_doc = {}
        if fields is None:
            row = self.conn.execute(
                "SELECT fields_json FROM hydrated_docs WHERE vespa_doc_id = ? AND status = 'ok'",
                (doc_id,),
            ).fetchone()
            fields = json.loads(row[0]) if row and row[0] else {}
        title = normalize_ws(
            graph_doc.get("title") or fields.get("title") or fields.get("fileName") or doc_id
        )
        return {
            "doc_id": doc_id,
            "graph_id": graph_doc.get("graph_id"),
            "title": title,
            "date": graph_doc.get("date") or fields.get("document_date") or "",
            "top_section": graph_doc.get("topSection") or "",
            "section": graph_doc.get("section") or "",
            "subsection": graph_doc.get("subsection") or "",
            "detail_url": graph_doc.get("detailUrl") or "",
        }

    def chunk(self, doc_id: str, index: int, max_chars: int = 1800) -> dict[str, Any]:
        fields = self.fields(doc_id)
        return self._chunk_from_fields(doc_id, index, fields, max_chars)

    @staticmethod
    def _chunk_from_fields(
        doc_id: str,
        index: int,
        fields: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        chunks = coerce_chunks(fields.get("chunks_summary"))
        if not 0 <= index < len(chunks):
            raise DataUnavailable(f"chunk {index} is outside {doc_id} (total={len(chunks)})")
        meta = chunk_metadata(fields.get("chunks_map")).get(index, {})
        return {
            "doc_id": doc_id,
            "index": index,
            "text": clean_text(chunks[index], max_chars),
            "pages": list(meta.get("page_numbers") or []),
            "labels": list(meta.get("block_labels") or []),
        }

    def chunks(
        self,
        doc_id: str,
        start: int,
        limit: int,
        max_chars: int = 1800,
    ) -> list[dict[str, Any]]:
        fields = self.fields(doc_id)
        total = len(coerce_chunks(fields.get("chunks_summary")))
        if total == 0:
            raise DataUnavailable(f"document has no chunks: {doc_id}")
        start = max(0, int(start))
        if start >= total:
            raise DataUnavailable(f"start chunk {start} is outside {doc_id} (total={total})")
        limit = max(1, min(int(limit), 20))
        return [
            self._chunk_from_fields(doc_id, index, fields, max_chars)
            for index in range(start, min(start + limit, total))
        ]

    def overview(
        self,
        doc_id: str,
        max_snippets: int = 10,
        max_chars: int = 900,
    ) -> dict[str, Any]:
        fields = self.fields(doc_id)
        chunks = coerce_chunks(fields.get("chunks_summary"))
        if not chunks:
            raise DataUnavailable(f"document has no chunks: {doc_id}")
        count = max(2, min(int(max_snippets), 14, len(chunks)))
        if len(chunks) <= count:
            indices = list(range(len(chunks)))
        else:
            indices = sorted({round(i * (len(chunks) - 1) / (count - 1)) for i in range(count)})
        return {
            "document": self.metadata(doc_id, fields),
            "total_chunks": len(chunks),
            "sampling": "evenly_spaced_including_opening_and_end",
            "chunks": [
                self._chunk_from_fields(doc_id, index, fields, max_chars)
                for index in indices
            ],
        }

    def search(self, doc_id: str, query: str, limit: int = 6, max_chars: int = 1800) -> list[dict[str, Any]]:
        fields = self.fields(doc_id)
        chunks = coerce_chunks(fields.get("chunks_summary"))
        tokens = [
            token for token in dict.fromkeys(re.findall(r"[a-z0-9]+", query.lower()))
            if len(token) >= 3 and token not in STOPWORDS
        ]
        if not tokens:
            return []
        normalized = [clean_text(chunk, max(len(chunk), 1)).lower() for chunk in chunks]
        frequencies = {
            token: sum(1 for text in normalized if re.search(rf"\b{re.escape(token)}\b", text))
            for token in tokens
        }
        phrase = normalize_ws(query).lower()
        scored: list[tuple[float, int]] = []
        for index, text in enumerate(normalized):
            score = 0.0
            for token in tokens:
                count = len(re.findall(rf"\b{re.escape(token)}\b", text))
                if count:
                    score += math.log((len(chunks) + 1) / (frequencies[token] + 1)) + 1.0
                    score += min(count, 3) * 0.25
            if phrase and phrase in text:
                score += 5.0
            if score:
                scored.append((score, index))
        indices = [index for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))]
        return [
            self._chunk_from_fields(doc_id, index, fields, max_chars)
            for index in indices[: max(1, min(limit, 10))]
        ]


class CorpusSearch:
    def __init__(self, vespa_query_url: str) -> None:
        self.url = vespa_query_url.rstrip("/") + "/"

    def search(
        self,
        query: str,
        *,
        semantic_query: str = "",
        limit: int = 10,
        exclude_doc_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        query = normalize_ws(query)[:200]
        semantic_query = normalize_ws(semantic_query)[:500]
        if len(query) < 2:
            raise DataUnavailable("corpus search query is too short")
        limit = max(1, min(int(limit), 8))
        excluded = set(exclude_doc_ids)
        requested_hits = min(20, limit + len(excluded))
        alpha = 0.3 if semantic_query else 0.0
        if alpha:
            where = "(userInput(@query) or ({targetHits:200}nearestNeighbor(chunk_embeddings, e)))"
        else:
            where = "userInput(@query)"
        body: dict[str, Any] = {
            "yql": f"select * from kb_items where {where}",
            "query": query,
            "hits": requested_hits,
            "timeout": "30s",
            "ranking.profile": "default_native_dynamic_chunks_file_v6_rsf_vec",
            "input.query(alpha)": alpha,
            "presentation.summary": "lean",
            "input.query(summary_chunks)": 3,
        }
        if alpha:
            body["e5_query"] = (
                "Instruct: Given a question, retrieve relevant SEBI / legal document passages "
                f"that answer it\nQuery: {semantic_query}"
            )
            body["input.query(e)"] = "embed(@e5_query)"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise DataUnavailable(f"corpus search failed: {type(exc).__name__}: {exc}") from exc

        hits: list[dict[str, Any]] = []
        for child in data.get("root", {}).get("children", []) or []:
            fields = child.get("fields") or {}
            doc_id = normalize_ws(fields.get("docId"))
            if not doc_id or doc_id in excluded:
                continue
            chunks = self._hit_chunks(fields)
            hits.append(
                {
                    "doc_id": doc_id,
                    "title": normalize_ws(fields.get("title") or fields.get("fileName") or doc_id),
                    "date": fields.get("document_date") or "",
                    "score": float(child.get("relevance") or 0.0),
                    "chunks": chunks,
                }
            )
            if len(hits) >= limit:
                break
        total = data.get("root", {}).get("fields", {}).get("totalCount")
        return {"query": query, "semantic_query": semantic_query, "total_count": total, "hits": hits}

    @staticmethod
    def _hit_chunks(fields: dict[str, Any]) -> list[dict[str, Any]]:
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
        indices = [index for _, index in ranked_indices[:2]]
        summary = fields.get("top_chunks_summary")
        position_cells = (
            ((fields.get("summaryfeatures") or {}).get("best_summary_chunks") or {}).get("cells") or {}
        )
        positions = sorted(int(index) for index in position_cells if str(index).isdigit())
        chunks: list[dict[str, Any]] = []
        if isinstance(summary, list) and positions:
            by_index = {index: summary[pos] for pos, index in enumerate(positions) if pos < len(summary)}
            use_indices = indices or positions[:2]
            for index in use_indices:
                if index in by_index:
                    chunks.append({"index": index, "text": clean_text(chunk_text(by_index[index]))})
            return chunks

        raw_summary = fields.get("chunks_summary")
        raw_positions = fields.get("chunks_pos_summary")
        if isinstance(raw_summary, list):
            if isinstance(raw_positions, list):
                by_index = {
                    int(index): raw_summary[pos]
                    for pos, index in enumerate(raw_positions)
                    if pos < len(raw_summary) and isinstance(index, int)
                }
            else:
                by_index = {index: value for index, value in enumerate(raw_summary)}
            use_indices = indices or list(by_index)[:2]
            for index in use_indices:
                if index in by_index:
                    chunks.append({"index": index, "text": clean_text(chunk_text(by_index[index]))})
        return chunks
