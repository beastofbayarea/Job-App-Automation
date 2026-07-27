#!/usr/bin/env python3
"""Run job URLs through the orchestrator sequentially, stopping on any failure."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from paths import OUTPUT_DIR, PROJECT_ROOT, SRC_DIR


def _slug(url: str) -> str:
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    company = "".join(char if char.isalnum() else "_" for char in parts[0]).strip("_")
    posting = parts[-1].split("?")[0]
    return f"{company}_{posting}"


def _write_progress(path: Path, payload: dict[str, object]) -> None:
    # Write-then-rename so a reader never observes a partially written file.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    urls = [
        line.strip()
        for line in args.queue.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    progress_path = OUTPUT_DIR / "job_url_queue_progress.json"
    orchestrator = SRC_DIR / "orchestrator.py"

    for index, url in enumerate(urls[args.start_index :], start=args.start_index):
        slug = _slug(url)
        company = unquote(urlparse(url).path.split("/")[1])
        result_path = OUTPUT_DIR / f"orchestration_{slug}.json"
        print(f"QUEUE_START index={index + 1}/{len(urls)} url={url}", flush=True)
        command = [
            sys.executable,
            str(orchestrator),
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
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        if completed.stdout:
            print(completed.stdout, end="", flush=True)
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr, flush=True)
        result: dict[str, object] = {}
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(payload, list) and len(payload) == 1:
                result = payload[0]
        except (OSError, json.JSONDecodeError):
            pass

        confirmed = (
            completed.returncode == 0
            and result.get("success") is True
            and result.get("submitted") is True
            and result.get("confirmed") is True
            and result.get("status") == "SUBMITTED & CONFIRMED"
        )
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
