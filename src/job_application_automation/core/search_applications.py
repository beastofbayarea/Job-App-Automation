"""Submit verified-live VPS search results through the guarded orchestrator."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from .artifacts import atomic_write_text, read_json
from .contracts import EngineResult
from .engine_shared import ATS_HOST_MARKERS, detect_ats_job_url
from .identity import canonical_job_url
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path


SUPPORTED_PLATFORMS = frozenset(ATS_HOST_MARKERS)
DEFAULT_MAX_ATTEMPTS_PER_ATS = int(RUNTIME_CONFIG.application["vps_max_attempts_per_ats"])
DEFAULT_RESULTS_DIR = resolve_runtime_path(
    RUNTIME_CONFIG.application["vps_application_results_dir"]
)
DEFAULT_STATE_FILE = resolve_runtime_path(RUNTIME_CONFIG.application["vps_application_state_file"])
DEFAULT_FAILURE_REPORT = resolve_runtime_path(
    RUNTIME_CONFIG.application["vps_application_failure_report"]
)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "jobs": {}}
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid VPS application state: {path}")
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _save_failure_report(
    path: Path,
    *,
    run_started_at: datetime,
    failures: list[dict[str, Any]],
    attempted_by_ats: dict[str, int],
    confirmed_by_ats: dict[str, int],
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            {
                "version": 1,
                "run_started_at": run_started_at.isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "attempted_by_ats": attempted_by_ats,
                "confirmed_by_ats": confirmed_by_ats,
                "failure_count": len(failures),
                "failures": failures,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _eligible_jobs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("private application input must be a JSON array")
    eligible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in payload:
        if not isinstance(value, dict):
            continue
        if str(value.get("live_status", "")).strip().lower() != "live":
            continue
        if not all(
            str(value.get(key, "")).strip()
            for key in ("job_url", "company", "title", "description")
        ):
            continue
        declared_platform = str(value.get("platform", "")).strip().lower()
        candidate_urls = [
            str(value.get("apply_url", "")).strip(),
            str(value.get("job_url", "")).strip(),
        ]
        application_url = next(
            (candidate_url for candidate_url in candidate_urls if detect_ats_job_url(candidate_url)),
            "",
        )
        platform = detect_ats_job_url(application_url) if application_url else None
        if platform not in SUPPORTED_PLATFORMS:
            continue
        if declared_platform in SUPPORTED_PLATFORMS and declared_platform != platform:
            continue
        try:
            canonical_url = canonical_job_url(application_url)
        except ValueError:
            continue
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        job = dict(value)
        job["platform"] = platform
        job["_canonical_url"] = canonical_url
        job["_application_url"] = application_url
        eligible.append(job)
    return eligible


def _confirmed_urls(submission_log_path: Path) -> set[str]:
    if not submission_log_path.exists():
        return set()
    payload = read_json(submission_log_path)
    if not isinstance(payload, dict):
        raise ValueError("submission log root must be an object")
    confirmed: set[str] = set()
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        if str(value.get("status", "")).strip() != "SUBMITTED & CONFIRMED":
            continue
        try:
            confirmed.add(canonical_job_url(str(value.get("job_url", ""))))
        except ValueError:
            continue
    return confirmed


def _archived_document_urls(document_state_path: Path) -> set[str]:
    if not document_state_path.exists():
        return set()
    payload = _load_state(document_state_path)
    archived: set[str] = set()
    for job_url, value in payload["jobs"].items():
        if not isinstance(value, dict) or value.get("status") != "archived":
            continue
        try:
            archived.add(canonical_job_url(str(job_url)))
        except ValueError:
            continue
    return archived


def _result_path(results_dir: Path, canonical_url: str) -> Path:
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
    return results_dir / f"application_{digest}.json"


def _read_single_result(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return {}
    return payload[0]


def _is_confirmed(result: dict[str, Any]) -> bool:
    try:
        return EngineResult.from_payload(result).is_confirmed_submission
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automatically submit eligible verified-live VPS search results."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, default=Path("src/job_automation.py"))
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--submission-log", type=Path, required=True)
    parser.add_argument(
        "--document-state",
        type=Path,
        help="When supplied, attempt only URLs with an archived document pair.",
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--failure-report", type=Path, default=DEFAULT_FAILURE_REPORT)
    parser.add_argument(
        "--max-attempts-per-ats",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS_PER_ATS,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(RUNTIME_CONFIG.application["queue_timeout_seconds"]),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_attempts_per_ats <= 0:
        raise SystemExit("--max-attempts-per-ats must be greater than zero")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")

    state = _load_state(args.state)
    records: dict[str, Any] = state["jobs"]
    jobs = _eligible_jobs(_load_json(args.input))
    if args.document_state is not None:
        archived_urls = _archived_document_urls(args.document_state)
        jobs = [job for job in jobs if str(job["_canonical_url"]) in archived_urls]
    if not args.submission_log.exists():
        atomic_write_text(args.submission_log, "{}\n", encoding="utf-8")
    confirmed_urls = _confirmed_urls(args.submission_log)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    run_started_at = datetime.now(UTC)
    attempted_by_ats = {platform: 0 for platform in sorted(SUPPORTED_PLATFORMS)}
    confirmed_by_ats = {platform: 0 for platform in sorted(SUPPORTED_PLATFORMS)}
    failures: list[dict[str, Any]] = []
    _save_failure_report(
        args.failure_report,
        run_started_at=run_started_at,
        failures=failures,
        attempted_by_ats=attempted_by_ats,
        confirmed_by_ats=confirmed_by_ats,
    )

    for job in jobs:
        canonical_url = str(job["_canonical_url"])
        platform = str(job["platform"]).strip().lower()
        if canonical_url in confirmed_urls:
            continue
        # Every prior attempt remains terminal until a human deliberately
        # removes its state entry, preventing accidental duplicate submissions.
        if canonical_url in records:
            continue
        if attempted_by_ats[platform] >= args.max_attempts_per_ats:
            continue

        attempted_by_ats[platform] += 1
        result_path = _result_path(args.results_dir, canonical_url)
        result_path.unlink(missing_ok=True)
        command = [
            sys.executable,
            str(args.launcher),
            "apply",
            "--url",
            str(job["_application_url"]),
            "--company",
            str(job["company"]).strip(),
            "--role",
            str(job["title"]).strip(),
            "--config",
            str(args.profile),
            "--results-file",
            str(result_path),
            "--submission-log-file",
            str(args.submission_log),
            "--live-submit",
            "--no-shuffle",
            "--timeout",
            str(args.timeout),
        ]
        started_at = datetime.now(UTC)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return_code = completed.returncode
            error = ""
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except OSError as exc:
            return_code = 127
            error = str(exc)
            stdout = ""
            stderr = ""
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n", flush=True)
        if stderr:
            print(
                stderr,
                end="" if stderr.endswith("\n") else "\n",
                file=sys.stderr,
                flush=True,
            )
        result = _read_single_result(result_path)
        confirmed = return_code == 0 and _is_confirmed(result)
        status = "confirmed" if confirmed else "failed"
        record = {
            "status": status,
            "job_url": str(job["job_url"]).strip(),
            "company": str(job["company"]).strip(),
            "title": str(job["title"]).strip(),
            "platform": platform,
            "started_at": started_at.isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "exit_code": return_code,
            "result_status": str(result.get("status", "NO_RESULT")),
            "evidence_path": str(result_path),
            "result": result,
            "stdout_tail": stdout[-20000:],
            "stderr_tail": stderr[-20000:],
            **({"error": error} if error else {}),
        }
        records[canonical_url] = record
        _save_state(args.state, state)

        if not confirmed:
            failure = {
                "job_url": record["job_url"],
                "company": record["company"],
                "title": record["title"],
                "platform": platform,
                "exit_code": return_code,
                "result_status": record["result_status"],
                "error": error or str(result.get("error", "")),
                "detail": str(result.get("detail", "")),
                "missing_fields": result.get("missing_fields", []),
                "evidence_path": str(result_path),
                "stdout_tail": record["stdout_tail"],
                "stderr_tail": record["stderr_tail"],
            }
            failures.append(failure)
            _save_failure_report(
                args.failure_report,
                run_started_at=run_started_at,
                failures=failures,
                attempted_by_ats=attempted_by_ats,
                confirmed_by_ats=confirmed_by_ats,
            )
            print(
                "VPS_APPLICATION_FAILED "
                f"url={job['job_url']} exit_code={return_code} "
                f"status={result.get('status', 'NO_RESULT')} "
                f"evidence={result_path}",
                flush=True,
            )
            continue
        confirmed_by_ats[platform] += 1
        confirmed_urls.add(canonical_url)
        print(
            "VPS_APPLICATION_CONFIRMED "
            f"ats={platform} count={confirmed_by_ats[platform]} url={job['job_url']}",
            flush=True,
        )

    _save_failure_report(
        args.failure_report,
        run_started_at=run_started_at,
        failures=failures,
        attempted_by_ats=attempted_by_ats,
        confirmed_by_ats=confirmed_by_ats,
    )
    print(
        f"VPS applications: eligible={len(jobs)}, attempted_by_ats={attempted_by_ats}, "
        f"confirmed_by_ats={confirmed_by_ats}, failures={len(failures)}, "
        f"limit_per_ats={args.max_attempts_per_ats}",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
