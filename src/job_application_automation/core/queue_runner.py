#!/usr/bin/env python3
"""Run job URLs through the orchestrator sequentially, stopping on any failure."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlparse

from .artifacts import read_json, write_json
from .contracts import EngineResult
from .paths import CLI_ENTRYPOINT, OUTPUT_DIR, PROJECT_ROOT
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path


DEFAULT_QUEUE_TIMEOUT_SECONDS = int(RUNTIME_CONFIG.application["queue_timeout_seconds"])
DEFAULT_QUEUE_PROGRESS_FILE = resolve_runtime_path(
    RUNTIME_CONFIG.application["queue_progress_file"]
)


def _slug(url: str) -> str:
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if not parts:
        raise ValueError("Queue URL must contain a company or posting path segment")
    company = "".join(char if char.isalnum() else "_" for char in parts[0]).strip("_")
    posting = "".join(char if char.isalnum() else "_" for char in parts[-1]).strip("_")
    if not company or not posting:
        raise ValueError("Queue URL must contain a valid company and posting path segment")
    return f"{company}_{posting}"


def _write_progress(path: Path, payload: dict[str, object]) -> None:
    """Persist queue progress through the shared atomic artifact boundary."""
    write_json(path, payload, indent=2)


def _confirmed_submission(payload: dict[str, object]) -> bool:
    """Accept only a validated confirmed engine result in a live queue."""
    try:
        return EngineResult.from_payload(payload).is_confirmed_submission
    except ValueError:
        return False


def _company_from_url(url: str) -> str:
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if not parts:
        raise ValueError("Queue URL must contain a company path segment")
    return parts[0]


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=DEFAULT_QUEUE_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if args.start_index < 0:
        parser.error("--start-index must be zero or greater")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    urls = [
        line.strip() for line in args.queue.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    progress_path = DEFAULT_QUEUE_PROGRESS_FILE
    orchestrator = CLI_ENTRYPOINT

    for index, url in enumerate(urls[args.start_index :], start=args.start_index):
        slug = _slug(url)
        try:
            company = _company_from_url(url)
        except ValueError as exc:
            print(f"QUEUE_STOP index={index + 1}/{len(urls)} error={exc}", flush=True)
            return 2
        result_path = OUTPUT_DIR / f"orchestration_{slug}.json"
        print(f"QUEUE_START index={index + 1}/{len(urls)} url={url}", flush=True)
        command = [
            sys.executable,
            str(orchestrator),
            "apply",
            "--url",
            url,
            "--company",
            company,
            "--live-submit",
            "--no-shuffle",
            "--timeout",
            str(args.timeout),
            "--results-file",
            str(result_path),
        ]
        if result_path.exists():
            result_path.unlink()
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        if completed.stdout:
            print(completed.stdout, end="", flush=True)
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr, flush=True)
        result: dict[str, object] = {}
        try:
            payload = read_json(result_path)
            if isinstance(payload, list) and len(payload) == 1:
                result = payload[0]
        except (OSError, ValueError):
            pass

        confirmed = completed.returncode == 0 and _confirmed_submission(result)
        progress = {
            "queue_count": len(urls),
            "last_index": index,
            "last_url": url,
            "confirmed": confirmed,
            "result": result,
        }
        _write_progress(progress_path, progress)
        if not confirmed:
            print(
                f"QUEUE_STOP index={index + 1}/{len(urls)} "
                f"returncode={completed.returncode} "
                f"status={result.get('status', 'NO_RESULT')}",
                flush=True,
            )
            return 1
        print(f"QUEUE_CONFIRMED index={index + 1}/{len(urls)}", flush=True)

    print(f"QUEUE_COMPLETE count={len(urls)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
