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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict
from collections.abc import Mapping, Sequence
from urllib.parse import unquote, urlparse

import openpyxl

from ..mail.pool import load_email_pool
from ..resume.cover_letter_ai import PROMPT_TEMPLATE_VERSION
from .adapters import CommandResult, ProcessRunner, ProcessSettings
from .artifacts import read_json as read_json_artifact
from .artifacts import write_json as write_json_artifact
from .contracts import EngineMode, EngineRequest, EngineResult
from .engine_shared import (
    ATS_HOST_MARKERS as ATS_HOSTS,
)
from .engine_shared import (
    ORCHESTRATOR_CONFIG_ENV,
    ORCHESTRATOR_CURRENT_TITLE_ENV,
    ORCHESTRATOR_INVOCATION_ENV,
    current_title_from_resume,
    detect_ats_job_url,
    email_from_resume,
    load_json_config,
)
from .engine_shared import (
    RESULT_PREFIX as ENGINE_RESULT_PREFIX,
)
from .engine_shared import (
    mask_email as _mask_email,
)
from .paths import CLI_ENTRYPOINT, CONFIG_DIR, OUTPUT_DIR, SRC_DIR, resolve_existing
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from .submission_log import SubmissionLog, SubmissionRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ATSOrchestrator")

DEFAULT_TRACKER_FILE = resolve_runtime_path(RUNTIME_CONFIG.application["tracker_file"])
DEFAULT_RESUME_FILE = resolve_runtime_path(RUNTIME_CONFIG.application["base_resume_file"])
DEFAULT_CONFIG_FILE = CONFIG_DIR / "candidate_profile_config.json"
DEFAULT_RESULTS_FILE = resolve_runtime_path(RUNTIME_CONFIG.application["results_file"])
DEFAULT_SUBMISSION_LOG_FILE = resolve_runtime_path(
    RUNTIME_CONFIG.application["submission_log_file"]
)
DEFAULT_EMAIL_POOL_FILE = resolve_runtime_path(
    RUNTIME_CONFIG.application["candidate_email_pool_file"]
)
DEFAULT_ENGINE_TIMEOUT_SECONDS = int(RUNTIME_CONFIG.application["engine_timeout_seconds"])
DEFAULT_RESUME_TIMEOUT_SECONDS = int(RUNTIME_CONFIG.application["resume_timeout_seconds"])
MIN_COVER_LETTER_BYTES = 1_000
SCREENSHOT_EXTENSIONS = {".jpeg", ".jpg", ".png"}

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


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessTimeoutError(TimeoutError):
    def __init__(self, timeout: int, stdout: str = "", stderr: str = "") -> None:
        super().__init__(f"Process exceeded {timeout} seconds")
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr


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
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
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


def _invalid_engine_result(detail: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "status": "INVALID_ENGINE_RESULT",
    }
    if detail:
        payload["detail"] = detail
    return payload


def parse_engine_result(result: ProcessResult, live_submit: bool) -> dict[str, Any]:
    """Parse and validate the structured result marker with a legacy fallback."""
    combined = f"{result.stdout}\n{result.stderr}"
    for line in reversed(combined.splitlines()):
        if line.startswith(ENGINE_RESULT_PREFIX):
            try:
                contract_result = EngineResult.from_wire_line(line)
            except ValueError as exc:
                return _invalid_engine_result(str(exc))
            payload = dict(contract_result.to_payload())
            # In live mode, a successful prefill is diagnostic progress, not a
            # successful application. The result contract makes that safety
            # distinction explicit while preserving the provider status text.
            if live_submit and not contract_result.is_confirmed_submission:
                payload["success"] = False
            return payload

    # No ENGINE_RESULT_JSON: marker (see emit_engine_result in
    # engine_shared.py) was found in the child's output, so
    # fall back to scraping the human-readable "Final Outcome ->" log line
    # that older/ad-hoc engine invocations may still print.
    final_outcome = ""
    for line in combined.splitlines():
        if "Final Outcome ->" in line:
            final_outcome = line.split("Final Outcome ->", 1)[1].strip()

    successful_statuses = {"PREFILLED_ONLY", "SUBMITTED & CONFIRMED"}
    success = result.returncode == 0 and final_outcome in successful_statuses
    # In live mode, only an actually confirmed submission counts as success;
    # a merely prefilled form must not be reported as a completed application.
    if live_submit and final_outcome != "SUBMITTED & CONFIRMED":
        success = False
    return {
        "success": success,
        "status": final_outcome or f"EXIT_{result.returncode}_NO_STRUCTURED_RESULT",
        "legacy_result": True,
    }


def _write_results(
    results_path: Path,
    results: Sequence[Mapping[str, Any]],
) -> None:
    """Atomically persist the complete result snapshot."""
    write_json_artifact(results_path, list(results), indent=2, ensure_ascii=False)


