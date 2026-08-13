#!/usr/bin/env python3
"""Global OpenCode/Kimi supervisor for exploration, QA generation, and review."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from coverage_store import normalize_ws
from run_frontier_batch import (
    DEFAULT_CACHE_DB,
    DEFAULT_OPENCODE_CONFIG_HOME,
    DEFAULT_OPENCODE_CONTEXT_LIMIT,
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_OPENCODE_OUTPUT_LIMIT,
    DEFAULT_OPENCODE_PROVIDER_TIMEOUT_MS,
    DEFAULT_REVIEW_DATASET,
    DEFAULT_VESPA_URL,
    canonical_opencode_model,
    ensure_opencode_config,
    opencode_model_slug,
    opencode_workspace_slug,
    pending_review_question_ids,
    review_decision_coverage,
)


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
REPO_ROOT = METADATA_GRAPH_DIR.parent.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
GLOBAL_ROOT = OUTPUT_ROOT / "global_supervisor"
SNAPSHOT_ROOT = GLOBAL_ROOT / "snapshots"
LOG_ROOT = GLOBAL_ROOT / "logs"
DECISION_ROOT = GLOBAL_ROOT / "decisions"
STATUS_PATH = GLOBAL_ROOT / "status.md"
LOCK_PATH = GLOBAL_ROOT / "global_supervisor.lock"
RUN_WITH_ENV = SCRIPT_DIR / "run_with_server_env.sh"
RUN_FRONTIER_BATCH = SCRIPT_DIR / "run_frontier_batch.py"
RUN_EXPLORER_BATCH = SCRIPT_DIR / "run_explorer_batch.py"
VALIDATE_FRONTIERS = SCRIPT_DIR / "validate_frontier_candidates.py"
SEARCH_HEALTH = SCRIPT_DIR / "search_health_check.py"
LEDGER_PATH = OUTPUT_ROOT / "frontier_ledger.jsonl"
QUESTIONS_DIR = OUTPUT_ROOT / "questions"
REVIEW_DECISIONS_DIR = OUTPUT_ROOT / "review_decisions"
RUN_CARDS_DIR = OUTPUT_ROOT / "run_cards"
FRONTIER_CANDIDATES_DIR = OUTPUT_ROOT / "frontier_candidates"
FRONTIER_FAILED_CANDIDATES_DIR = OUTPUT_ROOT / "frontier_candidate_failures"
FRONTIER_VALIDATIONS_DIR = OUTPUT_ROOT / "frontier_candidate_validations"
DATASET_PATH = REPO_ROOT / "questions" / "Kimi+Opencode_Questions.csv"
HANDOFF_DIR = SCRIPT_DIR / "handoff"

SNAPSHOT_SCHEMA_VERSION = 1
ALLOWED_ACTIONS = {
    "status_only",
    "validate_frontier_candidates",
    "start_explorers",
    "start_question_batch",
    "review_questions",
    "sync_runs",
}
PROCESS_HINTS = (
    "run_frontier_batch.py run",
    "run_frontier_batch.py review",
    "run_frontier_batch.py sync",
    "run_explorer_batch.py run",
    "opencode-worker-",
    "opencode-reviewer-",
    "opencode-explorer-",
    "kimi_run_",
    "explorer_run_",
)


def classify_process(command: str) -> str:
    if "run_explorer_batch.py run" in command:
        return "explorer_harness"
    if "run_frontier_batch.py run" in command:
        return "worker_harness"
    if "run_frontier_batch.py review" in command:
        return "review_harness"
    if "run_frontier_batch.py sync" in command:
        return "sync_harness"
    if "opencode-explorer-" in command or "--title explorer_run_" in command:
        return "explorer_agent"
    if "opencode-worker-" in command or "--title kimi_run_" in command:
        return "worker_agent"
    if "opencode-reviewer-" in command or "--title review-" in command:
        return "reviewer_agent"
    return ""


def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return local_now().isoformat(timespec="seconds")


def repo_display(path: Path | str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return str(candidate)
    try:
        return str(candidate.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(candidate)


def portable_text(value: Any) -> str:
    text = str(value or "")
    repo_root = str(REPO_ROOT)
    home = str(Path.home())
    if repo_root:
        text = text.replace(repo_root, ".")
    if home and home != "/":
        text = text.replace(home, "$HOME")
    return text


def portable_value(value: Any) -> Any:
    if isinstance(value, str):
        return portable_text(value)
    if isinstance(value, Path):
        return repo_display(value)
    if isinstance(value, list):
        return [portable_value(item) for item in value]
    if isinstance(value, dict):
        return {key: portable_value(item) for key, item in value.items()}
    return value


def stamp() -> str:
    return local_now().strftime("%Y%m%d_%H%M%S")


def truncate(value: Any, limit: int = 400) -> str:
    text = normalize_ws(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def run_command(command: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    started = time.time()
    payload: dict[str, Any] = {
        "command": portable_value(command),
        "started_at": iso_now(),
        "timeout_seconds": timeout_seconds,
    }
    try:
        result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, timeout=timeout_seconds)
    except FileNotFoundError as exc:
        payload.update({"ok": False, "returncode": None, "error": portable_text(f"executable not found: {exc}")})
    except OSError as exc:
        payload.update({"ok": False, "returncode": None, "error": portable_text(f"{type(exc).__name__}: {exc}")})
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        payload.update(
            {
                "ok": False,
                "timeout": True,
                "returncode": None,
                "stdout": portable_text(stdout[-6000:]),
                "stderr": portable_text(stderr[-6000:]),
            }
        )
    else:
        payload.update(
            {
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": portable_text(result.stdout[-6000:]),
                "stderr": portable_text(result.stderr[-6000:]),
            }
        )
    payload["elapsed_seconds"] = round(time.time() - started, 2)
    return payload


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def collect_search_health(args: argparse.Namespace) -> dict[str, Any]:
    if args.skip_search_health:
        return {"ok": None, "skipped": True, "message": "search health skipped"}
    command = [sys.executable, str(SEARCH_HEALTH), "--json", "--vespa-url", args.vespa_url]
    result = run_command(command, timeout_seconds=args.search_timeout_seconds)
    parsed: dict[str, Any] | None = None
    if result.get("stdout"):
        try:
            parsed = json.loads(result["stdout"])
        except json.JSONDecodeError:
            parsed = None
    return {
        "ok": bool(parsed.get("ok")) if isinstance(parsed, dict) else False,
        "skipped": False,
        "checks": parsed.get("checks") if isinstance(parsed, dict) else [],
        "command_result": result,
    }


def collect_ledger() -> dict[str, Any]:
    rows = read_jsonl(LEDGER_PATH)
    counts: dict[str, int] = {}
    for row in rows:
        status = normalize_ws(row.get("status")) or "unknown"
        counts[status] = counts.get(status, 0) + 1

    def brief(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "frontier_id": normalize_ws(row.get("frontier_id")),
            "status": normalize_ws(row.get("status")),
            "title": truncate(row.get("title"), 180),
            "run_id": normalize_ws(row.get("assigned_run_id")),
            "memory_id": normalize_ws(row.get("memory_id")),
            "outcome": truncate(row.get("outcome"), 220),
            "updated_at": row.get("updated_at"),
        }

    return {
        "path": repo_display(LEDGER_PATH),
        "exists": LEDGER_PATH.exists(),
        "counts": counts,
        "ready_sample": [brief(row) for row in rows if normalize_ws(row.get("status")) == "ready"][:8],
        "continue_generation_sample": [brief(row) for row in rows if normalize_ws(row.get("status")) == "continue_generation"][:8],
        "assigned_rows": [brief(row) for row in rows if normalize_ws(row.get("status")) == "assigned"],
        "running_rows": [brief(row) for row in rows if normalize_ws(row.get("status")) == "running"],
        "needs_review_rows": [brief(row) for row in rows if normalize_ws(row.get("status")) == "needs_review"],
    }


def collect_processes() -> dict[str, Any]:
    result = run_command(["ps", "-axo", "pid=,ppid=,stat=,etime=,command="], timeout_seconds=15)
    active: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}
    current_pid = os.getpid()
    for line in (result.get("stdout") or "").splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        pid, ppid, stat, elapsed, command = parts
        if int(pid) == current_pid or "ps -axo" in command:
            continue
        process_type = classify_process(command)
        if process_type or any(hint in command for hint in PROCESS_HINTS):
            if process_type:
                type_counts[process_type] = type_counts.get(process_type, 0) + 1
            active.append(
                {
                    "pid": int(pid),
                    "ppid": int(ppid) if ppid.isdigit() else ppid,
                    "stat": stat,
                    "elapsed": elapsed,
                    "type": process_type or "unknown",
                    "command": truncate(command, 900),
                }
            )
    explorer_agents = int(type_counts.get("explorer_agent") or 0)
    worker_agents = int(type_counts.get("worker_agent") or 0)
    reviewer_agents = int(type_counts.get("reviewer_agent") or 0)
    worker_side_agents = worker_agents + reviewer_agents
    explorer_harnesses = int(type_counts.get("explorer_harness") or 0)
    worker_harnesses = (
        int(type_counts.get("worker_harness") or 0)
        + int(type_counts.get("review_harness") or 0)
        + int(type_counts.get("sync_harness") or 0)
    )
    return {
        "command_result": {key: value for key, value in result.items() if key != "stdout"},
        "process_scan_ok": bool(result.get("ok")),
        "active_processes": active,
        "active_process_count": len(active),
        "process_type_counts": type_counts,
        "explorer_agent_count": explorer_agents,
        "worker_agent_count": worker_agents,
        "reviewer_agent_count": reviewer_agents,
        "worker_side_agent_count": worker_side_agents,
        "explorer_harness_count": explorer_harnesses,
        "worker_harness_count": worker_harnesses,
    }


def collect_pending_questions() -> dict[str, Any]:
    totals = {"rows": 0, "pending": 0, "accepted": 0, "rejected": 0, "other": 0}
    files: list[dict[str, Any]] = []
    if not QUESTIONS_DIR.exists():
        return {"totals": totals, "files_with_pending": []}
    for path in sorted(QUESTIONS_DIR.glob("*.jsonl")):
        counts = {"rows": 0, "pending": 0, "accepted": 0, "rejected": 0, "other": 0}
        for row in read_jsonl(path):
            if not normalize_ws(row.get("id") or row.get("question_id")):
                continue
            counts["rows"] += 1
            totals["rows"] += 1
            review = row.get("manual_review") if isinstance(row.get("manual_review"), dict) else {}
            status = normalize_ws(review.get("status")).lower() or "pending"
            if status not in {"pending", "accepted", "rejected"}:
                status = "other"
            counts[status] += 1
            totals[status] += 1
        if counts["pending"]:
            files.append({"path": repo_display(path), **counts})
    return {"totals": totals, "files_with_pending": files}


def collect_review_state() -> dict[str, Any]:
    pending_runs: list[dict[str, Any]] = []
    for item in collect_pending_questions().get("files_with_pending", []):
        path = Path(item["path"])
        rows = read_jsonl(path)
        run_id = normalize_ws(rows[0].get("run_id")) if rows else path.stem
        decisions = REVIEW_DECISIONS_DIR / f"{run_id}.json"
        pending_ids = pending_review_question_ids(path)
        coverage = {
            "pending_ids": pending_ids,
            "decision_ids": [],
            "missing_ids": pending_ids,
            "error": "",
            "complete": False,
        }
        if decisions.exists() and decisions.stat().st_size > 0:
            coverage = review_decision_coverage(path, decisions)
        if not decisions.exists() or decisions.stat().st_size == 0:
            coverage_status = "missing"
        elif coverage.get("error"):
            coverage_status = "invalid"
        elif coverage.get("complete"):
            coverage_status = "complete"
        else:
            coverage_status = "incomplete"
        pending_runs.append(
            {
                "run_id": run_id,
                "is_kimi_run": not run_id.lower().startswith("opus"),
                "pending_question_count": item["pending"],
                "questions_path": repo_display(path),
                "run_card_exists": (RUN_CARDS_DIR / f"{run_id}.json").exists(),
                "decisions_exist": decisions.exists() and decisions.stat().st_size > 0,
                "decision_coverage": coverage_status,
                "decision_count": len(coverage.get("decision_ids") or []),
                "missing_decision_count": len(coverage.get("missing_ids") or []),
                "missing_decision_ids": list(coverage.get("missing_ids") or [])[:20],
                "decision_error": normalize_ws(coverage.get("error")),
            }
        )
    return {
        "pending_run_reviews": pending_runs,
        "pending_kimi_run_review_count": sum(1 for item in pending_runs if item["is_kimi_run"]),
        "pending_non_kimi_run_review_count": sum(1 for item in pending_runs if not item["is_kimi_run"]),
        "incomplete_decision_run_count": sum(1 for item in pending_runs if item["decision_coverage"] in {"incomplete", "invalid"}),
    }


def collect_explorer_state() -> dict[str, Any]:
    candidate_files = sorted(FRONTIER_CANDIDATES_DIR.glob("*.jsonl")) if FRONTIER_CANDIDATES_DIR.exists() else []
    failed_candidate_files = sorted(FRONTIER_FAILED_CANDIDATES_DIR.glob("*.jsonl")) if FRONTIER_FAILED_CANDIDATES_DIR.exists() else []
    validation_files = sorted(FRONTIER_VALIDATIONS_DIR.glob("*.json")) if FRONTIER_VALIDATIONS_DIR.exists() else []
    promoted = 0
    for path in validation_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        promoted += int(data.get("promoted_count") or 0)
    return {
        "candidate_file_count": len(candidate_files),
        "unvalidated_candidate_file_count": len(candidate_files),
        "unvalidated_candidate_files": [repo_display(path) for path in candidate_files[:12]],
        "failed_candidate_file_count": len(failed_candidate_files),
        "failed_candidate_files": [repo_display(path) for path in failed_candidate_files[:12]],
        "validation_file_count": len(validation_files),
        "promoted_count_from_reports": promoted,
    }


def last_events(root: Path, *, limit: int = 6) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    event_paths = sorted(root.glob("*/events.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for path in event_paths[:limit]:
        events = read_jsonl(path)
        out.append(
            {
                "batch_id": path.parent.name,
                "events_path": repo_display(path),
                "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                "last_event": events[-1] if events else {},
            }
        )
    return out


def decision_signals(snapshot: dict[str, Any], args: argparse.Namespace) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    search = snapshot["search_health"]
    ledger_counts = snapshot["ledger"]["counts"]
    processes = snapshot["processes"]
    pending = snapshot["pending_questions"]["totals"]
    explorer = snapshot["explorer_state"]
    review = snapshot["review_state"]
    search_ok = search.get("ok") is True
    active_total_agents = int(processes.get("explorer_agent_count") or 0) + int(processes.get("worker_side_agent_count") or 0)
    total_slots = max(0, args.max_total_agents - active_total_agents)
    explorer_slots = min(max(0, args.max_explorer_agents - int(processes.get("explorer_agent_count") or 0)), total_slots)
    worker_slots = min(max(0, args.max_worker_agents - int(processes.get("worker_side_agent_count") or 0)), total_slots)
    explorer_harnesses = int(processes.get("explorer_harness_count") or 0)
    worker_harnesses = int(processes.get("worker_harness_count") or 0)
    ready_count = int(ledger_counts.get("ready") or 0)
    schedulable_count = ready_count + int(ledger_counts.get("continue_generation") or 0)
    pending_count = int(pending.get("pending") or 0)

    if not processes.get("process_scan_ok"):
        return [{"code": "process_scan_failed", "message": "Could not inspect active processes; non-status actions will be blocked."}]
    if search.get("ok") is False:
        out.append({"code": "search_unhealthy", "message": "Search-dependent work is unsafe until search health passes."})
    if processes.get("active_process_count"):
        out.append({"code": "agents_active", "message": f"Active capacity: total {active_total_agents}/{args.max_total_agents}, explorers {processes.get('explorer_agent_count', 0)}/{args.max_explorer_agents}, worker-side {processes.get('worker_side_agent_count', 0)}/{args.max_worker_agents}."})
    if explorer.get("unvalidated_candidate_file_count"):
        if explorer_harnesses or int(processes.get("explorer_agent_count") or 0):
            out.append({"code": "candidate_validation_waiting", "message": "Explorer candidate files exist while explorer work is still active."})
        else:
            out.append({"code": "candidate_validation_ready", "message": "Explorer candidate files are waiting for deterministic validation/promotion."})
    if explorer.get("failed_candidate_file_count"):
        out.append({"code": "frontier_candidate_failures", "message": "Some candidate files failed validation and were moved out of the live queue for inspection."})

    if ready_count < args.min_ready_frontiers and search_ok:
        if explorer_slots and not explorer_harnesses:
            out.append({"code": "frontier_buffer_low", "message": f"Ready-frontier count is {ready_count}, below target {args.min_ready_frontiers}; explorer slots are available."})
        else:
            out.append({"code": "frontier_buffer_low_no_explorer_slot", "message": "Ready-frontier count is low, but explorer capacity or harness state is busy."})
    elif ready_count < args.min_ready_frontiers:
        out.append({"code": "frontier_buffer_low_search_blocked", "message": "Ready-frontier count is below target, but search health is not passing."})
    else:
        out.append({"code": "frontier_buffer_healthy", "message": f"Ready-frontier count is {ready_count}, at or above target {args.min_ready_frontiers}."})

    if review.get("incomplete_decision_run_count") and worker_slots and not worker_harnesses:
        out.append({"code": "incomplete_review_decisions", "message": "At least one pending run has incomplete/invalid decision coverage; review can rerun those pending decisions instead of re-exporting stale partial decisions."})
    elif pending_count >= args.max_pending_questions_before_generation and worker_slots and not worker_harnesses:
        out.append({"code": "review_backlog_high", "message": "Review backlog is at or above the configured generation guardrail."})
    elif pending_count and worker_harnesses:
        out.append({"code": "review_backlog_waiting_on_worker_side", "message": "Generated question rows are pending review/export, but a worker-side harness is active."})
    elif pending_count:
        out.append({"code": "review_backlog_present", "message": f"{pending_count} generated question row(s) are pending review/export."})

    if pending_count >= args.max_pending_questions_before_generation:
        out.append({"code": "generation_backlog_guardrail", "message": "Question generation will be blocked while pending review backlog is at or above the configured threshold."})
    elif schedulable_count > 0 and search_ok:
        if worker_slots and not worker_harnesses:
            out.append({"code": "question_generation_available", "message": "Schedulable frontiers exist, search is healthy, backlog is below the guardrail, and worker-side slots are available."})
        else:
            out.append({"code": "question_generation_waiting_on_worker_side", "message": "Schedulable frontiers exist, but worker-side capacity or harness state is busy."})
    elif schedulable_count <= 0:
        out.append({"code": "no_schedulable_frontiers", "message": "No ready or continue_generation frontiers are currently schedulable."})

    if not out:
        out.append({"code": "quiet", "message": "No obvious bottleneck signal was detected."})
    if review.get("pending_non_kimi_run_review_count"):
        out.append({"code": "non_kimi_pending", "message": "Non-Kimi pending reviews exist; review only if explicitly allowed."})
    return out


def build_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": iso_now(),
        "repo_root": repo_display(REPO_ROOT),
        "output_root": repo_display(OUTPUT_ROOT),
        "handoff_index": repo_display(HANDOFF_DIR / "README.md"),
        "search_health": collect_search_health(args),
        "ledger": collect_ledger(),
        "processes": collect_processes(),
        "pending_questions": collect_pending_questions(),
        "review_state": collect_review_state(),
        "explorer_state": collect_explorer_state(),
        "recent_question_batches": last_events(OUTPUT_ROOT / "batch_runs"),
        "recent_explorer_batches": last_events(OUTPUT_ROOT / "explorer_batch_runs"),
        "dataset": {"path": repo_display(DATASET_PATH), "row_count": count_csv_rows(DATASET_PATH)},
        "policy": {
            "min_ready_frontiers": args.min_ready_frontiers,
            "max_pending_questions_before_generation": args.max_pending_questions_before_generation,
            "max_total_agents": args.max_total_agents,
            "max_explorer_agents": args.max_explorer_agents,
            "max_worker_agents": args.max_worker_agents,
            "max_explorers_per_tick": args.max_explorers_per_tick,
            "max_explorer_parallel": args.max_explorer_parallel,
            "max_question_runs_per_tick": args.max_question_runs_per_tick,
            "max_worker_parallel": args.max_worker_parallel,
            "max_reviews_per_tick": args.max_reviews_per_tick,
        },
    }
    snapshot["decision_signals"] = decision_signals(snapshot, args)
    return snapshot


def write_snapshot(snapshot: dict[str, Any]) -> Path:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_ROOT / f"snapshot_{stamp()}.json"
    snapshot["snapshot_path"] = repo_display(path)
    portable_snapshot = portable_value(snapshot)
    snapshot.clear()
    snapshot.update(portable_snapshot)
    path.write_text(json.dumps(portable_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def render_status(snapshot: dict[str, Any], execution: dict[str, Any] | None = None) -> str:
    counts = snapshot["ledger"]["counts"]
    search = snapshot["search_health"]
    pending = snapshot["pending_questions"]["totals"]
    explorer = snapshot["explorer_state"]
    policy = snapshot.get("policy", {})
    processes = snapshot["processes"]
    active_total_agents = int(processes.get("explorer_agent_count") or 0) + int(processes.get("worker_side_agent_count") or 0)
    lines = [
        "# Global Kimi/OpenCode Supervisor Status",
        "",
        f"Generated: {snapshot['generated_at']}",
        f"Snapshot: `{snapshot.get('snapshot_path', '')}`",
        "",
        "## Current State",
        f"- Search health: {'skipped' if search.get('skipped') else ('pass' if search.get('ok') else 'fail')}",
        f"- Process scan: {'pass' if processes.get('process_scan_ok') else 'fail'}",
        f"- Active processes: {processes.get('active_process_count', 0)}",
        f"- Agent slots: total={active_total_agents}/{policy.get('max_total_agents', 0)} explorers={processes.get('explorer_agent_count', 0)}/{policy.get('max_explorer_agents', 0)} worker_side={processes.get('worker_side_agent_count', 0)}/{policy.get('max_worker_agents', 0)}",
        f"- Active harnesses: explorers={processes.get('explorer_harness_count', 0)} worker_side={processes.get('worker_harness_count', 0)}",
        f"- Ledger: ready={counts.get('ready', 0)} continue_generation={counts.get('continue_generation', 0)} assigned={counts.get('assigned', 0)} running={counts.get('running', 0)} needs_review={counts.get('needs_review', 0)} saturated={counts.get('saturated', 0)}",
        f"- Pending generated questions: {pending.get('pending', 0)}",
        f"- Pending reviews: kimi={snapshot['review_state'].get('pending_kimi_run_review_count', 0)} non_kimi={snapshot['review_state'].get('pending_non_kimi_run_review_count', 0)}",
        f"- Explorer candidates: files={explorer.get('candidate_file_count', 0)} unvalidated={explorer.get('unvalidated_candidate_file_count', 0)} promoted_from_reports={explorer.get('promoted_count_from_reports', 0)}",
        f"- Explorer candidate failures: files={explorer.get('failed_candidate_file_count', 0)}",
        f"- Export dataset rows: {snapshot['dataset'].get('row_count', 0)}",
        "",
        "## Decision Signals",
    ]
    for item in snapshot.get("decision_signals", []):
        lines.append(f"- `{item.get('code')}`: {item.get('message')}")
    if execution:
        lines.extend(["", "## Last Execution", f"- action: `{execution.get('action')}`", f"- ok: {execution.get('ok')}", f"- reason: {execution.get('reason', '')}"])
        if execution.get("command_result"):
            result = execution["command_result"]
            lines.append(f"- returncode: {result.get('returncode')}")
            if result.get("stderr"):
                lines.append(f"- stderr: {truncate(result.get('stderr'), 700)}")
    return "\n".join(lines).rstrip() + "\n"


def write_status(snapshot: dict[str, Any], execution: dict[str, Any] | None = None) -> None:
    GLOBAL_ROOT.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(render_status(snapshot, execution), encoding="utf-8")


def worker_command(model: str) -> str:
    return (
        "env 'XDG_CONFIG_HOME={opencode_config_home}' "
        "XDG_DATA_HOME=/private/tmp/opencode-worker-{opencode_workspace_slug}-{opencode_model_slug}-{run_id} "
        "opencode run --dir '{cwd}' --agent judge -m {opencode_model} --auto --title {run_id} "
        "\"Read and execute the run prompt at {prompt_path}. Follow the run card and agent instructions exactly. "
        "Write the required artifacts to the paths in the run card. Do not modify pipeline code. Do not call background subagents.\""
    )


def reviewer_command(model: str) -> str:
    return (
        "env 'XDG_CONFIG_HOME={opencode_config_home}' "
        "XDG_DATA_HOME=/private/tmp/opencode-reviewer-{opencode_workspace_slug}-{opencode_model_slug}-{run_id} "
        "opencode run --dir '{cwd}' --agent judge -m {opencode_model} --auto --title review-{run_id} "
        "\"Read and execute the review prompt at {prompt_path}. Write decisions to {decisions_path} "
        "and the review summary to {review_summary_path}. Do not generate new questions.\""
    )


def build_wake_prompt(snapshot_path: Path, decision_path: Path, args: argparse.Namespace) -> str:
    decision_display = repo_display(decision_path)
    snapshot_display = repo_display(snapshot_path)
    runbook_display = repo_display(HANDOFF_DIR / "SUPERVISOR_RUNBOOK.md")
    handoff_display = repo_display(HANDOFF_DIR / "README.md")
    state_machine_display = repo_display(HANDOFF_DIR / "PIPELINE_STATE_MACHINE.md")
    recovery_display = repo_display(HANDOFF_DIR / "KNOWN_FAILURES_AND_RECOVERY.md")
    return f"""You are the global OpenCode/Kimi supervisor for the SEBI memory-training QA pipeline.

