#!/usr/bin/env python3
"""Prepare, run, and sync batches of frontier-led OpenCode/Kimi run cards."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from coverage_store import DEFAULT_COVERAGE_DB, normalize_ws
from frontier_ledger import (
    DEFAULT_LEDGER_PATH,
    DEFAULT_LEDGER_VIEW_PATH,
    is_schedulable_frontier,
    load_ledger,
    render_markdown,
    update_ledger_frontier,
)
from run_identity import card_run_id, card_memory_id, format_values, frontier_assigned_run_id
from sebi_retrieval import default_graph_db


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
REPO_ROOT = METADATA_GRAPH_DIR.parent.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
DEFAULT_BATCH_ROOT = OUTPUT_ROOT / "batch_runs"
DEFAULT_GRAPH_DB = default_graph_db(METADATA_GRAPH_DIR)
DEFAULT_CACHE_DB = METADATA_GRAPH_DIR / "output" / "hydration" / "doc_cache.sqlite"
DEFAULT_VESPA_URL = (
    os.environ.get("METADATA_KG_VESPA_QUERY_URL")
    or os.environ.get("VESPA_QUERY_URL")
    or "http://localhost:18081/search/"
)
DEFAULT_REVIEW_DATASET = REPO_ROOT / "questions" / "Kimi+Opencode_Questions.csv"
DEFAULT_OPENCODE_CONFIG_HOME = OUTPUT_ROOT / "opencode_config_home"
DEFAULT_OPENCODE_PROVIDER_TIMEOUT_MS = 20 * 60 * 1000
DEFAULT_OPENCODE_MODEL = os.environ.get("OPENCODE_PIPELINE_MODEL", "litellm/kimi-latest")
DEFAULT_OPENCODE_CONTEXT_LIMIT = int(os.environ.get("OPENCODE_PIPELINE_CONTEXT_LIMIT", "256000"))
DEFAULT_OPENCODE_OUTPUT_LIMIT = int(os.environ.get("OPENCODE_PIPELINE_OUTPUT_LIMIT", "32000"))
CREATE_REVIEW_RUN_CARD = SCRIPT_DIR / "create_review_run.py"
REVIEW_AND_EXPORT = SCRIPT_DIR / "review_and_export_questions.py"
class FormatValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"unknown worker command placeholder `{key}`")


def now_batch_id() -> str:
    return time.strftime("frontier_batch_%Y%m%d_%H%M%S")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def has_nonblank_lines(path: Path) -> bool:
    if not path.exists():
        return False
    return any(line.strip() for line in path.read_text(encoding="utf-8").splitlines())


def canonical_opencode_model(value: str) -> str:
    model = normalize_ws(value)
    if not model:
        model = DEFAULT_OPENCODE_MODEL
    if "/" not in model:
        model = f"litellm/{model}"
    return model


def litellm_model_id(opencode_model: str) -> str:
    provider, model_id = canonical_opencode_model(opencode_model).split("/", 1)
    if provider != "litellm" or not model_id:
        raise SystemExit(
            f"unsupported OpenCode model `{opencode_model}`; this pipeline config generator supports litellm/<model>"
        )
    return model_id


def opencode_model_slug(opencode_model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", canonical_opencode_model(opencode_model)).strip("-") or "model"


def opencode_workspace_slug() -> str:
    configured = normalize_ws(os.environ.get("OPENCODE_WORKSPACE_NAMESPACE"))
    if configured:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", configured).strip("-") or "workspace"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", REPO_ROOT.name).strip("-") or "workspace"
    digest = hashlib.sha256(str(REPO_ROOT).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def run_card_path(output_root: Path, run_id: str) -> Path:
    return output_root / "run_cards" / f"{run_id}.json"


def append_log(batch_dir: Path, event: dict[str, Any]) -> None:
    batch_dir.mkdir(parents=True, exist_ok=True)
    event = {"ts": int(time.time()), **event}
    with (batch_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def ensure_opencode_config(args: argparse.Namespace, batch_dir: Path) -> Path:
    """Create the dedicated OpenCode config used by pipeline workers/reviewers."""
    config_home = Path(args.opencode_config_home)
    config_path = config_home / "opencode" / "opencode.json"
    primary_model = canonical_opencode_model(args.opencode_model)
    configured_models = list(
        dict.fromkeys(
            [primary_model]
            + [canonical_opencode_model(item) for item in getattr(args, "opencode_extra_model", []) or []]
        )
    )
    litellm_models = {
        litellm_model_id(model): {
            "name": litellm_model_id(model),
            "modalities": {
                "input": ["text", "image"],
                "output": ["text"],
            },
            "limit": {
                "context": int(args.opencode_context_limit),
                "output": int(args.opencode_output_limit),
            },
        }
        for model in configured_models
    }
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "model": primary_model,
        "agent": {
            "judge": {
                "mode": "primary",
                "temperature": 0.4,
                "top_p": 1,
            },
        },
        "provider": {
            "litellm": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Juspay",
                "options": {
                    "baseURL": "{env:LITELLM_BASE_URL}",
                    "apiKey": "{env:LITELLM_API_KEY}",
                    "timeout": int(args.opencode_provider_timeout_ms),
                },
                "models": litellm_models,
            },
        },
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists() or config_path.read_text(encoding="utf-8") != text:
        config_path.write_text(text, encoding="utf-8")
    append_log(
        batch_dir,
        {
            "event": "opencode_config_prepared",
            "config_home": str(config_home),
            "config_path": str(config_path),
            "opencode_model": primary_model,
            "opencode_models": configured_models,
            "provider_timeout_ms": int(args.opencode_provider_timeout_ms),
        },
    )
    return config_home


def opencode_command_extra(args: argparse.Namespace, batch_dir: Path) -> dict[str, str]:
    config_home = ensure_opencode_config(args, batch_dir)
    return {
        "opencode_config_home": str(config_home),
        "opencode_model": canonical_opencode_model(args.opencode_model),
        "opencode_model_slug": opencode_model_slug(args.opencode_model),
        "opencode_workspace_slug": opencode_workspace_slug(),
        "opencode_provider_timeout_ms": str(int(args.opencode_provider_timeout_ms)),
    }


def ledger_view_path(ledger_path: Path) -> Path:
    if ledger_path == DEFAULT_LEDGER_PATH:
        return DEFAULT_LEDGER_VIEW_PATH
    return ledger_path.with_suffix(".md")


def export_ledger_view(ledger_path: Path, output_path: Path | None = None) -> None:
    output_path = output_path or ledger_view_path(ledger_path)
    rows = load_ledger(ledger_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(rows), encoding="utf-8")


def search_health_check(args: argparse.Namespace, batch_dir: Path) -> bool:
    if getattr(args, "skip_search_health", False):
        append_log(batch_dir, {"event": "search_health_skipped"})
        return True
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "search_health_check.py"),
        "--json",
        "--vespa-url",
        args.vespa_url,
    ]
    if getattr(args, "skip_docker_health", False):
        cmd.append("--skip-docker")
    for name in getattr(args, "health_required_container", []) or []:
        cmd.extend(["--required-container", name])
    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)
    append_log(
        batch_dir,
        {
            "event": "search_health_check",
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        },
    )
    if result.returncode == 0:
        return True
    append_log(batch_dir, {"event": "batch_blocked_search_health"})
    print("search health check failed; no Kimi workers started", file=sys.stderr)
    print(result.stdout.strip() or result.stderr.strip(), file=sys.stderr)
    return False


def select_frontiers(
    ledger_path: Path,
    *,
    frontier_ids: list[str],
    max_runs: int,
    include_needs_review: bool,
) -> list[str]:
    rows = load_ledger(ledger_path)
    if frontier_ids:
        wanted = set(frontier_ids)
        selected = [row["frontier_id"] for row in rows if row["frontier_id"] in wanted]
        missing = sorted(wanted.difference(selected))
        if missing:
            raise ValueError(f"frontier(s) not found: {', '.join(missing)}")
        return selected[:max_runs] if max_runs > 0 else selected

    allowed = {"ready", "continue_generation"}
    if include_needs_review:
        allowed.add("needs_review")
    selected = [
        row["frontier_id"]
        for row in rows
        if row["status"] in allowed
        and (row["status"] == "needs_review" or is_schedulable_frontier(row))
    ]
    return selected[:max_runs] if max_runs > 0 else selected


def prepare_run(
    frontier_id: str,
    *,
    pass_id: str,
    ledger_path: Path,
    output_root: Path,
    coverage_db: Path,
    graph_db: Path,
    max_prior_questions: int,
    batch_dir: Path,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "create_frontier_run.py"),
        "--frontier-id",
        frontier_id,
        "--pass-id",
        pass_id,
        "--frontier-ledger",
        str(ledger_path),
        "--output-root",
        str(output_root),
        "--coverage-db",
        str(coverage_db),
        "--graph-db",
        str(graph_db),
        "--max-prior-questions",
        str(max_prior_questions),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        append_log(
            batch_dir,
            {
                "event": "prepare_failed",
                "frontier_id": frontier_id,
                "returncode": result.returncode,
                "stderr": result.stderr.strip(),
                "stdout": result.stdout.strip(),
            },
        )
        raise RuntimeError(f"failed to prepare {frontier_id}: {result.stderr.strip() or result.stdout.strip()}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"run-card creator returned unexpected output for {frontier_id}: {result.stdout}")
    card = load_json(Path(lines[0]))
    run_id = card_run_id(card)
    append_log(
        batch_dir,
        {
            "event": "prepared",
            "frontier_id": frontier_id,
            "run_id": run_id,
            "run_card_path": normalize_ws(card.get("run_card_path")),
            "prompt_path": card["prompt_path"],
        },
    )
    return card


def command_for_card(template: str, card: dict[str, Any], extra: dict[str, Any] | None = None) -> list[str]:
    values = FormatValues({"cwd": str(REPO_ROOT)})
    values.update(format_values(card))
    for key, value in (extra or {}).items():
        values[key] = normalize_ws(value)
    try:
        rendered = template.format_map(values)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    return shlex.split(rendered)


def command_for_run(template: str, card: dict[str, Any], extra: dict[str, Any] | None = None) -> list[str]:
    return command_for_card(template, card, extra)


def mark_running(card: dict[str, Any], ledger_path: Path) -> None:
    frontier_id = normalize_ws(card.get("frontier_id"))
    if not frontier_id:
        return
    update_ledger_frontier(
        ledger_path,
        frontier_id,
        status="running",
        run_id=card_run_id(card),
        memory_id=card_memory_id(card),
    )


def mark_needs_review(card: dict[str, Any], ledger_path: Path, note: str) -> None:
    frontier_id = normalize_ws(card.get("frontier_id"))
    if not frontier_id:
        return
    update_ledger_frontier(
        ledger_path,
        frontier_id,
        status="needs_review",
        run_id=card_run_id(card),
        memory_id=card_memory_id(card),
        outcome=note,
    )


def mechanical_check(card: dict[str, Any], *, graph_db: Path, cache_db: Path, batch_dir: Path) -> bool:
    output_path = Path(normalize_ws(card.get("output_path")))
    if not has_nonblank_lines(output_path):
        append_log(
            batch_dir,
            {
                "event": "mechanical_skipped",
                "run_id": card_run_id(card),
                "reason": "no nonblank question rows",
            },
        )
        return True
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "mechanical_check_outputs.py"),
        str(output_path),
        "--graph-db",
        str(graph_db),
        "--cache-db",
        str(cache_db),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)
    append_log(
        batch_dir,
        {
            "event": "mechanical_check",
            "run_id": card_run_id(card),
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        },
    )
    return result.returncode == 0


def sync_run(
    card: dict[str, Any],
    *,
    ledger_path: Path,
    coverage_db: Path,
    graph_db: Path,
    cache_db: Path,
    batch_dir: Path,
    skip_mechanical: bool,
    require_summary: bool,
) -> bool:
    run_id = card_run_id(card)
    summary_path = Path(normalize_ws(card.get("summary_path")))
    if require_summary and not summary_path.exists():
        mark_needs_review(card, ledger_path, "worker finished without summary; Codex review required")
        export_ledger_view(ledger_path)
        append_log(batch_dir, {"event": "sync_blocked_missing_summary", "run_id": run_id})
        return False

    if not skip_mechanical and not mechanical_check(card, graph_db=graph_db, cache_db=cache_db, batch_dir=batch_dir):
        mark_needs_review(card, ledger_path, "mechanical check failed; Codex review required before crossing out")
        export_ledger_view(ledger_path)
        append_log(batch_dir, {"event": "sync_blocked_mechanical", "run_id": run_id})
        return False

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "update_coverage_from_run.py"),
        "--run-card",
        normalize_ws(card.get("run_card_path")),
        "--coverage-db",
        str(coverage_db),
        "--frontier-ledger",
        str(ledger_path),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)
    append_log(
        batch_dir,
        {
            "event": "synced",
            "run_id": run_id,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        },
    )
    if result.returncode != 0:
        mark_needs_review(card, ledger_path, "coverage sync failed; Codex review required")
        export_ledger_view(ledger_path)
        return False

    export_ledger_view(ledger_path)
    return True


def load_question_rows(path: Path) -> list[dict[str, Any]]:
    rows, _errors = load_question_rows_with_errors(path)
    return rows


def load_question_rows_with_errors(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not path.exists():
        return rows, [{"line_number": 0, "error": f"file does not exist: {path}", "line_preview": ""}]
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw_line = line.rstrip("\n")
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                errors.append(
                    {
                        "line_number": line_number,
                        "error": "invalid JSON",
                        "line_preview": raw_line[:240],
                    }
                )
                continue
            if not isinstance(value, dict):
                errors.append(
                    {
                        "line_number": line_number,
                        "error": "row is not a JSON object",
                        "line_preview": raw_line[:240],
                    }
                )
                continue
            rows.append(value)
    return rows, errors


def write_question_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sanitize_partial_question_file(
    card: dict[str, Any],
    args: argparse.Namespace,
    batch_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    run_id = card_run_id(card)
    output_path = Path(normalize_ws(card.get("output_path")))
    rows, errors = load_question_rows_with_errors(output_path)
    if not rows or not errors:
        return rows, errors

    raw_dir = args.output_root / "salvage_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{run_id}_{int(time.time())}.jsonl"
    raw_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")
    write_question_rows(output_path, rows)
    append_log(
        batch_dir,
        {
            "event": "worker_partial_salvage_sanitized",
            "run_id": run_id,
            "valid_rows": len(rows),
            "invalid_rows": len(errors),
            "raw_path": str(raw_path),
            "cleaned_output_path": str(output_path),
            "errors": errors[:5],
        },
    )
    return rows, errors


def has_pending_review_rows(path: Path) -> bool:
    return bool(pending_review_question_ids(path))


def pending_review_question_ids(path: Path) -> list[str]:
    ids: list[str] = []
    for row in load_question_rows(path):
        review = row.get("manual_review") if isinstance(row.get("manual_review"), dict) else {}
        status = normalize_ws(review.get("status")).lower() or "pending"
        if status != "pending":
            continue
        qid = normalize_ws(row.get("id") or row.get("question_id"))
        if qid:
            ids.append(qid)
    return ids


def review_decision_ids(path: Path) -> tuple[set[str], str]:
    try:
        raw = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return set(), f"{type(exc).__name__}: {exc}"
    ids: set[str] = set()
    if isinstance(raw, dict) and isinstance(raw.get("decisions"), list):
        for item in raw["decisions"]:
            if isinstance(item, dict):
                qid = normalize_ws(item.get("id") or item.get("question_id"))
                if qid:
                    ids.add(qid)
        return ids, ""
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                qid = normalize_ws(item.get("id") or item.get("question_id"))
                if qid:
                    ids.add(qid)
        return ids, ""
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in {"run_id", "reviewed_by", "review_run_id"}:
                continue
            if isinstance(value, (dict, str)):
                qid = normalize_ws(key)
                if qid:
                    ids.add(qid)
        return ids, ""
    return set(), "decisions JSON is not an object or list"


def review_decision_coverage(output_path: Path, decisions_path: Path) -> dict[str, Any]:
    pending_ids = pending_review_question_ids(output_path)
    decision_ids, error = review_decision_ids(decisions_path)
    missing_ids = [qid for qid in pending_ids if qid not in decision_ids]
    return {
        "pending_ids": pending_ids,
        "decision_ids": sorted(decision_ids),
        "missing_ids": missing_ids,
        "error": error,
        "complete": not error and not missing_ids,
    }


def is_kimi_run_id(run_id: str) -> bool:
    return not normalize_ws(run_id).lower().startswith("opus")


def pending_review_run_ids(output_root: Path, *, max_reviews: int, include_non_kimi: bool) -> list[str]:
    questions_dir = output_root / "questions"
    if not questions_dir.exists():
        return []
    selected: list[str] = []
    seen: set[str] = set()
    for path in sorted(questions_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime):
        rows = load_question_rows(path)
        run_id = ""
        if rows:
            run_id = normalize_ws(rows[0].get("run_id"))
        run_id = run_id or path.stem
        if not include_non_kimi and not is_kimi_run_id(run_id):
            continue
        if run_id in seen:
            continue
        if has_pending_review_rows(path):
            selected.append(run_id)
            seen.add(run_id)
        if max_reviews > 0 and len(selected) >= max_reviews:
            break
    return selected

def create_review_card(
    card: dict[str, Any],
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any] | None:
    run_id = card_run_id(card)
    cmd = [
        sys.executable,
        str(CREATE_REVIEW_RUN_CARD),
        "--run-id",
        run_id,
        "--output-root",
        str(args.output_root),
    ]
    if getattr(args, "force_review", False) or overwrite:
        cmd.append("--overwrite")
    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)
    append_log(
        batch_dir,
        {
            "event": "review_run_card_create",
            "run_id": run_id,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        },
    )
    if result.returncode == 3:
        decisions_path = args.output_root / "review_decisions" / f"{run_id}.json"
        if decisions_path.exists():
            review_card_path = args.output_root / "review_run_cards" / f"{run_id}.json"
            if review_card_path.exists():
                return load_json(review_card_path)
        return None
    if result.returncode != 0:
        mark_needs_review(card, args.ledger, "review run-card creation failed; inspect review setup")
        export_ledger_view(args.ledger)
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        mark_needs_review(card, args.ledger, "review run-card creator returned no path")
        export_ledger_view(args.ledger)
        return None
    return load_json(Path(lines[0]))


def export_review_decisions(
    card: dict[str, Any],
    review_card: dict[str, Any],
    args: argparse.Namespace,
    batch_dir: Path,
) -> bool:
    run_id = card_run_id(card)
    decisions_path = Path(normalize_ws(review_card.get("decisions_path")))
    cmd = [
        sys.executable,
        str(REVIEW_AND_EXPORT),
        "--run-id",
        run_id,
        "--decisions-json",
        str(decisions_path),
        "--dataset",
        str(args.review_dataset),
        "--coverage-db",
        str(args.coverage_db),
        "--output-root",
        str(args.output_root),
    ]
    lock_path = args.output_root / "review_export.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            result = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    append_log(
        batch_dir,
        {
            "event": "review_export",
            "run_id": run_id,
            "decisions_path": str(decisions_path),
            "lock_path": str(lock_path),
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        },
    )
    if result.returncode != 0:
        mark_needs_review(card, args.ledger, "review export failed; inspect decisions JSON")
        export_ledger_view(args.ledger)
        return False
    return True


def run_reviewer_if_needed(card: dict[str, Any], args: argparse.Namespace, batch_dir: Path) -> bool:
    ok, review = start_reviewer_if_needed(card, args, batch_dir)
    if not ok or not review:
        return ok

    run_id = card_run_id(card)
    process = review["process"]
    try:
        returncode = process.wait(timeout=args.reviewer_timeout_seconds if args.reviewer_timeout_seconds > 0 else None)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        close_reviewer_handles(review)
        append_log(
            batch_dir,
            {
                "event": "reviewer_timeout",
                "run_id": run_id,
                "timeout_seconds": args.reviewer_timeout_seconds,
            },
        )
        mark_needs_review(card, args.ledger, "question reviewer timed out; inspect review logs")
        export_ledger_view(args.ledger)
        return False
    return finish_reviewer(review, args, batch_dir, returncode=returncode)


def close_reviewer_handles(review: dict[str, Any]) -> None:
    for key in ("stdout_handle", "stderr_handle"):
        handle = review.get(key)
        if handle and not handle.closed:
            handle.close()


def start_reviewer_if_needed(
    card: dict[str, Any],
    args: argparse.Namespace,
    batch_dir: Path,
) -> tuple[bool, dict[str, Any] | None]:
    if not getattr(args, "reviewer_command", ""):
        return True, None

    run_id = card_run_id(card)
    output_path = Path(normalize_ws(card.get("output_path")))
    if not has_nonblank_lines(output_path):
        append_log(batch_dir, {"event": "reviewer_skipped_no_questions", "run_id": run_id})
        return True, None

    decisions_path = args.output_root / "review_decisions" / f"{run_id}.json"
    overwrite_review_card = False
    if decisions_path.exists() and has_nonblank_lines(decisions_path) and not getattr(args, "force_review", False):
        coverage = review_decision_coverage(output_path, decisions_path)
        if not coverage["complete"]:
            overwrite_review_card = True
            append_log(
                batch_dir,
                {
                    "event": "reviewer_existing_decisions_incomplete",
                    "run_id": run_id,
                    "decisions_path": str(decisions_path),
                    "pending_count": len(coverage["pending_ids"]),
                    "decision_count": len(coverage["decision_ids"]),
                    "missing_ids": coverage["missing_ids"][:20],
                    "error": coverage["error"],
                },
            )
        else:
            append_log(
                batch_dir,
                {
                    "event": "reviewer_skipped_existing_decisions",
                    "run_id": run_id,
                    "decisions_path": str(decisions_path),
                },
            )
            if getattr(args, "skip_review_export", False):
                return True, None
            review_card_path = args.output_root / "review_run_cards" / f"{run_id}.json"
            if review_card_path.exists():
                return export_review_decisions(card, load_json(review_card_path), args, batch_dir), None
            return True, None

    if not pending_review_question_ids(output_path) and not getattr(args, "force_review", False):
        append_log(
            batch_dir,
            {
                "event": "reviewer_skipped_no_pending_rows",
                "run_id": run_id,
            },
        )
        return True, None

    review_card = create_review_card(card, args, batch_dir, overwrite=overwrite_review_card)
    if not review_card:
        return False, None

    command = command_for_card(args.reviewer_command, review_card, opencode_command_extra(args, batch_dir))
    stdout_path = batch_dir / f"{run_id}.reviewer.stdout.log"
    stderr_path = batch_dir / f"{run_id}.reviewer.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=stdout_handle,
        stderr=stderr_handle,
    )
    append_log(
        batch_dir,
            {
                "event": "reviewer_started",
                "run_id": run_id,
                "review_run_id": normalize_ws(review_card.get("review_run_id")),
                "pid": process.pid,
                "command": command,
            },
    )
    return True, {
        "process": process,
        "card": card,
        "review_card": review_card,
        "stdout_handle": stdout_handle,
        "stderr_handle": stderr_handle,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "started_at": time.time(),
    }


def finish_reviewer(
    review: dict[str, Any],
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    returncode: int,
) -> bool:
    close_reviewer_handles(review)
    card = review["card"]
    review_card = review["review_card"]
    run_id = card_run_id(card)
    append_log(
        batch_dir,
        {
            "event": "reviewer_finished",
            "run_id": run_id,
            "returncode": returncode,
            "stdout_path": str(review["stdout_path"]),
            "stderr_path": str(review["stderr_path"]),
        },
    )
    if returncode != 0:
        mark_needs_review(card, args.ledger, f"question reviewer exited {returncode}; inspect review logs")
        export_ledger_view(args.ledger)
        return False

    decisions_path = Path(normalize_ws(review_card.get("decisions_path")))
    if not has_nonblank_lines(decisions_path):
        append_log(
            batch_dir,
            {
                "event": "reviewer_missing_decisions",
                "run_id": run_id,
                "decisions_path": str(decisions_path),
            },
        )
        mark_needs_review(card, args.ledger, "question reviewer finished without decisions JSON")
        export_ledger_view(args.ledger)
        return False

    if getattr(args, "skip_review_export", False):
        return True
    return export_review_decisions(card, review_card, args, batch_dir)


def poll_reviewers(
    running_reviewers: list[dict[str, Any]],
    args: argparse.Namespace,
    batch_dir: Path,
) -> tuple[list[dict[str, Any]], int]:
    still_running: list[dict[str, Any]] = []
    failures = 0
    for review in running_reviewers:
        process = review["process"]
        card = review["card"]
        run_id = card_run_id(card)
        returncode = process.poll()
        if returncode is None and args.reviewer_timeout_seconds > 0:
            elapsed = time.time() - review["started_at"]
            if elapsed > args.reviewer_timeout_seconds:
                process.terminate()
                try:
                    returncode = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait()
                close_reviewer_handles(review)
                append_log(
                    batch_dir,
                    {
                        "event": "reviewer_timeout",
                        "run_id": run_id,
                        "timeout_seconds": args.reviewer_timeout_seconds,
                    },
                )
                mark_needs_review(card, args.ledger, "question reviewer timed out; inspect review logs")
                export_ledger_view(args.ledger)
                failures += 1
                continue
        if returncode is None:
            still_running.append(review)
            continue
        if not finish_reviewer(review, args, batch_dir, returncode=returncode):
            failures += 1
    return still_running, failures


def start_pending_reviewers(
    review_queue: list[dict[str, Any]],
    running_reviewers: list[dict[str, Any]],
    args: argparse.Namespace,
    batch_dir: Path,
) -> int:
    failures = 0
    reviewer_parallel = max(1, int(getattr(args, "parallel", 1) or 1))
    while review_queue and len(running_reviewers) < reviewer_parallel:
        card = review_queue.pop(0)
        ok, review = start_reviewer_if_needed(card, args, batch_dir)
        if not ok:
            failures += 1
        elif review:
            running_reviewers.append(review)
    return failures


def prepare_many(args: argparse.Namespace) -> list[dict[str, Any]]:
    batch_dir = args.batch_root / args.batch_id
    frontier_ids = select_frontiers(
        args.ledger,
        frontier_ids=args.frontier_id,
        max_runs=args.max_runs,
        include_needs_review=args.include_needs_review,
    )
    cards: list[dict[str, Any]] = []
    for frontier_id in frontier_ids:
        card = prepare_run(
            frontier_id,
            pass_id=args.pass_id,
            ledger_path=args.ledger,
            output_root=args.output_root,
            coverage_db=args.coverage_db,
            graph_db=args.graph_db,
            max_prior_questions=args.max_prior_questions,
            batch_dir=batch_dir,
        )
        cards.append(card)
    export_ledger_view(args.ledger)
    append_log(batch_dir, {"event": "prepare_complete", "count": len(cards)})
    return cards


def salvage_partial_worker_output(
    card: dict[str, Any],
    args: argparse.Namespace,
    batch_dir: Path,
    *,
    returncode: int,
    reason: str,
) -> bool:
    run_id = card_run_id(card)
    output_path = Path(normalize_ws(card.get("output_path")))
    summary_path = Path(normalize_ws(card.get("summary_path")))
    rows, parse_errors = sanitize_partial_question_file(card, args, batch_dir)
    if not rows:
        append_log(
            batch_dir,
            {
                "event": "worker_partial_salvage_skipped",
                "run_id": run_id,
                "reason": "no valid JSON question rows",
                "output_path": str(output_path),
                "returncode": returncode,
                "parse_errors": parse_errors[:5],
            },
        )
        mark_needs_review(card, args.ledger, f"{reason}; no valid question rows to salvage")
        export_ledger_view(args.ledger)
        return False

    append_log(
        batch_dir,
            {
                "event": "worker_partial_salvage_candidate",
                "run_id": run_id,
                "reason": reason,
            "valid_rows": len(rows),
            "invalid_rows_dropped": len(parse_errors),
            "summary_exists": summary_path.exists(),
            "returncode": returncode,
        },
    )

    if not args.skip_mechanical and not mechanical_check(card, graph_db=args.graph_db, cache_db=args.cache_db, batch_dir=batch_dir):
        mark_needs_review(
            card,
            args.ledger,
            f"{reason}; partial question rows failed mechanical check",
        )
        export_ledger_view(args.ledger)
        append_log(batch_dir, {"event": "worker_partial_salvage_blocked_mechanical", "run_id": run_id})
        return False

    if not sync_run(
        card,
        ledger_path=args.ledger,
        coverage_db=args.coverage_db,
        graph_db=args.graph_db,
        cache_db=args.cache_db,
        batch_dir=batch_dir,
        skip_mechanical=True,
        require_summary=False,
    ):
        return False

    if not summary_path.exists():
        mark_needs_review(
            card,
            args.ledger,
            f"partial_timeout - salvaged {len(rows)} valid question row(s) after {reason}; summary missing and frontier may still need follow-up",
        )
        export_ledger_view(args.ledger)

    if not run_reviewer_if_needed(card, args, batch_dir):
        return False

    if not summary_path.exists():
        mark_needs_review(
            card,
            args.ledger,
            f"partial_timeout - salvaged and reviewed/exported {len(rows)} valid question row(s); summary missing and frontier may still need follow-up",
        )
        export_ledger_view(args.ledger)

    append_log(
        batch_dir,
        {
            "event": "worker_partial_salvage_complete",
            "run_id": run_id,
            "valid_rows": len(rows),
            "summary_exists": summary_path.exists(),
        },
    )
    return True


def run_cards(args: argparse.Namespace, cards: list[dict[str, Any]]) -> int:
    if not args.worker_command:
        print(f"{args.command} requires --worker-command; use `prepare` to create run cards only", file=sys.stderr)
        return 2
    batch_dir = args.batch_root / args.batch_id
    pending = list(cards)
    running: list[tuple[subprocess.Popen[str], dict[str, Any], Any, Any, float]] = []
    review_queue: list[dict[str, Any]] = []
    running_reviewers: list[dict[str, Any]] = []
    failures = 0
    salvages = 0

    while pending or running or review_queue or running_reviewers:
        while pending and len(running) < args.parallel:
            card = pending.pop(0)
            run_id = card_run_id(card)
            command = command_for_run(args.worker_command, card, opencode_command_extra(args, batch_dir))
            mark_running(card, args.ledger)
            stdout_path = batch_dir / f"{run_id}.stdout.log"
            stderr_path = batch_dir / f"{run_id}.stderr.log"
            stdout_handle = stdout_path.open("w", encoding="utf-8")
            stderr_handle = stderr_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                text=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
            running.append((process, card, stdout_handle, stderr_handle, time.time()))
            append_log(
                batch_dir,
                {
                    "event": "worker_started",
                    "run_id": run_id,
                    "frontier_id": normalize_ws(card.get("frontier_id")),
                    "pid": process.pid,
                    "command": command,
                },
            )
        time.sleep(args.poll_seconds)
        still_running: list[tuple[subprocess.Popen[str], dict[str, Any], Any, Any, float]] = []
        for process, card, stdout_handle, stderr_handle, started_at in running:
            run_id = card_run_id(card)
            returncode = process.poll()
            if returncode is None and args.worker_timeout_seconds > 0:
                elapsed = time.time() - started_at
                if elapsed > args.worker_timeout_seconds:
                    process.terminate()
                    try:
                        returncode = process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        returncode = process.wait()
                    stdout_handle.close()
                    stderr_handle.close()
                    append_log(
                        batch_dir,
                        {
                            "event": "worker_timeout",
                            "run_id": run_id,
                            "elapsed_seconds": round(elapsed, 1),
                            "returncode": returncode,
                        },
                    )
                    reason = f"worker timed out after {int(args.worker_timeout_seconds)}s"
                    if salvage_partial_worker_output(card, args, batch_dir, returncode=returncode, reason=reason):
                        salvages += 1
                    else:
                        failures += 1
                    continue
            if returncode is None:
                still_running.append((process, card, stdout_handle, stderr_handle, started_at))
                continue
            stdout_handle.close()
            stderr_handle.close()
            append_log(batch_dir, {"event": "worker_finished", "run_id": run_id, "returncode": returncode})
            if returncode != 0:
                reason = f"worker command exited {returncode}"
                if salvage_partial_worker_output(card, args, batch_dir, returncode=returncode, reason=reason):
                    salvages += 1
                else:
                    failures += 1
                continue
            if not sync_run(
                card,
                ledger_path=args.ledger,
                coverage_db=args.coverage_db,
                graph_db=args.graph_db,
                cache_db=args.cache_db,
                batch_dir=batch_dir,
                skip_mechanical=args.skip_mechanical,
                require_summary=True,
            ):
                failures += 1
                continue
            if getattr(args, "reviewer_command", ""):
                review_queue.append(card)
                append_log(batch_dir, {"event": "reviewer_queued", "run_id": run_id})
        running = still_running
        failures += start_pending_reviewers(review_queue, running_reviewers, args, batch_dir)
        running_reviewers, reviewer_failures = poll_reviewers(running_reviewers, args, batch_dir)
        failures += reviewer_failures
        failures += start_pending_reviewers(review_queue, running_reviewers, args, batch_dir)

    export_ledger_view(args.ledger)
    append_log(batch_dir, {"event": "run_complete", "failures": failures, "salvages": salvages})
    return 0 if failures == 0 else 1


def run_many(args: argparse.Namespace) -> int:
    if not search_health_check(args, args.batch_root / args.batch_id):
        return 2
    cards = prepare_many(args)
    return run_cards(args, cards)


def run_existing_many(args: argparse.Namespace) -> int:
    if not args.run_id:
        print("run-existing requires at least one --run-id", file=sys.stderr)
        return 2
    batch_dir = args.batch_root / args.batch_id
    cards: list[dict[str, Any]] = []
    for run_id in args.run_id:
        path = run_card_path(args.output_root, run_id)
        if not path.exists():
            print(f"run card not found: {path}", file=sys.stderr)
            return 2
        card = load_json(path)
        cards.append(card)
        append_log(
            batch_dir,
            {
                "event": "loaded_existing_run",
                "run_id": card_run_id(card),
                "frontier_id": normalize_ws(card.get("frontier_id")),
                "run_card_path": str(path),
            },
        )
    if not search_health_check(args, batch_dir):
        return 2
    return run_cards(args, cards)


def sync_many(args: argparse.Namespace) -> int:
    batch_dir = args.batch_root / args.batch_id
    rows = load_ledger(args.ledger)
    if args.frontier_id:
        allowed_ids = set(args.frontier_id)
        rows = [row for row in rows if row["frontier_id"] in allowed_ids]
    else:
        rows = [row for row in rows if row["status"] in {"assigned", "running", "needs_review"}]
    synced = 0
    failures = 0
    for row in rows:
        run_id = frontier_assigned_run_id(row)
        if not run_id:
            continue
        path = run_card_path(args.output_root, run_id)
        if not path.exists():
            append_log(
                batch_dir,
                {
                    "event": "sync_missing_run_card",
                    "frontier_id": row["frontier_id"],
                    "run_id": run_id,
                    "run_card_path": str(path),
                },
            )
            continue
        card = load_json(path)
        if sync_run(
            card,
            ledger_path=args.ledger,
            coverage_db=args.coverage_db,
            graph_db=args.graph_db,
            cache_db=args.cache_db,
            batch_dir=batch_dir,
            skip_mechanical=args.skip_mechanical,
            require_summary=args.require_summary,
        ):
            synced += 1
            if not run_reviewer_if_needed(card, args, batch_dir):
                failures += 1
        else:
            failures += 1
    export_ledger_view(args.ledger)
    append_log(batch_dir, {"event": "sync_complete", "synced": synced, "failures": failures})
    print(f"synced={synced} failures={failures}")
    return 0 if failures == 0 else 1


def review_many(args: argparse.Namespace) -> int:
    if not args.reviewer_command:
        print("review requires --reviewer-command", file=sys.stderr)
        return 2
    batch_dir = args.batch_root / args.batch_id
    run_ids = list(args.run_id)
    if not run_ids:
        run_ids = pending_review_run_ids(
            args.output_root,
            max_reviews=args.max_reviews,
            include_non_kimi=args.include_non_kimi_reviews,
        )
    review_queue: list[dict[str, Any]] = []
    running_reviewers: list[dict[str, Any]] = []
    reviewed = 0
    failures = 0
    for run_id in run_ids:
        path = run_card_path(args.output_root, run_id)
        if not path.exists():
            append_log(
                batch_dir,
                {
                    "event": "review_missing_run_card",
                    "run_id": run_id,
                    "run_card_path": str(path),
                },
            )
            failures += 1
            continue
        review_queue.append(load_json(path))
    while review_queue or running_reviewers:
        while review_queue and len(running_reviewers) < max(1, args.parallel):
            card = review_queue.pop(0)
            ok, review = start_reviewer_if_needed(card, args, batch_dir)
            if not ok:
                failures += 1
            elif review:
                running_reviewers.append(review)
            else:
                reviewed += 1
        if not running_reviewers:
            continue
        time.sleep(args.poll_seconds)
        before = len(running_reviewers)
        running_reviewers, reviewer_failures = poll_reviewers(running_reviewers, args, batch_dir)
        finished = before - len(running_reviewers)
        failures += reviewer_failures
        reviewed += max(0, finished - reviewer_failures)
    append_log(batch_dir, {"event": "review_complete", "reviewed": reviewed, "failures": failures})
    print(f"reviewed={reviewed} failures={failures}")
    return 0 if failures == 0 else 1


def print_status(args: argparse.Namespace) -> int:
    rows = load_ledger(args.ledger)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    for status in ["ready", "continue_generation", "assigned", "running", "needs_review", "saturated", "rejected", "duplicate"]:
        print(f"{status}={counts.get(status, 0)}")
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--coverage-db", type=Path, default=DEFAULT_COVERAGE_DB)
    parser.add_argument("--graph-db", type=Path, default=DEFAULT_GRAPH_DB)
    parser.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE_DB)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    parser.add_argument("--batch-id", default=now_batch_id())
    parser.add_argument("--opencode-config-home", type=Path, default=DEFAULT_OPENCODE_CONFIG_HOME)
    parser.add_argument(
        "--opencode-model",
        default=DEFAULT_OPENCODE_MODEL,
        help="Primary OpenCode model for generated config. Bare names are treated as litellm/<name>.",
    )
    parser.add_argument(
        "--opencode-extra-model",
        action="append",
        default=[],
        help="Additional litellm/<model> to declare in the generated OpenCode config, e.g. reviewer model.",
    )
    parser.add_argument("--opencode-context-limit", type=int, default=DEFAULT_OPENCODE_CONTEXT_LIMIT)
    parser.add_argument("--opencode-output-limit", type=int, default=DEFAULT_OPENCODE_OUTPUT_LIMIT)
    parser.add_argument("--opencode-provider-timeout-ms", type=int, default=DEFAULT_OPENCODE_PROVIDER_TIMEOUT_MS)


def add_search_health_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skip-search-health", action="store_true")
    parser.add_argument("--skip-docker-health", action="store_true")
    parser.add_argument("--vespa-url", default=DEFAULT_VESPA_URL)
    parser.add_argument("--health-required-container", action="append", default=[])


def add_review_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reviewer-command", default="")
    parser.add_argument("--reviewer-timeout-seconds", type=float, default=1500.0)
    parser.add_argument("--review-dataset", type=Path, default=DEFAULT_REVIEW_DATASET)
    parser.add_argument("--skip-review-export", action="store_true")
    parser.add_argument("--force-review", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Create run cards from schedulable frontiers.")
    add_common(prepare_parser)
    prepare_parser.add_argument("--frontier-id", action="append", default=[])
    prepare_parser.add_argument("--max-runs", type=int, default=1)
    prepare_parser.add_argument("--pass-id", default=now_batch_id())
    prepare_parser.add_argument("--max-prior-questions", type=int, default=25)
    prepare_parser.add_argument("--include-needs-review", action="store_true")

    run_parser = subparsers.add_parser("run", help="Create run cards and launch worker commands.")
    add_common(run_parser)
    run_parser.add_argument("--frontier-id", action="append", default=[])
    run_parser.add_argument("--max-runs", type=int, default=1)
    run_parser.add_argument("--pass-id", default=now_batch_id())
    run_parser.add_argument("--max-prior-questions", type=int, default=25)
    run_parser.add_argument("--include-needs-review", action="store_true")
    run_parser.add_argument("--parallel", type=int, default=1)
    run_parser.add_argument("--poll-seconds", type=float, default=5.0)
    run_parser.add_argument("--worker-command", default="")
    run_parser.add_argument("--worker-timeout-seconds", type=float, default=0)
    run_parser.add_argument("--skip-mechanical", action="store_true")
    add_search_health_options(run_parser)
    add_review_options(run_parser)

    run_existing_parser = subparsers.add_parser("run-existing", help="Launch worker commands for existing run cards.")
    add_common(run_existing_parser)
    run_existing_parser.add_argument("--run-id", action="append", default=[])
    run_existing_parser.add_argument("--parallel", type=int, default=1)
    run_existing_parser.add_argument("--poll-seconds", type=float, default=5.0)
    run_existing_parser.add_argument("--worker-command", default="")
    run_existing_parser.add_argument("--worker-timeout-seconds", type=float, default=0)
    run_existing_parser.add_argument("--skip-mechanical", action="store_true")
    add_search_health_options(run_existing_parser)
    add_review_options(run_existing_parser)

    sync_parser = subparsers.add_parser("sync", help="Sync completed assigned/running frontier runs.")
    add_common(sync_parser)
    sync_parser.add_argument("--frontier-id", action="append", default=[])
    sync_parser.add_argument("--skip-mechanical", action="store_true")
    sync_parser.add_argument("--require-summary", action="store_true")
    add_review_options(sync_parser)

    review_parser = subparsers.add_parser("review", help="Run Kimi review for pending question runs.")
    add_common(review_parser)
    review_parser.add_argument("--run-id", action="append", default=[])
    review_parser.add_argument("--max-reviews", type=int, default=1)
    review_parser.add_argument("--parallel", type=int, default=1)
    review_parser.add_argument("--poll-seconds", type=float, default=5.0)
    review_parser.add_argument("--include-non-kimi-reviews", action="store_true")
    add_review_options(review_parser)

    status_parser = subparsers.add_parser("status", help="Print ledger status counts.")
    add_common(status_parser)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "prepare":
            cards = prepare_many(args)
            for card in cards:
                print(f"{card['frontier_id']}\t{card_run_id(card)}\t{card['prompt_path']}")
            return 0
        if args.command == "run":
            return run_many(args)
        if args.command == "run-existing":
            return run_existing_many(args)
        if args.command == "sync":
            return sync_many(args)
        if args.command == "review":
            return review_many(args)
        if args.command == "status":
            return print_status(args)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
