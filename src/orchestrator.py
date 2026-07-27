#!/usr/bin/env python3
"""
ATS-aware job application orchestrator.

Reads jobs from an Excel tracker, detects the ATS from each URL, selects the
matching engine, optionally generates a URL-specific resume, and persists a
structured result after every job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, TypedDict
from urllib.parse import unquote, urlparse

import openpyxl

from engine_shared import (
    ATS_HOST_MARKERS as ATS_HOSTS,
    ORCHESTRATOR_INVOCATION_ENV,
    ORCHESTRATOR_CONFIG_ENV,
    ORCHESTRATOR_CURRENT_TITLE_ENV,
    RESULT_PREFIX as ENGINE_RESULT_PREFIX,
    email_from_resume,
    current_title_from_resume,
    load_json_config,
    mask_email as _mask_email,
    validate_ats_url,
)
from paths import CONFIG_DIR, DATA_DIR, OUTPUT_DIR, SRC_DIR, resolve_existing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ATSOrchestrator")

SCRIPT_DIR = SRC_DIR

DEFAULT_TRACKER_FILE = DATA_DIR / "ai_product_manager_job_tracker.xlsx"
DEFAULT_RESUME_FILE = DATA_DIR / "shivam_singh_ai_product_manager_resume.pdf"
DEFAULT_CONFIG_FILE = CONFIG_DIR / "candidate_profile_config.json"
DEFAULT_RESULTS_FILE = OUTPUT_DIR / "orchestration_results.json"
SCREENSHOT_EXTENSIONS = {".jpeg", ".jpg", ".png"}

SUPPORTED_ATS = tuple(ATS_HOSTS)

DEFAULT_ENGINE_FILES: Mapping[str, Path] = {
    "ashby": SCRIPT_DIR / "engine_ashby.py",
    "greenhouse": SCRIPT_DIR / "engine_greenhouse.py",
    "lever": SCRIPT_DIR / "engine_lever.py",
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


def detect_ats(url: str) -> Optional[str]:
    """Return the supported ATS for an HTTPS URL, or None."""
    if not isinstance(url, str) or not url.strip():
        return None
    for ats in ATS_HOSTS:
        if validate_ats_url(url, ats):
            return ats
    return None


def _find_header(headers: Sequence[str], aliases: tuple[str, ...], label: str) -> int:
    for index, header in enumerate(headers):
        if header in aliases:
            return index
    for index, header in enumerate(headers):
        if any(alias in header for alias in aliases):
            return index
    raise ValueError(f"Tracker is missing a recognizable {label} column; found headers: {headers}")


def load_jobs_from_tracker(tracker_path: Path) -> list[JobRecord]:
    """Load validated Ashby, Greenhouse, and Lever entries from the active sheet."""
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
            jobs.append(
                {
                    "row_number": row_number,
                    "company": str(row[company_index]).strip() if row[company_index] else "Company",
                    "role": str(row[role_index]).strip() if row[role_index] else "Product Manager",
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
        raise ValueError(
            "URL must be a supported HTTPS Ashby, Greenhouse, or Lever job URL"
        )
    path_parts = [
        unquote(part).strip()
        for part in urlparse(url).path.split("/")
        if part.strip()
    ]
    inferred_company = path_parts[0].replace("-", " ").strip().title() if path_parts else "Company"
    return {
        "row_number": 1,
        "company": company.strip() or inferred_company,
        "role": role.strip() or "Product Manager",
        "url": url.strip(),
        "ats": ats,
    }


def resolve_engine_path(raw_path: Path) -> Path:
    """Resolve an engine path relative to the source directory."""
    return resolve_existing(raw_path, SRC_DIR).resolve()


def _engine_mode_flag(*, live_submit: bool, fill_only: bool, dry_run: bool) -> str:
    """Return the engine mode flag using the established precedence."""
    if fill_only:
        return "--fill-only"
    if live_submit:
        return "--live-submit"
    if dry_run:
        return "--dry-run"
    return "--dry-run"


def build_engine_command(
    engine_path: Path,
    url: str,
    resume_path: Path,
    company: str,
    role: str,
    email: str,
    live_submit: bool,
    headed: bool = False,
    fill_only: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """Construct the standardized engine CLI invocation."""
    cmd = [
        sys.executable,
        str(engine_path),
        "--url", url,
        "--resume", str(resume_path),
        "--company", company,
        "--role", role,
        "--email", email,
    ]
    cmd.append(
        _engine_mode_flag(
            live_submit=live_submit,
            fill_only=fill_only,
            dry_run=dry_run,
        )
    )

    if headed:
        cmd.append("--headed")
    return cmd


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
    env: Optional[Mapping[str, str]] = None,
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
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )
    process = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        env=dict(env) if env is not None else None,
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
        )


def _invalid_engine_result(detail: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "status": "INVALID_ENGINE_RESULT",
    }
    if detail:
        payload["detail"] = detail
    return payload


def parse_engine_result(result: ProcessResult, live_submit: bool) -> dict[str, Any]:
    """Parse the structured result marker, with a narrow legacy fallback."""
    combined = f"{result.stdout}\n{result.stderr}"
    for line in reversed(combined.splitlines()):
        if line.startswith(ENGINE_RESULT_PREFIX):
            try:
                payload = json.loads(line[len(ENGINE_RESULT_PREFIX):])
            except json.JSONDecodeError as exc:
                return _invalid_engine_result(str(exc))
            if not isinstance(payload, dict):
                return _invalid_engine_result()
            payload.setdefault("success", False)
            payload.setdefault("status", "UNKNOWN")
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
    results_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = results_path.with_suffix(results_path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(list(results), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, results_path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                logger.warning("Could not remove temporary results file: %s", temporary)


def cleanup_post_run_artifacts(results_path: Path) -> None:
    """Preserve results, screenshots, and submission proof for auditability."""
    logger.info(
        "Post-run artifacts preserved for diagnosis and confirmation; results=%s",
        results_path,
    )


def _personalized_resume_path(company: str, role: str, url: str) -> Path:
    safe_company = "".join(character if character.isalnum() else "_" for character in company).strip("_")
    safe_role = "".join(character if character.isalnum() else "_" for character in role).strip("_")
    # Deterministic hash of the URL keeps the filename stable across retries
    # of the same posting, which is what lets generate_personalized_resume()
    # detect and reuse an already-generated resume instead of regenerating it.
    posting_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return OUTPUT_DIR / f"{safe_company}_{safe_role}_{posting_hash}_Resume.pdf"


def generate_personalized_resume(
    company: str,
    role: str,
    url: str,
    timeout_seconds: int,
    email: str = "",
) -> Optional[Path]:
    generator = SCRIPT_DIR / "resume_generate.py"
    if not generator.exists():
        logger.warning("Resume generator not found: %s", generator)
        return None

    output_path = _personalized_resume_path(company, role, url)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # The 5000-byte floor is a cheap sanity check that the existing file is a
    # real rendered PDF rather than a truncated or failed prior write.
    if (
        os.environ.get("JOB_APP_FORCE_RESUME_REGENERATION") != "1"
        and output_path.exists()
        and output_path.stat().st_size > 5000
    ):
        logger.info(
            "Reusing existing position-specific resume for retry: %s",
            output_path.name,
        )
        return output_path
    command = [
        sys.executable,
        str(generator),
        "--company", company,
        "--role", role,
        "--url", url,
        "--output", str(output_path),
    ]
    if email:
        command.extend(["--email", email])
    # Track mtime rather than trusting returncode alone: the generator could
    # exit 0 without actually rewriting output_path, which would silently
    # leave a stale or undersized file behind and must be treated as failure.
    before_mtime = output_path.stat().st_mtime_ns if output_path.exists() else None
    try:
        result = run_command(command, timeout_seconds)
    except ProcessTimeoutError:
        logger.warning("Resume generation timed out after %d seconds.", timeout_seconds)
        return None
    except OSError as exc:
        logger.warning("Could not start resume generator: %s", exc)
        return None

    after_mtime = output_path.stat().st_mtime_ns if output_path.exists() else None
    changed = before_mtime is None or after_mtime != before_mtime
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 5000 or not changed:
        logger.warning(
            "Resume generation failed (exit=%d): %s",
            result.returncode,
            (result.stderr or result.stdout)[-300:],
        )
        return None
    return output_path


def _validate_orchestrator_inputs(
    *,
    tracker_path: Optional[Path],
    require_tracker: bool,
    resume_path: Path,
    config_path: Optional[Path],
    timeout_seconds: int,
    resume_timeout_seconds: int,
) -> None:
    required_files = [
        ("Resume", resume_path),
    ]
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
    limit: Optional[int],
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
    tracker_path: Optional[Path],
    resume_path: Path,
    config_path: Optional[Path],
    results_path: Path,
    limit: Optional[int] = None,
    start_index: int = 0,
    live_submit: bool = False,
    fill_only: bool = False,
    dry_run: bool = False,
    shuffle: bool = True,
    headed: bool = False,
    timeout_seconds: int = 180,
    personalize_resume: bool = True,
    resume_timeout_seconds: int = 600,
    direct_url: Optional[str] = None,
    direct_company: str = "",
    direct_role: str = "",
) -> list[dict[str, Any]]:
    """Run the ATS-aware application loop and persist progress."""
    if not personalize_resume:
        raise ValueError(
            "Resume personalization is mandatory for every orchestrated application."
        )
    _validate_orchestrator_inputs(
        tracker_path=tracker_path,
        require_tracker=not bool(direct_url),
        resume_path=resume_path,
        config_path=config_path,
        timeout_seconds=timeout_seconds,
        resume_timeout_seconds=resume_timeout_seconds,
    )
    if config_path is None:
        raise ValueError("A profile configuration is required.")
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

    results: list[dict[str, Any]] = []
    for index, job in enumerate(jobs, start=1):
        ats = job["ats"]
        engine_path = engine_paths.get(ats)
        base_result = _job_result_base(job)
        if not engine_path or not engine_path.is_file():
            _append_and_persist(
                results,
                {**base_result, "status": "ENGINE_NOT_FOUND", "success": False},
                results_path,
            )
            continue

        try:
            email = email_from_resume(resume_path, fallback_email)
            generated = generate_personalized_resume(
            job["company"],
            job["role"],
            job["url"],
            resume_timeout_seconds,
            email,
            )
        except Exception as exc:
            logger.error("Resume identity extraction failed for row %s: %s", job["row_number"], exc)
            _append_and_persist(results, {**base_result, "status": "RESUME_IDENTITY_EXTRACTION_FAILED", "success": False}, results_path)
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
                    "engine": engine_path.name,
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
            email = email_from_resume(target_resume, fallback_email)
            current_title = current_title_from_resume(target_resume)
        except Exception as exc:
            logger.error("Generated resume identity extraction failed for row %s: %s", job["row_number"], exc)
            _append_and_persist(results, {**base_result, "engine": engine_path.name, "resume": target_resume.name, "email": _mask_email(email), "status": "GENERATED_RESUME_IDENTITY_INVALID", "success": False}, results_path)
            continue
        logger.info(
            "[%d/%d] row=%s ats=%s company=%s role=%s email=%s",
            index, len(jobs), job["row_number"], ats, job["company"], job["role"], _mask_email(email),
        )

        command = build_engine_command(
            engine_path,
            job["url"],
            target_resume,
            job["company"],
            job["role"],
            email,
            live_submit,
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
            process_result = run_command(
                command,
                timeout_seconds,
                env=engine_env,
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

        _append_and_persist(
            results,
            {
                **base_result,
                "engine": engine_path.name,
                "resume": target_resume.name,
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
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE))
    parser.add_argument("--results-file", default=str(DEFAULT_RESULTS_FILE))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--resume-timeout", type=int, default=600)
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

    parser.add_argument("--engine", default=None, help="Deprecated alias for --ashby-engine")
    parser.add_argument("--ashby-engine", default=None)
    parser.add_argument("--greenhouse-engine", default=None)
    parser.add_argument("--lever-engine", default=None)
    return parser


def _resolve_engine_paths(args: argparse.Namespace) -> dict[str, Path]:
    raw_engines: Mapping[str, str | Path] = {
        "ashby": args.ashby_engine or args.engine or DEFAULT_ENGINE_FILES["ashby"],
        "greenhouse": args.greenhouse_engine or DEFAULT_ENGINE_FILES["greenhouse"],
        "lever": args.lever_engine or DEFAULT_ENGINE_FILES["lever"],
    }
    return {
        ats: resolve_engine_path(Path(path))
        for ats, path in raw_engines.items()
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    results_path = Path(args.results_file).resolve()
    try:
        results = run_orchestrator(
            engine_paths=_resolve_engine_paths(args),
            tracker_path=Path(args.tracker).resolve() if args.tracker else None,
            resume_path=Path(args.resume).resolve(),
            config_path=Path(args.config).resolve() if args.config else None,
            results_path=results_path,
            limit=args.limit,
            start_index=args.start_index,
            live_submit=args.live_submit,
            fill_only=args.fill_only,
            # Defaults to dry-run whenever --live-submit was not explicitly
            # requested, even if --dry-run itself was omitted, so a bare
            # invocation can never submit an application by accident.
            dry_run=args.dry_run or not args.live_submit,
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
