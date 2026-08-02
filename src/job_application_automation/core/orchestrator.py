#!/usr/bin/env python3
"""
ATS-aware job application orchestrator.

Reads jobs from an Excel tracker, detects the ATS from each URL, selects the
matching engine, optionally generates a URL-specific resume, and persists a
structured result after every job.

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. State-Machine Resilience Engine (Temporal.io / Prefect / Celery Orchestration):
   - Replace sequential Excel-row iterations and fragile subprocess invocation loops
     with a durable workflow orchestration engine (e.g. Temporal or Prefect).
   - Benefit: Automatic workflow state checkpoints, automatic step-level retries
     upon engine timeout/crash, and distributed parallel execution across multi-node worker pools.

2. Reactive Event-Driven Application Pipeline (Kafka / RabbitMQ / Redis Streams):
   - Decouple job application steps into asynchronous micro-events:
     JobDiscovered -> TailoringRequested -> BrowserSessionAllocated -> Submitted -> Verified.
   - Benefit: Maximizes throughput by running LLM resume tailoring asynchronously
     in parallel while Playwright engines submit previously tailored applications.

3. Live Browser Context Pool with Hot-Swappable Fingerprints:
   - Instead of launching fresh `python -m job_automation engine ...` subprocesses
     per job row (incurring cold startup penalty), orchestrate an in-memory Playwright
     browser pool with dynamic anti-detection profile rotations and residential proxy binding.
==============================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, TypedDict
from collections.abc import Mapping, Sequence
from urllib.parse import unquote, urlparse

import openpyxl  # type: ignore[import-untyped]

from ..mail.pool import load_email_pool
from ..resume.cover_letter_ai import PROMPT_TEMPLATE_VERSION
from .adapters import CommandResult, ProcessRunner, ProcessSettings
from .application_pipeline import (
    LEDGER_PERSIST_FAILED_STATUS,
    ApplicationPipeline,
    ApplicationTarget,
    EngineOutcome,
    PipelineConfig,
    PipelineOperations,
    ProcessResult,
    ProcessTimeoutError,
    SubmissionPersistence as _SubmissionPersistence,
)
from .artifacts import read_json as read_json_artifact
from .artifacts import write_json as write_json_artifact
from .ats_urls import ATS_HOST_MARKERS as ATS_HOSTS
from .ats_urls import detect_ats_job_url
from .contracts import EngineMode, EngineRequest, EngineResult
from .engine_shared import (
    current_title_from_resume,
    email_from_resume,
    load_json_config,
)
from .engine_shared import (
    RESULT_PREFIX as ENGINE_RESULT_PREFIX,
)
from .engine_shared import (
    mask_email as _mask_email,
)
from .exceptions import InputContractError
from .paths import CLI_ENTRYPOINT, CONFIG_DIR, OUTPUT_DIR, SRC_DIR, resolve_existing
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from .screenshots import (
    cleanup_application_screenshot_directory,
    create_application_screenshot_directory,
)
from .submission_log import SubmissionLog, SubmissionRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ATSOrchestrator")

DEFAULT_TRACKER_FILE = resolve_runtime_path(RUNTIME_CONFIG.application.tracker_file)
DEFAULT_RESUME_FILE = resolve_runtime_path(RUNTIME_CONFIG.application.base_resume_file)
DEFAULT_CONFIG_FILE = CONFIG_DIR / "candidate_profile_config.json"
DEFAULT_RESULTS_FILE = resolve_runtime_path(RUNTIME_CONFIG.application.results_file)
DEFAULT_SUBMISSION_LOG_FILE = resolve_runtime_path(RUNTIME_CONFIG.application.submission_log_file)
DEFAULT_EMAIL_POOL_FILE = resolve_runtime_path(RUNTIME_CONFIG.application.candidate_email_pool_file)
DEFAULT_ENGINE_TIMEOUT_SECONDS = RUNTIME_CONFIG.application.engine_timeout_seconds
DEFAULT_RESUME_TIMEOUT_SECONDS = RUNTIME_CONFIG.application.resume_timeout_seconds
MIN_COVER_LETTER_BYTES = 1_000
SUPPORTED_ATS = tuple(ATS_HOSTS)

DEFAULT_ENGINE_FILES: Mapping[str, Path] = {
    "ashby": CLI_ENTRYPOINT,
    "greenhouse": CLI_ENTRYPOINT,
    "lever": CLI_ENTRYPOINT,
    "workable": CLI_ENTRYPOINT,
    "smartrecruiters": CLI_ENTRYPOINT,
}


class JobRecord(TypedDict):
    row_number: int
    company: str
    role: str
    url: str
    ats: str


class SubprocessRunner:
    """Production adapter for the injectable process-runner contract."""

    def run(self, command: Sequence[str], settings: ProcessSettings) -> CommandResult:
        result = run_command(
            command,
            settings.timeout_seconds,
            env=settings.environment,
        )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def detect_ats(url: str) -> str | None:
    """Return the supported ATS for a job-specific HTTPS URL, or None."""
    return detect_ats_job_url(url)


def _find_header(headers: Sequence[str], aliases: tuple[str, ...], label: str) -> int:
    for index, header in enumerate(headers):
        if header in aliases:
            return index
    for index, header in enumerate(headers):
        if any(alias in header for alias in aliases):
            return index
    raise ValueError(f"Tracker is missing a recognizable {label} column; found headers: {headers}")


def load_jobs_from_tracker(tracker_path: Path) -> list[JobRecord]:
    """Load validated job-specific supported ATS entries from the active sheet."""
    if not tracker_path.exists():
        raise FileNotFoundError(f"Tracker file not found: {tracker_path}")

    workbook = openpyxl.load_workbook(tracker_path, data_only=True, read_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        first_row = next(rows, None)
        if not first_row:
            return []

        headers = [str(value).strip().lower() if value else "" for value in first_row]
        company_index = _find_header(headers, ("company", "company name"), "company")
        role_index = _find_header(headers, ("title", "job title", "role", "role title"), "role")
        url_index = _find_header(headers, ("url", "job url", "link", "job link"), "URL")

        jobs: list[JobRecord] = []
        required_index = max(company_index, role_index, url_index)
        for row_number, row in enumerate(rows, start=2):
            if not row or len(row) <= required_index:
                continue
            url = str(row[url_index]).strip() if row[url_index] else ""
            ats = detect_ats(url)
            if not ats:
                continue
            company_val = str(row[company_index]).strip() if row[company_index] else ""
            role_val = str(row[role_index]).strip() if row[role_index] else ""
            if not company_val:
                logger.warning(
                    "Tracker row %d has empty company; defaulting to 'Company'", row_number
                )
                company_val = "Company"
            if not role_val:
                logger.warning(
                    "Tracker row %d has empty role; defaulting to 'Product Manager'", row_number
                )
                role_val = "Product Manager"
            jobs.append(
                {
                    "row_number": row_number,
                    "company": company_val,
                    "role": role_val,
                    "url": url,
                    "ats": ats,
                }
            )
        return jobs
    finally:
        workbook.close()


def job_from_url(
    url: str,
    *,
    company: str = "",
    role: str = "",
) -> JobRecord:
    """Build one validated job record directly from an ATS URL."""
    ats = detect_ats(url)
    if not ats:
        supported = ", ".join(ATS_HOSTS)
        raise ValueError(f"URL must be a job-specific HTTPS URL for a supported ATS: {supported}")
    path_parts = [unquote(part).strip() for part in urlparse(url).path.split("/") if part.strip()]
    inferred_company = path_parts[0].replace("-", " ").strip().title() if path_parts else "Company"
    final_company = company.strip() or inferred_company
    final_role = role.strip() or "Product Manager"
    if not company.strip():
        logger.warning("No company specified for URL; using '%s'", final_company)
    if not role.strip():
        logger.warning("No role specified for URL; defaulting to 'Product Manager'")
    return {
        "row_number": 1,
        "company": final_company,
        "role": final_role,
        "url": url.strip(),
        "ats": ats,
    }


def resolve_engine_path(raw_path: Path) -> Path:
    """Resolve an engine path relative to the source directory."""
    return resolve_existing(raw_path, SRC_DIR).resolve()


def _uses_project_cli(engine_path: Path) -> bool:
    """Return whether *engine_path* is the bundled unified command runner."""
    return engine_path.resolve() == CLI_ENTRYPOINT.resolve()


def _engine_label(engine_path: Path, ats: str) -> str:
    """Return an audit-friendly label for bundled and custom engines."""
    return f"internal:{ats}" if _uses_project_cli(engine_path) else engine_path.name


def _engine_mode_flag(*, live_submit: bool, fill_only: bool, dry_run: bool) -> str:
    """Return the engine mode flag using the established precedence."""
    return _engine_mode(
        live_submit=live_submit,
        fill_only=fill_only,
        dry_run=dry_run,
    ).cli_flag


def _engine_mode(*, live_submit: bool, fill_only: bool, dry_run: bool) -> EngineMode:
    """Resolve legacy boolean flags to one explicit typed engine mode."""
    if fill_only:
        return EngineMode.FILL_ONLY
    if live_submit:
        return EngineMode.LIVE_SUBMIT
    if dry_run:
        return EngineMode.DRY_RUN
    return EngineMode.DRY_RUN


def build_engine_command(
    engine_path: Path,
    url: str,
    resume_path: Path,
    company: str,
    role: str,
    email: str,
    live_submit: bool,
    cover_letter_path: Path | None = None,
    headed: bool = False,
    fill_only: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """Construct the standardized engine CLI invocation."""
    request = EngineRequest(
        ats=detect_ats(url) or "unknown",
        url=url,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        company=company,
        role=role,
        email=email,
        mode=_engine_mode(
            live_submit=live_submit,
            fill_only=fill_only,
            dry_run=dry_run,
        ),
        headed=headed,
    )
    if _uses_project_cli(engine_path):
        return [
            sys.executable,
            str(engine_path),
            "engine",
            request.ats,
            *request.cli_arguments(),
        ]
    return [sys.executable, str(engine_path), *request.cli_arguments()]


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # /T kills the whole descendant tree: the engine subprocess itself
            # spawns a Chrome/Playwright browser process that would otherwise
            # survive a plain process.kill() of just the Python child.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("Could not terminate process tree %s: %s", process.pid, exc)
    finally:
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Could not kill child process %s: %s", process.pid, exc)


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_command(
    cmd: Sequence[str],
    timeout_seconds: int,
    *,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run a child with bounded lifetime and descendant cleanup."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if not cmd:
        raise ValueError("cmd must contain at least one argument")

    # CREATE_NEW_PROCESS_GROUP lets _terminate_process_tree() target the whole
    # tree via taskkill /T; on POSIX, start_new_session is the equivalent so
    # killpg() below can reach descendants instead of only the direct child.
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )
    # ProcessSettings.environment defaults to an empty mapping (not None) when
    # a caller has no overrides to add, so treat any provided mapping as
    # additions layered onto the parent environment rather than a full
    # replacement; otherwise the child loses PATH/APPDATA/PYTHONPATH and
    # cannot locate the interpreter's own installed packages.
    merged_env = {**os.environ, **env} if env is not None else None
    process = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        env=merged_env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return ProcessResult(process.returncode, stdout or "", stderr or "")
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise ProcessTimeoutError(
            timeout_seconds,
            _as_text(stdout) or _as_text(exc.stdout),
            _as_text(stderr) or _as_text(exc.stderr),
        ) from exc


def _invalid_engine_outcome(detail: str = "") -> EngineOutcome:
    return EngineOutcome(
        success=False,
        status="INVALID_ENGINE_RESULT",
        detail=detail,
    )


def _parse_engine_outcome(
    result: ProcessResult,
    mode: EngineMode,
    expected_ats: str,
) -> EngineOutcome:
    """Parse child output into the typed pipeline result boundary."""
    combined = f"{result.stdout}\n{result.stderr}"
    for line in reversed(combined.splitlines()):
        if not line.startswith(ENGINE_RESULT_PREFIX):
            continue
        try:
            contract_result = EngineResult.from_wire_line(line)
        except ValueError as exc:
            return _invalid_engine_outcome(str(exc))
        return EngineOutcome.from_engine_result(
            contract_result,
            expected_ats=expected_ats or contract_result.ats,
            live_submit=mode is EngineMode.LIVE_SUBMIT,
        )

    final_outcome = ""
    for line in combined.splitlines():
        if "Final Outcome ->" in line:
            final_outcome = line.split("Final Outcome ->", 1)[1].strip()

    successful_statuses = {"PREFILLED_ONLY", "SUBMITTED & CONFIRMED"}
    success = result.returncode == 0 and final_outcome in successful_statuses
    if mode is EngineMode.LIVE_SUBMIT and final_outcome != "SUBMITTED & CONFIRMED":
        success = False
    return EngineOutcome(
        success=success,
        status=final_outcome or f"EXIT_{result.returncode}_NO_STRUCTURED_RESULT",
        ats=expected_ats,
        legacy_result=True,
    )


def parse_engine_result(result: ProcessResult, live_submit: bool) -> dict[str, Any]:
    """Parse and validate the structured result marker with a legacy fallback."""
    outcome = _parse_engine_outcome(
        result,
        EngineMode.LIVE_SUBMIT if live_submit else EngineMode.DRY_RUN,
        "",
    )
    return dict(outcome.to_payload(include_ats=True, namespace_details=False))


def _write_results(
    results_path: Path,
    results: Sequence[Mapping[str, Any]],
) -> None:
    """Atomically persist the complete result snapshot."""
    write_json_artifact(results_path, list(results), indent=2, ensure_ascii=False)


def _submission_quarantine_path(path: Path) -> Path:
    """Return the private sidecar used when confirmed-ledger persistence fails."""
    suffix = path.suffix or ".json"
    return path.with_name(f"{path.stem}_quarantine{suffix}")


def _load_submission_log(
    path: Path,
    *,
    strict: bool = False,
    label: str = "submission log",
) -> SubmissionLog:
    """Load a ledger-like artifact, failing closed when live safety requires it."""
    log = SubmissionLog()
    if path.exists():
        try:
            log.load(path, strict=strict)
        except (OSError, ValueError) as exc:
            if strict:
                raise ValueError(f"Could not safely load {label} {path}: {exc}") from exc
            logger.warning("Could not load existing %s %s: %s", label, path, exc)
    return log


def _record_submission(
    submission_log: SubmissionLog,
    submission_log_path: Path,
    *,
    job: JobRecord,
    email: str,
    resume_path: Path,
    cover_letter_path: Path | None,
    status: str,
) -> _SubmissionPersistence:
    """Persist a confirmed submission or durably quarantine it for manual review."""
    submission: SubmissionRecord | None = None
    try:
        submission = SubmissionRecord(
            company=job["company"],
            role=job["role"],
            job_url=job["url"],
            ats=job["ats"],
            status=status,
            email_used=email,
            resume_filename=resume_path.name,
            cover_letter_filename=(cover_letter_path.name if cover_letter_path is not None else ""),
        )
        submission_log.record(submission)
        submission_log.save(submission_log_path)
    except (OSError, ValueError) as exc:
        persistence_error = f"{type(exc).__name__}: {exc}"
        logger.error("Could not persist confirmed submission for %s: %s", job["url"], exc)
        quarantine_path = _submission_quarantine_path(submission_log_path)
        if submission is None:
            return _SubmissionPersistence(
                persisted=False,
                error=persistence_error,
                quarantine_path=quarantine_path,
                quarantine_error="Submission record validation failed before quarantine persistence",
            )
        try:
            quarantine_log = _load_submission_log(
                quarantine_path,
                strict=True,
                label="submission quarantine",
            )
            quarantine_log.record(
                SubmissionRecord(
                    company=submission.company,
                    role=submission.role,
                    job_url=submission.job_url,
                    ats=submission.ats,
                    status=LEDGER_PERSIST_FAILED_STATUS,
                    email_used=submission.email_used,
                    resume_filename=submission.resume_filename,
                    cover_letter_filename=submission.cover_letter_filename,
                    applied_at=submission.applied_at,
                )
            )
            quarantine_log.save(quarantine_path)
        except (OSError, ValueError) as quarantine_exc:
            quarantine_error = f"{type(quarantine_exc).__name__}: {quarantine_exc}"
            logger.error(
                "Could not persist submission quarantine for %s: %s",
                job["url"],
                quarantine_exc,
            )
            return _SubmissionPersistence(
                persisted=False,
                error=persistence_error,
                quarantine_path=quarantine_path,
                quarantine_error=quarantine_error,
            )
        return _SubmissionPersistence(
            persisted=False,
            error=persistence_error,
            quarantine_path=quarantine_path,
        )
    return _SubmissionPersistence(persisted=True)


def _is_confirmed_submission(outcome: Mapping[str, object]) -> bool:
    """Accept only a validated, confirmed result for the submission log."""
    try:
        return EngineResult.from_payload(outcome).is_confirmed_submission
    except ValueError:
        return False


def _personalized_document_stem(company: str, role: str, url: str, email: str = "") -> str:
    safe_company = "".join(
        character if character.isalnum() else "_" for character in company
    ).strip("_")
    safe_role = "".join(character if character.isalnum() else "_" for character in role).strip("_")
    identity = f"{url.strip()}|{email.strip().casefold()}"
    posting_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{safe_company}_{safe_role}_{posting_hash}"


def _personalized_resume_path(
    company: str,
    role: str,
    url: str,
    email: str = "",
) -> Path:
    """Return a stable job-and-email-specific resume path."""
    return OUTPUT_DIR / f"{_personalized_document_stem(company, role, url, email)}_Resume.pdf"


def _personalized_cover_letter_path(
    company: str,
    role: str,
    url: str,
    email: str,
) -> Path:
    """Return the matching job-and-email-specific cover-letter path."""
    return OUTPUT_DIR / (
        f"{_personalized_document_stem(company, role, url, email)}_Cover_Letter.pdf"
    )


def _cover_letter_audit_is_current(audit_path: Path) -> bool:
    try:
        payload = read_json_artifact(audit_path)
    except (OSError, ValueError):
        return False
    return bool(
        isinstance(payload, Mapping)
        and payload.get("prompt_template_version") == PROMPT_TEMPLATE_VERSION
    )


def generate_personalized_resume(
    company: str,
    role: str,
    url: str,
    timeout_seconds: int,
    email: str = "",
    process_runner: ProcessRunner | None = None,
) -> Path | None:
    generator = CLI_ENTRYPOINT
    if not generator.exists():
        logger.warning("Resume generator not found: %s", generator)
        return None

    output_path = _personalized_resume_path(company, role, url, email)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # The 5000-byte floor is a cheap sanity check that the existing file is a
    # real rendered PDF rather than a truncated or failed prior write.
    if (
        os.environ.get("JOB_APP_FORCE_RESUME_REGENERATION") != "1"
        and output_path.exists()
        and output_path.stat().st_size > 5000
    ):
        try:
            existing_email = email_from_resume(output_path, "").strip().casefold()
        except ValueError:
            existing_email = ""
        if not email or existing_email == email.strip().casefold():
            logger.info(
                "Reusing existing position-specific resume for retry: %s",
                output_path.name,
            )
            return output_path
    tmp_output_path = output_path.with_name(
        f".tmp_{os.getpid()}_{random.randint(1000, 9999)}_{output_path.name}"
    )
    command = [
        sys.executable,
        str(generator),
        "resume",
        "--company",
        company,
        "--role",
        role,
        "--url",
        url,
        "--output",
        str(tmp_output_path),
    ]
    if email:
        command.extend(["--email", email])

    try:
        command_result = (process_runner or SubprocessRunner()).run(
            command,
            ProcessSettings(timeout_seconds=timeout_seconds),
        )
        result = ProcessResult(
            command_result.returncode,
            command_result.stdout,
            command_result.stderr,
        )
    except ProcessTimeoutError:
        logger.warning("Resume generation timed out after %d seconds.", timeout_seconds)
        if tmp_output_path.exists():
            tmp_output_path.unlink(missing_ok=True)
        return None
    except OSError as exc:
        logger.warning("Could not start resume generator: %s", exc)
        if tmp_output_path.exists():
            tmp_output_path.unlink(missing_ok=True)
        return None

    if (
        result.returncode == 0
        and tmp_output_path.exists()
        and tmp_output_path.stat().st_size > 5000
    ):
        try:
            os.replace(tmp_output_path, output_path)
            return output_path
        except OSError as exc:
            logger.warning("Could not replace output resume file: %s", exc)
            tmp_output_path.unlink(missing_ok=True)
            return None
    logger.warning(
        "Resume generation failed (exit=%d): %s",
        result.returncode,
        (result.stderr or result.stdout)[-300:],
    )
    if tmp_output_path.exists():
        tmp_output_path.unlink(missing_ok=True)
    return None


def generate_personalized_cover_letter(
    company: str,
    role: str,
    url: str,
    email: str,
    profile_path: Path,
    timeout_seconds: int,
    process_runner: ProcessRunner | None = None,
) -> Path | None:
    """Generate and atomically promote one validated position-specific cover letter."""
    generator = CLI_ENTRYPOINT
    if not generator.exists():
        logger.warning("Cover-letter generator not found: %s", generator)
        return None
    output_path = _personalized_cover_letter_path(company, role, url, email)
    audit_path = output_path.with_name(f"{output_path.stem}.audit.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if (
        os.environ.get("JOB_APP_FORCE_COVER_LETTER_REGENERATION") != "1"
        and output_path.exists()
        and output_path.stat().st_size >= MIN_COVER_LETTER_BYTES
        and _cover_letter_audit_is_current(audit_path)
    ):
        logger.info(
            "Reusing existing position-specific cover letter for retry: %s",
            output_path.name,
        )
        return output_path

    tmp_output_path = output_path.with_name(
        f".tmp_{os.getpid()}_{random.randint(1000, 9999)}_{output_path.name}"
    )
    tmp_audit_path = tmp_output_path.with_name(f"{tmp_output_path.stem}.audit.json")
    command = [
        sys.executable,
        str(generator),
        "cover-letter",
        "--company",
        company,
        "--role",
        role,
        "--url",
        url,
        "--email",
        email,
        "--profile",
        str(profile_path),
        "--output",
        str(tmp_output_path),
    ]
    try:
        command_result = (process_runner or SubprocessRunner()).run(
            command,
            ProcessSettings(timeout_seconds=timeout_seconds),
        )
        result = ProcessResult(
            command_result.returncode,
            command_result.stdout,
            command_result.stderr,
        )
    except ProcessTimeoutError:
        logger.warning("Cover-letter generation timed out after %d seconds.", timeout_seconds)
        tmp_output_path.unlink(missing_ok=True)
        tmp_audit_path.unlink(missing_ok=True)
        return None
    except OSError as exc:
        logger.warning("Could not start cover-letter generator: %s", exc)
        tmp_output_path.unlink(missing_ok=True)
        tmp_audit_path.unlink(missing_ok=True)
        return None

    if (
        result.returncode == 0
        and tmp_output_path.exists()
        and tmp_output_path.stat().st_size >= MIN_COVER_LETTER_BYTES
        and tmp_audit_path.is_file()
    ):
        try:
            os.replace(tmp_output_path, output_path)
            os.replace(tmp_audit_path, audit_path)
            return output_path
        except OSError as exc:
            logger.warning("Could not promote cover-letter artifacts: %s", exc)
    else:
        logger.warning(
            "Cover-letter generation failed (exit=%d): %s",
            result.returncode,
            (result.stderr or result.stdout)[-500:],
        )
    tmp_output_path.unlink(missing_ok=True)
    tmp_audit_path.unlink(missing_ok=True)
    return None


def _random_job_emails(
    jobs: Sequence[JobRecord],
    *,
    email_override: str,
    email_pool_path: Path,
    prepared_resume_path: Path | None,
    fallback_email: str,
) -> list[str]:
    """Choose a unique random pool address per job, preserving prepared-document identity."""
    if not jobs:
        return []
    if email_override:
        return [email_override] * len(jobs)
    if prepared_resume_path is not None:
        prepared_email = email_from_resume(prepared_resume_path, fallback_email).strip().casefold()
        return [prepared_email] * len(jobs)
    pool = [email.strip().casefold() for email in load_email_pool(email_pool_path)]
    if len(pool) < len(jobs):
        raise ValueError(
            f"Candidate email pool has {len(pool)} addresses for {len(jobs)} jobs; "
            "unique random assignment is required."
        )
    return random.sample(pool, len(jobs))


def _assign_job_emails(
    jobs: Sequence[JobRecord],
    *,
    engine_paths: Mapping[str, Path],
    live_submit: bool,
    submission_log: SubmissionLog,
    submission_quarantine: SubmissionLog,
    email_override: str,
    email_pool_path: Path,
    prepared_resume_path: Path | None,
    fallback_email: str,
) -> tuple[list[str], list[bool]]:
    """Assign emails only after terminal-ledger and engine-availability gates."""
    requires_email: list[bool] = []
    for job in jobs:
        if live_submit and (
            submission_log.find_by_job_url(job["url"])
            or submission_quarantine.find_by_job_url(job["url"])
        ):
            requires_email.append(False)
            continue
        engine_path = engine_paths.get(job["ats"])
        requires_email.append(bool(engine_path and engine_path.is_file()))

    actionable_jobs = [job for job, required in zip(jobs, requires_email, strict=True) if required]
    actionable_emails = iter(
        _random_job_emails(
            actionable_jobs,
            email_override=email_override,
            email_pool_path=email_pool_path,
            prepared_resume_path=prepared_resume_path,
            fallback_email=fallback_email,
        )
    )
    emails = [next(actionable_emails) if required else "" for required in requires_email]
    return emails, requires_email


def _validate_orchestrator_inputs(
    *,
    tracker_path: Path | None,
    require_tracker: bool,
    resume_path: Path,
    prepared_resume_path: Path | None,
    cover_letter_path: Path | None,
    config_path: Path | None,
    timeout_seconds: int,
    resume_timeout_seconds: int,
) -> None:
    required_files = [
        (
            "Prepared resume" if prepared_resume_path is not None else "Resume",
            prepared_resume_path or resume_path,
        ),
    ]
    if cover_letter_path is not None:
        required_files.append(("Cover letter", cover_letter_path))
    if require_tracker:
        if tracker_path is None:
            raise InputContractError("Tracker path is required when --url is not provided")
        required_files.insert(0, ("Tracker", tracker_path))
    for label, path in required_files:
        if not path.is_file():
            raise FileNotFoundError(f"{label} file not found: {path}")
    if config_path and not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    if timeout_seconds <= 0:
        raise InputContractError("Engine timeout must be greater than zero")
    if resume_timeout_seconds <= 0:
        raise InputContractError("Resume timeout must be greater than zero")


def _select_jobs(
    jobs: list[JobRecord],
    *,
    shuffle: bool,
    start_index: int,
    limit: int | None,
) -> list[JobRecord]:
    selected = list(jobs)
    if start_index > 0:
        selected = selected[start_index:]
    if shuffle:
        random.shuffle(selected)
    if limit and limit > 0:
        selected = selected[:limit]
    return selected


def _mode_name(*, live_submit: bool, fill_only: bool) -> str:
    if live_submit:
        return "LIVE"
    if fill_only:
        return "FILL_ONLY"
    return "DRY_RUN"


def _pipeline_operations(
    *,
    config_path: Path,
    resume_timeout_seconds: int,
    process_runner: ProcessRunner | None,
) -> PipelineOperations:
    """Bind the typed pipeline to the established orchestrator patch seams."""

    def generate_resume(target: ApplicationTarget, email: str) -> Path | None:
        return generate_personalized_resume(
            target.company,
            target.role,
            target.url,
            resume_timeout_seconds,
            email,
            process_runner,
        )

    def generate_cover_letter(target: ApplicationTarget, email: str) -> Path | None:
        return generate_personalized_cover_letter(
            target.company,
            target.role,
            target.url,
            email,
            config_path,
            resume_timeout_seconds,
            process_runner,
        )

    def build_command(
        engine_path: Path,
        target: ApplicationTarget,
        target_resume: Path,
        target_cover_letter: Path,
        email: str,
        mode: EngineMode,
        headed: bool,
    ) -> Sequence[str]:
        return build_engine_command(
            engine_path,
            target.url,
            target_resume,
            target.company,
            target.role,
            email,
            mode is EngineMode.LIVE_SUBMIT,
            cover_letter_path=target_cover_letter,
            headed=headed,
            fill_only=mode is EngineMode.FILL_ONLY,
            dry_run=mode is EngineMode.DRY_RUN,
        )

    def run_process(command: Sequence[str], settings: ProcessSettings) -> CommandResult:
        return (process_runner or SubprocessRunner()).run(command, settings)

    def create_screenshot_directory(inherited: str | Path | None) -> Path:
        return create_application_screenshot_directory(inherited=inherited)

    def record_submission(
        submission_log: SubmissionLog,
        submission_log_path: Path,
        target: ApplicationTarget,
        email: str,
        target_resume: Path,
        target_cover_letter: Path,
        status: str,
    ) -> _SubmissionPersistence:
        job: JobRecord = {
            "row_number": target.row_number,
            "company": target.company,
            "role": target.role,
            "url": target.url,
            "ats": target.ats,
        }
        return _record_submission(
            submission_log,
            submission_log_path,
            job=job,
            email=email,
            resume_path=target_resume,
            cover_letter_path=target_cover_letter,
            status=status,
        )

    return PipelineOperations(
        generate_resume=generate_resume,
        generate_cover_letter=generate_cover_letter,
        read_resume_email=email_from_resume,
        read_current_title=current_title_from_resume,
        engine_label=_engine_label,
        build_engine_command=build_command,
        run_process=run_process,
        parse_engine_result=_parse_engine_outcome,
        create_screenshot_directory=create_screenshot_directory,
        cleanup_screenshot_directory=cleanup_application_screenshot_directory,
        mask_email=_mask_email,
        record_submission=record_submission,
        write_results=_write_results,
    )


def run_orchestrator(
    engine_paths: Mapping[str, Path],
    tracker_path: Path | None,
    resume_path: Path,
    config_path: Path | None,
    results_path: Path,
    prepared_resume_path: Path | None = None,
    cover_letter_path: Path | None = None,
    email_override: str = "",
    email_pool_path: Path = DEFAULT_EMAIL_POOL_FILE,
    submission_log_path: Path = DEFAULT_SUBMISSION_LOG_FILE,
    limit: int | None = None,
    start_index: int = 0,
    live_submit: bool = False,
    fill_only: bool = False,
    dry_run: bool = False,
    shuffle: bool = True,
    headed: bool = False,
    timeout_seconds: int = DEFAULT_ENGINE_TIMEOUT_SECONDS,
    personalize_resume: bool = True,
    resume_timeout_seconds: int = DEFAULT_RESUME_TIMEOUT_SECONDS,
    direct_url: str | None = None,
    direct_company: str = "",
    direct_role: str = "",
    process_runner: ProcessRunner | None = None,
) -> list[dict[str, Any]]:
    """Run the ATS-aware application loop and persist progress."""
    if not personalize_resume:
        raise InputContractError(
            "Resume personalization is mandatory for every orchestrated application."
        )
    _validate_orchestrator_inputs(
        tracker_path=tracker_path,
        require_tracker=not bool(direct_url),
        resume_path=resume_path,
        prepared_resume_path=prepared_resume_path,
        cover_letter_path=cover_letter_path,
        config_path=config_path,
        timeout_seconds=timeout_seconds,
        resume_timeout_seconds=resume_timeout_seconds,
    )
    if config_path is None:
        raise InputContractError("A profile configuration is required.")
    if prepared_resume_path is not None and not direct_url:
        raise InputContractError("--prepared-resume can only be used with --url")
    if cover_letter_path is not None and prepared_resume_path is None:
        raise InputContractError("--cover-letter requires --prepared-resume")
    normalized_email_override = email_override.strip().lower()
    if normalized_email_override and (
        "@" not in normalized_email_override or normalized_email_override.startswith("@")
    ):
        raise InputContractError("Email override must contain a local part and @")
    profile_config = load_json_config(config_path)
    fallback_email = str(profile_config["candidate"].get("fallback_email", "")).strip()

    source_jobs = (
        [
            job_from_url(
                direct_url,
                company=direct_company,
                role=direct_role,
            )
        ]
        if direct_url
        else load_jobs_from_tracker(tracker_path)  # type: ignore[arg-type]
    )
    jobs = _select_jobs(
        source_jobs,
        shuffle=shuffle,
        start_index=start_index,
        limit=limit,
    )
    logger.info(
        "Loaded %d supported jobs | mode=%s | shuffle=%s",
        len(jobs),
        _mode_name(live_submit=live_submit, fill_only=fill_only),
        shuffle,
    )

    submission_log = _load_submission_log(
        submission_log_path,
        strict=live_submit,
    )
    submission_quarantine_path = _submission_quarantine_path(submission_log_path)
    submission_quarantine = (
        _load_submission_log(
            submission_quarantine_path,
            strict=True,
            label="submission quarantine",
        )
        if live_submit
        else SubmissionLog()
    )
    job_emails, email_required = _assign_job_emails(
        jobs,
        engine_paths=engine_paths,
        live_submit=live_submit,
        submission_log=submission_log,
        submission_quarantine=submission_quarantine,
        email_override=normalized_email_override,
        email_pool_path=email_pool_path,
        prepared_resume_path=prepared_resume_path,
        fallback_email=fallback_email,
    )
    targets = [
        ApplicationTarget(
            row_number=job["row_number"],
            company=job["company"],
            role=job["role"],
            url=job["url"],
            ats=job["ats"],
        )
        for job in jobs
    ]
    pipeline = ApplicationPipeline(
        targets=targets,
        emails=job_emails,
        email_required=email_required,
        config=PipelineConfig(
            engine_paths=engine_paths,
            results_path=results_path,
            submission_log_path=submission_log_path,
            submission_quarantine_path=submission_quarantine_path,
            config_path=config_path,
            prepared_resume_path=prepared_resume_path,
            prepared_cover_letter_path=cover_letter_path,
            mode=_engine_mode(
                live_submit=live_submit,
                fill_only=fill_only,
                dry_run=dry_run,
            ),
            headed=headed,
            timeout_seconds=timeout_seconds,
            fallback_email=fallback_email,
        ),
        submission_log=submission_log,
        submission_quarantine=submission_quarantine,
        operations=_pipeline_operations(
            config_path=config_path,
            resume_timeout_seconds=resume_timeout_seconds,
            process_runner=process_runner,
        ),
    )
    return pipeline.run()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ATS-aware application orchestrator")
    parser.add_argument(
        "--url",
        help="Run the complete workflow for one ATS job URL; ignores --tracker",
    )
    parser.add_argument("--company", default="", help="Company metadata for --url mode")
    parser.add_argument("--role", default="", help="Role metadata for --url mode")
    parser.add_argument("--tracker", default=str(DEFAULT_TRACKER_FILE))
    parser.add_argument("--resume", default=str(DEFAULT_RESUME_FILE))
    parser.add_argument(
        "--prepared-resume",
        default="",
        help="Use this already personalized resume for a direct --url application",
    )
    parser.add_argument(
        "--cover-letter",
        default="",
        help="Attach this personalized cover letter when the ATS form offers an upload",
    )
    parser.add_argument(
        "--email",
        default="",
        help="Override random pool selection and require both personalized documents to match",
    )
    parser.add_argument(
        "--email-pool",
        default=str(DEFAULT_EMAIL_POOL_FILE),
        help="Candidate email pool used for unique random per-job assignment",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE))
    parser.add_argument("--results-file", default=str(DEFAULT_RESULTS_FILE))
    parser.add_argument("--submission-log-file", default=str(DEFAULT_SUBMISSION_LOG_FILE))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=DEFAULT_ENGINE_TIMEOUT_SECONDS)
    parser.add_argument("--resume-timeout", type=int, default=DEFAULT_RESUME_TIMEOUT_SECONDS)
    parser.add_argument("--no-shuffle", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--personalize-resume",
        action="store_true",
        default=True,
        help="Generate a URL-specific personalized resume (mandatory and always enabled)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live-submit", action="store_true")
    mode.add_argument("--fill-only", action="store_true")
    mode.add_argument("--dry-run", action="store_true")

    parser.add_argument(
        "--engine",
        default=None,
        help="Deprecated alias for --ashby-engine custom script override",
    )
    parser.add_argument("--ashby-engine", default=None, help="Custom Ashby engine script")
    parser.add_argument("--greenhouse-engine", default=None, help="Custom Greenhouse engine script")
    parser.add_argument("--lever-engine", default=None, help="Custom Lever engine script")
    parser.add_argument("--workable-engine", default=None, help="Custom Workable engine script")
    parser.add_argument(
        "--smartrecruiters-engine", default=None, help="Custom SmartRecruiters engine script"
    )
    return parser


def _resolve_engine_paths(args: argparse.Namespace) -> dict[str, Path]:
    raw_engines: dict[str, str | Path] = {}
    for ats, default_path in DEFAULT_ENGINE_FILES.items():
        attr_name = f"{ats.replace('-', '_')}_engine"
        override = getattr(args, attr_name, None)
        if ats == "ashby" and not override:
            override = getattr(args, "engine", None)
        raw_engines[ats] = override or default_path
    return {ats: resolve_engine_path(Path(path)) for ats, path in raw_engines.items()}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    results_path = Path(args.results_file).resolve()
    try:
        results = run_orchestrator(
            engine_paths=_resolve_engine_paths(args),
            tracker_path=Path(args.tracker).resolve() if args.tracker else None,
            resume_path=Path(args.resume).resolve(),
            prepared_resume_path=(
                Path(args.prepared_resume).resolve() if args.prepared_resume else None
            ),
            cover_letter_path=(Path(args.cover_letter).resolve() if args.cover_letter else None),
            email_override=args.email,
            email_pool_path=Path(args.email_pool).resolve(),
            config_path=Path(args.config).resolve() if args.config else None,
            results_path=results_path,
            submission_log_path=Path(args.submission_log_file).resolve(),
            limit=args.limit,
            start_index=args.start_index,
            live_submit=args.live_submit,
            fill_only=args.fill_only,
            # dry_run is only active when neither --live-submit nor --fill-only
            # was explicitly requested, ensuring a bare invocation cannot submit
            # an application by accident while still letting --fill-only work
            # correctly with its own mode flag.
            dry_run=not args.live_submit and not args.fill_only,
            shuffle=not args.no_shuffle,
            headed=args.headed,
            timeout_seconds=args.timeout,
            personalize_resume=args.personalize_resume,
            resume_timeout_seconds=args.resume_timeout,
            direct_url=args.url,
            direct_company=args.company,
            direct_role=args.role,
        )
    except (OSError, ValueError, openpyxl.utils.exceptions.InvalidFileException) as exc:
        logger.error("Orchestration could not start: %s", exc)
        return 2
    # Exit codes: 0 = every job succeeded, 1 = ran but at least one job
    # failed, 2 = orchestration could not even start (see except above).
    return 0 if all(result.get("success") for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