HARD SAFETY RULES - READ FIRST:
- You are the judgment layer. You supervise; you do not repair.
- Never edit code, prompts, configs, the live frontier ledger, accepted datasets, generated question files, review decisions, or batch artifacts.
- Never run arbitrary shell commands. Your only control surface is the single JSON decision at the end of this wakeup.
- Never write {decision_display} yourself. Deterministic Python code records your JSON decision and executes only whitelisted actions.
- Never start search-dependent work unless the snapshot says search_health.ok is true.
- Never start work if the active-process scan failed. Unknown process state is unsafe.
- Never exceed the concurrency policy below. The executor will clamp or block counts, but you should reason with the limits yourself.
- If the correct next step needs code changes, manual ledger surgery, file deletion, arbitrary commands, or human interpretation outside the allowed actions, choose status_only and state the exact blocker.

CONCURRENCY POLICY:
- The total spawned explorer + worker-side LLM agents must be <= {args.max_total_agents}.
- Explorer agents running concurrently must be <= {args.max_explorer_agents}.
- Worker-side agents running concurrently must be <= {args.max_worker_agents}. Worker-side agents include question generators and reviewers.
- Explorers and worker-side agents may run side by side when both pools have capacity.
- Do not start another explorer batch while an explorer harness is active.
- Do not start another worker/review/sync batch while a worker-side harness is active.
- Prefer side-by-side progress over filling every possible slot with one kind of work.
- Unused slots are acceptable when the per-side cap preserves capacity for the other side.

