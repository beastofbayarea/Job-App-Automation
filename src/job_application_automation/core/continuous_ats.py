"""Continuously prepare and submit one guarded ATS application per cycle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .artifacts import atomic_write_text, read_json
from .contracts import EngineResult
from .identity import canonical_job_url, normalize_email
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from ..mail.pool import load_email_pool


SHARED_INPUT = resolve_runtime_path("output/vps_generation_jobs.json")
# Retained for import compatibility; supervised workers use provider-specific
# input files so parallel refreshes cannot overwrite each other.
DEFAULT_INPUT = SHARED_INPUT
DEFAULT_SUBMISSION_LOG = resolve_runtime_path(RUNTIME_CONFIG.application["submission_log_file"])
DEFAULT_PROFILE = resolve_runtime_path("config/candidate_profile_config.json")
DEFAULT_EMAIL_POOL = resolve_runtime_path(RUNTIME_CONFIG.application["candidate_email_pool_file"])
DEFAULT_LAUNCHER = resolve_runtime_path("src/job_automation.py")
TERMINAL_STATUSES = frozenset({"confirmed", "failed", "manual_review"})
RESUMABLE_STATUSES = frozenset({"preparing", "documents_ready"})
ATS_PLATFORM_PATTERN = re.compile(r"^[a-z][a-z0-9]*$")


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_state(path: Path, ats_platform: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "jobs": {}}
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid continuous {ats_platform} state: {path}")
    if payload.get("version") != 1:
        raise ValueError(f"unsupported continuous {ats_platform} state version: {path}")
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _eligible_jobs(payload: Any, ats_platform: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError(f"continuous {ats_platform} input must be a JSON array")
    eligible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in payload:
        if not isinstance(value, dict):
            continue
        if str(value.get("platform", "")).strip().lower() != ats_platform:
            continue
        if str(value.get("live_status", "")).strip().lower() != "live":
            continue
        if not all(
            str(value.get(key, "")).strip()
            for key in ("job_url", "company", "title", "description")
        ):
            continue
        try:
            canonical_url = canonical_job_url(value["job_url"])
        except ValueError:
            continue
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        job = dict(value)
        job["_canonical_url"] = canonical_url
        eligible.append(job)
    return eligible


def _confirmed_urls(path: Path, ats_platform: str) -> set[str]:
    if not path.exists():
        return set()
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("submission log root must be an object")
    confirmed: set[str] = set()
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        if str(value.get("status", "")).strip() != "SUBMITTED & CONFIRMED":
            continue
        if str(value.get("ats", "")).strip().lower() != ats_platform:
            continue
        try:
            confirmed.add(canonical_job_url(value.get("job_url", "")))
        except ValueError:
            continue
    return confirmed


def _select_job(
    jobs: list[dict[str, Any]],
    state: Mapping[str, Any],
    confirmed_urls: set[str],
    ats_platform: str,
    *,
    choice: Callable[[Sequence[dict[str, Any]]], dict[str, Any]] = random.choice,
) -> dict[str, Any] | None:
    records = state.get("jobs", {})
    if not isinstance(records, Mapping):
        raise ValueError(f"continuous {ats_platform} state jobs must be an object")
    by_url = {str(job["_canonical_url"]): job for job in jobs}
    for canonical_url, record in records.items():
        if (
            isinstance(record, Mapping)
            and record.get("status") in RESUMABLE_STATUSES
            and canonical_url in by_url
        ):
            return by_url[canonical_url]
    fresh = [
        job
        for job in jobs
        if str(job["_canonical_url"]) not in records
        and str(job["_canonical_url"]) not in confirmed_urls
    ]
    return choice(fresh) if fresh else None


def _reconcile_interrupted_submissions(state: dict[str, Any]) -> int:
    """Quarantine attempts that may have submitted before the process stopped."""
    reconciled = 0
    for record in state["jobs"].values():
        if not isinstance(record, dict) or record.get("status") != "application_started":
            continue
        record.update(
            {
                "status": "manual_review",
                "stage": "application",
                "result_status": "INTERRUPTED_AFTER_APPLICATION_START",
                "updated_at": _now(),
            }
        )
        reconciled += 1
    return reconciled


def _run_command(command: list[str], timeout_seconds: int) -> CommandOutcome:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
        return CommandOutcome(
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        return CommandOutcome(124, stdout or "", stderr or "", timed_out=True)
    except OSError as exc:
        return CommandOutcome(127, "", str(exc))


def _masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    visible = local[:1] if local else ""
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def _sleep_between_cycles(delay: int, ats_platform: str) -> bool:
    try:
        time.sleep(delay)
    except KeyboardInterrupt:
        print(
            f"{ats_platform.upper()}_WORKER_STOPPED signal=keyboard_interrupt",
            flush=True,
        )
        return False
    return True


def _job_digest(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]


def _valid_pdf(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 1000 and path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def _read_result(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return {}
    return payload[0]


def _strictly_confirmed(result: Mapping[str, Any]) -> bool:
    try:
        return EngineResult.from_payload(result).is_confirmed_submission
    except ValueError:
        return False


def _diagnostics(outcome: CommandOutcome) -> dict[str, Any]:
    return {
        "exit_code": outcome.return_code,
        "timed_out": outcome.timed_out,
        "stdout_tail": outcome.stdout[-20000:],
        "stderr_tail": outcome.stderr[-20000:],
    }


def _prepare_documents(
    *,
    job: Mapping[str, Any],
    ats_platform: str,
    email: str,
    launcher: Path,
    profile: Path,
    output_dir: Path,
    timeout_seconds: int,
) -> CommandOutcome:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    descriptor, job_description_name = tempfile.mkstemp(
        prefix=f"continuous-{ats_platform}-jd-",
        suffix=".txt",
        dir=output_dir.parent,
        text=True,
    )
    job_description_path = Path(job_description_name)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(str(job["description"]).strip())
        command = [
            sys.executable,
            str(launcher),
            "documents",
            "generate",
            "--url",
            str(job["job_url"]).strip(),
            "--company",
            str(job["company"]).strip(),
            "--role",
            str(job["title"]).strip(),
            "--email",
            email,
            "--location",
            str(job.get("location", "")).strip(),
            "--jd-file",
            str(job_description_path),
            "--profile",
            str(profile),
            "--output-dir",
            str(output_dir),
            "--overwrite",
        ]
        return _run_command(command, timeout_seconds)
    finally:
        job_description_path.unlink(missing_ok=True)


def _apply(
    *,
    job: Mapping[str, Any],
    email: str,
    launcher: Path,
    profile: Path,
    resume_path: Path,
    cover_letter_path: Path,
    result_path: Path,
    submission_log: Path,
    engine_timeout_seconds: int,
    process_timeout_seconds: int,
) -> CommandOutcome:
    command = [
        sys.executable,
        str(launcher),
        "apply",
        "--url",
        str(job["job_url"]).strip(),
        "--company",
        str(job["company"]).strip(),
        "--role",
        str(job["title"]).strip(),
        "--config",
        str(profile),
        "--prepared-resume",
        str(resume_path),
        "--cover-letter",
        str(cover_letter_path),
        "--email",
        email,
        "--results-file",
        str(result_path),
        "--submission-log-file",
        str(submission_log),
        "--live-submit",
        "--no-shuffle",
        "--timeout",
        str(engine_timeout_seconds),
    ]
    return _run_command(command, process_timeout_seconds)


def process_one(
    *,
    ats_platform: str,
    input_path: Path,
    profile: Path,
    email_pool: Path,
    launcher: Path,
    state_path: Path,
    results_dir: Path,
    documents_dir: Path,
    submission_log: Path,
    document_timeout_seconds: int,
    engine_timeout_seconds: int,
    application_timeout_seconds: int,
) -> str:
    state = _load_state(state_path, ats_platform)
    jobs = _eligible_jobs(_load_json(input_path), ats_platform)
    job = _select_job(
        jobs,
        state,
        _confirmed_urls(submission_log, ats_platform),
        ats_platform,
    )
    if job is None:
        return "no_work"

    canonical_url = str(job["_canonical_url"])
    records: dict[str, Any] = state["jobs"]
    record = records.get(canonical_url)
    if not isinstance(record, dict):
        email = normalize_email(random.choice(load_email_pool(email_pool)))
        digest = _job_digest(canonical_url)
        record = {
            "status": "preparing",
            "stage": "documents",
            "job_url": str(job["job_url"]).strip(),
            "company": str(job["company"]).strip(),
            "title": str(job["title"]).strip(),
            "platform": ats_platform,
            "email": email,
            "document_dir": str(documents_dir / digest),
            "result_path": str(results_dir / f"application_{digest}.json"),
            "started_at": _now(),
            "updated_at": _now(),
        }
        records[canonical_url] = record
        _save_state(state_path, state)
    else:
        email = normalize_email(record.get("email", ""))

    print(
        f"{ats_platform.upper()}_CYCLE_START "
        f"company={record['company']!r} role={record['title']!r} "
        f"email={_masked_email(email)}",
        flush=True,
    )

    output_dir = Path(str(record["document_dir"]))
    resume_path = output_dir / "resume.pdf"
    cover_letter_path = output_dir / "cover_letter.pdf"
    result_path = Path(str(record["result_path"]))

    if record["status"] == "preparing":
        document_outcome = _prepare_documents(
            job=job,
            ats_platform=ats_platform,
            email=email,
            launcher=launcher,
            profile=profile,
            output_dir=output_dir,
            timeout_seconds=document_timeout_seconds,
        )
        if (
            document_outcome.return_code != 0
            or not _valid_pdf(resume_path)
            or not _valid_pdf(cover_letter_path)
        ):
            record.update(
                {
                    "status": "failed",
                    "stage": "documents",
                    "result_status": (
                        "DOCUMENT_GENERATION_TIMED_OUT"
                        if document_outcome.timed_out
                        else "DOCUMENT_GENERATION_FAILED"
                    ),
                    "resume_valid": _valid_pdf(resume_path),
                    "cover_letter_valid": _valid_pdf(cover_letter_path),
                    "updated_at": _now(),
                    **_diagnostics(document_outcome),
                }
            )
            _save_state(state_path, state)
            print(
                f"{ats_platform.upper()}_CYCLE_FAILED stage=documents "
                f"url_digest={_job_digest(canonical_url)} "
                f"status={record['result_status']}",
                flush=True,
            )
            return "failed"
        record.update(
            {
                "status": "documents_ready",
                "stage": "application",
                "resume_filename": resume_path.name,
                "cover_letter_filename": cover_letter_path.name,
                "updated_at": _now(),
                **_diagnostics(document_outcome),
            }
        )
        _save_state(state_path, state)

    results_dir.mkdir(parents=True, exist_ok=True)
    submission_log.parent.mkdir(parents=True, exist_ok=True)
    if not submission_log.exists():
        atomic_write_text(submission_log, "{}\n", encoding="utf-8")
    result_path.unlink(missing_ok=True)
    record.update(
        {
            "status": "application_started",
            "stage": "application",
            "application_started_at": _now(),
            "updated_at": _now(),
        }
    )
    _save_state(state_path, state)

    application_outcome = _apply(
        job=job,
        email=email,
        launcher=launcher,
        profile=profile,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        result_path=result_path,
        submission_log=submission_log,
        engine_timeout_seconds=engine_timeout_seconds,
        process_timeout_seconds=application_timeout_seconds,
    )
    result = _read_result(result_path)
    ledger_confirmed = canonical_url in _confirmed_urls(submission_log, ats_platform)
    confirmed = (
        application_outcome.return_code == 0 and _strictly_confirmed(result) and ledger_confirmed
    )
    possibly_submitted = bool(result.get("submitted")) or application_outcome.timed_out
    status = "confirmed" if confirmed else ("manual_review" if possibly_submitted else "failed")
    record.update(
        {
            "status": status,
            "stage": "application",
            "result_status": str(result.get("status", "NO_RESULT")),
            "ledger_confirmed": ledger_confirmed,
            "result": result,
            "updated_at": _now(),
            **_diagnostics(application_outcome),
        }
    )
    _save_state(state_path, state)
    if confirmed:
        print(
            f"{ats_platform.upper()}_CYCLE_CONFIRMED "
            f"url_digest={_job_digest(canonical_url)} status=SUBMITTED_AND_CONFIRMED",
            flush=True,
        )
        return "confirmed"
    print(
        f"{ats_platform.upper()}_CYCLE_FAILED "
        f"stage=application url_digest={_job_digest(canonical_url)} "
        f"status={record['result_status']} disposition={status}",
        flush=True,
    )
    return status


def _refresh_jobs(
    *,
    ats_platform: str,
    launcher: Path,
    input_path: Path,
    timeout_seconds: int,
) -> CommandOutcome:
    command = [
        sys.executable,
        str(launcher),
        "search",
        "--role-type",
        "Product Manager",
        "--ats-platform",
        ats_platform,
        "--verify-live",
        "--private-generation-output",
        str(input_path),
    ]
    return _run_command(command, timeout_seconds)


def _platform_output_path(ats_platform: str, suffix: str = "") -> Path:
    name = f"continuous_{ats_platform}{suffix}"
    return resolve_runtime_path(f"output/{name}")


def _validate_platform(ats_platform: str) -> str:
    """Accept installed ATS engines without maintaining a second provider registry."""
    normalized = str(ats_platform).strip().lower()
    if not ATS_PLATFORM_PATTERN.fullmatch(normalized):
        raise ValueError("continuous ATS platform must contain only lowercase letters and digits")
    engine_module = f"job_application_automation.engines.{normalized}"
    if importlib.util.find_spec(engine_module) is None:
        raise ValueError(f"continuous ATS engine is not installed: {normalized}")
    return normalized


def _seed_platform_input(input_path: Path, ats_platform: str) -> int:
    """Seed a dedicated provider list from the latest shared VPS search output."""
    if input_path.exists() or not SHARED_INPUT.is_file():
        return 0
    jobs = _eligible_jobs(_load_json(SHARED_INPUT), ats_platform)
    payload = [
        {key: value for key, value in job.items() if key != "_canonical_url"} for job in jobs
    ]
    atomic_write_text(
        input_path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(payload)


def build_parser(ats_platform: str) -> argparse.ArgumentParser:
    ats_platform = _validate_platform(ats_platform)
    parser = argparse.ArgumentParser(
        description=(
            f"Continuously select one verified-live {ats_platform.title()} job, "
            "generate a personalized "
            "resume and cover letter with a random configured email, submit it through the "
            "guarded orchestrator, then wait 2-5 minutes."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=_platform_output_path(ats_platform, "_jobs.json"),
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--email-pool", type=Path, default=DEFAULT_EMAIL_POOL)
    parser.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    parser.add_argument(
        "--state",
        type=Path,
        default=_platform_output_path(ats_platform, "_state.json"),
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=_platform_output_path(ats_platform, "_results"),
    )
    parser.add_argument(
        "--documents-dir",
        type=Path,
        default=_platform_output_path(ats_platform, "_documents"),
    )
    parser.add_argument("--submission-log", type=Path, default=DEFAULT_SUBMISSION_LOG)
    parser.add_argument("--sleep-min-seconds", type=int, default=120)
    parser.add_argument("--sleep-max-seconds", type=int, default=300)
    parser.add_argument("--document-timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--engine-timeout-seconds",
        type=int,
        default=int(RUNTIME_CONFIG.application["queue_timeout_seconds"]),
    )
    parser.add_argument("--application-timeout-seconds", type=int, default=420)
    parser.add_argument("--refresh-timeout-seconds", type=int, default=3600)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one job and return; intended for diagnostics and tests",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    ats_platform: str | None = None,
) -> int:
    if ats_platform is None:
        platform_parser = argparse.ArgumentParser(add_help=False)
        platform_parser.add_argument("--ats-platform", required=True)
        platform_args, argv = platform_parser.parse_known_args(argv)
        ats_platform = platform_args.ats_platform
    ats_platform = _validate_platform(ats_platform)
    args = build_parser(ats_platform).parse_args(argv)
    for label, value in (
        ("sleep minimum", args.sleep_min_seconds),
        ("sleep maximum", args.sleep_max_seconds),
        ("document timeout", args.document_timeout_seconds),
        ("engine timeout", args.engine_timeout_seconds),
        ("application timeout", args.application_timeout_seconds),
        ("refresh timeout", args.refresh_timeout_seconds),
    ):
        if value <= 0:
            raise SystemExit(f"{label} must be greater than zero")
    if args.sleep_min_seconds > args.sleep_max_seconds:
        raise SystemExit("sleep minimum cannot exceed sleep maximum")
    for label, path in (
        ("profile", args.profile),
        ("email pool", args.email_pool),
        ("launcher", args.launcher),
    ):
        if not path.is_file():
            raise SystemExit(f"{label} file not found: {path}")

    seeded = _seed_platform_input(args.input, ats_platform)
    if seeded:
        print(
            f"{ats_platform.upper()}_INPUT_SEEDED count={seeded}",
            flush=True,
        )

    state = _load_state(args.state, ats_platform)
    reconciled = _reconcile_interrupted_submissions(state)
    if reconciled:
        _save_state(args.state, state)
        print(
            f"{ats_platform.upper()}_INTERRUPTED_QUARANTINED count={reconciled}",
            flush=True,
        )

    while True:
        try:
            if not args.input.is_file():
                outcome = _refresh_jobs(
                    ats_platform=ats_platform,
                    launcher=args.launcher,
                    input_path=args.input,
                    timeout_seconds=args.refresh_timeout_seconds,
                )
                if outcome.return_code != 0:
                    print(
                        f"{ats_platform.upper()}_REFRESH_FAILED "
                        f"exit_code={outcome.return_code} timed_out={outcome.timed_out}",
                        flush=True,
                    )
                    cycle_status = "refresh_failed"
                else:
                    cycle_status = "refreshed"
            else:
                cycle_status = process_one(
                    ats_platform=ats_platform,
                    input_path=args.input,
                    profile=args.profile,
                    email_pool=args.email_pool,
                    launcher=args.launcher,
                    state_path=args.state,
                    results_dir=args.results_dir,
                    documents_dir=args.documents_dir,
                    submission_log=args.submission_log,
                    document_timeout_seconds=args.document_timeout_seconds,
                    engine_timeout_seconds=args.engine_timeout_seconds,
                    application_timeout_seconds=args.application_timeout_seconds,
                )
                if cycle_status == "no_work":
                    outcome = _refresh_jobs(
                        ats_platform=ats_platform,
                        launcher=args.launcher,
                        input_path=args.input,
                        timeout_seconds=args.refresh_timeout_seconds,
                    )
                    print(
                        f"{ats_platform.upper()}_REFRESH_FINISHED "
                        f"exit_code={outcome.return_code} timed_out={outcome.timed_out}",
                        flush=True,
                    )
                    cycle_status = "refreshed" if outcome.return_code == 0 else "refresh_failed"
        except KeyboardInterrupt:
            print(
                f"{ats_platform.upper()}_WORKER_STOPPED signal=keyboard_interrupt",
                flush=True,
            )
            return 130
        except Exception as exc:
            print(
                f"{ats_platform.upper()}_CYCLE_EXCEPTION "
                f"type={type(exc).__name__} detail={str(exc)[:500]!r}",
                file=sys.stderr,
                flush=True,
            )
            cycle_status = "exception"

        if args.once:
            return 0 if cycle_status in {"confirmed", "refreshed"} else 1
        delay = random.randint(args.sleep_min_seconds, args.sleep_max_seconds)
        print(
            f"{ats_platform.upper()}_CYCLE_SLEEP seconds={delay} prior_status={cycle_status}",
            flush=True,
        )
        if not _sleep_between_cycles(delay, ats_platform):
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
