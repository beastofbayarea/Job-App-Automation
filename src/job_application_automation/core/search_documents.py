"""Generate and privately archive document pairs from a search result snapshot."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from .artifacts import atomic_write_text
from .observability import initialize_observability
from .runtime_config import RUNTIME_CONFIG


UTC = timezone.utc
DEFAULT_MAX_JOBS = RUNTIME_CONFIG.application.vps_max_document_jobs
DEFAULT_RETRY_JOBS = RUNTIME_CONFIG.application.vps_document_retry_jobs


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _candidate_email(profile_path: Path) -> str:
    profile = _load_json(profile_path)
    candidate = profile.get("candidate", {}) if isinstance(profile, dict) else {}
    contact = candidate.get("contact", {}) if isinstance(candidate, dict) else {}
    email = str(contact.get("fallback_email", "")).strip().lower()
    if "@" not in email:
        raise ValueError(f"candidate fallback email is missing from {profile_path}")
    return email


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "jobs": {}}
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid document-generation state: {path}")
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _eligible_jobs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("private generation input must be a JSON array")
    jobs: list[dict[str, Any]] = []
    for value in payload:
        if not isinstance(value, dict):
            continue
        if str(value.get("live_status", "")).strip().lower() != "live":
            continue
        if not all(str(value.get(key, "")).strip() for key in ("job_url", "company", "title")):
            continue
        if not str(value.get("description", "")).strip():
            continue
        jobs.append(value)
    return jobs


def _select_pending_jobs(
    jobs: list[dict[str, Any]],
    records: dict[str, Any],
    *,
    max_jobs: int,
    retry_jobs: int,
) -> list[dict[str, Any]]:
    pending = [
        job for job in jobs if records.get(str(job["job_url"]), {}).get("status") != "archived"
    ]
    if not max_jobs:
        return pending

    fresh = [job for job in pending if str(job["job_url"]) not in records]
    retries = [job for job in pending if str(job["job_url"]) in records]
    retry_budget = min(retry_jobs, max_jobs, len(retries))
    fresh_budget = min(max_jobs - retry_budget, len(fresh))
    remaining = max_jobs - retry_budget - fresh_budget
    selected_urls = {
        str(job["job_url"])
        for job in (
            fresh[: fresh_budget + remaining]
            + retries[: retry_budget + max(0, remaining - (len(fresh) - fresh_budget))]
        )
    }
    return [job for job in pending if str(job["job_url"]) in selected_urls][:max_jobs]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and archive one CV/cover-letter pair for every live search result."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--vps-config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, default=Path("src/job_automation.py"))
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=DEFAULT_MAX_JOBS,
        help=(
            "Maximum document jobs per run; 0 processes every eligible job "
            f"(default: {DEFAULT_MAX_JOBS})."
        ),
    )
    parser.add_argument(
        "--retry-jobs",
        type=int,
        default=DEFAULT_RETRY_JOBS,
        help=(
            "Reserve up to this many bounded-run slots for prior failures "
            f"(default: {DEFAULT_RETRY_JOBS})."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_jobs < 0:
        raise SystemExit("--max-jobs cannot be negative")
    if args.retry_jobs < 0:
        raise SystemExit("--retry-jobs cannot be negative")
    if args.max_jobs and args.retry_jobs > args.max_jobs:
        raise SystemExit("--retry-jobs cannot exceed --max-jobs")

    telemetry = initialize_observability(worker_kind="document_archive")

    email = _candidate_email(args.profile)
    state = _load_state(args.state)
    records: dict[str, Any] = state["jobs"]
    jobs = _eligible_jobs(_load_json(args.input))
    all_pending = [
        job for job in jobs if records.get(str(job["job_url"]), {}).get("status") != "archived"
    ]
    pending = _select_pending_jobs(
        jobs,
        records,
        max_jobs=args.max_jobs,
        retry_jobs=args.retry_jobs,
    )
    selected_retries = sum(1 for job in pending if str(job["job_url"]) in records)

    failures = 0
    for job in pending:
        url = str(job["job_url"]).strip()
        now = datetime.now(UTC).isoformat()
        description = str(job["description"]).strip()
        jd_path = ""
        try:
            args.state.parent.mkdir(parents=True, exist_ok=True)
            descriptor, jd_path = tempfile.mkstemp(
                prefix="job-description-",
                suffix=".txt",
                dir=args.state.parent,
                text=True,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(description)
            command = [
                sys.executable,
                str(args.launcher),
                "documents",
                "generate",
                "--url",
                url,
                "--company",
                str(job["company"]).strip(),
                "--role",
                str(job["title"]).strip(),
                "--email",
                email,
                "--location",
                str(job.get("location", "")).strip(),
                "--jd-file",
                jd_path,
                "--profile",
                str(args.profile),
                "--config",
                str(args.vps_config),
                "--archive",
                "--overwrite",
            ]
            completed = subprocess.run(command, check=False)
            status = "archived" if completed.returncode == 0 else "failed"
            records[url] = {
                "status": status,
                "company": str(job["company"]).strip(),
                "title": str(job["title"]).strip(),
                "updated_at": now,
                "exit_code": completed.returncode,
            }
            if completed.returncode != 0:
                failures += 1
                telemetry.emit(
                    "document_archive_failed",
                    provider=str(job.get("platform", "")),
                    stage="document_archive",
                    cycle_status="failed",
                    exit_code=completed.returncode,
                )
        except OSError as exc:
            failures += 1
            records[url] = {
                "status": "failed",
                "company": str(job["company"]).strip(),
                "title": str(job["title"]).strip(),
                "updated_at": now,
                "error": str(exc),
            }
            telemetry.emit(
                "document_archive_failed",
                provider=str(job.get("platform", "")),
                stage="document_archive",
                cycle_status="failed",
                error_type=type(exc),
            )
        finally:
            if jd_path:
                Path(jd_path).unlink(missing_ok=True)
            _save_state(args.state, state)

    archived = sum(
        1
        for value in records.values()
        if isinstance(value, dict) and value.get("status") == "archived"
    )
    print(
        f"Document archive: eligible={len(jobs)}, pending_total={len(all_pending)}, "
        f"processed={len(pending)}, retries={selected_retries}, "
        f"archived_total={archived}, failures={failures}, max_jobs={args.max_jobs}"
    )
    telemetry.emit(
        "worker_cycle_complete",
        level="error" if failures else "info",
        stage="document_archive",
        cycle_status="failed" if failures else "archived",
        failure_count=failures,
    )
    telemetry.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
