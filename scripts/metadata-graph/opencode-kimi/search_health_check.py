#!/usr/bin/env python3
"""Preflight health checks for SEBI Vespa corpus search."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

DEFAULT_VESPA_URL = (
    os.environ.get("METADATA_KG_VESPA_QUERY_URL")
    or os.environ.get("VESPA_QUERY_URL")
    or "http://localhost:18081/search/"
)
DEFAULT_QUERY = "NISM Series XXV A communique"
DEFAULT_SEMANTIC_QUERY = "NISM Series-XXV-A communique"


def env_csv(name: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    return tuple(item.strip() for item in raw.split(",") if item.strip())


DEFAULT_REQUIRED_CONTAINERS = env_csv(
    "METADATA_KG_REQUIRED_CONTAINERS",
    env_csv("SEARCH_REQUIRED_CONTAINERS", ("metadata-kg-vespa", "metadata-kg-tei-batch-proxy")),
)
DEFAULT_RANK_PROFILE = "default_native_dynamic_chunks_file_v6_rsf_vec"
E5_TASK = "Given a question, retrieve relevant SEBI / legal document passages that answer it"


@dataclass
class Check:
    name: str
    status: str
    message: str
    detail: dict[str, Any] | None = None


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
    if isinstance(value, list):
        return [portable_value(item) for item in value]
    if isinstance(value, dict):
        return {key: portable_value(item) for key, item in value.items()}
    return value


def normalize_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


def post_json(url: str, body: dict[str, Any], *, timeout: float) -> tuple[int, str, dict[str, Any] | None]:
    request = urllib.request.Request(
        url.rstrip("/") + "/",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return response.status, text, json.loads(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        return exc.code, text, data


def vespa_errors(data: dict[str, Any] | None, text: str) -> list[str]:
    if not isinstance(data, dict):
        return [text[:800]] if text else ["non-JSON Vespa response"]
    errors = data.get("root", {}).get("errors") or []
    messages: list[str] = []
    for error in errors:
        if isinstance(error, dict):
            messages.append(
                normalize_ws(
                    error.get("message")
                    or error.get("summary")
                    or json.dumps(error, ensure_ascii=False)
                )
            )
    return messages


def hit_count(data: dict[str, Any] | None) -> int:
    if not isinstance(data, dict):
        return 0
    return len(data.get("root", {}).get("children") or [])


def total_count(data: dict[str, Any] | None) -> Any:
    if not isinstance(data, dict):
        return None
    return data.get("root", {}).get("fields", {}).get("totalCount")


def check_docker(required_containers: list[str], *, timeout: float) -> Check:
    if not required_containers:
        return Check("docker_containers", "skip", "no required containers configured")
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return Check("docker_containers", "fail", "docker executable not found")
    except subprocess.TimeoutExpired:
        return Check("docker_containers", "fail", f"docker ps timed out after {timeout:g}s")
    if result.returncode != 0:
        return Check(
            "docker_containers",
            "fail",
            "docker ps failed",
            {"stderr": portable_text(result.stderr.strip()), "stdout": portable_text(result.stdout.strip())},
        )

    statuses: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip() or "\t" not in line:
            continue
        name, status = line.split("\t", 1)
        statuses[name] = status
    missing = [name for name in required_containers if name not in statuses]
    not_up = [name for name in required_containers if name in statuses and not statuses[name].startswith("Up")]
    if missing or not_up:
        return Check(
            "docker_containers",
            "fail",
            "required search container(s) are not running",
            {"missing": missing, "not_up": {name: statuses.get(name, "") for name in not_up}},
        )
    return Check(
        "docker_containers",
        "pass",
        "required search containers are running",
        {"containers": {name: statuses[name] for name in required_containers}},
    )


def check_tcp_port(host: str, port: int, *, timeout: float) -> Check:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return Check("embedding_host_port", "pass", f"{host}:{port} is accepting TCP connections")
    except OSError as exc:
        return Check(
            "embedding_host_port",
            "warn",
            f"{host}:{port} is not accepting TCP connections",
            {"error": portable_text(exc)},
        )


def lexical_body(query: str, *, hits: int) -> dict[str, Any]:
    return {
        "yql": "select * from kb_items where userInput(@query)",
        "query": query,
        "hits": hits,
        "timeout": "15s",
        "ranking.profile": DEFAULT_RANK_PROFILE,
        "input.query(alpha)": 0.0,
        "presentation.summary": "lean",
        "input.query(summary_chunks)": 1,
    }


def semantic_body(query: str, semantic_query: str, *, hits: int) -> dict[str, Any]:
    return {
        "yql": "select * from kb_items where (userInput(@query) or ({targetHits:200}nearestNeighbor(chunk_embeddings, e)))",
        "query": query,
        "hits": hits,
        "timeout": "30s",
        "ranking.profile": DEFAULT_RANK_PROFILE,
        "input.query(alpha)": 0.3,
        "presentation.summary": "lean",
        "input.query(summary_chunks)": 1,
        "e5_query": f"Instruct: {E5_TASK}\nQuery: {semantic_query}",
        "input.query(e)": "embed(@e5_query)",
    }


def check_vespa(
    *,
    name: str,
    url: str,
    body: dict[str, Any],
    timeout: float,
) -> Check:
    try:
        status, text, data = post_json(url, body, timeout=timeout)
    except Exception as exc:
        return Check(name, "fail", portable_text(f"Vespa request failed: {type(exc).__name__}: {exc}"))

    errors = [portable_text(error) for error in vespa_errors(data, text)]
    detail = {
        "http_status": status,
        "total_count": total_count(data),
        "hits": hit_count(data),
    }
    if errors:
        detail["errors"] = errors
    if status >= 400 or errors:
        message = errors[0] if errors else f"HTTP {status}"
        return Check(name, "fail", message, detail)
    return Check(name, "pass", "Vespa query succeeded", detail)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vespa-url", default=DEFAULT_VESPA_URL)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--semantic-query", default=DEFAULT_SEMANTIC_QUERY)
    parser.add_argument("--hits", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=40.0)
    parser.add_argument("--docker-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--required-container", action="append", default=[])
    parser.add_argument(
        "--check-embedding-port",
        action="store_true",
        help="Legacy host-port probe; the normal end-to-end semantic check verifies the TEI Docker endpoint.",
    )
    parser.add_argument("--embedding-host", default="localhost")
    parser.add_argument("--embedding-port", type=int, default=8090)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    required = args.required_container or list(DEFAULT_REQUIRED_CONTAINERS)
    checks: list[Check] = []

    if args.skip_docker:
        checks.append(Check("docker_containers", "skip", "docker container check skipped"))
    else:
        checks.append(check_docker(required, timeout=args.docker_timeout_seconds))

    if args.check_embedding_port:
        checks.append(
            check_tcp_port(
                args.embedding_host,
                args.embedding_port,
                timeout=min(args.timeout_seconds, 5.0),
            )
        )
    hits = max(1, min(int(args.hits), 8))
    checks.append(
        check_vespa(
            name="vespa_lexical",
            url=args.vespa_url,
            body=lexical_body(args.query, hits=hits),
            timeout=args.timeout_seconds,
        )
    )
    checks.append(
        check_vespa(
            name="vespa_semantic",
            url=args.vespa_url,
            body=semantic_body(args.query, args.semantic_query, hits=hits),
            timeout=args.timeout_seconds,
        )
    )

    ok = not any(check.status == "fail" for check in checks)
    payload = {
        "ok": ok,
        "vespa_url": args.vespa_url,
        "query": args.query,
        "semantic_query": args.semantic_query,
        "checks": [asdict(check) for check in checks],
    }
    if args.json:
        print(json.dumps(portable_value(payload), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"{check.status.upper()}\t{check.name}\t{portable_text(check.message)}")
            if check.detail and check.status in {"fail", "warn"}:
                print(json.dumps(portable_value(check.detail), ensure_ascii=False, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
