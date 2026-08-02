"""Typed application service shared by continuous ATS worker entrypoints."""

from __future__ import annotations

import hashlib
import os
import random
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from ..mail.pool import load_email_pool
from ..search.backlog import remove_confirmed_job
from .application_candidates import application_url
from .artifacts import atomic_write_text, read_json
from .contracts import EngineResult
from .continuous_worker_candidates import (
    JobIdentity,
    RESUMABLE_STATUSES,
    load_exact_confirmed_ledger_index,
)
from .continuous_worker_models import CommandOutcome, CycleStatus
from .continuous_worker_runtime import WorkerTelemetry
from .continuous_worker_state import load_worker_state, save_worker_state, utc_now_iso
from .identity import canonical_job_url, normalize_email
from .observability import NOOP_TELEMETRY
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from .screenshots import (
    APPLICATION_SCREENSHOT_DIR_ENV,
    cleanup_application_screenshot_directory,
    create_application_screenshot_directory,
)


SHARED_INPUT = resolve_runtime_path("output/vps_generation_jobs.json")
DEFAULT_INPUT = SHARED_INPUT
DEFAULT_SUBMISSION_LOG = resolve_runtime_path(RUNTIME_CONFIG.application.submission_log_file)
DEFAULT_BACKLOG = resolve_runtime_path(RUNTIME_CONFIG.application.vps_job_backlog_file)
DEFAULT_PROFILE = resolve_runtime_path("config/candidate_profile_config.json")
DEFAULT_EMAIL_POOL = resolve_runtime_path(RUNTIME_CONFIG.application.candidate_email_pool_file)
DEFAULT_LAUNCHER = resolve_runtime_path("src/job_automation.py")
AMBIGUOUS_SUBMISSION_STATUSES = frozenset({"SUBMIT_ATTEMPT_UNCONFIRMED", "SUBMISSION_UNCONFIRMED"})
ENGINE_CONFIRMATION_FIELDS = (
    "success",
    "status",
    "ats",
    "submitted",
    "confirmed",
    "test_mode",
    "error",
    "detail",
)


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        timeout_seconds: int,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> CommandOutcome: ...


class DocumentPreparer(Protocol):
    def __call__(
        self,
        *,
        job: Mapping[str, Any],
        ats_platform: str,
        email: str,
        launcher: Path,
        profile: Path,
        output_dir: Path,
        timeout_seconds: int,
        generate_cover_letter: bool = True,
    ) -> CommandOutcome: ...


class ApplicationRunner(Protocol):
    def __call__(
        self,
        *,
        job: Mapping[str, Any],
        email: str,
        launcher: Path,
        profile: Path,
        resume_path: Path,
        cover_letter_path: Path | None,
        result_path: Path,
        submission_log: Path,
        screenshot_dir: Path,
        engine_timeout_seconds: int,
        process_timeout_seconds: int,
    ) -> CommandOutcome: ...


class ScreenshotDirectoryCreator(Protocol):
    def __call__(self, *, output_root: str | Path) -> Path: ...


class ScreenshotDirectoryCleaner(Protocol):
    def __call__(
        self,
        directory: str | Path,
        *,
        output_root: str | Path,
    ) -> tuple[int, int]: ...