READING CONTRACT:
- Read {runbook_display} for the decision charter.
- Read {snapshot_display} completely enough to understand the whole pipeline state.
- Consult these only when the snapshot raises a relevant question: {handoff_display}, {state_machine_display}, {recovery_display}.
- Treat snapshot decision_signals as observations, not commands. They exist to keep you from missing state, not to choose for you.

YOUR OBJECTIVE:
Maximize long-run accepted, evidence-grounded SEBI-document memory QA coverage. Think like an operator looking at the whole board, not like a script matching the first condition.

Before choosing, understand:
- Infrastructure: search health, process scan, stale or active harnesses, and available total slots.
- Frontier supply: ready/continue_generation count, whether the ready buffer is below {args.min_ready_frontiers}, candidate files awaiting validation, and failed candidate files.
- Worker side: pending generated rows, per-run decision coverage, incomplete/invalid reviews, active generators/reviewers, and whether recent batches changed state.
- Progress: accepted dataset row count, recent batch events, repeated actions that produced no new rows or no state transition.
- Risk: anything that looks corrupt, ambiguous, looping, partially written, or outside the allowed actions.

OPERATING GUIDELINES:
- Use your judgment to choose the highest-value legal action for this wakeup. A small pending review backlog should not monopolize the whole pipeline when exploration or generation is the real bottleneck.
- Keep the frontier buffer healthy over time. A buffer below {args.min_ready_frontiers} is a real supply problem, but it is not a reason to ignore urgent worker-side failures.
- Do not start explorers merely to hoard frontier supply above {args.min_ready_frontiers}. Once the buffer is at or above that target, spend worker-side capacity on generation/review when it is healthy to do so.
- Question generation is useful when schedulable frontiers exist, search is healthy, worker-side capacity is free, and pending reviews are below the configured guardrail ({args.max_pending_questions_before_generation}). Generation batches start reviewers as generators finish.
- Review is useful when pending rows are large enough to threaten throughput, when generation is unavailable, or when reviewer decisions are missing/incomplete/invalid.
- If a reviewer failed to decide all pending rows, you may choose review_questions to rerun review for the remaining pending decisions. The runner/exporter now checks decision coverage and avoids re-exporting stale partial decisions.
- Candidate validation is useful after explorers finish and candidate files are waiting. It is deterministic and promotes only basic-valid candidates.
- Do not repeat a no-progress action just because it is available. If the same target/action recently produced no dataset growth and no state change, choose a different legal action or status_only with the stuck condition.
- When active harnesses are already doing useful work, it is often correct to choose status_only so the next wakeup sees fresh artifacts. If the other side of the pipeline has independent capacity and a clear bottleneck, you may use it.

