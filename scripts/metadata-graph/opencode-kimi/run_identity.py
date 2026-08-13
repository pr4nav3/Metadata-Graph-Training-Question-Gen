#!/usr/bin/env python3
"""Shared ID naming helpers for the OpenCode/Kimi pipeline."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


RUN_ID_RE = re.compile(r"^kimi_run_(\d{6})$")
FRONTIER_ID_RE = re.compile(r"^frontier_(\d{4,})$")


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def card_run_id(card: dict[str, Any]) -> str:
    return normalize_ws(card.get("run_id"))


def row_run_id(row: dict[str, Any]) -> str:
    return normalize_ws(row.get("run_id"))


def card_memory_id(card: dict[str, Any]) -> str:
    return normalize_ws(card.get("memory_id"))


def row_memory_id(row: dict[str, Any]) -> str:
    return normalize_ws(row.get("memory_id"))


def frontier_assigned_run_id(row: dict[str, Any]) -> str:
    return normalize_ws(row.get("assigned_run_id"))


def frontier_memory_id(row: dict[str, Any]) -> str:
    return normalize_ws(row.get("memory_id"))


def default_memory_id(frontier_id: str, run_id: str = "") -> str:
    match = FRONTIER_ID_RE.fullmatch(normalize_ws(frontier_id))
    if match:
        return f"memory_{int(match.group(1)):04d}"
    run_id = normalize_ws(run_id)
    return f"memory_{run_id}" if run_id else ""


def next_run_id(run_cards_dir: Path) -> str:
    highest = 0
    if run_cards_dir.exists():
        for path in run_cards_dir.glob("*.json"):
            match = RUN_ID_RE.fullmatch(path.stem)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"kimi_run_{highest + 1:06d}"


def format_values(card: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in card.items():
        if isinstance(value, (str, int, float, bool)):
            values[key] = normalize_ws(value)
    return values