def run_command(
    command: list[str],
    timeout_seconds: int,
    *,
    environment: Mapping[str, str] | None = None,
) -> CommandOutcome:
    """Run a child with bounded lifetime and descendant cleanup on both platforms."""
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=os.name != "nt",
            env={**os.environ, **environment} if environment is not None else None,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return CommandOutcome(process.returncode, stdout or "", stderr or "")
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                )
            else:
                kill_process_group = cast(
                    Callable[[int, int], None],
                    vars(os)["killpg"],
                )
                try:
                    kill_process_group(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    kill_process_group(process.pid, cast(int, vars(signal)["SIGKILL"]))
                except ProcessLookupError:
                    pass
            stdout, stderr = process.communicate()
            return CommandOutcome(124, stdout or "", stderr or "", timed_out=True)
    except OSError as exc:
        return CommandOutcome(127, "", str(exc))


def masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    visible = local[:1] if local else ""
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def job_digest(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]


def valid_pdf(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 1000 and path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def read_application_result(path: Path) -> dict[str, Any]:
    try:
        payload: object = read_json(path)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return {}
    return payload[0]


def engine_confirmation_view(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return only fields owned by the engine wire contract from a pipeline snapshot."""
    return {key: result[key] for key in ENGINE_CONFIRMATION_FIELDS if key in result}


def strictly_confirmed(result: Mapping[str, Any]) -> bool:
    """Validate confirmation from only the engine wire view of a pipeline snapshot."""
    try:
        return bool(
            EngineResult.from_payload(engine_confirmation_view(result)).is_confirmed_submission
        )
    except ValueError:
        return False


def requires_manual_review(result: Mapping[str, Any], outcome: CommandOutcome) -> bool:
    """Quarantine every possibly submitted or explicitly challenged attempt."""
    return (
        bool(result.get("submitted"))
        or result.get("captcha_present") is True
        or outcome.timed_out
        or str(result.get("status", "")) in AMBIGUOUS_SUBMISSION_STATUSES
    )


def outcome_diagnostics(outcome: CommandOutcome) -> dict[str, Any]:
    return {
        "exit_code": outcome.return_code,
        "timed_out": outcome.timed_out,
        "stdout_tail": outcome.stdout[-2000:],
        "stderr_tail": outcome.stderr[-2000:],
    }


def prepare_documents(
    *,
    job: Mapping[str, Any],
    ats_platform: str,
    email: str,
    launcher: Path,
    profile: Path,
    output_dir: Path,
    timeout_seconds: int,
    generate_cover_letter: bool = True,
    runner: CommandRunner = run_command,
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
        command = [sys.executable, str(launcher)]
        if generate_cover_letter:
            command += ["documents", "generate"]
        else:
            command += ["resume"]
        command += [
            "--url",
            application_url(job),
            "--company",
            str(job["company"]).strip(),
            "--role",
            str(job["title"]).strip(),
            "--email",
            email,
            "--location",
            str(job.get("location", "")).strip(),
        ]
        if generate_cover_letter:
            command += [
                "--jd-file",
                str(job_description_path),
                "--profile",
                str(profile),
                "--output-dir",
                str(output_dir),
                "--overwrite",
            ]
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            command += [
                "--jd-overview",
                str(job["description"]).strip(),
                "--output",
                str(output_dir / "resume.pdf"),
            ]
        return runner(command, timeout_seconds)
    finally:
        job_description_path.unlink(missing_ok=True)


def apply_job(
    *,
    job: Mapping[str, Any],
    email: str,
    launcher: Path,
    profile: Path,
    resume_path: Path,
    cover_letter_path: Path | None,
    result_path: Path,
    submission_log: Path,
    screenshot_dir: Path,
    engine_timeout_seconds: int,
    process_timeout_seconds: int,
    runner: CommandRunner = run_command,
) -> CommandOutcome:
    command = [
        sys.executable,
        str(launcher),
        "apply",
        "--url",
        application_url(job),
        "--company",
        str(job["company"]).strip(),
        "--role",
        str(job["title"]).strip(),
        "--config",
        str(profile),
        "--prepared-resume",
        str(resume_path),
        "--email",
        email,
        "--headed",
        "--results-file",
        str(result_path),
        "--submission-log-file",
        str(submission_log),
        "--live-submit",
        "--no-shuffle",
        "--timeout",
        str(engine_timeout_seconds),
    ]
    if cover_letter_path is not None:
        command[command.index("--email"):command.index("--email")] = [
            "--cover-letter",
            str(cover_letter_path),
        ]
    return runner(
        command,
        process_timeout_seconds,
        environment={APPLICATION_SCREENSHOT_DIR_ENV: str(screenshot_dir)},
    )


@dataclass(frozen=True, slots=True)
class SelectedJobApplicationConfig:
    """Stable paths and timeouts required to apply one already-selected job."""

    ats_platform: str
    profile: Path
    email_pool: Path
    launcher: Path
    state_path: Path
    results_dir: Path
    documents_dir: Path
    submission_log: Path
    document_timeout_seconds: int
    engine_timeout_seconds: int
    application_timeout_seconds: int
    generate_cover_letter: bool = True
    backlog_path: Path | None = None
    ledger_identity_for_url: JobIdentity = canonical_job_url


@dataclass(frozen=True, slots=True)
class SelectedJobApplicationDependencies:
    """Patchable side effects used by the selected-job application service."""

    prepare_documents: DocumentPreparer
    apply_job: ApplicationRunner
    load_email_pool: Callable[[Path], list[str]]
    choose_email: Callable[[Sequence[str]], str]
    now: Callable[[], str]
    create_screenshot_directory: ScreenshotDirectoryCreator
    cleanup_screenshot_directory: ScreenshotDirectoryCleaner
    prune_backlog: Callable[[Path, str], bool]


def default_application_dependencies() -> SelectedJobApplicationDependencies:
    return SelectedJobApplicationDependencies(
        prepare_documents=prepare_documents,
        apply_job=apply_job,
        load_email_pool=load_email_pool,
        choose_email=random.choice,
        now=utc_now_iso,
        create_screenshot_directory=create_application_screenshot_directory,
        cleanup_screenshot_directory=cleanup_application_screenshot_directory,
        prune_backlog=remove_confirmed_job,
    )


@dataclass(frozen=True, slots=True)
class SelectedJobApplicationService:
    """Prepare and apply one selected candidate without loading or selecting another."""

    config: SelectedJobApplicationConfig
    telemetry: WorkerTelemetry = NOOP_TELEMETRY
    dependencies: SelectedJobApplicationDependencies = field(
        default_factory=default_application_dependencies
    )

    def process(self, job: Mapping[str, Any]) -> CycleStatus:
        config = self.config
        dependencies = self.dependencies
        application_job_url = application_url(job)
        canonical_url = canonical_job_url(application_job_url)
        candidate_identity = config.ledger_identity_for_url(application_job_url)
        confirmed_index = load_exact_confirmed_ledger_index(
            config.submission_log,
            config.ats_platform,
            identity_for_url=config.ledger_identity_for_url,
        )
        if confirmed_index.contains(candidate_identity):
            return "no_work"

        state = load_worker_state(config.state_path, config.ats_platform)
        records = state["jobs"]
        record = records.get(canonical_url)
        if isinstance(record, dict) and record.get("status") not in RESUMABLE_STATUSES:
            return "no_work"
        if not isinstance(record, dict):
            email = normalize_email(
                dependencies.choose_email(dependencies.load_email_pool(config.email_pool))
            )
            digest = job_digest(canonical_url)
            record = {
                "status": "preparing",
                "stage": "documents",
                "job_url": application_job_url,
                "company": str(job["company"]).strip(),
                "title": str(job["title"]).strip(),
                "platform": config.ats_platform,
                "email": email,
                "document_dir": str(config.documents_dir / digest),
                "result_path": str(config.results_dir / f"application_{digest}.json"),
                "started_at": dependencies.now(),
                "updated_at": dependencies.now(),
            }
            records[canonical_url] = record
            save_worker_state(config.state_path, state)
        else:
            email = normalize_email(record.get("email", ""))

        print(
            f"{config.ats_platform.upper()}_CYCLE_START "
            f"company={record['company']!r} role={record['title']!r} "
            f"email={masked_email(email)}",
            flush=True,
        )

        output_dir = Path(str(record["document_dir"]))
        resume_path = output_dir / "resume.pdf"
        cover_letter_path = output_dir / "cover_letter.pdf"
        result_path = Path(str(record["result_path"]))

        if record["status"] == "preparing":
            document_arguments = {
                "job": job,
                "ats_platform": config.ats_platform,
                "email": email,
                "launcher": config.launcher,
                "profile": config.profile,
                "output_dir": output_dir,
                "timeout_seconds": config.document_timeout_seconds,
            }
            if not config.generate_cover_letter:
                document_arguments["generate_cover_letter"] = False
            document_outcome = dependencies.prepare_documents(
                **document_arguments,
            )
            if (
                document_outcome.return_code != 0
                or not valid_pdf(resume_path)
                or (config.generate_cover_letter and not valid_pdf(cover_letter_path))
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
                        "resume_valid": valid_pdf(resume_path),
                        "cover_letter_valid": (
                            valid_pdf(cover_letter_path) if config.generate_cover_letter else None
                        ),
                        "updated_at": dependencies.now(),
                        **outcome_diagnostics(document_outcome),
                    }
                )
                save_worker_state(config.state_path, state)
                print(
                    f"{config.ats_platform.upper()}_CYCLE_FAILED stage=documents "
                    f"url_digest={job_digest(canonical_url)} "
                    f"status={record['result_status']}",
                    flush=True,
                )
                self.telemetry.emit(
                    "document_generation_failed",
                    provider=config.ats_platform,
                    stage="documents",
                    cycle_status=str(record["result_status"]),
                    exit_code=document_outcome.return_code,
                    timed_out=document_outcome.timed_out,
                )
                return "failed"
            record.update(
                {
                    "status": "documents_ready",
                    "stage": "application",
                    "resume_filename": resume_path.name,
                    "cover_letter_filename": (
                        cover_letter_path.name if config.generate_cover_letter else ""
                    ),
                    "updated_at": dependencies.now(),
                    **outcome_diagnostics(document_outcome),
                }
            )
            save_worker_state(config.state_path, state)

        config.results_dir.mkdir(parents=True, exist_ok=True)
        config.submission_log.parent.mkdir(parents=True, exist_ok=True)
        if not config.submission_log.exists():
            atomic_write_text(config.submission_log, "{}\n", encoding="utf-8")
        result_path.unlink(missing_ok=True)
        record.update(
            {
                "status": "application_started",
                "stage": "application",
                "application_started_at": dependencies.now(),
                "updated_at": dependencies.now(),
            }
        )
        save_worker_state(config.state_path, state)

        screenshot_root = config.results_dir.parent
        screenshot_dir = dependencies.create_screenshot_directory(output_root=screenshot_root)
        try:
            application_outcome = dependencies.apply_job(
                job=job,
                email=email,
                launcher=config.launcher,
                profile=config.profile,
                resume_path=resume_path,
                cover_letter_path=(cover_letter_path if config.generate_cover_letter else None),
                result_path=result_path,
                submission_log=config.submission_log,
                screenshot_dir=screenshot_dir,
                engine_timeout_seconds=config.engine_timeout_seconds,
                process_timeout_seconds=config.application_timeout_seconds,
            )
        finally:
            try:
                files_deleted, bytes_deleted = dependencies.cleanup_screenshot_directory(
                    screenshot_dir,
                    output_root=screenshot_root,
                )
                print(
                    f"{config.ats_platform.upper()}_SCREENSHOTS_CLEANED "
                    f"files={files_deleted} bytes={bytes_deleted}",
                    flush=True,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"{config.ats_platform.upper()}_SCREENSHOT_CLEANUP_FAILED error={exc}",
                    file=sys.stderr,
                    flush=True,
                )

        result = read_application_result(result_path)
        confirmed_index = load_exact_confirmed_ledger_index(
            config.submission_log,
            config.ats_platform,
            identity_for_url=config.ledger_identity_for_url,
        )
        ledger_confirmed = confirmed_index.contains(candidate_identity)
        engine_confirmed = strictly_confirmed(result)
        confirmed = application_outcome.return_code == 0 and engine_confirmed and ledger_confirmed
        result_status = str(result.get("status", "NO_RESULT"))
        if result_status in {"BROWSER_SESSION_FAILED", "ENGINE_EXECUTION_ERROR"}:
            self.telemetry.emit(
                "browser_session_failed",
                provider=config.ats_platform,
                stage="application",
                cycle_status=result_status,
                exit_code=application_outcome.return_code,
                timed_out=application_outcome.timed_out,
            )
        if engine_confirmed and not ledger_confirmed:
            self.telemetry.emit(
                "ledger_persist_failed",
                provider=config.ats_platform,
                stage="submission_ledger",
                cycle_status="confirmed_without_ledger",
                exit_code=application_outcome.return_code,
                timed_out=application_outcome.timed_out,
            )
        manual_review_required = requires_manual_review(result, application_outcome)
        status: CycleStatus = (
            "confirmed" if confirmed else ("manual_review" if manual_review_required else "failed")
        )
        record.update(
            {
                "status": status,
                "stage": "application",
                "result_status": result_status,
                "ledger_confirmed": ledger_confirmed,
                "result": result,
                "updated_at": dependencies.now(),
                **outcome_diagnostics(application_outcome),
            }
        )
        save_worker_state(config.state_path, state)
        if confirmed:
            if config.backlog_path is not None:
                try:
                    dependencies.prune_backlog(config.backlog_path, canonical_url)
                except (OSError, TimeoutError, ValueError) as exc:
                    print(
                        f"{config.ats_platform.upper()}_BACKLOG_PRUNE_DEFERRED "
                        f"url_digest={job_digest(canonical_url)} error={exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            print(
                f"{config.ats_platform.upper()}_CYCLE_CONFIRMED "
                f"url_digest={job_digest(canonical_url)} status=SUBMITTED_AND_CONFIRMED",
                flush=True,
            )
            return "confirmed"
        print(
            f"{config.ats_platform.upper()}_CYCLE_FAILED "
            f"stage=application url_digest={job_digest(canonical_url)} "
            f"status={record['result_status']} disposition={status}",
            flush=True,
        )
        return status