ALLOWED ACTIONS:
- status_only: observe, log a blocker, or wait for active work.
- validate_frontier_candidates: run deterministic validation/promotion for explorer candidate files.
- start_explorers: spawn OpenCode explorer agents to produce frontier candidates.
- start_question_batch: spawn question-generation workers; their reviewers run immediately as each generator finishes.
- review_questions: spawn reviewer/export workers for pending generated rows, including incomplete or invalid decision coverage.
- sync_runs: run mechanical sync/export where existing artifacts need deterministic bookkeeping.

Schema:
{{
  "action": "status_only|validate_frontier_candidates|start_explorers|start_question_batch|review_questions|sync_runs",
  "reason": "short reason grounded in the full snapshot, including the bottleneck you are addressing",
  "params": {{
    "count": {args.max_explorers_per_tick},
    "max_runs": {args.max_question_runs_per_tick},
    "max_reviews": {args.max_reviews_per_tick}
  }}
}}

COUNT GUIDANCE:
- Omit params when you want the executor to use currently available capacity.
- Use count for start_explorers, max_runs for start_question_batch, max_reviews for review_questions.
- Pick smaller counts when state is ambiguous; use more capacity when the bottleneck is clear and healthy.

CAPS ENFORCED BY CODE:
- total spawned agents concurrently <= {args.max_total_agents}
- explorer agents concurrently <= {args.max_explorer_agents}
- worker-side agents concurrently <= {args.max_worker_agents}
- explorers started per tick <= {args.max_explorers_per_tick}
- question runs started per tick <= {args.max_question_runs_per_tick}
- reviews per tick <= {args.max_reviews_per_tick}

