#!/usr/bin/env python3
"""
Build SEBI metadata graph v2.

This is an audit-first replacement for the older phased graph builder. It keeps
the old graph untouched and writes a second graph under:

  scripts/metadata-graph/graph_v2/
  scripts/metadata-graph/output_v2/

Core design choices:
  * keep scrape Document and attachment/PDF identities separate
  * join every attachment independently
  * only choose active, SEBI, completed Postgres rows as primary joins
  * preserve raw Vespa reference/PAN occurrences on VespaItem nodes
  * canonicalize identifiers before resolving citations
  * write validation gates that fail loudly on source row loss

Usage:
  python3 scripts/metadata-graph/build_metadata_graph_v2.py
  python3 scripts/metadata-graph/build_metadata_graph_v2.py --pg-csv /tmp/pg.csv --vespa-jsonl /tmp/vespa.jsonl
  python3 scripts/metadata-graph/build_metadata_graph_v2.py --no-fetch --pg-csv ... --vespa-jsonl ...
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import pickle
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

try:
    import networkx as nx
except ImportError:  # pragma: no cover - handled in main validation.
    nx = None


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SCRAPE_DB = PROJECT_ROOT / "SEBI-14K-share/source-files/files/scrape_state.db"
GRAPH_DIR = SCRIPT_DIR / "graph_v2"
OUTPUT_DIR = SCRIPT_DIR / "output_v2"
LOG_DIR = OUTPUT_DIR / "logs"
STAGING_DB = OUTPUT_DIR / "metadata_graph_v2_staging.sqlite"
GRAPH_SQLITE = OUTPUT_DIR / "sebi_metadata_graph_v2.sqlite"
GRAPH_PICKLE = OUTPUT_DIR / "sebi_metadata_graph_v2.pkl"
DEFAULT_HYDRATION_CACHE_DB = SCRIPT_DIR / "output" / "hydration" / "doc_cache.sqlite"
DEFAULT_VESPA_OWNER_CANDIDATES_JSONL: Path | None = None

SEBI_COLLECTIONS = {"Enforcements", "Filings", "Legal", "Media", "Reports"}
NON_DOCUMENT_REFERENCE_KINDS = {"legal_provision", "legal_reference_text"}
STRICT_FIRST_CHUNK_OWNER_KINDS = {
    "adjudication_order_id",
    "circular_id",
    "order_id",
    "quasi_judicial_order_id",
    "sebi_identifier",
    "whole_time_member_order_id",
}
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


PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
RC_RE = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"(?:Recovery[\s._-]+(?:Certificate|Proceedings)"
    r"(?:[\s._-]+(?:No\.?|Number|under))?[\s._-]*(?:RC[\s._-]*)?)"
    r"|(?:Certificate[\s._-]+(?:No\.?|Number)?[\s._-]*(?:RC[\s._-]*)?)"
    r"|(?:R\.?[\s._-]*C\.?[\s._-]*(?:No\.?)?[\s._-]*)"
    r"|(?:RC[\s._-]*(?:No\.?)?[\s._-]*)"
    r")"
    r"(\d{1,6})[\s._-]*(?:of|/)[\s._-]*(\d{4})(?!\d)",
    re.I,
)
ORDER_ID_RE = re.compile(r"\b(?:Order|WTM|QJA|NRO|AO)/[A-Z0-9][A-Z0-9./_-]+", re.I)
SEBI_ID_RE = re.compile(r"\bSEBI/[A-Z0-9][A-Z0-9./_-]+", re.I)
CIR_ID_RE = re.compile(r"\bCIR/[A-Z0-9][A-Z0-9./_-]+", re.I)
LEGAL_PROVISION_RE = re.compile(
    r"\b(section|regulation|rule)\s+"
    r"([0-9]+[A-Z]*(?:\([A-Za-z0-9ivxlcdmIVXLCDM.-]+\))*)\s+of\s+"
    r"(?:the\s+)?([^.;,\n]+?(?:Act|Regulations?|Rules?)(?:,\s*\d{4})?)",
    re.I,
)
LEGAL_REFERENCE_MARKER_RE = re.compile(
    r"\b(?:section|regulation|rule|sub[-\s]?section|act|regulations?|rules?|schedule)\b",
    re.I,
)
FILENAME_SEBI_DOC_ID_RE = re.compile(r"_(\d{4,7})(?:-\d+)?\.pdf$", re.I)
COMPANY_SUFFIX_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.'(),/-]*(?:\s+[A-Z][A-Za-z0-9&.'(),/-]*){0,12}\s+"
    r"(?:Limited|Ltd|Pvt\.?\s*Ltd|Private Limited|LLP|Bank|Corporation|Corp\.?))",
    re.I,
)
IN_MATTER_RE = re.compile(
    r"in the matter of\s+(.+?)(?:\s+under\s+|\s+for\s+|\s+issued\s+|\s+\(|\s+\[|\.?$)",
    re.I,
)
AGAINST_RE = re.compile(
    r"\bagainst\s+(.+?)(?:\s+in the matter of\s+|\s+\(PAN\b|\s+\[PAN\b|\.?$)",
    re.I,
)
RECEIVED_FROM_RE = re.compile(
    r"(?:received from|application received from|request received from)\s+(.+?)"
    r"(?:\s+with\s+respect|\s+in\s+relation|\s+related\s+to|\s+seeking|\s+under\s+|\s+for\s+|\s*$)",
    re.I,
)

RECOVERY_EVENT_PATTERNS = [
    ("notice_of_demand", re.compile(r"\bnotice of demand\b", re.I)),
    ("notice_of_attachment", re.compile(r"\bnotice of attachment\b|\battachment order\b", re.I)),
    ("release_order", re.compile(r"\brelease order\b", re.I)),
    ("completion_order", re.compile(r"\bcompletion (?:order|certificate)|\bcompletion of recovery", re.I)),
    ("remittance_order", re.compile(r"\bremittance order\b|\bgeneral remittance\b", re.I)),
    ("cancellation", re.compile(r"\bcancellation\b", re.I)),
    ("sale_certificate", re.compile(r"\bsale certificate\b", re.I)),
    ("auction_notice", re.compile(r"\bauction\b", re.I)),
    ("recovery_certificate", re.compile(r"\brecovery certificate\b|\bcertificate no\.?\s*rc", re.I)),
]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha1_short(value: str, length: int = 16) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_")
    return safe or sha1_short(str(value))


def normalize_ws(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_name(value: str | None) -> str:
    s = normalize_ws(value)
    s = s.strip(" .,:;\"'")
    s = re.sub(r"\s+", " ", s)
    s = s.replace("Pvt. Ltd.", "Pvt Ltd").replace("Pvt.Ltd.", "Pvt Ltd")
    return s


def normalize_legal_instrument_name(value: str | None) -> str:
    text = normalize_ws(value)
    text = text.strip(" .,:;\"'")
    text = re.sub(r"^(?:the\s+)+", "", text, flags=re.I)
    text = re.sub(r"\bIncome\s*[- ]\s*tax\b", "Income-tax", text, flags=re.I)
    text = re.sub(r"\bSEBI\s+Act(?:,\s*1992)?\b", "SEBI Act, 1992", text, flags=re.I)
    text = re.sub(r"\bCompanies\s+Act(?:,\s*2013)?\b", "Companies Act, 2013", text, flags=re.I)
    return normalize_ws(text)


def legal_instrument_kind(value: str) -> str:
    if re.search(r"\bAct\b", value, re.I):
        return "act"
    if re.search(r"\bRegulations?\b", value, re.I):
        return "regulations"
    if re.search(r"\bRules?\b", value, re.I):
        return "rules"
    return "legal_instrument"


def parse_legal_provision(value: Any) -> dict[str, str] | None:
    raw = normalize_ws(str(value or ""))
    match = LEGAL_PROVISION_RE.search(raw)
    if not match:
        return None
    provision_type = match.group(1).capitalize()
    provision_number = re.sub(r"\s+", "", match.group(2).upper())
    instrument_raw = normalize_ws(match.group(3)).rstrip(" .")
    instrument = normalize_legal_instrument_name(instrument_raw)
    canonical = f"{provision_type} {provision_number} of {instrument}"
    return {
        "provisionType": provision_type,
        "provisionNumber": provision_number,
        "instrument": instrument,
        "instrumentRaw": instrument_raw,
        "canonical": canonical,
    }


def is_legal_reference_text(value: Any) -> bool:
    text = normalize_ws(str(value or ""))
    if not text:
        return False
    return bool(LEGAL_REFERENCE_MARKER_RE.search(text))


def node_id_for_identifier(kind: str, canonical: str) -> str:
    return f"ident_{kind}_{sha1_short(kind.lower() + '::' + canonical.lower())}"


def node_id_for_entity(kind: str, canonical: str) -> str:
    return f"entity_{kind}_{sha1_short(kind.lower() + '::' + canonical.lower())}"


def node_id_for_legal_instrument(canonical: str) -> str:
    return f"legal_instrument_{sha1_short(canonical.lower(), 16)}"


def node_id_for_legal_provision(canonical: str) -> str:
    return f"legal_provision_{sha1_short(canonical.lower(), 16)}"


def node_id_for_legal_reference(canonical: str) -> str:
    return f"legal_reference_{sha1_short(canonical.lower(), 16)}"


@dataclass(frozen=True)
class Identifier:
    kind: str
    canonical: str
    raw: str

    @property
    def id(self) -> str:
        return node_id_for_identifier(self.kind, self.canonical)


class GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.edge_seen: set[tuple[str, str, str, str]] = set()
        self.edge_counter = 0

    def add_node(self, node_id: str, node_type: str, **attrs: Any) -> dict[str, Any]:
        existing = self.nodes.get(node_id)
        if existing is None:
            node = {"id": node_id, "type": node_type, **attrs}
            self.nodes[node_id] = node
            return node

        for key, value in attrs.items():
            if value in (None, "", [], {}):
                continue
            old = existing.get(key)
            if old in (None, "", [], {}):
                existing[key] = value
            elif key == "aliases":
                merged = set(old or [])
                merged.update(value or [])
                existing[key] = sorted(merged)
            elif key == "sources":
                merged = set(old or [])
                merged.update(value or [])
                existing[key] = sorted(merged)
            elif key == "occurrenceCount" and isinstance(value, int):
                existing[key] = int(old or 0) + value
        return existing

    def add_edge(
        self,
        edge_type: str,
        source: str,
        target: str,
        unique_key: str | None = None,
        **attrs: Any,
    ) -> dict[str, Any] | None:
        if unique_key is not None:
            key = (edge_type, source, target, unique_key)
            if key in self.edge_seen:
                return None
            self.edge_seen.add(key)
            edge_id = f"edge_{edge_type}_{sha1_short('::'.join(key), 20)}"
        else:
            self.edge_counter += 1
            edge_id = f"edge_{self.edge_counter:09d}"
        edge = {
            "id": edge_id,
            "type": edge_type,
            "source": source,
            "target": target,
            **attrs,
        }
        self.edges.append(edge)
        return edge


def normalize_identifier(raw_value: Any, hint: str | None = None) -> Identifier | None:
    raw = normalize_ws(str(raw_value or ""))
    if not raw:
        return None

    pan = PAN_RE.search(raw.upper())
    if hint == "pan" and pan:
        return Identifier("pan", pan.group(0).upper(), raw)
    if hint == "sebi_document_id" and re.fullmatch(r"\d{4,7}", raw):
        return Identifier("sebi_document_id", raw, raw)

    rc = RC_RE.search(raw)
    if rc:
        canonical = f"RC {int(rc.group(1))} of {rc.group(2)}"
        return Identifier("recovery_certificate", canonical, raw)

    order = ORDER_ID_RE.search(raw)
    if order:
        canonical = re.sub(r"[^A-Za-z0-9/._-]+", "", order.group(0)).upper()
        if canonical.startswith("ORDER/"):
            kind = "order_id"
        elif canonical.startswith("WTM/"):
            kind = "whole_time_member_order_id"
        elif canonical.startswith("QJA/"):
            kind = "quasi_judicial_order_id"
        elif canonical.startswith("AO/"):
            kind = "adjudication_order_id"
        else:
            kind = "order_id"
        return Identifier(kind, canonical, raw)

    sebi_id = SEBI_ID_RE.search(raw)
    if sebi_id:
        canonical = re.sub(r"[^A-Za-z0-9/._-]+", "", sebi_id.group(0)).upper()
        kind = "circular_id" if "/CIR/" in canonical or "/P/CIR/" in canonical else "sebi_identifier"
        return Identifier(kind, canonical, raw)

    cir_id = CIR_ID_RE.search(raw)
    if cir_id:
        canonical = re.sub(r"[^A-Za-z0-9/._-]+", "", cir_id.group(0)).upper()
        return Identifier("circular_id", canonical, raw)

    legal = parse_legal_provision(raw)
    if legal:
        return Identifier("legal_provision", legal["canonical"], raw)

    if re.fullmatch(r"\d{4,7}", raw):
        return Identifier("sebi_document_id", raw, raw)

    if hint == "reference_text" and is_legal_reference_text(raw):
        return Identifier("legal_reference_text", raw, raw)
    if hint == "reference_text":
        return Identifier("reference_text", raw, raw)
    return None


def identifiers_from_text(text: str | None, include_reference_text: bool = False) -> list[Identifier]:
    text = text or ""
    out: list[Identifier] = []

    for match in RC_RE.finditer(text):
        ident = normalize_identifier(match.group(0))
        if ident:
            out.append(ident)
    for match in PAN_RE.finditer(text.upper()):
        ident = normalize_identifier(match.group(0), "pan")
        if ident:
            out.append(ident)
    for regex in (ORDER_ID_RE, SEBI_ID_RE, CIR_ID_RE, LEGAL_PROVISION_RE):
        for match in regex.finditer(text):
            ident = normalize_identifier(match.group(0))
            if ident:
                out.append(ident)
    if include_reference_text:
        ident = normalize_identifier(text, "reference_text")
        if ident:
            out.append(ident)

    seen: set[tuple[str, str]] = set()
    deduped: list[Identifier] = []
    for ident in out:
        key = (ident.kind, ident.canonical.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(ident)
    return deduped


def compact_identifier_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def identifier_compact_forms(ident: Identifier) -> set[str]:
    values = {compact_identifier_text(ident.canonical)}
    if ident.kind == "recovery_certificate":
        match = re.search(r"\bRC\s+(\d+)\s+of\s+(\d{4})\b", ident.canonical, re.I)
        if match:
            number, year = match.groups()
            values.add(compact_identifier_text(f"Recovery Certificate No. {number} of {year}"))
            values.add(compact_identifier_text(f"Certificate No. {number} of {year}"))
            values.add(compact_identifier_text(f"RC{number} of {year}"))
    return {value for value in values if value}


def text_contains_identifier_form(value: Any, ident: Identifier) -> bool:
    haystack = compact_identifier_text(value)
    return bool(haystack and any(needle in haystack for needle in identifier_compact_forms(ident)))


def text_contains_identifier_form_near_front(value: Any, ident: Identifier, max_chars: int = 350) -> bool:
    return text_contains_identifier_form(str(value or "")[:max_chars], ident)


def load_vespa_owner_candidates(paths: Path | list[Path] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if paths is None:
        return [], {"enabled": False, "paths": [], "records": 0, "acceptedHits": 0}
    if isinstance(paths, Path):
        candidate_paths = [paths]
    else:
        candidate_paths = list(paths)
    report: dict[str, Any] = {
        "enabled": True,
        "paths": [str(path) for path in candidate_paths],
        "files": [],
        "records": 0,
        "acceptedHits": 0,
        "loadErrors": [],
    }

    records: list[dict[str, Any]] = []
    for path in candidate_paths:
        file_report = {"path": str(path), "exists": path.exists(), "records": 0, "acceptedHits": 0}
        report["files"].append(file_report)
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                report["loadErrors"].append({"path": str(path), "line": line_number, "error": str(exc)})
                continue
            if not isinstance(record, dict):
                report["loadErrors"].append({"path": str(path), "line": line_number, "error": "record is not an object"})
                continue
            records.append(record)
            accepted_hits = len(record.get("acceptedOwnerCandidates") or [])
            file_report["records"] += 1
            file_report["acceptedHits"] += accepted_hits
            report["acceptedHits"] += accepted_hits
    report["records"] = len(records)
    return records, report


def candidate_hit_evidence_text(hit: dict[str, Any]) -> str:
    chunks = hit.get("chunks") or []
    chunk_texts = [
        normalize_ws(str(chunk.get("text") or ""))
        for chunk in chunks
        if isinstance(chunk, dict)
    ]
    return normalize_ws(" ".join(chunk_texts))[:1200]


def candidate_hit_chunk_index(hit: dict[str, Any]) -> int | None:
    chunks = hit.get("chunks") or []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        try:
            return int(chunk.get("index"))
        except (TypeError, ValueError):
            continue
    return None


def vespa_owner_candidate_is_valid(record: dict[str, Any], hit: dict[str, Any], ident: Identifier) -> tuple[bool, str]:
    if record.get("identifierId") and record.get("identifierId") != ident.id:
        return False, "identifier_id_mismatch"
    if normalize_ws(record.get("canonical")) != ident.canonical:
        return False, "canonical_mismatch"
    if record.get("kind") != ident.kind:
        return False, "kind_mismatch"
    if hit.get("reason") != "identity_field_or_opening_chunk_exact_match":
        return False, "non_owner_reason"
    if not hit.get("graphDocId"):
        return False, "missing_graph_doc_id"

    signals = hit.get("signals") or {}
    if not signals.get("compatibleKind"):
        return False, "incompatible_document_kind"
    identity_field_match = any(
        signals.get(key)
        for key in ("titleMatch", "fileNameMatch", "documentIdMatch")
    )
    target_text = f"{hit.get('title') or ''} {hit.get('fileName') or ''}"
    master_circular_context = bool(
        re.search(r"(?:^|[^A-Za-z0-9])master[-_\s]+circular(?:$|[^A-Za-z0-9])", target_text, re.I)
    )
    if ident.kind in {"circular_id", "sebi_identifier"}:
        if master_circular_context and not identity_field_match:
            return False, "circular_match_inside_master_circular_context"
        chunk_match = bool(signals.get("firstChunkFrontMatch"))
    else:
        chunk_match = bool(signals.get("firstChunkMatch")) if ident.kind in STRICT_FIRST_CHUNK_OWNER_KINDS else bool(signals.get("earlyChunkMatch"))
    if not identity_field_match and not chunk_match:
        return False, "no_strict_identity_signal"

    evidence_text = " ".join(
        [
            str(hit.get("title") or ""),
            str(hit.get("fileName") or ""),
            candidate_hit_evidence_text(hit),
        ]
    )
    if not text_contains_identifier_form(evidence_text, ident):
        return False, "evidence_text_missing_identifier"
    if ident.kind in {"circular_id", "sebi_identifier"} and not identity_field_match:
        if not text_contains_identifier_form_near_front(candidate_hit_evidence_text(hit), ident):
            return False, "circular_identifier_not_in_header_front"
    return True, "strict_owner_identity"


def identifiers_from_filename(filename: str | None) -> list[Identifier]:
    basename = os.path.basename(filename or "")
    out = identifiers_from_text(basename)
    match = FILENAME_SEBI_DOC_ID_RE.search(basename)
    if match:
        ident = normalize_identifier(match.group(1), "sebi_document_id")
        if ident:
            out.append(ident)
    seen: set[tuple[str, str]] = set()
    deduped: list[Identifier] = []
    for ident in out:
        key = (ident.kind, ident.canonical.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(ident)
    return deduped


def run_identifier_self_checks() -> None:
    expected_rc = {
        "Recovery Certificate No. 8540 of 2025": "RC 8540 of 2025",
        "Certificate No. 8540 of 2025_ Notice of Demand": "RC 8540 of 2025",
        "Certificate No. RC9024 of 2026_A.P. Nos. 15178 & 15179": "RC 9024 of 2026",
        "RC9083 of 2026": "RC 9083 of 2026",
        "Recovery Certificate no. RC8370 of 2024": "RC 8370 of 2024",
        "R.C.No.1799 of 2018 drawn against Nilesh Palande": "RC 1799 of 2018",
        "Recovery Proceedings under 5476 of 2022 against Arun Mantex Limited": "RC 5476 of 2022",
        "General Remittance Order in Certificate No RC 7782 of 2024_AP Nos 12130": "RC 7782 of 2024",
    }
    for raw, expected in expected_rc.items():
        ident = normalize_identifier(raw)
        if not ident or ident.kind != "recovery_certificate" or ident.canonical != expected:
            raise AssertionError(f"RC identifier self-check failed for {raw!r}: {ident}")

    false_positive_cases = [
        "Order passed in SEBI Spl Case 448 of 2014",
        "AP Nos 15178 and 15179 of 2026",
        "No. 60/2018",
    ]
    for raw in false_positive_cases:
        ident = normalize_identifier(raw)
        if ident and ident.kind == "recovery_certificate":
            raise AssertionError(f"RC identifier false positive for {raw!r}: {ident}")

    expected_legal = {
        "Section 115AD of the IT Act": "Section 115AD of IT Act",
        "section 115AD(1)(ii) of the IT Act": "Section 115AD(1)(II) of IT Act",
        "Section 28A of the SEBI Act": "Section 28A of SEBI Act, 1992",
        "Regulation 2(1)(l) of SEBI (SAST) Regulations, 2011": "Regulation 2(1)(L) of SEBI (SAST) Regulations, 2011",
    }
    for raw, expected in expected_legal.items():
        ident = normalize_identifier(raw)
        if not ident or ident.kind != "legal_provision" or ident.canonical != expected:
            raise AssertionError(f"legal identifier self-check failed for {raw!r}: {ident}")

    legal_text = normalize_identifier("Second Schedule to the Income-tax Act, 1961", "reference_text")
    if not legal_text or legal_text.kind != "legal_reference_text":
        raise AssertionError(f"legal reference-text self-check failed: {legal_text}")


def recovery_event_type(title: str | None) -> str | None:
    title = title or ""
    for event_type, regex in RECOVERY_EVENT_PATTERNS:
        if regex.search(title):
            return event_type
    return None


def extract_title_entities(title: str | None, top_section: str | None) -> list[dict[str, str]]:
    title = normalize_ws(title)
    if not title:
        return []

    candidates: list[dict[str, str]] = []
    for regex, default_kind in (
        (IN_MATTER_RE, "company"),
        (AGAINST_RE, "person"),
        (RECEIVED_FROM_RE, "company"),
    ):
        match = regex.search(title)
        if match:
            raw = normalize_name(match.group(1))
            raw = re.split(r"\s+\bPAN\b|\s+\bin respect of\b", raw, maxsplit=1, flags=re.I)[0]
            raw = normalize_name(raw)
            if len(raw) >= 3:
                kind = "company" if re.search(r"\b(?:Limited|Ltd|Private|Pvt|LLP|Bank|Corp)", raw, re.I) else default_kind
                candidates.append({"kind": kind, "name": raw, "raw": match.group(1)})

    for match in COMPANY_SUFFIX_RE.finditer(title):
        raw = normalize_name(match.group(1))
        if len(raw) >= 3:
            candidates.append({"kind": "company", "name": raw, "raw": match.group(1)})

    if top_section == "Filings" and len(title) >= 3:
        # Many filing titles are only the issuer name plus offer-document type.
        issuer = re.sub(
            r"\s*[-:]\s*(?:RHP|DRHP|Prospectus|Letter of Offer|Final Offer Document|SME Prospectus).*$",
            "",
            title,
            flags=re.I,
        )
        issuer = normalize_name(issuer)
        if len(issuer) >= 3 and not issuer.lower().startswith(("20", "draft ", "final ")):
            candidates.append({"kind": "company", "name": issuer, "raw": title})

    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for candidate in candidates:
        canonical = normalize_name(candidate["name"])
        lower = canonical.lower()
        if lower in {"sebi", "securities and exchange board of india", "the sebi"}:
            continue
        key = (candidate["kind"], lower)
        if key not in seen:
            seen.add(key)
            out.append({"kind": candidate["kind"], "name": canonical, "raw": candidate["raw"]})
    return out


def query_scrape_db() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    conn = sqlite3.connect(SCRAPE_DB)
    conn.row_factory = sqlite3.Row
    docs = [dict(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]
    atts = [dict(r) for r in conn.execute("SELECT * FROM attachments ORDER BY doc_id, idx, id")]
    conn.close()
    baselines = {
        "scrape_documents_total": len(docs),
        "scrape_documents_resolved": sum(1 for d in docs if d.get("status") == "resolved"),
        "scrape_attachments_total": len(atts),
        "scrape_attachments_downloaded": sum(1 for a in atts if a.get("status") == "downloaded"),
    }
    return docs, atts, baselines


def chunk_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("chunk") or value.get("text") or "")
    return ""


def first_list_items(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def identity_text_from_vespa_fields(fields: dict[str, Any], fallback_file_name: str = "") -> str:
    """Return conservative text likely to identify the document itself.

    This deliberately uses titles, headings, and opening heading lines, not the
    whole body. A document body may cite many other circulars/orders; treating
    every body identifier as the document's own identifier would create false
    doc-to-doc links.
    """
    parts: list[str] = []
    for value in (
        fields.get("title"),
        fields.get("fileName"),
        fields.get("document_title"),
        fallback_file_name,
    ):
        text = normalize_ws(str(value or ""))
        if text:
            parts.append(text)

    for item in first_list_items(fields.get("chunks_map"), 3):
        if not isinstance(item, dict):
            continue
        for heading in first_list_items(item.get("headings"), 8):
            text = normalize_ws(str(heading or ""))
            if text:
                parts.append(text)
        for label in first_list_items(item.get("block_labels"), 12):
            text = normalize_ws(str(label or ""))
            if text.lower().startswith("heading:"):
                parts.append(normalize_ws(text.split(":", 1)[1]))

    for chunk in first_list_items(fields.get("chunks_summary"), 3):
        lines = chunk_text(chunk).splitlines()
        heading_lines = []
        for line in lines[:40]:
            stripped = line.strip()
            if stripped.startswith("#"):
                heading_lines.append(stripped.lstrip("#").strip())
            elif heading_lines and not stripped:
                break
        parts.extend(heading_lines[:10])

    seen: set[str] = set()
    deduped: list[str] = []
    for part in parts:
        text = normalize_ws(part)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            deduped.append(text)
    return "\n".join(deduped)[:20000]


def load_hydrated_fields(path: Path | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path:
        return {}, {"enabled": False, "path": "", "ok_rows": 0, "usable_identity_rows": 0}
    if not path.exists():
        return {}, {
            "enabled": True,
            "path": str(path),
            "exists": False,
            "ok_rows": 0,
            "usable_identity_rows": 0,
        }
    conn = sqlite3.connect(path)
    rows = conn.execute(
        """
        SELECT vespa_doc_id, fields_json
        FROM hydrated_docs
        WHERE status = 'ok' AND fields_json IS NOT NULL AND fields_json != ''
        """
    ).fetchall()
    conn.close()
    fields_by_doc_id: dict[str, dict[str, Any]] = {}
    for doc_id, raw_json in rows:
        try:
            fields = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(fields, dict):
            continue
        actual_doc_id = normalize_ws(fields.get("docId")) or normalize_ws(doc_id)
        if not actual_doc_id:
            continue
        fields_by_doc_id[actual_doc_id] = fields
    usable = sum(
        1
        for fields in fields_by_doc_id.values()
        if identity_text_from_vespa_fields(fields)
    )
    return fields_by_doc_id, {
        "enabled": True,
        "path": str(path),
        "exists": True,
        "ok_rows": len(rows),
        "loaded_rows": len(fields_by_doc_id),
        "usable_identity_rows": usable,
    }


def fetch_postgres_rows() -> list[dict[str, Any]]:
    query = """
        SELECT
            ci.id::text,
            ci.collection_id::text,
            ci.parent_id::text,
            ci.name,
            ci.type,
            ci.vespa_doc_id,
            ci.original_name,
            ci.storage_path,
            ci.file_size::text,
            ci.checksum,
            ci.upload_status,
            ci.deleted_at::text,
            c.name AS collection_name,
            c.deleted_at::text AS collection_deleted_at
        FROM collection_items ci
        JOIN collections c ON c.id = ci.collection_id
        ORDER BY ci.id
    """
    postgres_container = (
        os.environ.get("METADATA_KG_POSTGRES_CONTAINER")
        or os.environ.get("XYNE_POSTGRES_CONTAINER")
        or "metadata-kg-xyne-db"
    )
    cmd = [
        "docker",
        "exec",
        postgres_container,
        "psql",
        "-U",
        "xyne",
        "-d",
        "xyne",
        "-c",
        f"COPY ({query}) TO STDOUT WITH CSV HEADER",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"Postgres export failed: {result.stderr.strip()}")
    return list(csv.DictReader(io.StringIO(result.stdout)))


def load_postgres_rows(path: Path | None) -> list[dict[str, Any]]:
    if path:
        with path.open(newline="") as f:
            return list(csv.DictReader(f))
    return fetch_postgres_rows()


def visit_vespa() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    continuation = None
    batches = 0

    while True:
        url = VESPA_VISIT_URL
        if continuation:
            url += "&continuation=" + urllib.parse.quote(continuation)
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        docs = data.get("documents") or []
        for item in docs:
            fields = item.get("fields") or {}
            rows.append(
                {
                    "docId": fields.get("docId"),
                    "fileName": fields.get("fileName"),
                    "entity": fields.get("entity"),
                    "document_id": fields.get("document_id"),
                    "document_date": fields.get("document_date"),
                    "referenced_ids": fields.get("referenced_ids"),
                    "pan_ids": fields.get("pan_ids"),
                    "identity_text": identity_text_from_vespa_fields(fields),
                    "fields_count": len(fields),
                }
            )

        batches += 1
        continuation = data.get("continuation")
        if not continuation or not docs:
            break
        if batches % 10 == 0:
            print(f"    Vespa visit batch {batches}: {len(rows)} rows")
    return rows


def load_vespa_rows(path: Path | None) -> list[dict[str, Any]]:
    if path:
        with path.open() as f:
            return [json.loads(line) for line in f if line.strip()]
    return visit_vespa()


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_staging_db(
    scrape_docs: list[dict[str, Any]],
    scrape_atts: list[dict[str, Any]],
    pg_rows: list[dict[str, Any]],
    vespa_rows: list[dict[str, Any]],
    join_decisions: list[dict[str, Any]],
    hydrated_fields_by_doc_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    hydrated_fields_by_doc_id = hydrated_fields_by_doc_id or {}
    if STAGING_DB.exists():
        STAGING_DB.unlink()
    conn = sqlite3.connect(STAGING_DB)
    conn.execute("CREATE TABLE scrape_documents (id INTEGER PRIMARY KEY, status TEXT, attrs_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE scrape_attachments (id INTEGER PRIMARY KEY, doc_id INTEGER, status TEXT, attrs_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE postgres_collection_items (id TEXT PRIMARY KEY, type TEXT, collection_name TEXT, upload_status TEXT, deleted_at TEXT, attrs_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE vespa_items (doc_id TEXT PRIMARY KEY, basename TEXT, attrs_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE vespa_identity_texts (doc_id TEXT PRIMARY KEY, source TEXT, chars INTEGER, text TEXT)")
    conn.execute("CREATE TABLE join_decisions (attachment_id INTEGER PRIMARY KEY, scrape_doc_id INTEGER, basename TEXT, status TEXT, attrs_json TEXT NOT NULL)")

    for doc in scrape_docs:
        conn.execute(
            "INSERT INTO scrape_documents (id, status, attrs_json) VALUES (?, ?, ?)",
            (doc["id"], doc.get("status"), json_dumps(doc)),
        )
    for att in scrape_atts:
        conn.execute(
            "INSERT INTO scrape_attachments (id, doc_id, status, attrs_json) VALUES (?, ?, ?, ?)",
            (att["id"], att.get("doc_id"), att.get("status"), json_dumps(att)),
        )
    for row in pg_rows:
        conn.execute(
            "INSERT OR REPLACE INTO postgres_collection_items (id, type, collection_name, upload_status, deleted_at, attrs_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.get("id"),
                row.get("type"),
                row.get("collection_name"),
                row.get("upload_status"),
                row.get("deleted_at"),
                json_dumps(row),
            ),
        )
    for row in vespa_rows:
        doc_id = row.get("docId")
        if not doc_id:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO vespa_items (doc_id, basename, attrs_json) VALUES (?, ?, ?)",
            (doc_id, os.path.basename(row.get("fileName") or ""), json_dumps(row)),
        )
        identity_text = normalize_ws(str(row.get("identity_text") or ""))
        source = "vespa_metadata"
        hydrated_fields = hydrated_fields_by_doc_id.get(doc_id)
        if hydrated_fields:
            hydrated_identity_text = identity_text_from_vespa_fields(hydrated_fields, row.get("fileName") or "")
            if hydrated_identity_text:
                identity_text = hydrated_identity_text
                source = "hydration_cache"
        if identity_text:
            conn.execute(
                "INSERT OR REPLACE INTO vespa_identity_texts (doc_id, source, chars, text) VALUES (?, ?, ?, ?)",
                (doc_id, source, len(identity_text), identity_text),
            )
    for row in join_decisions:
        conn.execute(
            "INSERT INTO join_decisions (attachment_id, scrape_doc_id, basename, status, attrs_json) VALUES (?, ?, ?, ?, ?)",
            (
                row["attachmentId"],
                row["scrapeDocId"],
                row["basename"],
                row["status"],
                json_dumps(row),
            ),
        )
    conn.execute("CREATE INDEX idx_stage_att_doc ON scrape_attachments(doc_id)")
    conn.execute("CREATE INDEX idx_stage_vespa_basename ON vespa_items(basename)")
    conn.execute("CREATE INDEX idx_stage_join_status ON join_decisions(status)")
    conn.commit()
    conn.close()


def build_join_decisions(
    downloaded_atts: list[dict[str, Any]],
    pg_rows: list[dict[str, Any]],
    vespa_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    active_sebi_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    completed_active_sebi_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pg_by_vespa: dict[str, dict[str, Any]] = {}

    for row in pg_rows:
        if row.get("type") != "file":
            continue
        is_active_sebi = (
            row.get("collection_name") in SEBI_COLLECTIONS
            and not row.get("deleted_at")
            and not row.get("collection_deleted_at")
        )
        if not is_active_sebi:
            continue
        name = row.get("name") or ""
        active_sebi_by_name[name].append(row)
        if row.get("upload_status") == "completed":
            completed_active_sebi_by_name[name].append(row)
        if row.get("vespa_doc_id"):
            pg_by_vespa[row["vespa_doc_id"]] = row

    vespa_by_doc_id = {row["docId"]: row for row in vespa_rows if row.get("docId")}
    vespa_by_basename: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in vespa_rows:
        basename = os.path.basename(row.get("fileName") or "")
        if basename:
            vespa_by_basename[basename].append(row)

    decisions: list[dict[str, Any]] = []
    for att in downloaded_atts:
        basename = os.path.basename(att.get("local_path") or "")
        completed_candidates = completed_active_sebi_by_name.get(basename, [])
        active_candidates = active_sebi_by_name.get(basename, [])
        selected_pg: dict[str, Any] | None = None
        pg_status = "no_active_pg_candidate"
        pg_reason = ""

        if completed_candidates:
            checksum_matches = [
                row
                for row in completed_candidates
                if row.get("checksum") and att.get("sha256") and row.get("checksum") == att.get("sha256")
            ]
            if len(checksum_matches) == 1:
                selected_pg = checksum_matches[0]
                pg_status = "completed_active_checksum"
            elif len(completed_candidates) == 1:
                selected_pg = completed_candidates[0]
                pg_status = "completed_active_exact_name"
            else:
                pg_status = "ambiguous_completed_active_pg"
                pg_reason = f"{len(completed_candidates)} completed active candidates"
        elif active_candidates:
            pg_status = "no_completed_pg_candidate"
            pg_reason = f"{len(active_candidates)} active candidates, none completed"

        basename_vespa = vespa_by_basename.get(basename, [])
        selected_vespa: dict[str, Any] | None = None
        vespa_status = "no_vespa_candidate"
        vespa_reason = ""

        if selected_pg and selected_pg.get("vespa_doc_id") in vespa_by_doc_id:
            selected_vespa = vespa_by_doc_id[selected_pg["vespa_doc_id"]]
            vespa_status = "postgres_vespa_doc_id"
        elif len(basename_vespa) == 1:
            selected_vespa = basename_vespa[0]
            vespa_status = "exact_filename"
        elif len(basename_vespa) > 1:
            vespa_status = "ambiguous_filename"
            vespa_reason = f"{len(basename_vespa)} Vespa candidates share basename"
        elif selected_pg and selected_pg.get("vespa_doc_id"):
            vespa_status = "pg_vespa_missing"
            vespa_reason = selected_pg.get("vespa_doc_id") or ""

        if selected_pg and selected_vespa:
            status = "ok"
        elif selected_vespa:
            status = "vespa_only"
        elif selected_pg:
            status = "pg_only"
        else:
            status = pg_status

        decisions.append(
            {
                "attachmentId": att["id"],
                "scrapeDocId": att["doc_id"],
                "attachmentIdx": att.get("idx"),
                "basename": basename,
                "status": status,
                "pgStatus": pg_status,
                "pgReason": pg_reason,
                "vespaStatus": vespa_status,
                "vespaReason": vespa_reason,
                "selectedCollectionItemId": selected_pg.get("id") if selected_pg else None,
                "selectedVespaDocId": selected_vespa.get("docId") if selected_vespa else None,
                "completedPgCandidateIds": [row.get("id") for row in completed_candidates],
                "activePgCandidateIds": [row.get("id") for row in active_candidates],
                "vespaCandidateDocIds": [row.get("docId") for row in basename_vespa],
            }
        )

    report = {
        "completed_active_pg_matches_by_attachment": sum(1 for d in decisions if d["pgStatus"].startswith("completed_active")),
        "active_noncompleted_pg_matches_by_attachment": sum(1 for d in decisions if d["pgStatus"] == "no_completed_pg_candidate"),
        "no_active_pg_match_by_attachment": sum(1 for d in decisions if d["pgStatus"] == "no_active_pg_candidate"),
        "selected_vespa_by_attachment": sum(1 for d in decisions if d.get("selectedVespaDocId")),
        "join_status_counts": dict(Counter(d["status"] for d in decisions)),
        "pg_status_counts": dict(Counter(d["pgStatus"] for d in decisions)),
        "vespa_status_counts": dict(Counter(d["vespaStatus"] for d in decisions)),
    }
    return decisions, report


def add_identifier_node(
    graph: GraphBuilder,
    identifier: Identifier,
    source_label: str,
    example_source_id: str,
) -> str:
    node = graph.add_node(
        identifier.id,
        "Identifier",
        kind=identifier.kind,
        canonical=identifier.canonical,
        name=identifier.canonical,
        aliases=[],
        sources=[],
        occurrenceCount=0,
        exampleSourceIds=[],
    )
    aliases = set(node.get("aliases") or [])
    if identifier.raw != identifier.canonical:
        aliases.add(identifier.raw)
    node["aliases"] = sorted(aliases)

    sources = set(node.get("sources") or [])
    sources.add(source_label)
    node["sources"] = sorted(sources)

    examples = list(node.get("exampleSourceIds") or [])
    if example_source_id not in examples and len(examples) < 20:
        examples.append(example_source_id)
    node["exampleSourceIds"] = examples
    node["occurrenceCount"] = int(node.get("occurrenceCount") or 0) + 1
    if identifier.kind in {"legal_provision", "legal_reference_text"}:
        legal_node_id = add_legal_node(graph, identifier, source_label, example_source_id)
        if legal_node_id:
            graph.add_edge(
                "identifies_legal_reference",
                identifier.id,
                legal_node_id,
                unique_key=legal_node_id,
            )
    return identifier.id


def add_legal_node(
    graph: GraphBuilder,
    identifier: Identifier,
    source_label: str,
    example_source_id: str,
) -> str | None:
    if identifier.kind == "legal_provision":
        parts = parse_legal_provision(identifier.canonical) or parse_legal_provision(identifier.raw)
        if not parts:
            return None
        instrument_id = node_id_for_legal_instrument(parts["instrument"])
        instrument_node = graph.add_node(
            instrument_id,
            "LegalInstrument",
            kind=legal_instrument_kind(parts["instrument"]),
            canonical=parts["instrument"],
            name=parts["instrument"],
            aliases=[],
            sources=[],
        )
        aliases = set(instrument_node.get("aliases") or [])
        if parts["instrumentRaw"] and parts["instrumentRaw"] != parts["instrument"]:
            aliases.add(parts["instrumentRaw"])
        instrument_node["aliases"] = sorted(aliases)
        sources = set(instrument_node.get("sources") or [])
        sources.add(source_label)
        instrument_node["sources"] = sorted(sources)

        provision_id = node_id_for_legal_provision(parts["canonical"])
        provision_node = graph.add_node(
            provision_id,
            "LegalProvision",
            kind=parts["provisionType"].lower(),
            canonical=parts["canonical"],
            name=parts["canonical"],
            provisionType=parts["provisionType"],
            provisionNumber=parts["provisionNumber"],
            instrument=parts["instrument"],
            aliases=[],
            sources=[],
            exampleSourceIds=[],
        )
        provision_aliases = set(provision_node.get("aliases") or [])
        if identifier.raw != parts["canonical"]:
            provision_aliases.add(identifier.raw)
        provision_node["aliases"] = sorted(provision_aliases)
        provision_sources = set(provision_node.get("sources") or [])
        provision_sources.add(source_label)
        provision_node["sources"] = sorted(provision_sources)
        examples = list(provision_node.get("exampleSourceIds") or [])
        if example_source_id not in examples and len(examples) < 20:
            examples.append(example_source_id)
        provision_node["exampleSourceIds"] = examples
        graph.add_edge("provision_of", provision_id, instrument_id, unique_key=instrument_id)
        return provision_id

    if identifier.kind == "legal_reference_text":
        reference_id = node_id_for_legal_reference(identifier.canonical)
        node = graph.add_node(
            reference_id,
            "LegalReference",
            kind="unparsed_legal_reference",
            canonical=identifier.canonical,
            name=identifier.canonical,
            aliases=[],
            sources=[],
            exampleSourceIds=[],
        )
        sources = set(node.get("sources") or [])
        sources.add(source_label)
        node["sources"] = sorted(sources)
        examples = list(node.get("exampleSourceIds") or [])
        if example_source_id not in examples and len(examples) < 20:
            examples.append(example_source_id)
        node["exampleSourceIds"] = examples
        return reference_id

    return None


def add_legal_reference_edge(
    graph: GraphBuilder,
    source_node_id: str,
    identifier: Identifier,
    *,
    source_field: str,
    raw: str,
    occurrence_index: int | None = None,
    unique_key: str | None = None,
) -> None:
    if identifier.kind not in {"legal_provision", "legal_reference_text"}:
        return
    target_id = add_legal_node(graph, identifier, source_field, source_node_id)
    if not target_id:
        return
    attrs: dict[str, Any] = {
        "sourceField": source_field,
        "raw": raw,
        "viaIdentifier": identifier.id,
    }
    if occurrence_index is not None:
        attrs["occurrenceIndex"] = occurrence_index
    graph.add_edge(
        "references_legal_provision" if identifier.kind == "legal_provision" else "references_legal_reference",
        source_node_id,
        target_id,
        unique_key=unique_key,
        **attrs,
    )


def add_entity_node(
    graph: GraphBuilder,
    kind: str,
    name: str,
    raw: str,
    source_doc_id: str,
) -> str:
    canonical = normalize_name(name)
    entity_id = node_id_for_entity(kind, canonical)
    node = graph.add_node(
        entity_id,
        "Entity",
        kind=kind,
        name=canonical,
        aliases=[],
        occurrenceCount=0,
        exampleDocIds=[],
    )
    aliases = set(node.get("aliases") or [])
    if raw and raw != canonical:
        aliases.add(normalize_ws(raw))
    node["aliases"] = sorted(aliases)
    examples = list(node.get("exampleDocIds") or [])
    if source_doc_id not in examples and len(examples) < 20:
        examples.append(source_doc_id)
    node["exampleDocIds"] = examples
    node["occurrenceCount"] = int(node.get("occurrenceCount") or 0) + 1
    return entity_id


def build_taxonomy(
    graph: GraphBuilder,
    resolved_docs: list[dict[str, Any]],
) -> dict[tuple[str, str, str], str]:
    taxonomy_key_to_id: dict[tuple[str, str, str], str] = {}

    def add_tax(kind: str, path_parts: tuple[str, ...], parent_id: str | None) -> str:
        key = (kind, *path_parts)
        node_id = "tax_" + sha1_short("::".join(key), 14)
        if key not in taxonomy_key_to_id:
            taxonomy_key_to_id[key] = node_id
            graph.add_node(
                node_id,
                "TaxonomyNode",
                kind=kind,
                name=path_parts[-1],
                path="/".join(path_parts),
                parentId=parent_id,
            )
            if parent_id:
                graph.add_edge("child_of", node_id, parent_id, unique_key="taxonomy")
        return node_id

    for doc in resolved_docs:
        top = normalize_ws(doc.get("top_section"))
        section = normalize_ws(doc.get("section"))
        subsection = normalize_ws(doc.get("subsection"))
        top_id = add_tax("top_section", (top,), None)
        section_id = add_tax("section", (top, section), top_id)
        if subsection:
            leaf_id = add_tax("subsection", (top, section, subsection), section_id)
        else:
            leaf_id = section_id
        taxonomy_key_to_id[("doc_leaf", str(doc["id"]), "")] = leaf_id

    return taxonomy_key_to_id


def build_graph(
    scrape_docs: list[dict[str, Any]],
    scrape_atts: list[dict[str, Any]],
    pg_rows: list[dict[str, Any]],
    vespa_rows: list[dict[str, Any]],
    join_decisions: list[dict[str, Any]],
    hydrated_fields_by_doc_id: dict[str, dict[str, Any]] | None = None,
    vespa_owner_candidates: list[dict[str, Any]] | None = None,
) -> tuple[GraphBuilder, dict[str, Any]]:
    hydrated_fields_by_doc_id = hydrated_fields_by_doc_id or {}
    vespa_owner_candidates = vespa_owner_candidates or []
    graph = GraphBuilder()
    resolved_docs = [d for d in scrape_docs if d.get("status") == "resolved"]
    downloaded_atts = [a for a in scrape_atts if a.get("status") == "downloaded"]

    docs_by_id = {d["id"]: d for d in resolved_docs}
    atts_by_doc: dict[int, list[dict[str, Any]]] = defaultdict(list)
    atts_by_id = {a["id"]: a for a in downloaded_atts}
    for att in downloaded_atts:
        atts_by_doc[att["doc_id"]].append(att)

    decisions_by_att = {d["attachmentId"]: d for d in join_decisions}
    decisions_by_vespa_doc_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in join_decisions:
        for doc_id in decision.get("vespaCandidateDocIds") or []:
            decisions_by_vespa_doc_id[doc_id].append(decision)

    active_sebi_pg_rows = [
        row
        for row in pg_rows
        if row.get("collection_name") in SEBI_COLLECTIONS
        and not row.get("deleted_at")
        and not row.get("collection_deleted_at")
    ]
    pg_by_id = {row["id"]: row for row in pg_rows if row.get("id")}
    active_folders = [row for row in active_sebi_pg_rows if row.get("type") == "folder"]
    active_files = [row for row in active_sebi_pg_rows if row.get("type") == "file"]
    folder_by_id = {row["id"]: row for row in active_folders}

    vespa_by_doc_id = {row["docId"]: row for row in vespa_rows if row.get("docId")}
    vespa_by_basename: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in vespa_rows:
        basename = os.path.basename(row.get("fileName") or "")
        if basename:
            vespa_by_basename[basename].append(row)

    taxonomy_map = build_taxonomy(graph, resolved_docs)

    # Collections, folders, and collection items.
    collection_names: dict[str, str] = {}
    for row in active_sebi_pg_rows:
        coll_id = row.get("collection_id")
        if not coll_id:
            continue
        collection_names[coll_id] = row.get("collection_name") or ""
    for coll_id, name in collection_names.items():
        graph.add_node(
            f"collection_{safe_id(coll_id)}",
            "Collection",
            pgId=coll_id,
            name=name,
        )

    def folder_path(row: dict[str, Any]) -> str:
        parts = [row.get("name") or ""]
        parent_id = row.get("parent_id")
        seen: set[str] = set()
        while parent_id and parent_id in folder_by_id and parent_id not in seen:
            seen.add(parent_id)
            parent = folder_by_id[parent_id]
            parts.append(parent.get("name") or "")
            parent_id = parent.get("parent_id")
        parts.append(row.get("collection_name") or "")
        return "/".join(reversed([p for p in parts if p]))

    folder_paths: dict[str, str] = {}
    for row in active_folders:
        fid = row["id"]
        path = folder_path(row)
        folder_paths[fid] = path
        folder_node_id = f"folder_{safe_id(fid)}"
        graph.add_node(
            folder_node_id,
            "Folder",
            pgId=fid,
            name=row.get("name") or "",
            path=path,
            collection=row.get("collection_name") or "",
            parentId=row.get("parent_id") or None,
        )
        coll_id = row.get("collection_id")
        if coll_id:
            graph.add_edge(
                "contains_folder",
                f"collection_{safe_id(coll_id)}",
                folder_node_id,
                unique_key=fid,
            )
        parent_id = row.get("parent_id")
        if parent_id and parent_id in folder_by_id:
            graph.add_edge(
                "child_of",
                folder_node_id,
                f"folder_{safe_id(parent_id)}",
                unique_key="folder",
            )

        topic_id = f"topic_{sha1_short(path.lower(), 14)}"
        graph.add_node(
            topic_id,
            "Topic",
            name=row.get("name") or "",
            path=path,
            sourceFolderId=fid,
        )
        graph.add_edge("has_topic", folder_node_id, topic_id, unique_key=fid)

    for row in active_files:
        item_id = row["id"]
        ci_node_id = f"ci_{safe_id(item_id)}"
        graph.add_node(
            ci_node_id,
            "CollectionItem",
            pgId=item_id,
            name=row.get("name") or "",
            collection=row.get("collection_name") or "",
            collectionId=row.get("collection_id") or "",
            parentId=row.get("parent_id") or None,
            vespaDocId=row.get("vespa_doc_id") or None,
            storagePath=row.get("storage_path") or "",
            fileSize=row.get("file_size") or "",
            checksum=row.get("checksum") or "",
            uploadStatus=row.get("upload_status") or "",
        )
        parent_id = row.get("parent_id")
        if parent_id and parent_id in folder_by_id:
            graph.add_edge(
                "stored_in_folder",
                ci_node_id,
                f"folder_{safe_id(parent_id)}",
                unique_key=parent_id,
            )

    # Scrape documents and attachments.
    doc_entity_ids: dict[str, set[str]] = defaultdict(set)
    doc_rc_ids: dict[str, set[str]] = defaultdict(set)
    doc_title_identifiers: dict[str, list[tuple[Identifier, str]]] = defaultdict(list)
    identifier_sources: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"documents": set(), "attachments": set(), "vespa": set()})
    source_doc_for_attachment = {att["id"]: f"doc_{att['doc_id']}" for att in downloaded_atts}
    attachment_nodes_for_vespa: dict[str, set[str]] = defaultdict(set)
    parent_title_identifier_attachment_edges = 0
    vespa_identity_identifier_edges = 0
    attachment_identity_identifier_edges = 0
    vespa_owner_candidate_hits_loaded = 0
    vespa_owner_candidate_hits_accepted = 0
    vespa_owner_candidate_hits_rejected = 0
    vespa_owner_candidate_document_identity_edges = 0
    vespa_owner_candidate_attachment_identity_edges = 0
    vespa_owner_candidate_vespa_identity_edges = 0

    for doc in resolved_docs:
        doc_node_id = f"doc_{doc['id']}"
        graph.add_node(
            doc_node_id,
            "Document",
            scrapeDbId=doc["id"],
            title=doc.get("title") or "",
            name=doc.get("title") or "",
            date=doc.get("date_iso") or "",
            topSection=doc.get("top_section") or "",
            section=doc.get("section") or "",
            subsection=doc.get("subsection") or "",
            sid=doc.get("sid"),
            ssid=doc.get("ssid"),
            smid=doc.get("smid"),
            detailUrl=doc.get("detail_url") or "",
            status=doc.get("status") or "",
            attachmentCount=len(atts_by_doc.get(doc["id"], [])),
        )
        leaf_id = taxonomy_map.get(("doc_leaf", str(doc["id"]), ""))
        if leaf_id:
            graph.add_edge("classified_as", doc_node_id, leaf_id, unique_key="taxonomy")

        for ident in identifiers_from_text(doc.get("title")):
            ident_id = add_identifier_node(graph, ident, "scrape_title", doc_node_id)
            graph.add_edge(
                "has_identifier",
                doc_node_id,
                ident_id,
                unique_key=f"title:{ident_id}",
                sourceField="title",
                raw=ident.raw,
            )
            add_legal_reference_edge(
                graph,
                doc_node_id,
                ident,
                source_field="title",
                raw=ident.raw,
                unique_key=f"title:{ident_id}",
            )
            identifier_sources[ident_id]["documents"].add(doc_node_id)
            doc_title_identifiers[doc_node_id].append((ident, ident_id))
            if ident.kind == "recovery_certificate":
                doc_rc_ids[doc_node_id].add(ident_id)

        for entity in extract_title_entities(doc.get("title"), doc.get("top_section")):
            entity_id = add_entity_node(graph, entity["kind"], entity["name"], entity["raw"], doc_node_id)
            doc_entity_ids[doc_node_id].add(entity_id)
            graph.add_edge(
                "mentions_entity",
                doc_node_id,
                entity_id,
                unique_key=entity_id,
                sourceField="title",
                raw=entity["raw"],
            )

    for att in downloaded_atts:
        att_node_id = f"att_{att['id']}"
        basename = os.path.basename(att.get("local_path") or "")
        decision = decisions_by_att.get(att["id"], {})
        graph.add_node(
            att_node_id,
            "AttachmentFile",
            attachmentId=att["id"],
            scrapeDocId=att.get("doc_id"),
            idx=att.get("idx"),
            label=att.get("label") or "",
            originalFilename=att.get("original_filename") or "",
            localPath=att.get("local_path") or "",
            basename=basename,
            sha256=att.get("sha256") or "",
            sizeBytes=att.get("size_bytes"),
            status=att.get("status") or "",
            joinStatus=decision.get("status"),
            selectedCollectionItemId=decision.get("selectedCollectionItemId"),
            selectedVespaDocId=decision.get("selectedVespaDocId"),
        )
        doc_node_id = f"doc_{att['doc_id']}"
        graph.add_edge(
            "has_attachment",
            doc_node_id,
            att_node_id,
            unique_key=str(att["id"]),
            idx=att.get("idx"),
            label=att.get("label") or "",
        )
        if len(atts_by_doc.get(att["doc_id"], [])) == 1:
            for ident, ident_id in doc_title_identifiers.get(doc_node_id, []):
                edge = graph.add_edge(
                    "has_identifier",
                    att_node_id,
                    ident_id,
                    unique_key=f"parent_title:{ident_id}",
                    sourceField="parent_scrape_title",
                    raw=ident.raw,
                )
                add_legal_reference_edge(
                    graph,
                    att_node_id,
                    ident,
                    source_field="parent_scrape_title",
                    raw=ident.raw,
                    unique_key=f"parent_title:{ident_id}",
                )
                if edge:
                    parent_title_identifier_attachment_edges += 1
                identifier_sources[ident_id]["attachments"].add(att_node_id)

        for ident in identifiers_from_filename(basename):
            ident_id = add_identifier_node(graph, ident, "attachment_filename", att_node_id)
            graph.add_edge(
                "has_identifier",
                att_node_id,
                ident_id,
                unique_key=f"filename:{ident_id}",
                sourceField="filename",
                raw=ident.raw,
            )
            add_legal_reference_edge(
                graph,
                att_node_id,
                ident,
                source_field="filename",
                raw=ident.raw,
                unique_key=f"filename:{ident_id}",
            )
            identifier_sources[ident_id]["attachments"].add(att_node_id)
            if ident.kind == "recovery_certificate":
                doc_rc_ids[doc_node_id].add(ident_id)

        selected_ci = decision.get("selectedCollectionItemId")
        if selected_ci and selected_ci in pg_by_id:
            ci_node_id = f"ci_{safe_id(selected_ci)}"
            graph.add_edge(
                "indexed_as",
                att_node_id,
                ci_node_id,
                unique_key=selected_ci,
                method=decision.get("pgStatus"),
            )
            pg_row = pg_by_id[selected_ci]
            parent_id = pg_row.get("parent_id")
            if parent_id and parent_id in folder_by_id:
                folder_node_id = f"folder_{safe_id(parent_id)}"
                graph.add_edge("belongs_to_folder", att_node_id, folder_node_id, unique_key=parent_id)
                path = folder_paths.get(parent_id)
                if path:
                    topic_id = f"topic_{sha1_short(path.lower(), 14)}"
                    graph.add_edge("tagged_with_topic", att_node_id, topic_id, unique_key=topic_id)
                    graph.add_edge("tagged_with_topic", doc_node_id, topic_id, unique_key=topic_id)

        for vespa_candidate in vespa_by_basename.get(basename, []):
            vespa_doc_id = vespa_candidate.get("docId")
            if not vespa_doc_id:
                continue
            attachment_nodes_for_vespa[vespa_doc_id].add(att_node_id)
            graph.add_edge(
                "vespa_item",
                att_node_id,
                f"vespa_{safe_id(vespa_doc_id)}",
                unique_key=vespa_doc_id,
                method="filename",
                selected=vespa_doc_id == decision.get("selectedVespaDocId"),
            )

    # Vespa items and raw metadata occurrence edges.
    raw_ref_occurrences_matching_scrape = 0
    raw_pan_occurrences_matching_scrape = 0
    matching_vespa_doc_ids: set[str] = set()
    reference_occurrences: list[dict[str, Any]] = []

    for row in vespa_rows:
        vespa_doc_id = row.get("docId")
        if not vespa_doc_id:
            continue
        basename = os.path.basename(row.get("fileName") or "")
        linked_att_ids = sorted(attachment_nodes_for_vespa.get(vespa_doc_id) or [])
        if linked_att_ids:
            matching_vespa_doc_ids.add(vespa_doc_id)
        vespa_node_id = f"vespa_{safe_id(vespa_doc_id)}"
        hydrated_fields = hydrated_fields_by_doc_id.get(vespa_doc_id)
        hydrated_identity_text = (
            identity_text_from_vespa_fields(hydrated_fields, row.get("fileName") or "")
            if hydrated_fields
            else ""
        )
        identity_text = hydrated_identity_text or normalize_ws(str(row.get("identity_text") or ""))
        identity_source = "hydration_cache" if hydrated_identity_text else ("vespa_identity_text" if identity_text else "")
        identity_identifiers = identifiers_from_text(identity_text)
        graph.add_node(
            vespa_node_id,
            "VespaItem",
            docId=vespa_doc_id,
            name=basename or vespa_doc_id,
            fileName=row.get("fileName") or "",
            basename=basename,
            entity=row.get("entity"),
            documentId=row.get("document_id"),
            documentDate=row.get("document_date"),
            referencedIds=row.get("referenced_ids"),
            panIds=row.get("pan_ids"),
            matchedAttachmentIds=linked_att_ids,
            identityTextSource=identity_source,
            identityIdentifierCount=len(identity_identifiers),
        )

        doc_identifier = normalize_identifier(row.get("document_id"), "sebi_document_id")
        if doc_identifier:
            ident_id = add_identifier_node(graph, doc_identifier, "vespa_document_id", vespa_node_id)
            graph.add_edge(
                "has_identifier",
                vespa_node_id,
                ident_id,
                unique_key=f"document_id:{ident_id}",
                sourceField="document_id",
                raw=doc_identifier.raw,
            )
            identifier_sources[ident_id]["vespa"].add(vespa_node_id)
            for att_node_id in linked_att_ids:
                graph.add_edge(
                    "has_identifier",
                    att_node_id,
                    ident_id,
                    unique_key=f"vespa_document_id:{vespa_doc_id}:{ident_id}",
                    sourceField="vespa_document_id",
                    raw=doc_identifier.raw,
                )
                identifier_sources[ident_id]["attachments"].add(att_node_id)

        for ident in identity_identifiers:
            ident_id = add_identifier_node(graph, ident, "vespa_identity_text", vespa_node_id)
            edge = graph.add_edge(
                "has_identifier",
                vespa_node_id,
                ident_id,
                unique_key=f"identity_text:{ident_id}",
                sourceField=identity_source or "vespa_identity_text",
                raw=ident.raw,
            )
            add_legal_reference_edge(
                graph,
                vespa_node_id,
                ident,
                source_field=identity_source or "vespa_identity_text",
                raw=ident.raw,
                unique_key=f"identity_text:{ident_id}",
            )
            if edge:
                vespa_identity_identifier_edges += 1
            identifier_sources[ident_id]["vespa"].add(vespa_node_id)
            for att_node_id in linked_att_ids:
                edge = graph.add_edge(
                    "has_identifier",
                    att_node_id,
                    ident_id,
                    unique_key=f"vespa_identity_text:{vespa_doc_id}:{ident_id}",
                    sourceField=identity_source or "vespa_identity_text",
                    raw=ident.raw,
                )
                add_legal_reference_edge(
                    graph,
                    att_node_id,
                    ident,
                    source_field=identity_source or "vespa_identity_text",
                    raw=ident.raw,
                    unique_key=f"vespa_identity_text:{vespa_doc_id}:{ident_id}",
                )
                if edge:
                    attachment_identity_identifier_edges += 1
                identifier_sources[ident_id]["attachments"].add(att_node_id)
                if ident.kind == "recovery_certificate":
                    source_doc = source_doc_for_attachment.get(int(att_node_id.replace("att_", "")))
                    if source_doc:
                        doc_rc_ids[source_doc].add(ident_id)

        for idx, ref in enumerate(row.get("referenced_ids") or []):
            ident = normalize_identifier(ref) or normalize_identifier(ref, "reference_text")
            if not ident:
                continue
            ident_id = add_identifier_node(graph, ident, "vespa_referenced_ids", vespa_node_id)
            graph.add_edge(
                "references_identifier",
                vespa_node_id,
                ident_id,
                sourceField="referenced_ids",
                occurrenceIndex=idx,
                raw=ref,
            )
            add_legal_reference_edge(
                graph,
                vespa_node_id,
                ident,
                source_field="referenced_ids",
                occurrence_index=idx,
                raw=ref,
            )
            reference_occurrences.append(
                {
                    "vespaNodeId": vespa_node_id,
                    "vespaDocId": vespa_doc_id,
                    "identifierId": ident_id,
                    "identifierKind": ident.kind,
                    "raw": ref,
                    "linkedAttachmentIds": linked_att_ids,
                }
            )
            if linked_att_ids:
                raw_ref_occurrences_matching_scrape += 1
                for att_node_id in linked_att_ids:
                    graph.add_edge(
                        "references_identifier",
                        att_node_id,
                        ident_id,
                        unique_key=f"{vespa_doc_id}:{idx}:{ident_id}",
                        sourceField="referenced_ids",
                        occurrenceIndex=idx,
                        raw=ref,
                    )
                    add_legal_reference_edge(
                        graph,
                        att_node_id,
                        ident,
                        source_field="referenced_ids",
                        occurrence_index=idx,
                        raw=ref,
                        unique_key=f"{vespa_doc_id}:{idx}:{ident_id}",
                    )

        for idx, pan in enumerate(row.get("pan_ids") or []):
            ident = normalize_identifier(pan, "pan")
            if not ident:
                continue
            ident_id = add_identifier_node(graph, ident, "vespa_pan_ids", vespa_node_id)
            graph.add_edge(
                "mentions_identifier",
                vespa_node_id,
                ident_id,
                sourceField="pan_ids",
                occurrenceIndex=idx,
                raw=pan,
            )
            if linked_att_ids:
                raw_pan_occurrences_matching_scrape += 1
                for att_node_id in linked_att_ids:
                    graph.add_edge(
                        "mentions_identifier",
                        att_node_id,
                        ident_id,
                        unique_key=f"{vespa_doc_id}:{idx}:{ident_id}",
                        sourceField="pan_ids",
                        occurrenceIndex=idx,
                        raw=pan,
                    )

    for record in vespa_owner_candidates:
        ident = normalize_identifier(record.get("canonical"))
        accepted_hits = record.get("acceptedOwnerCandidates") or []
        vespa_owner_candidate_hits_loaded += len(accepted_hits)
        if not ident or ident.kind in NON_DOCUMENT_REFERENCE_KINDS:
            vespa_owner_candidate_hits_rejected += len(accepted_hits)
            continue
        ident_id = ident.id
        if record.get("identifierId") and record.get("identifierId") != ident_id:
            vespa_owner_candidate_hits_rejected += len(accepted_hits)
            continue

        for hit in accepted_hits:
            if not isinstance(hit, dict):
                vespa_owner_candidate_hits_rejected += 1
                continue
            valid, reason = vespa_owner_candidate_is_valid(record, hit, ident)
            graph_doc_id = normalize_ws(hit.get("graphDocId"))
            if not valid or graph_doc_id not in graph.nodes or graph.nodes[graph_doc_id].get("type") != "Document":
                vespa_owner_candidate_hits_rejected += 1
                continue

            vespa_owner_candidate_hits_accepted += 1
            evidence_chunk = candidate_hit_chunk_index(hit)
            evidence_text = candidate_hit_evidence_text(hit)
            vespa_doc_id = normalize_ws(hit.get("docId"))
            source_field = "vespa_owner_candidate"
            provenance = {
                "sourceField": source_field,
                "raw": ident.raw,
                "resolution": "vespa_exact_owner_identity",
                "evidenceReason": reason,
                "evidenceVespaDocId": vespa_doc_id,
                "evidenceChunk": evidence_chunk,
                "evidenceText": evidence_text,
                "evidenceSignals": hit.get("signals") or {},
            }
            add_identifier_node(graph, ident, source_field, graph_doc_id)
            if graph.add_edge(
                "has_identifier",
                graph_doc_id,
                ident_id,
                unique_key=f"vespa_owner_candidate:{graph_doc_id}:{ident_id}:{vespa_doc_id}",
                **provenance,
            ):
                vespa_owner_candidate_document_identity_edges += 1
            identifier_sources[ident_id]["documents"].add(graph_doc_id)

            vespa_node_id = f"vespa_{safe_id(vespa_doc_id)}" if vespa_doc_id else ""
            if vespa_node_id and vespa_node_id in graph.nodes:
                if graph.add_edge(
                    "has_identifier",
                    vespa_node_id,
                    ident_id,
                    unique_key=f"vespa_owner_candidate:{vespa_node_id}:{ident_id}",
                    **provenance,
                ):
                    vespa_owner_candidate_vespa_identity_edges += 1
                identifier_sources[ident_id]["vespa"].add(vespa_node_id)

            for att_node_id in sorted(attachment_nodes_for_vespa.get(vespa_doc_id) or []):
                if graph.add_edge(
                    "has_identifier",
                    att_node_id,
                    ident_id,
                    unique_key=f"vespa_owner_candidate:{att_node_id}:{ident_id}:{vespa_doc_id}",
                    **provenance,
                ):
                    vespa_owner_candidate_attachment_identity_edges += 1
                identifier_sources[ident_id]["attachments"].add(att_node_id)

    # Identifier resolution edges from raw references.
    cites_doc_seen: set[tuple[str, str, str]] = set()
    cites_file_seen: set[tuple[str, str, str]] = set()
    unresolved_reference_occurrences = 0
    resolved_reference_occurrences = 0
    non_document_reference_occurrences = 0
    document_like_reference_occurrences = 0

    for occurrence in reference_occurrences:
        ident_id = occurrence["identifierId"]
        if occurrence.get("identifierKind") in NON_DOCUMENT_REFERENCE_KINDS:
            non_document_reference_occurrences += 1
            continue
        document_like_reference_occurrences += 1
        targets = identifier_sources.get(ident_id, {})
        target_atts = set(targets.get("attachments") or set())
        target_docs = set(targets.get("documents") or set())
        if not target_atts and not target_docs:
            unresolved_reference_occurrences += 1
            continue
        source_atts = set(occurrence.get("linkedAttachmentIds") or [])
        if not source_atts:
            unresolved_reference_occurrences += 1
            continue
        any_resolved = False
        for source_att in source_atts:
            source_doc = source_doc_for_attachment.get(int(source_att.replace("att_", "")))
            for target_att in target_atts:
                target_doc = source_doc_for_attachment.get(int(target_att.replace("att_", "")))
                if not target_doc or target_att == source_att:
                    continue
                file_key = (source_att, target_att, ident_id)
                if file_key not in cites_file_seen:
                    cites_file_seen.add(file_key)
                    graph.add_edge(
                        "cites_file",
                        source_att,
                        target_att,
                        unique_key="::".join(file_key),
                        viaIdentifier=ident_id,
                        raw=occurrence["raw"],
                        resolution="identifier_exact",
                    )
                if source_doc and target_doc and source_doc != target_doc:
                    doc_key = (source_doc, target_doc, ident_id)
                    if doc_key not in cites_doc_seen:
                        cites_doc_seen.add(doc_key)
                        graph.add_edge(
                            "cites_document",
                            source_doc,
                            target_doc,
                            unique_key="::".join(doc_key),
                            viaIdentifier=ident_id,
                            raw=occurrence["raw"],
                            resolution="identifier_exact",
                        )
                    any_resolved = True
            for target_doc in target_docs:
                if source_doc and source_doc != target_doc:
                    doc_key = (source_doc, target_doc, ident_id)
                    if doc_key not in cites_doc_seen:
                        cites_doc_seen.add(doc_key)
                        graph.add_edge(
                            "cites_document",
                            source_doc,
                            target_doc,
                            unique_key="::".join(doc_key),
                            viaIdentifier=ident_id,
                            raw=occurrence["raw"],
                            resolution="identifier_exact",
                        )
                    any_resolved = True
        if any_resolved:
            resolved_reference_occurrences += 1
        else:
            unresolved_reference_occurrences += 1

    # Recovery event and chronology edges through canonical RC identifiers.
    docs_by_rc: dict[str, list[str]] = defaultdict(list)
    for doc_id, rc_ids in doc_rc_ids.items():
        doc = docs_by_id.get(int(doc_id.replace("doc_", "")))
        title = doc.get("title") if doc else ""
        event_type = recovery_event_type(title)
        for rc_id in rc_ids:
            graph.add_edge(
                "recovery_event_for",
                doc_id,
                rc_id,
                unique_key=rc_id,
                eventType=event_type,
            )
            docs_by_rc[rc_id].append(doc_id)

    for rc_id, doc_ids in docs_by_rc.items():
        unique_doc_ids = sorted(set(doc_ids), key=lambda d: (graph.nodes[d].get("date") or "", d))
        for prev_doc, next_doc in zip(unique_doc_ids, unique_doc_ids[1:]):
            graph.add_edge(
                "follows_recovery_event",
                prev_doc,
                next_doc,
                unique_key=rc_id,
                viaIdentifier=rc_id,
            )

    # Informal guidance attachment pairing inside the same scrape document.
    for doc_id, doc_atts in atts_by_doc.items():
        if len(doc_atts) < 2:
            continue
        request_atts = [
            f"att_{att['id']}"
            for att in doc_atts
            if re.search(r"applicant|request", att.get("label") or "", re.I)
        ]
        response_atts = [
            f"att_{att['id']}"
            for att in doc_atts
            if re.search(r"guidance letter|sebi", att.get("label") or "", re.I)
        ]
        for request_att in request_atts:
            for response_att in response_atts:
                if request_att != response_att:
                    graph.add_edge(
                        "sebi_response_to_guidance_request",
                        response_att,
                        request_att,
                        unique_key=f"{response_att}:{request_att}",
                    )

    # Co-mention aggregates with true weights.
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_examples: dict[tuple[str, str], list[str]] = defaultdict(list)
    for doc_id, entity_ids in doc_entity_ids.items():
        for a, b in combinations(sorted(entity_ids), 2):
            if a == b:
                continue
            pair = (a, b)
            pair_counts[pair] += 1
            if len(pair_examples[pair]) < 20:
                pair_examples[pair].append(doc_id)
    for (a, b), weight in pair_counts.items():
        graph.add_edge(
            "co_mentioned_with",
            a,
            b,
            unique_key=f"{a}:{b}",
            weight=weight,
            evidenceDocIds=pair_examples[(a, b)],
        )

    report = {
        "matching_vespa_items": len(matching_vespa_doc_ids),
        "raw_ref_occurrences_matching_scrape": raw_ref_occurrences_matching_scrape,
        "raw_pan_occurrences_matching_scrape": raw_pan_occurrences_matching_scrape,
        "resolved_reference_occurrences": resolved_reference_occurrences,
        "unresolved_reference_occurrences": unresolved_reference_occurrences,
        "non_document_reference_occurrences": non_document_reference_occurrences,
        "document_like_reference_occurrences": document_like_reference_occurrences,
        "cites_document_edges": len(cites_doc_seen),
        "cites_file_edges": len(cites_file_seen),
        "parent_title_identifier_attachment_edges": parent_title_identifier_attachment_edges,
        "vespa_identity_identifier_edges": vespa_identity_identifier_edges,
        "attachment_identity_identifier_edges": attachment_identity_identifier_edges,
        "vespa_owner_candidate_hits_loaded": vespa_owner_candidate_hits_loaded,
        "vespa_owner_candidate_hits_accepted": vespa_owner_candidate_hits_accepted,
        "vespa_owner_candidate_hits_rejected": vespa_owner_candidate_hits_rejected,
        "vespa_owner_candidate_document_identity_edges": vespa_owner_candidate_document_identity_edges,
        "vespa_owner_candidate_attachment_identity_edges": vespa_owner_candidate_attachment_identity_edges,
        "vespa_owner_candidate_vespa_identity_edges": vespa_owner_candidate_vespa_identity_edges,
        "recovery_identifiers_with_docs": len(docs_by_rc),
        "recovery_chronology_edges": sum(1 for e in graph.edges if e["type"] == "follows_recovery_event"),
        "co_mention_edges": len(pair_counts),
    }
    return graph, report


def persist_graph(graph: GraphBuilder) -> dict[str, Any]:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    nodes = list(graph.nodes.values())
    edges = graph.edges
    write_jsonl(GRAPH_DIR / "nodes.jsonl", nodes)
    write_jsonl(GRAPH_DIR / "edges.jsonl", edges)

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        by_type[node["type"]].append(node)
    for node_type, records in by_type.items():
        write_jsonl(GRAPH_DIR / f"nodes_{node_type.lower()}.jsonl", records)

    by_edge_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        by_edge_type[edge["type"]].append(edge)
    for edge_type, records in by_edge_type.items():
        write_jsonl(GRAPH_DIR / f"edges_{edge_type}.jsonl", records)

    if GRAPH_SQLITE.exists():
        GRAPH_SQLITE.unlink()
    conn = sqlite3.connect(GRAPH_SQLITE)
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT,
            title TEXT,
            kind TEXT,
            date TEXT,
            attrs_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE edges (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            type TEXT NOT NULL,
            attrs_json TEXT NOT NULL
        )
        """
    )
    for node in nodes:
        conn.execute(
            "INSERT INTO nodes (id, type, name, title, kind, date, attrs_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                node["id"],
                node["type"],
                node.get("name", ""),
                node.get("title", ""),
                node.get("kind", ""),
                node.get("date", ""),
                json.dumps(node, ensure_ascii=False),
            ),
        )
    for edge in edges:
        conn.execute(
            "INSERT INTO edges (id, source, target, type, attrs_json) VALUES (?, ?, ?, ?, ?)",
            (
                edge["id"],
                edge["source"],
                edge["target"],
                edge["type"],
                json.dumps(edge, ensure_ascii=False),
            ),
        )
    for name, ddl in {
        "idx_nodes_type": "nodes(type)",
        "idx_nodes_name": "nodes(name)",
        "idx_nodes_kind": "nodes(kind)",
        "idx_edges_type": "edges(type)",
        "idx_edges_source": "edges(source)",
        "idx_edges_target": "edges(target)",
    }.items():
        conn.execute(f"CREATE INDEX {name} ON {ddl}")
    conn.commit()
    conn.close()

    if nx is None:
        pickle_info = {"written": False, "reason": "networkx not installed"}
    else:
        G = nx.MultiDiGraph()
        for node in nodes:
            attrs = {k: v for k, v in node.items() if k != "id"}
            G.add_node(node["id"], **attrs)
        for edge in edges:
            attrs = {k: v for k, v in edge.items() if k not in {"id", "source", "target"}}
            G.add_edge(edge["source"], edge["target"], key=edge["id"], **attrs)
        with GRAPH_PICKLE.open("wb") as f:
            pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
        pickle_info = {
            "written": True,
            "path": str(GRAPH_PICKLE),
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
        }

    return {
        "node_file": str(GRAPH_DIR / "nodes.jsonl"),
        "edge_file": str(GRAPH_DIR / "edges.jsonl"),
        "sqlite": str(GRAPH_SQLITE),
        "pickle": pickle_info,
        "node_counts": dict(Counter(n["type"] for n in nodes)),
        "edge_counts": dict(Counter(e["type"] for e in edges)),
    }


