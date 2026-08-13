#!/usr/bin/env python3
"""Spawn OpenCode frontier-explorer runs that write candidate frontiers only."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from coverage_store import normalize_ws
from run_frontier_batch import (
    DEFAULT_OPENCODE_CONFIG_HOME,
    DEFAULT_OPENCODE_CONTEXT_LIMIT,
    DEFAULT_OPENCODE_MODEL,
    DEFAULT_OPENCODE_OUTPUT_LIMIT,
    DEFAULT_OPENCODE_PROVIDER_TIMEOUT_MS,
    canonical_opencode_model,
    ensure_opencode_config,
    opencode_model_slug,
    opencode_workspace_slug,
)


SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_GRAPH_DIR = SCRIPT_DIR.parent
REPO_ROOT = METADATA_GRAPH_DIR.parent.parent
OUTPUT_ROOT = METADATA_GRAPH_DIR / "output" / "opencode_kimi"
DEFAULT_BATCH_ROOT = OUTPUT_ROOT / "explorer_batch_runs"
DEFAULT_EXPLORER_MODEL = (
    normalize_ws(os.environ.get("OPENCODE_EXPLORER_MODEL"))
    or DEFAULT_OPENCODE_MODEL
)
FRONTIER_LEDGER = SCRIPT_DIR / "frontier_ledger.py"
DEFAULT_PROVIDER_FAILURE_GRACE_SECONDS = 60.0
DEFAULT_PROVIDER_FAILURE_MIN_ERRORS = 4
LOG_TAIL_CHARS = 6000
PROVIDER_CONNECT_ERROR_MARKER = "AI_APICallError: Cannot connect to API"
def repo_display(path: Path | str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return str(candidate)
    try:
        return str(candidate.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(candidate)


class FormatValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"unknown explorer command placeholder `{key}`")


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def now_batch_id() -> str:
    return f"explorer_batch_{stamp()}"


def append_log(batch_dir: Path, event: dict[str, Any]) -> None:
    batch_dir.mkdir(parents=True, exist_ok=True)
    event = {"ts": int(time.time()), **event}
    with (batch_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def export_previous_regions(args: argparse.Namespace, batch_dir: Path) -> None:
    if args.skip_previous_regions_refresh:
        append_log(batch_dir, {"event": "previous_regions_refresh_skipped"})
        return
    result = subprocess.run(
        [sys.executable, str(FRONTIER_LEDGER), "--ledger", str(args.ledger), "export-explored-regions"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    append_log(
        batch_dir,
        {
            "event": "previous_regions_refresh",
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        },
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "previous-region refresh failed")


def explorer_command_template() -> str:
    return (
        "env 'XDG_CONFIG_HOME={opencode_config_home}' "
        "'XDG_DATA_HOME={opencode_data_home}' "
        "'XDG_STATE_HOME={opencode_state_home}' "
        "opencode run --dir '{cwd}' --agent judge -m {opencode_model} --auto --title {explorer_run_id} "
        "\"Read and execute the explorer prompt at {prompt_path}. Write frontier candidates to "
        "{candidate_output_path} and the summary to {summary_path}. Do not modify pipeline code. "
        "Do not modify the live frontier ledger.\""
    )


def opencode_data_home(args: argparse.Namespace, card: dict[str, Any]) -> Path:
    return Path("/private/tmp") / (
        f"opencode-explorer-{opencode_workspace_slug()}-"
        f"{opencode_model_slug(args.opencode_model)}-{card['explorer_run_id']}"
    )


def opencode_state_home(args: argparse.Namespace, card: dict[str, Any]) -> Path:
    return Path("/private/tmp") / (
        f"opencode-explorer-state-{opencode_workspace_slug()}-"
        f"{opencode_model_slug(args.opencode_model)}-{card['explorer_run_id']}"
    )


def opencode_log_path(args: argparse.Namespace, card: dict[str, Any]) -> Path:
    return opencode_data_home(args, card) / "opencode" / "log" / "opencode.log"


def tail_text(path: Path, limit: int = LOG_TAIL_CHARS) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def terminate_process(process: subprocess.Popen[str]) -> int:
    process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def startup_failure_event(
    args: argparse.Namespace,
    card: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
    started_at: float,
) -> dict[str, Any] | None:
    stderr_tail = tail_text(stderr_path)
    log_path = opencode_log_path(args, card)
    opencode_log_tail = tail_text(log_path)
    combined = f"{stderr_tail}\n{opencode_log_tail}"
    provider_error_count = combined.count(PROVIDER_CONNECT_ERROR_MARKER)
    elapsed = time.time() - started_at
    if (
        provider_error_count >= args.provider_failure_min_errors
        and elapsed >= args.provider_failure_grace_seconds
    ):
        return {
            "event": "explorer_startup_failed",
            "explorer_run_id": card["explorer_run_id"],
            "reason": "opencode_provider_connectivity_failed",
            "elapsed_seconds": round(elapsed, 3),
            "provider_error_count": provider_error_count,
            "opencode_log_path": str(log_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stderr_tail": stderr_tail,
            "opencode_log_tail": opencode_log_tail,
            "watcher_error_seen": "Error starting FSEvents stream" in combined,
        }
    return None


def write_prompt(card: dict[str, Any]) -> None:
    prompt_path = Path(card["prompt_path"])
    guidance = normalize_ws(card.get("exploration_guidance"))
    lines = [
        "# Frontier Explorer Run",
        "",
        "You are an OpenCode frontier explorer for the SEBI memory-training QA pipeline.",
        "Your job is graph/corpus exploration only. You propose useful evidence regions;",
        "deterministic code decides whether they enter the live frontier ledger.",
        "Explore freely using the playbook and previous-region memory. Choose your own",
        "graph/corpus trail; the playbook already describes useful frontier patterns.",
        "",
        "Read these files first:",
        f"- Pipeline goal: {card['pipeline_goal']}",
        f"- Frontier playbook: {card['frontier_playbook']}",
        f"- Previous-region memory: {card['previous_explored_regions']}",
        f"- Explorer run card: {card['run_card_path']}",
        "",
        "Hard rules:",
        "- Do not edit pipeline code.",
        "- Do not run `frontier_ledger.py add` or otherwise mutate the live ledger.",
        "- Do not read coverage statistics unless the run card explicitly asks for them. This run does not.",
        "- Use the graph and corpus tools directly; do not infer seed relationships from titles alone.",
        "- If a search or hydration call falls back to metadata-only evidence, treat the relationship as unverified.",
        "- Prefer fewer, cleaner candidates over broad topic piles.",
        "- A candidate should be directional. Do not pack final-answer deltas, exact thresholds, or legal conclusions into `scope_hint`.",
        "",
        f"Maximum candidates to write: {card['max_candidates']}",
        "",
        "Candidate JSONL contract:",
        f"- Write one JSON object per line to `{card['candidate_output_path']}`.",
        "- Zero candidates is a valid result if you cannot verify clean frontiers.",
        "- Each candidate must use this shape:",
        "```json",
        "{",
        '  "title": "short label",',
        '  "kind": "exploratory",',
        '  "seed_doc_ids": ["clf-..."],',
        '  "candidate_doc_ids": [],',
        '  "scope_hint": "Directional relationship and candidate axes to inspect; no final-answer claims.",',
        '  "why_unexplored": "Why previous-region memory does not already cover this.",',
        '  "avoid": "Nearby regions or intents not to repeat.",',
        '  "source": "opencode_explorer",',
        f'  "source_ids": ["{card["explorer_run_id"]}"],',
        '  "notes": "Short verification note; name what you actually checked.",',
        '  "single_doc_ok": false,',
        '  "seed_roles": [',
        '    {"doc_id": "clf-...", "role": "baseline|consultation|final|amendment|notice|order|comparator", "verified_chunk_ids": ["clf-...#0"]}',
        "  ]",
        "}",
        "```",
        "",
        "Summary contract:",
        f"- Write `{card['summary_path']}`.",
        "- Include the graph/corpus trails tried, candidates written, candidates rejected, and any uncertainty.",
    ]
    if guidance:
        lines.insert(
            lines.index(f"Maximum candidates to write: {card['max_candidates']}"),
            f"Optional user-specified exploration guidance for this run: {guidance}",
        )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_card(args: argparse.Namespace, batch_dir: Path, index: int, exploration_guidance: str) -> dict[str, Any]:
    run_id = f"explorer_run_{stamp()}_{index + 1:02d}"
    run_cards_dir = args.output_root / "explorer_run_cards"
    prompts_dir = args.output_root / "explorer_prompts"
    candidates_dir = args.output_root / "frontier_candidates"
    summaries_dir = args.output_root / "explorer_summaries"
    for path in [run_cards_dir, prompts_dir, candidates_dir, summaries_dir]:
        path.mkdir(parents=True, exist_ok=True)

    card = {
        "explorer_run_id": run_id,
        "created_at": int(time.time()),
        "batch_id": args.batch_id,
        "exploration_guidance": exploration_guidance,
        "max_candidates": args.max_candidates,
        "pipeline_goal": repo_display(SCRIPT_DIR / "handoff" / "PIPELINE_GOAL.md"),
        "frontier_playbook": repo_display(SCRIPT_DIR / "handoff" / "FRONTIER_DISCOVERY_PLAYBOOK.md"),
        "previous_explored_regions": repo_display(args.output_root / "previous_explored_regions.md"),
        "research_cli": repo_display(SCRIPT_DIR / "sebi_research.py"),
        "frontier_ledger_path": repo_display(args.ledger),
        "run_card_path": repo_display(run_cards_dir / f"{run_id}.json"),
        "prompt_path": repo_display(prompts_dir / f"{run_id}.md"),
        "candidate_output_path": repo_display(candidates_dir / f"{run_id}.jsonl"),
        "summary_path": repo_display(summaries_dir / f"{run_id}.md"),
    }
    Path(card["run_card_path"]).write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_prompt(card)
    append_log(
        batch_dir,
        {
            "event": "explorer_prepared",
            "explorer_run_id": run_id,
            "exploration_guidance": exploration_guidance,
            "run_card_path": card["run_card_path"],
            "prompt_path": card["prompt_path"],
            "candidate_output_path": card["candidate_output_path"],
        },
    )
    return card


def selected_exploration_guidance(args: argparse.Namespace) -> list[str]:
    guidance = [normalize_ws(item) for item in args.exploration_guidance if normalize_ws(item)]
    count = max(0, args.count)
    if not guidance:
        return [""] * count
    return [guidance[index % len(guidance)] for index in range(count)]


def prepare(args: argparse.Namespace) -> list[dict[str, Any]]:
    batch_dir = args.batch_root / args.batch_id
    export_previous_regions(args, batch_dir)
    cards = [
        make_card(args, batch_dir, index, guidance)
        for index, guidance in enumerate(selected_exploration_guidance(args))
    ]
    append_log(batch_dir, {"event": "explorer_prepare_complete", "count": len(cards)})
    return cards


def command_for_card(template: str, card: dict[str, Any], args: argparse.Namespace, batch_dir: Path) -> list[str]:
    config_home = ensure_opencode_config(args, batch_dir)
    values = FormatValues(
        {
            **{key: normalize_ws(value) for key, value in card.items() if isinstance(value, (str, int, float, bool))},
            "cwd": str(REPO_ROOT),
            "opencode_config_home": str(config_home),
            "opencode_model": canonical_opencode_model(args.opencode_model),
            "opencode_model_slug": opencode_model_slug(args.opencode_model),
            "opencode_data_home": str(opencode_data_home(args, card)),
            "opencode_state_home": str(opencode_state_home(args, card)),
        }
    )
    return shlex.split(template.format_map(values))


def run_cards(args: argparse.Namespace, cards: list[dict[str, Any]]) -> int:
    if not cards:
        print("prepared=0")
        return 0
    batch_dir = args.batch_root / args.batch_id
    template = args.explorer_command or explorer_command_template()
    pending = list(cards)
    running: list[tuple[subprocess.Popen[str], dict[str, Any], Any, Any, float, Path, Path]] = []
    failures = 0

    while pending or running:
        while pending and len(running) < max(1, args.parallel):
            card = pending.pop(0)
            run_id = card["explorer_run_id"]
            command = command_for_card(template, card, args, batch_dir)
            stdout_path = batch_dir / f"{run_id}.stdout.log"
            stderr_path = batch_dir / f"{run_id}.stderr.log"
            stdout_handle = stdout_path.open("w", encoding="utf-8")
            stderr_handle = stderr_path.open("w", encoding="utf-8")
            process = subprocess.Popen(command, cwd=REPO_ROOT, text=True, stdout=stdout_handle, stderr=stderr_handle)
            running.append((process, card, stdout_handle, stderr_handle, time.time(), stdout_path, stderr_path))
            append_log(batch_dir, {"event": "explorer_started", "explorer_run_id": run_id, "pid": process.pid, "command": command})

        time.sleep(args.poll_seconds)
        still_running: list[tuple[subprocess.Popen[str], dict[str, Any], Any, Any, float, Path, Path]] = []
        for process, card, stdout_handle, stderr_handle, started_at, stdout_path, stderr_path in running:
            run_id = card["explorer_run_id"]
            returncode = process.poll()
            if returncode is None:
                failure_event = startup_failure_event(args, card, stdout_path, stderr_path, started_at)
                if failure_event:
                    returncode = terminate_process(process)
                    stdout_handle.close()
                    stderr_handle.close()
                    failures += 1
                    append_log(batch_dir, {**failure_event, "returncode": returncode})
                    continue
            if returncode is None and args.explorer_timeout_seconds > 0 and time.time() - started_at > args.explorer_timeout_seconds:
                returncode = terminate_process(process)
                stdout_handle.close()
                stderr_handle.close()
                failures += 1
                append_log(
                    batch_dir,
                    {
                        "event": "explorer_timeout",
                        "explorer_run_id": run_id,
                        "returncode": returncode,
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                        "opencode_log_path": str(opencode_log_path(args, card)),
                        "stdout_tail": tail_text(stdout_path),
                        "stderr_tail": tail_text(stderr_path),
                        "opencode_log_tail": tail_text(opencode_log_path(args, card)),
                    },
                )
                continue
            if returncode is None:
                still_running.append((process, card, stdout_handle, stderr_handle, started_at, stdout_path, stderr_path))
                continue
            stdout_handle.close()
            stderr_handle.close()
            if returncode != 0:
                failures += 1
            event = {
                "event": "explorer_finished",
                "explorer_run_id": run_id,
                "returncode": returncode,
                "candidate_output_path": card["candidate_output_path"],
                "summary_path": card["summary_path"],
            }
            if returncode != 0:
                event.update(
                    {
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                        "opencode_log_path": str(opencode_log_path(args, card)),
                        "stdout_tail": tail_text(stdout_path),
                        "stderr_tail": tail_text(stderr_path),
                        "opencode_log_tail": tail_text(opencode_log_path(args, card)),
                    }
                )
            append_log(batch_dir, event)
        running = still_running

    append_log(batch_dir, {"event": "explorer_run_complete", "failures": failures})
    return 0 if failures == 0 else 1


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", type=Path, default=OUTPUT_ROOT / "frontier_ledger.jsonl")
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    parser.add_argument("--batch-id", default=now_batch_id())
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument(
        "--exploration-guidance",
        action="append",
        default=[],
        help="Optional human-specified exploration guidance. No default guidance is assigned.",
    )
    parser.add_argument("--max-candidates", type=int, default=6)
    parser.add_argument("--skip-previous-regions-refresh", action="store_true")
    parser.add_argument("--opencode-config-home", type=Path, default=DEFAULT_OPENCODE_CONFIG_HOME)
    parser.add_argument("--opencode-model", default=DEFAULT_EXPLORER_MODEL)
    parser.add_argument("--opencode-extra-model", action="append", default=[])
    parser.add_argument("--opencode-context-limit", type=int, default=DEFAULT_OPENCODE_CONTEXT_LIMIT)
    parser.add_argument("--opencode-output-limit", type=int, default=DEFAULT_OPENCODE_OUTPUT_LIMIT)
    parser.add_argument("--opencode-provider-timeout-ms", type=int, default=DEFAULT_OPENCODE_PROVIDER_TIMEOUT_MS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Create explorer run cards and prompts.")
    add_common(prepare_parser)

    run_parser = subparsers.add_parser("run", help="Create explorer runs and launch OpenCode explorers.")
    add_common(run_parser)
    run_parser.add_argument("--parallel", type=int, default=1)
    run_parser.add_argument("--poll-seconds", type=float, default=5.0)
    run_parser.add_argument("--explorer-timeout-seconds", type=float, default=1800.0)
    run_parser.add_argument("--provider-failure-grace-seconds", type=float, default=DEFAULT_PROVIDER_FAILURE_GRACE_SECONDS)
    run_parser.add_argument("--provider-failure-min-errors", type=int, default=DEFAULT_PROVIDER_FAILURE_MIN_ERRORS)
    run_parser.add_argument("--explorer-command", default="")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cards = prepare(args)
        if args.command == "prepare":
            for card in cards:
                print(f"{card['explorer_run_id']}\t{card['prompt_path']}\t{card['candidate_output_path']}")
            return 0
        if args.command == "run":
            return run_cards(args, cards)
    except (RuntimeError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