Return exactly one JSON object and nothing else.
"""


def wake_supervisor(snapshot_path: Path, decision_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    prompt_path = LOG_ROOT / f"wake_{stamp()}.prompt.md"
    prompt = build_wake_prompt(snapshot_path, decision_path, args)
    prompt_path.write_text(prompt, encoding="utf-8")
    ensure_opencode_config(args, LOG_ROOT)

    command = [
        str(RUN_WITH_ENV),
        args.opencode_bin,
        "run",
        "--dir",
        str(REPO_ROOT),
        "--agent",
        args.agent,
        "-m",
        canonical_opencode_model(args.opencode_model),
        "--title",
        args.title,
        prompt,
    ]
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(args.opencode_config_home)
    env["XDG_DATA_HOME"] = (
        f"/private/tmp/opencode-global-supervisor-{opencode_workspace_slug()}-"
        f"{opencode_model_slug(args.opencode_model)}"
    )
    if args.dry_run:
        return {"ok": True, "dry_run": True, "prompt_path": repo_display(prompt_path), "command": portable_value(command[:-1] + ["<prompt>"])}
    started = time.time()
    try:
        result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, timeout=args.supervisor_timeout_seconds, env=env)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            "ok": False,
            "timeout": True,
            "prompt_path": repo_display(prompt_path),
            "stdout": portable_text(stdout[-6000:]),
            "stderr": portable_text(stderr[-6000:]),
        }
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "prompt_path": repo_display(prompt_path),
        "stdout": portable_text(result.stdout[-6000:]),
        "stderr": portable_text(result.stderr[-6000:]),
        "elapsed_seconds": round(time.time() - started, 2),
    }


def extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and normalize_ws(value.get("action")) in ALLOWED_ACTIONS:
            return value
    return None


def decision_from_wake(wake: dict[str, Any], decision_path: Path) -> dict[str, Any]:
    if not wake.get("ok"):
        decision = {"action": "status_only", "reason": "supervisor wake failed or timed out", "params": {}}
    else:
        decision = extract_json_object(normalize_ws(wake.get("stdout")))
        if not isinstance(decision, dict):
            decision = {"action": "status_only", "reason": "supervisor did not return a JSON decision", "params": {}}
    portable_decision = portable_value(decision)
    decision_path.write_text(json.dumps(portable_decision, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return portable_decision


def run_command_background(command: list[str], *, label: str) -> dict[str, Any]:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    stdout_path = LOG_ROOT / f"{label}_{stamp()}.stdout.log"
    stderr_path = LOG_ROOT / f"{label}_{stamp()}.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    except Exception as exc:
        stdout_handle.close()
        stderr_handle.close()
        return {"ok": False, "error": portable_text(f"{type(exc).__name__}: {exc}"), "command": portable_value(command)}
    stdout_handle.close()
    stderr_handle.close()
    return {
        "ok": True,
        "background": True,
        "pid": process.pid,
        "command": portable_value(command),
        "stdout_path": repo_display(stdout_path),
        "stderr_path": repo_display(stderr_path),
    }


def execute_decision(decision: dict[str, Any], snapshot: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    action = normalize_ws(decision.get("action")) or "status_only"
    params = decision.get("params") if isinstance(decision.get("params"), dict) else {}
    reason = normalize_ws(decision.get("reason"))
    if action not in ALLOWED_ACTIONS:
        return {"ok": False, "action": action, "reason": f"blocked unknown action: {action}"}

    processes = snapshot["processes"]
    search_ok = snapshot["search_health"].get("ok") is True
    pending_questions = int(snapshot["pending_questions"]["totals"].get("pending") or 0)
    explorer_agent_count = int(processes.get("explorer_agent_count") or 0)
    worker_side_agent_count = int(processes.get("worker_side_agent_count") or 0)
    explorer_harness_count = int(processes.get("explorer_harness_count") or 0)
    worker_harness_count = int(processes.get("worker_harness_count") or 0)
    active_total_agents = explorer_agent_count + worker_side_agent_count
    total_slots = max(0, args.max_total_agents - active_total_agents)
    explorer_slots = min(max(0, args.max_explorer_agents - explorer_agent_count), total_slots)
    worker_slots = min(max(0, args.max_worker_agents - worker_side_agent_count), total_slots)
    if action != "status_only" and not processes.get("process_scan_ok"):
        return {"ok": False, "action": action, "reason": "blocked because active-process scan failed"}
    if action in {"start_explorers", "start_question_batch"} and not search_ok:
        return {"ok": False, "action": action, "reason": "blocked because search health is not passing"}
    if action == "validate_frontier_candidates" and (explorer_agent_count or explorer_harness_count):
        return {"ok": False, "action": action, "reason": "blocked because explorers are still active"}
    if action == "start_explorers" and explorer_harness_count:
        return {"ok": False, "action": action, "reason": "blocked because an explorer batch harness is already active"}
    if action == "start_explorers" and explorer_slots <= 0:
        return {"ok": False, "action": action, "reason": "blocked because explorer agent capacity is full"}
    if action in {"start_question_batch", "review_questions", "sync_runs"} and worker_harness_count:
        return {"ok": False, "action": action, "reason": "blocked because a worker-side batch harness is already active"}
    if action in {"start_question_batch", "review_questions", "sync_runs"} and worker_slots <= 0:
        return {"ok": False, "action": action, "reason": "blocked because worker-side agent capacity is full"}
    if action == "start_question_batch" and pending_questions >= args.max_pending_questions_before_generation:
        return {"ok": False, "action": action, "reason": "blocked because pending review backlog is at or above threshold"}
    if action == "status_only":
        return {"ok": True, "action": action, "reason": reason or "status only"}

    command: list[str]
    timeout = args.action_timeout_seconds
    model = canonical_opencode_model(args.opencode_model)
    if action == "validate_frontier_candidates":
        command = [sys.executable, str(VALIDATE_FRONTIERS), "--promote"]
        timeout = min(timeout, 300)
    elif action == "start_explorers":
        requested = int(params.get("count") or explorer_slots or 1)
        count = max(1, min(requested, args.max_explorers_per_tick, explorer_slots))
        command = [
            str(RUN_WITH_ENV),
            sys.executable,
            str(RUN_EXPLORER_BATCH),
            "run",
            "--count",
            str(count),
            "--parallel",
            str(min(count, args.max_explorer_parallel, explorer_slots)),
            "--opencode-model",
            model,
            "--opencode-provider-timeout-ms",
            str(args.opencode_provider_timeout_ms),
            "--explorer-timeout-seconds",
            str(args.explorer_timeout_seconds),
        ]
        timeout = args.explorer_timeout_seconds * count + 120
    elif action == "start_question_batch":
        requested = int(params.get("max_runs") or worker_slots or 1)
        max_runs = max(1, min(requested, args.max_question_runs_per_tick, worker_slots))
        command = [
            str(RUN_WITH_ENV),
            sys.executable,
            str(RUN_FRONTIER_BATCH),
            "run",
            "--max-runs",
            str(max_runs),
            "--parallel",
            str(min(max_runs, args.max_worker_parallel, worker_slots)),
            "--poll-seconds",
            "20",
            "--worker-timeout-seconds",
            str(args.question_timeout_seconds),
            "--opencode-model",
            model,
            "--opencode-provider-timeout-ms",
            str(args.opencode_provider_timeout_ms),
            "--worker-command",
            worker_command(model),
            "--reviewer-timeout-seconds",
            str(args.review_timeout_seconds),
            "--reviewer-command",
            reviewer_command(model),
        ]
        timeout = args.question_timeout_seconds * max_runs + args.review_timeout_seconds * max_runs + 180
    elif action == "review_questions":
        requested = int(params.get("max_reviews") or worker_slots or 1)
        max_reviews = max(1, min(requested, args.max_reviews_per_tick, worker_slots))
        command = [
            str(RUN_WITH_ENV),
            sys.executable,
            str(RUN_FRONTIER_BATCH),
            "review",
            "--max-reviews",
            str(max_reviews),
            "--parallel",
            str(min(max_reviews, args.max_worker_parallel, worker_slots)),
            "--opencode-model",
            model,
            "--opencode-provider-timeout-ms",
            str(args.opencode_provider_timeout_ms),
            "--reviewer-timeout-seconds",
            str(args.review_timeout_seconds),
            "--reviewer-command",
            reviewer_command(model),
        ]
        timeout = args.review_timeout_seconds * max_reviews + 120
    else:
        command = [
            str(RUN_WITH_ENV),
            sys.executable,
            str(RUN_FRONTIER_BATCH),
            "sync",
            "--reviewer-timeout-seconds",
            str(args.review_timeout_seconds),
            "--reviewer-command",
            reviewer_command(model),
        ]

    if args.dry_run:
        return {"ok": True, "action": action, "reason": reason, "dry_run_command": portable_value(command)}
    if action in {"start_explorers", "start_question_batch", "review_questions", "sync_runs"}:
        result = run_command_background(command, label=action)
        return {"ok": bool(result.get("ok")), "action": action, "reason": reason, "command_result": result}
    result = run_command(command, timeout_seconds=timeout)
    return {"ok": bool(result.get("ok")), "action": action, "reason": reason, "command_result": result}


@contextmanager
def supervisor_lock() -> Any:
    GLOBAL_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_once(args: argparse.Namespace) -> int:
    DECISION_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot(args)
    snapshot_path = write_snapshot(snapshot)
    decision_path = DECISION_ROOT / f"decision_{stamp()}.json"
    wake = wake_supervisor(snapshot_path, decision_path, args) if not args.no_wake else {"ok": True, "skipped": True}
    if args.no_wake:
        decision = {"action": "status_only", "reason": "wake skipped", "params": {}}
        decision_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif args.dry_run and wake.get("dry_run"):
        decision = {"action": "status_only", "reason": "dry run; supervisor wake command prepared but not executed", "params": {}}
        decision_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        decision = decision_from_wake(wake, decision_path)
    execution = execute_decision(decision, snapshot, args)
    execution["wake"] = wake
    execution_path = LOG_ROOT / f"execution_{stamp()}.json"
    execution = portable_value(execution)
    execution_path.write_text(json.dumps({"decision": decision, "execution": execution}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_status(snapshot, execution)
    print(f"snapshot={repo_display(snapshot_path)}")
    print(f"decision={repo_display(decision_path)}")
    print(f"execution={repo_display(execution_path)}")
    print(f"status={repo_display(STATUS_PATH)}")
    print(f"action={execution.get('action')} ok={execution.get('ok')}")
    return 0


def run_with_lock(args: argparse.Namespace) -> int:
    try:
        with supervisor_lock():
            return run_once(args)
    except BlockingIOError:
        GLOBAL_ROOT.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(f"# Global Kimi/OpenCode Supervisor Status\n\nGenerated: {iso_now()}\n\nAnother supervisor tick is already running.\n", encoding="utf-8")
        print("another supervisor tick is already running", file=sys.stderr)
        return 0


def run_loop(args: argparse.Namespace) -> int:
    deadline = time.time() + max(0, args.duration_hours) * 3600
    while time.time() <= deadline:
        run_with_lock(args)
        if time.time() + args.interval_seconds > deadline:
            break
        time.sleep(args.interval_seconds)
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skip-search-health", action="store_true")
    parser.add_argument("--search-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--vespa-url", default=DEFAULT_VESPA_URL)
    parser.add_argument("--opencode-bin", default=os.environ.get("OPENCODE_BIN", "opencode"))
    parser.add_argument("--opencode-model", default=os.environ.get("OPENCODE_PIPELINE_MODEL", DEFAULT_OPENCODE_MODEL))
    parser.add_argument("--opencode-extra-model", action="append", default=[])
    parser.add_argument("--opencode-config-home", type=Path, default=DEFAULT_OPENCODE_CONFIG_HOME)
    parser.add_argument("--opencode-context-limit", type=int, default=DEFAULT_OPENCODE_CONTEXT_LIMIT)
    parser.add_argument("--opencode-output-limit", type=int, default=DEFAULT_OPENCODE_OUTPUT_LIMIT)
    parser.add_argument("--opencode-provider-timeout-ms", type=int, default=DEFAULT_OPENCODE_PROVIDER_TIMEOUT_MS)
    parser.add_argument("--agent", default=os.environ.get("GLOBAL_SUPERVISOR_AGENT", "judge"))
    parser.add_argument("--title", default="global-kimi-opencode-supervisor")
    parser.add_argument("--supervisor-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--action-timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--explorer-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--question-timeout-seconds", type=float, default=1500.0)
    parser.add_argument("--review-timeout-seconds", type=float, default=1500.0)
    parser.add_argument("--min-ready-frontiers", type=int, default=50)
    parser.add_argument("--max-pending-questions-before-generation", type=int, default=25)
    parser.add_argument("--max-total-agents", type=int, default=6)
    parser.add_argument("--max-explorer-agents", type=int, default=3)
    parser.add_argument("--max-worker-agents", type=int, default=3)
    parser.add_argument("--max-explorers-per-tick", type=int, default=3)
    parser.add_argument("--max-explorer-parallel", type=int, default=3)
    parser.add_argument("--max-question-runs-per-tick", type=int, default=3)
    parser.add_argument("--max-worker-parallel", type=int, default=3)
    parser.add_argument("--max-reviews-per-tick", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-wake", action="store_true", help="Write a snapshot/status without calling OpenCode.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    once_parser = subparsers.add_parser("once", help="Run one locked global supervisor tick.")
    add_common(once_parser)

    snapshot_parser = subparsers.add_parser("snapshot", help="Write snapshot/status only.")
    add_common(snapshot_parser)
    snapshot_parser.set_defaults(no_wake=True)

    loop_parser = subparsers.add_parser("loop", help="Run repeated global supervisor ticks for a bounded duration.")
    add_common(loop_parser)
    loop_parser.add_argument("--interval-seconds", type=float, default=480.0)
    loop_parser.add_argument("--duration-hours", type=float, default=24.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "loop":
        return run_loop(args)
    return run_with_lock(args)


if __name__ == "__main__":
    raise SystemExit(main())