def _load_submission_log(path: Path) -> SubmissionLog:
    """Load an existing submission log, tolerating a missing or corrupt file."""
    log = SubmissionLog()
    if path.exists():
        try:
            log.load(path)
        except (OSError, ValueError) as exc:
            logger.warning("Could not load existing submission log %s: %s", path, exc)
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
) -> None:
    """Best-effort append to the submission log; never fails the orchestration run."""
    try:
        submission_log.record(
            SubmissionRecord(
                company=job["company"],
                role=job["role"],
                job_url=job["url"],
                ats=job["ats"],
                status=status,
                email_used=email,
                resume_filename=resume_path.name,
                cover_letter_filename=(
                    cover_letter_path.name if cover_letter_path is not None else ""
                ),
            )
        )
        submission_log.save(submission_log_path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not record submission log entry for %s: %s", job["url"], exc)


def _is_confirmed_submission(outcome: Mapping[str, object]) -> bool:
    """Accept only a validated, confirmed result for the submission log."""
    try:
        return EngineResult.from_payload(outcome).is_confirmed_submission
    except ValueError:
        return False


def cleanup_post_run_artifacts(results_path: Path) -> None:
    """Preserve results, screenshots, and submission proof for auditability."""
    logger.info(
        "Post-run artifacts preserved for diagnosis and confirmation; results=%s",
        results_path,
    )


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
            raise ValueError("Tracker path is required when --url is not provided")
        required_files.insert(0, ("Tracker", tracker_path))
    for label, path in required_files:
        if not path.is_file():
            raise FileNotFoundError(f"{label} file not found: {path}")
    if config_path and not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    if timeout_seconds <= 0:
        raise ValueError("Engine timeout must be greater than zero")
    if resume_timeout_seconds <= 0:
        raise ValueError("Resume timeout must be greater than zero")


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


def _job_result_base(job: JobRecord) -> dict[str, Any]:
    return {
        "row": job["row_number"],
        "company": job["company"],
        "role": job["role"],
        "url": job["url"],
        "ats": job["ats"],
    }


def _append_and_persist(
    results: list[dict[str, Any]],
    result: dict[str, Any],
    results_path: Path,
) -> None:
    results.append(result)
    _write_results(results_path, results)


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
        raise ValueError("Resume personalization is mandatory for every orchestrated application.")
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
        raise ValueError("A profile configuration is required.")
    if prepared_resume_path is not None and not direct_url:
        raise ValueError("--prepared-resume can only be used with --url")
    if cover_letter_path is not None and prepared_resume_path is None:
        raise ValueError("--cover-letter requires --prepared-resume")
    normalized_email_override = email_override.strip().lower()
    if normalized_email_override and (
        "@" not in normalized_email_override or normalized_email_override.startswith("@")
    ):
        raise ValueError("Email override must contain a local part and @")
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
    job_emails = _random_job_emails(
        jobs,
        email_override=normalized_email_override,
        email_pool_path=email_pool_path,
        prepared_resume_path=prepared_resume_path,
        fallback_email=fallback_email,
    )

    logger.info(
        "Loaded %d supported jobs | mode=%s | shuffle=%s",
        len(jobs),
        _mode_name(live_submit=live_submit, fill_only=fill_only),
        shuffle,
    )

    submission_log = _load_submission_log(submission_log_path)
    results: list[dict[str, Any]] = []
    for index, job in enumerate(jobs, start=1):
        email = job_emails[index - 1]
        ats = job["ats"]
        engine_path = engine_paths.get(ats)
        base_result = _job_result_base(job)
        previous_submissions = submission_log.find_by_job_url(job["url"]) if live_submit else {}
        if previous_submissions:
            latest = max(
                previous_submissions.values(),
                key=lambda entry: str(entry.get("applied_at", "")),
            )
            logger.info(
                "[%d/%d] row=%s ats=%s company=%s role=%s already confirmed; skipping",
                index,
                len(jobs),
                job["row_number"],
                ats,
                job["company"],
                job["role"],
            )
            _append_and_persist(
                results,
                {
                    **base_result,
                    "engine": _engine_label(engine_path, ats) if engine_path else "",
                    "resume": str(latest.get("resume_filename", "")),
                    "cover_letter": str(latest.get("cover_letter_filename", "")),
                    "email": _mask_email(str(latest.get("email_used", ""))),
                    "status": "ALREADY_SUBMITTED",
                    "success": True,
                    "submitted": False,
                    "confirmed": True,
                    "test_mode": False,
                    "already_submitted": True,
                },
                results_path,
            )
            continue
        if not engine_path or not engine_path.is_file():
            _append_and_persist(
                results,
                {**base_result, "status": "ENGINE_NOT_FOUND", "success": False},
                results_path,
            )
            continue
        engine_label = _engine_label(engine_path, ats)

        try:
            generated = prepared_resume_path or generate_personalized_resume(
                job["company"],
                job["role"],
                job["url"],
                resume_timeout_seconds,
                email,
                process_runner,
            )
        except Exception as exc:
            logger.error("Resume identity extraction failed for row %s: %s", job["row_number"], exc)
            _append_and_persist(
                results,
                {**base_result, "status": "RESUME_IDENTITY_EXTRACTION_FAILED", "success": False},
                results_path,
            )
            continue
        if not generated:
            logger.error(
                "Mandatory personalized resume generation failed for %s; "
                "submission will not be attempted.",
                job["url"],
            )
            _append_and_persist(
                results,
                {
                    **base_result,
                    "engine": engine_label,
                    "resume": "",
                    "email": _mask_email(email),
                    "confirmed": False,
                    "submitted": False,
                    "success": False,
                    "status": "PERSONALIZED_RESUME_FAILED",
                },
                results_path,
            )
            continue
        target_resume = generated
        try:
            resume_email = email_from_resume(target_resume, fallback_email).strip().lower()
            if resume_email != email:
                raise ValueError(
                    "Personalized resume email does not match the assigned application email"
                )
            current_title = current_title_from_resume(target_resume)
        except Exception as exc:
            logger.error(
                "Generated resume identity extraction failed for row %s: %s", job["row_number"], exc
            )
            _append_and_persist(
                results,
                {
                    **base_result,
                    "engine": engine_label,
                    "resume": target_resume.name,
                    "email": _mask_email(email),
                    "status": "GENERATED_RESUME_IDENTITY_INVALID",
                    "success": False,
                },
                results_path,
            )
            continue
        target_cover_letter = cover_letter_path or generate_personalized_cover_letter(
            job["company"],
            job["role"],
            job["url"],
            email,
            config_path,
            resume_timeout_seconds,
            process_runner,
        )
        if not target_cover_letter:
            logger.error(
                "Mandatory personalized cover-letter generation failed for %s; "
                "submission will not be attempted.",
                job["url"],
            )
            _append_and_persist(
                results,
                {
                    **base_result,
                    "engine": engine_label,
                    "resume": target_resume.name,
                    "cover_letter": "",
                    "email": _mask_email(email),
                    "confirmed": False,
                    "submitted": False,
                    "success": False,
                    "status": "PERSONALIZED_COVER_LETTER_FAILED",
                },
                results_path,
            )
            continue
        logger.info(
            "[%d/%d] row=%s ats=%s company=%s role=%s email=%s",
            index,
            len(jobs),
            job["row_number"],
            ats,
            job["company"],
            job["role"],
            _mask_email(email),
        )

        command = build_engine_command(
            engine_path,
            job["url"],
            target_resume,
            job["company"],
            job["role"],
            email,
            live_submit,
            cover_letter_path=target_cover_letter,
            headed=headed,
            fill_only=fill_only,
            dry_run=dry_run,
        )

        try:
            engine_env = dict(os.environ)
            # Required so the engine's require_orchestrated_invocation() guard
            # (engine_shared.py) lets the run through; engines
            # refuse to process a job URL unless launched via this orchestrator.
            engine_env[ORCHESTRATOR_INVOCATION_ENV] = "1"
            if config_path is None:
                raise RuntimeError("A profile configuration is required for engine execution.")
            engine_env[ORCHESTRATOR_CONFIG_ENV] = str(config_path)
            engine_env[ORCHESTRATOR_CURRENT_TITLE_ENV] = current_title
            command_result = (process_runner or SubprocessRunner()).run(
                command,
                ProcessSettings(
                    timeout_seconds=timeout_seconds,
                    environment=engine_env,
                ),
            )
            process_result = ProcessResult(
                command_result.returncode,
                command_result.stdout,
                command_result.stderr,
            )
            outcome = parse_engine_result(process_result, live_submit)
            if not outcome.get("success"):
                logger.error(
                    "Engine diagnostics:\n%s",
                    (process_result.stdout + "\n" + process_result.stderr)[-8000:],
                )
        except ProcessTimeoutError as exc:
            outcome = {
                "success": False,
                "status": "TIMED_OUT",
                "timeout_seconds": exc.timeout,
            }
        except Exception as exc:
            logger.error("Engine execution failed: %s", exc)
            outcome = {"success": False, "status": "ENGINE_EXECUTION_ERROR", "detail": str(exc)}

        if _is_confirmed_submission(outcome):
            _record_submission(
                submission_log,
                submission_log_path,
                job=job,
                email=email,
                resume_path=target_resume,
                cover_letter_path=target_cover_letter,
                status=str(outcome["status"]),
            )
        _append_and_persist(
            results,
            {
                **base_result,
                "engine": engine_label,
                "resume": target_resume.name,
                "cover_letter": target_cover_letter.name,
                "email": _mask_email(email),
                **outcome,
            },
            results_path,
        )

    successful = sum(1 for result in results if result.get("success"))
    logger.info(
        "Orchestration complete: processed=%d successful=%d failed=%d results=%s",
        len(results),
        successful,
        len(results) - successful,
        results_path,
    )
    return results


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
    finally:
        cleanup_post_run_artifacts(results_path)
    # Exit codes: 0 = every job succeeded, 1 = ran but at least one job
    # failed, 2 = orchestration could not even start (see except above).
    return 0 if all(result.get("success") for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