def validate_graph(
    graph: GraphBuilder,
    scrape_docs: list[dict[str, Any]],
    scrape_atts: list[dict[str, Any]],
    pg_rows: list[dict[str, Any]],
    vespa_rows: list[dict[str, Any]],
    join_decisions: list[dict[str, Any]],
    build_report: dict[str, Any],
) -> dict[str, Any]:
    resolved_docs = [d for d in scrape_docs if d.get("status") == "resolved"]
    downloaded_atts = [a for a in scrape_atts if a.get("status") == "downloaded"]
    node_counts = Counter(n["type"] for n in graph.nodes.values())
    edge_counts = Counter(e["type"] for e in graph.edges)
    node_ids = set(graph.nodes)
    edge_ids = [e["id"] for e in graph.edges]

    active_sebi_files = [
        row
        for row in pg_rows
        if row.get("type") == "file"
        and row.get("collection_name") in SEBI_COLLECTIONS
        and not row.get("deleted_at")
        and not row.get("collection_deleted_at")
    ]
    completed_active_sebi_files = [row for row in active_sebi_files if row.get("upload_status") == "completed"]
    active_sebi_folders = [
        row
        for row in pg_rows
        if row.get("type") == "folder"
        and row.get("collection_name") in SEBI_COLLECTIONS
        and not row.get("deleted_at")
        and not row.get("collection_deleted_at")
    ]

    attachment_basenames = {
        os.path.basename(att.get("local_path") or "")
        for att in downloaded_atts
        if att.get("local_path")
    }
    matching_vespa_rows = [
        row
        for row in vespa_rows
        if os.path.basename(row.get("fileName") or "") in attachment_basenames
    ]
    matching_ref_occurrences = sum(len(row.get("referenced_ids") or []) for row in matching_vespa_rows)
    matching_pan_occurrences = sum(len(row.get("pan_ids") or []) for row in matching_vespa_rows)
    distinct_matching_refs = {
        normalize_ws(str(ref))
        for row in matching_vespa_rows
        for ref in (row.get("referenced_ids") or [])
        if normalize_ws(str(ref))
    }
    distinct_matching_pans = {
        normalize_ws(str(pan)).upper()
        for row in matching_vespa_rows
        for pan in (row.get("pan_ids") or [])
        if PAN_RE.fullmatch(normalize_ws(str(pan)).upper())
    }

    chosen_bad_pg = []
    pg_by_id = {row.get("id"): row for row in pg_rows}
    for decision in join_decisions:
        selected_id = decision.get("selectedCollectionItemId")
        if not selected_id:
            continue
        row = pg_by_id.get(selected_id)
        if not row:
            chosen_bad_pg.append({"attachmentId": decision["attachmentId"], "reason": "selected row not in pg export"})
            continue
        if row.get("collection_name") not in SEBI_COLLECTIONS or row.get("deleted_at") or row.get("collection_deleted_at") or row.get("upload_status") != "completed":
            chosen_bad_pg.append(
                {
                    "attachmentId": decision["attachmentId"],
                    "collectionItemId": selected_id,
                    "collection": row.get("collection_name"),
                    "uploadStatus": row.get("upload_status"),
                    "deletedAt": row.get("deleted_at"),
                }
            )

    checks = []

    def check(name: str, passed: bool, expected: Any, actual: Any, severity: str = "fail") -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "severity": severity,
                "expected": expected,
                "actual": actual,
            }
        )

    check("resolved_document_nodes", node_counts["Document"] == len(resolved_docs), len(resolved_docs), node_counts["Document"])
    check("downloaded_attachment_nodes", node_counts["AttachmentFile"] == len(downloaded_atts), len(downloaded_atts), node_counts["AttachmentFile"])
    check("has_attachment_edges", edge_counts["has_attachment"] == len(downloaded_atts), len(downloaded_atts), edge_counts["has_attachment"])
    check("active_sebi_collection_item_nodes", node_counts["CollectionItem"] == len(active_sebi_files), len(active_sebi_files), node_counts["CollectionItem"])
    check("active_sebi_folder_nodes", node_counts["Folder"] == len(active_sebi_folders), len(active_sebi_folders), node_counts["Folder"])
    check("taxonomy_child_edges", edge_counts["child_of"] >= 89, ">= 89 including taxonomy and folder", edge_counts["child_of"])
    check("matching_vespa_items_represented", build_report["matching_vespa_items"] == len(matching_vespa_rows), len(matching_vespa_rows), build_report["matching_vespa_items"])
    check("raw_reference_occurrences_preserved", edge_counts["references_identifier"] >= matching_ref_occurrences, f">= {matching_ref_occurrences}", edge_counts["references_identifier"])
    check("raw_pan_occurrences_preserved", edge_counts["mentions_identifier"] >= matching_pan_occurrences, f">= {matching_pan_occurrences}", edge_counts["mentions_identifier"])
    check("doc_to_doc_edges_nonzero", edge_counts["cites_document"] > 0, "> 0", edge_counts["cites_document"])
    check("file_to_file_edges_nonzero", edge_counts["cites_file"] > 0, "> 0", edge_counts["cites_file"])
    check("legal_provision_nodes_nonzero", node_counts["LegalProvision"] > 0, "> 0", node_counts["LegalProvision"])
    check("legal_references_separated", build_report.get("non_document_reference_occurrences", 0) > 0, "> 0", build_report.get("non_document_reference_occurrences", 0))
    check("no_bad_primary_pg_choices", len(chosen_bad_pg) == 0, 0, len(chosen_bad_pg))
    check("no_orphan_edge_sources", all(e["source"] in node_ids for e in graph.edges), 0, sum(1 for e in graph.edges if e["source"] not in node_ids))
    check("no_orphan_edge_targets", all(e["target"] in node_ids for e in graph.edges), 0, sum(1 for e in graph.edges if e["target"] not in node_ids))
    check("no_duplicate_edge_ids", len(edge_ids) == len(set(edge_ids)), len(edge_ids), len(set(edge_ids)))
    check("no_co_mention_self_loops", all(e["source"] != e["target"] for e in graph.edges if e["type"] == "co_mentioned_with"), 0, sum(1 for e in graph.edges if e["type"] == "co_mentioned_with" and e["source"] == e["target"]))

    warning_examples = {
        "resolved_docs_without_downloaded_attachment": [
            {"scrapeDbId": doc["id"], "title": doc.get("title")}
            for doc in resolved_docs
            if not any(att.get("doc_id") == doc["id"] for att in downloaded_atts)
        ][:20],
        "no_pg_match_examples": [
            {
                "attachmentId": d["attachmentId"],
                "scrapeDocId": d["scrapeDocId"],
                "basename": d["basename"],
                "status": d["status"],
            }
            for d in join_decisions
            if not d.get("selectedCollectionItemId")
        ][:20],
        "chosen_bad_pg_examples": chosen_bad_pg[:20],
    }

    failures = [c for c in checks if not c["passed"] and c["severity"] == "fail"]
    warnings = [c for c in checks if not c["passed"] and c["severity"] == "warn"]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "source_baselines": {
            "resolved_scrape_documents": len(resolved_docs),
            "downloaded_scrape_attachments": len(downloaded_atts),
            "active_sebi_files": len(active_sebi_files),
            "completed_active_sebi_files": len(completed_active_sebi_files),
            "active_sebi_folders": len(active_sebi_folders),
            "vespa_items_total": len(vespa_rows),
            "vespa_items_matching_scrape_attachment_filename": len(matching_vespa_rows),
            "raw_reference_occurrences_matching_scrape": matching_ref_occurrences,
            "distinct_raw_reference_strings_matching_scrape": len(distinct_matching_refs),
            "raw_pan_occurrences_matching_scrape": matching_pan_occurrences,
            "distinct_raw_pan_ids_matching_scrape": len(distinct_matching_pans),
        },
        "edge_counts": dict(edge_counts),
        "node_counts": dict(node_counts),
        "warning_examples": warning_examples,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, sort_keys=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-csv", type=Path, help="Use an existing Postgres collection_items CSV export.")
    parser.add_argument("--vespa-jsonl", type=Path, help="Use an existing Vespa metadata JSONL export.")
    parser.add_argument(
        "--hydration-cache-db",
        type=Path,
        default=DEFAULT_HYDRATION_CACHE_DB,
        help="Optional Kimi/OpenCode hydrated document cache used for header/self-identifier extraction.",
    )
    parser.add_argument(
        "--no-hydration-cache",
        action="store_true",
        help="Disable hydrated header/self-identifier extraction.",
    )
    parser.add_argument(
        "--vespa-owner-candidates-jsonl",
        type=Path,
        action="append",
        default=DEFAULT_VESPA_OWNER_CANDIDATES_JSONL,
        help="Optional audit JSONL of strict Vespa owner candidates to promote as identifier identity edges. May be supplied multiple times.",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Require --pg-csv and --vespa-jsonl instead of fetching from Docker/Vespa.",
    )
    parser.add_argument(
        "--allow-validation-fail",
        action="store_true",
        help="Write artifacts even if validation fails and return exit code 0.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.no_fetch and (not args.pg_csv or not args.vespa_jsonl):
        print("--no-fetch requires both --pg-csv and --vespa-jsonl", file=sys.stderr)
        return 2

    t0 = time.time()
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("SEBI metadata graph v2")
    print("=" * 72)
    run_identifier_self_checks()

    print("[1] Loading scrape_state.db")
    scrape_docs, scrape_atts, scrape_baselines = query_scrape_db()
    resolved_docs = [d for d in scrape_docs if d.get("status") == "resolved"]
    downloaded_atts = [a for a in scrape_atts if a.get("status") == "downloaded"]
    print(f"    resolved docs: {len(resolved_docs)}")
    print(f"    downloaded attachments: {len(downloaded_atts)}")

    print("[2] Loading Postgres collection_items")
    pg_rows = load_postgres_rows(args.pg_csv)
    print(f"    postgres rows: {len(pg_rows)}")

    print("[3] Loading Vespa metadata")
    vespa_rows = load_vespa_rows(args.vespa_jsonl)
    print(f"    vespa rows: {len(vespa_rows)}")

    print("[4] Loading hydrated identity text")
    hydrated_fields_by_doc_id, hydration_report = load_hydrated_fields(
        None if args.no_hydration_cache else args.hydration_cache_db
    )
    print(f"    hydrated rows: {hydration_report.get('loaded_rows', 0)}")
    print(f"    usable identity rows: {hydration_report.get('usable_identity_rows', 0)}")

    print("[4b] Loading Vespa owner candidates")
    vespa_owner_candidates, vespa_owner_candidate_report = load_vespa_owner_candidates(
        args.vespa_owner_candidates_jsonl
    )
    print(f"    candidate records: {vespa_owner_candidate_report.get('records', 0)}")
    print(f"    accepted hit records: {vespa_owner_candidate_report.get('acceptedHits', 0)}")

    print("[5] Building attachment join decisions")
    join_decisions, join_report = build_join_decisions(downloaded_atts, pg_rows, vespa_rows)
    print(f"    join statuses: {join_report['join_status_counts']}")

    print("[6] Writing staging database")
    write_staging_db(scrape_docs, scrape_atts, pg_rows, vespa_rows, join_decisions, hydrated_fields_by_doc_id)
    print(f"    {STAGING_DB}")

    print("[7] Building graph nodes and edges")
    graph, build_report = build_graph(
        scrape_docs,
        scrape_atts,
        pg_rows,
        vespa_rows,
        join_decisions,
        hydrated_fields_by_doc_id,
        vespa_owner_candidates,
    )
    print(f"    nodes: {len(graph.nodes)}")
    print(f"    edges: {len(graph.edges)}")
    print(f"    cites_document edges: {build_report['cites_document_edges']}")
    print(f"    cites_file edges: {build_report['cites_file_edges']}")

    print("[8] Validating graph")
    validation = validate_graph(graph, scrape_docs, scrape_atts, pg_rows, vespa_rows, join_decisions, build_report)
    print(f"    validation passed: {validation['passed']}")
    if validation["failures"]:
        for failure in validation["failures"][:10]:
            print(f"    FAIL {failure['name']}: expected {failure['expected']}, actual {failure['actual']}")

    print("[9] Persisting graph artifacts")
    persist_report = persist_graph(graph)
    print(f"    {persist_report['sqlite']}")

    final_report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - t0, 2),
        "scrape_baselines": scrape_baselines,
        "hydration_report": hydration_report,
        "vespa_owner_candidate_report": vespa_owner_candidate_report,
        "join_report": join_report,
        "build_report": build_report,
        "validation": validation,
        "persist": persist_report,
        "outputs": {
            "graph_dir": str(GRAPH_DIR),
            "output_dir": str(OUTPUT_DIR),
            "staging_db": str(STAGING_DB),
            "sqlite": str(GRAPH_SQLITE),
            "pickle": str(GRAPH_PICKLE),
        },
    }
    report_path = LOG_DIR / "metadata_graph_v2_build_report.json"
    write_report(report_path, final_report)
    print(f"    report: {report_path}")

    if not validation["passed"] and not args.allow_validation_fail:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
