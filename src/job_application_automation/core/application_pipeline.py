"""Typed stages for one ATS-aware application run.

The public orchestration function remains in :mod:`orchestrator`.  This module
owns the per-application state transitions so each terminal outcome can be
tested without invoking document generators, browsers, or artifact writers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping, Sequence

from .adapters import CommandResult, ProcessSettings
from .contracts import EngineMode, EngineResult, EngineStatus
from .engine_shared import (
    ORCHESTRATOR_CONFIG_ENV,
    ORCHESTRATOR_CURRENT_TITLE_ENV,
    ORCHESTRATOR_INVOCATION_ENV,
)
from .screenshots import APPLICATION_SCREENSHOT_DIR_ENV
from .submission_log import SubmissionLog

logger = logging.getLogger("ATSOrchestrator")

LEDGER_PERSIST_FAILED_STATUS = "LEDGER_PERSIST_FAILED"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured process output consumed by the engine-result parser."""

    returncode: int
    stdout: str
    stderr: str


class ProcessTimeoutError(TimeoutError):
    """A bounded child process exceeded its configured lifetime."""

    def __init__(self, timeout: int, stdout: str = "", stderr: str = "") -> None:
        super().__init__(f"Process exceeded {timeout} seconds")
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class SubmissionPersistence:
    """Outcome of recording a confirmed application in the permanent ledger."""

    persisted: bool
    error: str = ""
    quarantine_path: Path | None = None
    quarantine_error: str = ""


@dataclass(frozen=True, slots=True)
class ApplicationTarget:
    """Validated job identity shared by every application stage."""

    row_number: int
    company: str
    role: str
    url: str
    ats: str

    def base_result(self) -> dict[str, Any]:
        """Return the established common orchestration-result fields."""
        return {
            "row": self.row_number,
            "company": self.company,
            "role": self.role,
            "url": self.url,
            "ats": self.ats,
        }


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Validated run-level settings used by the application stages."""

    engine_paths: Mapping[str, Path]
    results_path: Path
    submission_log_path: Path
    submission_quarantine_path: Path
    config_path: Path
    prepared_resume_path: Path | None
    prepared_cover_letter_path: Path | None
    mode: EngineMode
    headed: bool
    timeout_seconds: int
    fallback_email: str

    @property
    def live_submit(self) -> bool:
        """Derive ledger policy from the single authoritative engine mode."""
        return self.mode is EngineMode.LIVE_SUBMIT


@dataclass(frozen=True, slots=True)
class EngineOutcome:
    """Typed engine result retained until the application checkpoint boundary."""

    success: bool
    status: str
    ats: str = ""
    submitted: bool | None = None
    confirmed: bool | None = None
    test_mode: bool | None = None
    error: str = ""
    detail: str = ""
    engine_details: Mapping[str, object] = field(default_factory=dict)
    legacy_result: bool | None = None
    timeout_seconds: int | None = None

    @classmethod
    def from_engine_result(
        cls,
        result: EngineResult,
        *,
        expected_ats: str,
        live_submit: bool,
    ) -> EngineOutcome:
        """Validate provider identity and retain provider extras under one namespace."""
        if result.ats != expected_ats.lower():
            return cls(
                success=False,
                status=EngineStatus.INVALID_ENGINE_RESULT.value,
                detail=(
                    f"engine result ATS {result.ats!r} does not match "
                    f"application target {expected_ats.lower()!r}"
                ),
            )
        success = result.success
        if live_submit and not result.is_confirmed_submission:
            success = False
        return cls(
            success=success,
            status=result.status,
            ats=result.ats,
            submitted=result.submitted,
            confirmed=result.confirmed,
            test_mode=result.test_mode,
            error=result.error,
            detail=result.detail,
            engine_details=result.extra,
        )

    @property
    def is_confirmed_submission(self) -> bool:
        return (
            self.success
            and self.status == EngineStatus.SUBMITTED_CONFIRMED.value
            and self.submitted is True
            and self.confirmed is True
            and self.test_mode is False
        )

    def to_payload(
        self,
        *,
        include_ats: bool = False,
        namespace_details: bool = True,
    ) -> dict[str, object]:
        """Serialize either for a pipeline checkpoint or the compatibility API."""
        payload: dict[str, object] = {
            "success": self.success,
            "status": self.status,
        }
        if include_ats and self.ats:
            payload["ats"] = self.ats
        optional_fields: tuple[tuple[str, object | None], ...] = (
            ("submitted", self.submitted),
            ("confirmed", self.confirmed),
            ("test_mode", self.test_mode),
            ("legacy_result", self.legacy_result),
            ("timeout_seconds", self.timeout_seconds),
        )
        for key, value in optional_fields:
            if value is not None:
                payload[key] = value
        if self.error:
            payload["error"] = self.error
        if self.detail:
            payload["detail"] = self.detail
        if self.engine_details:
            if namespace_details:
                payload["engine_details"] = dict(self.engine_details)
            else:
                provider_details = dict(self.engine_details)
                provider_details.update(payload)
                payload = provider_details
        return payload


@dataclass(frozen=True, slots=True)
class ApplicationDetails:
    """Typed non-identity fields for one terminal application result."""

    outcome: EngineOutcome
    engine: str | None = None
    resume: str | None = None
    cover_letter: str | None = None
    email: str | None = None
    already_submitted: bool | None = None
    ledger_persisted: bool | None = None
    manual_review_required: bool | None = None
    retry_safe: bool | None = None
    quarantine_persisted: bool | None = None
    quarantine_path: str = ""
    ledger_error: str = ""
    quarantine_error: str = ""
    engine_result: EngineOutcome | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        text_fields: tuple[tuple[str, str | None], ...] = (
            ("engine", self.engine),
            ("resume", self.resume),
            ("cover_letter", self.cover_letter),
            ("email", self.email),
        )
        for key, value in text_fields:
            if value is not None:
                payload[key] = value
        payload.update(self.outcome.to_payload())
        flag_fields: tuple[tuple[str, bool | None], ...] = (
            ("already_submitted", self.already_submitted),
            ("ledger_persisted", self.ledger_persisted),
            ("manual_review_required", self.manual_review_required),
            ("retry_safe", self.retry_safe),
            ("quarantine_persisted", self.quarantine_persisted),
        )
        for flag_key, flag_value in flag_fields:
            if flag_value is not None:
                payload[flag_key] = flag_value
        for error_key, error_value in (
            ("quarantine_path", self.quarantine_path),
            ("ledger_error", self.ledger_error),
            ("quarantine_error", self.quarantine_error),
        ):
            if error_value:
                payload[error_key] = error_value
        if self.engine_result is not None:
            payload["engine_result"] = self.engine_result.to_payload(
                include_ats=True,
                namespace_details=True,
            )
        return payload


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    """A target paired with its deterministic position and assigned identity."""

    target: ApplicationTarget
    ordinal: int
    total: int
    email: str
    email_required: bool


@dataclass(frozen=True, slots=True)
class ResolvedApplication:
    """An application whose engine exists and may prepare documents."""

    context: ApplicationContext
    engine_path: Path
    engine_label: str


@dataclass(frozen=True, slots=True)
class PreparedApplication:
    """An application with validated, identity-matched documents."""

    resolved: ResolvedApplication
    resume_path: Path
    cover_letter_path: Path
    current_title: str


@dataclass(frozen=True, slots=True)
class ExecutedApplication:
    """A prepared application paired with its lossless engine outcome."""

    prepared: PreparedApplication
    outcome: EngineOutcome


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    """One terminal result while retaining the legacy serialized shape."""

    target: ApplicationTarget
    details: ApplicationDetails

    def to_payload(self) -> dict[str, Any]:
        """Merge fields in the same order as the legacy orchestrator."""
        return {**self.target.base_result(), **self.details.to_payload()}


@dataclass(frozen=True, slots=True)
class PipelineCompletion:
    """A terminal result and whether the remaining run must stop."""

    result: ApplicationResult
    halt_pipeline: bool = False


GenerateResume = Callable[[ApplicationTarget, str], Path | None]
GenerateCoverLetter = Callable[[ApplicationTarget, str], Path | None]
ReadResumeEmail = Callable[[Path, str], str]
ReadCurrentTitle = Callable[[Path], str]
EngineLabel = Callable[[Path, str], str]
BuildEngineCommand = Callable[
    [Path, ApplicationTarget, Path, Path, str, EngineMode, bool],
    Sequence[str],
]
RunProcess = Callable[[Sequence[str], ProcessSettings], CommandResult]
ParseEngineResult = Callable[[ProcessResult, EngineMode, str], EngineOutcome]
CreateScreenshotDirectory = Callable[[str | Path | None], Path]
CleanupScreenshotDirectory = Callable[[Path], tuple[int, int]]
MaskEmail = Callable[[str], str]
RecordSubmission = Callable[
    [SubmissionLog, Path, ApplicationTarget, str, Path, Path, str],
    SubmissionPersistence,
]
WriteResults = Callable[[Path, Sequence[Mapping[str, Any]]], None]


@dataclass(frozen=True, slots=True)
class PipelineOperations:
    """Injected side effects used by the otherwise typed pipeline."""

    generate_resume: GenerateResume
    generate_cover_letter: GenerateCoverLetter
    read_resume_email: ReadResumeEmail
    read_current_title: ReadCurrentTitle
    engine_label: EngineLabel
    build_engine_command: BuildEngineCommand
    run_process: RunProcess
    parse_engine_result: ParseEngineResult
    create_screenshot_directory: CreateScreenshotDirectory
    cleanup_screenshot_directory: CleanupScreenshotDirectory
    mask_email: MaskEmail
    record_submission: RecordSubmission
    write_results: WriteResults


class ApplicationPipeline:
    """Advance selected jobs through explicit, typed application stages."""

    def __init__(
        self,
        *,
        targets: Sequence[ApplicationTarget],
        emails: Sequence[str],
        email_required: Sequence[bool] | None = None,
        config: PipelineConfig,
        submission_log: SubmissionLog,
        submission_quarantine: SubmissionLog,
        operations: PipelineOperations,
    ) -> None:
        if len(targets) != len(emails):
            raise ValueError("Every application target must have one assigned email")
        if email_required is not None and len(targets) != len(email_required):
            raise ValueError("Every application target must have one email-requirement decision")
        self._targets = tuple(targets)
        self._emails = tuple(emails)
        self._email_required = (
            tuple(email_required) if email_required is not None else (True,) * len(targets)
        )
        self._config = config
        self._submission_log = submission_log
        self._submission_quarantine = submission_quarantine
        self._operations = operations
        self._results: list[dict[str, Any]] = []

    @staticmethod
    def _completion(
        target: ApplicationTarget,
        details: ApplicationDetails,
        *,
        halt_pipeline: bool = False,
    ) -> PipelineCompletion:
        return PipelineCompletion(
            ApplicationResult(target=target, details=details),
            halt_pipeline=halt_pipeline,
        )

    def _safety_gate(
        self,
        context: ApplicationContext,
    ) -> ResolvedApplication | PipelineCompletion:
        target = context.target
        engine_path = self._config.engine_paths.get(target.ats)
        previous_submissions = (
            self._submission_log.find_by_job_url(target.url) if self._config.live_submit else {}
        )
        if previous_submissions:
            latest = max(
                previous_submissions.values(),
                key=lambda entry: str(entry.get("applied_at", "")),
            )
            logger.info(
                "[%d/%d] row=%s ats=%s company=%s role=%s already confirmed; skipping",
                context.ordinal,
                context.total,
                target.row_number,
                target.ats,
                target.company,
                target.role,
            )
            return self._completion(
                target,
                ApplicationDetails(
                    engine=(
                        self._operations.engine_label(engine_path, target.ats)
                        if engine_path
                        else ""
                    ),
                    resume=str(latest.get("resume_filename", "")),
                    cover_letter=str(latest.get("cover_letter_filename", "")),
                    email=self._operations.mask_email(str(latest.get("email_used", ""))),
                    outcome=EngineOutcome(
                        success=True,
                        status="ALREADY_SUBMITTED",
                        submitted=False,
                        confirmed=True,
                        test_mode=False,
                    ),
                    already_submitted=True,
                ),
            )

        previous_quarantines = (
            self._submission_quarantine.find_by_job_url(target.url)
            if self._config.live_submit
            else {}
        )
        if previous_quarantines:
            latest = max(
                previous_quarantines.values(),
                key=lambda entry: str(entry.get("applied_at", "")),
            )
            logger.error(
                "[%d/%d] row=%s ats=%s company=%s role=%s requires manual review; "
                "a prior confirmed submission could not be written to the ledger",
                context.ordinal,
                context.total,
                target.row_number,
                target.ats,
                target.company,
                target.role,
            )
            return self._completion(
                target,
                ApplicationDetails(
                    engine=(
                        self._operations.engine_label(engine_path, target.ats)
                        if engine_path
                        else ""
                    ),
                    resume=str(latest.get("resume_filename", "")),
                    cover_letter=str(latest.get("cover_letter_filename", "")),
                    email=self._operations.mask_email(str(latest.get("email_used", ""))),
                    outcome=EngineOutcome(
                        success=False,
                        status=LEDGER_PERSIST_FAILED_STATUS,
                        submitted=True,
                        confirmed=True,
                        test_mode=False,
                        detail=(
                            "A previous confirmed submission is quarantined because its ledger "
                            "write failed; automatic retry is disabled."
                        ),
                    ),
                    ledger_persisted=False,
                    manual_review_required=True,
                    retry_safe=False,
                    quarantine_persisted=True,
                    quarantine_path=str(self._config.submission_quarantine_path),
                ),
            )

        if not context.email_required or not engine_path or not engine_path.is_file():
            return self._completion(
                target,
                ApplicationDetails(outcome=EngineOutcome(success=False, status="ENGINE_NOT_FOUND")),
            )
        return ResolvedApplication(
            context=context,
            engine_path=engine_path,
            engine_label=self._operations.engine_label(engine_path, target.ats),
        )

    def _prepare_documents(
        self,
        resolved: ResolvedApplication,
    ) -> PreparedApplication | PipelineCompletion:
        context = resolved.context
        target = context.target
        try:
            generated = self._config.prepared_resume_path or self._operations.generate_resume(
                target,
                context.email,
            )
        except Exception as exc:
            logger.error("Resume identity extraction failed for row %s: %s", target.row_number, exc)
            return self._completion(
                target,
                ApplicationDetails(
                    outcome=EngineOutcome(
                        success=False,
                        status="RESUME_IDENTITY_EXTRACTION_FAILED",
                    )
                ),
            )
        if not generated:
            logger.error(
                "Mandatory personalized resume generation failed for %s; "
                "submission will not be attempted.",
                target.url,
            )
            return self._completion(
                target,
                ApplicationDetails(
                    engine=resolved.engine_label,
                    resume="",
                    email=self._operations.mask_email(context.email),
                    outcome=EngineOutcome(
                        success=False,
                        status="PERSONALIZED_RESUME_FAILED",
                        submitted=False,
                        confirmed=False,
                    ),
                ),
            )

        target_resume = generated
        try:
            resume_email = (
                self._operations.read_resume_email(
                    target_resume,
                    self._config.fallback_email,
                )
                .strip()
                .lower()
            )
            if resume_email != context.email:
                raise ValueError(
                    "Personalized resume email does not match the assigned application email"
                )
            current_title = self._operations.read_current_title(target_resume)
        except Exception as exc:
            logger.error(
                "Generated resume identity extraction failed for row %s: %s",
                target.row_number,
                exc,
            )
            return self._completion(
                target,
                ApplicationDetails(
                    engine=resolved.engine_label,
                    resume=target_resume.name,
                    email=self._operations.mask_email(context.email),
                    outcome=EngineOutcome(
                        success=False,
                        status="GENERATED_RESUME_IDENTITY_INVALID",
                    ),
                ),
            )

        try:
            target_cover_letter = (
                self._config.prepared_cover_letter_path
                or self._operations.generate_cover_letter(target, context.email)
            )
        except Exception as exc:
            logger.error(
                "Personalized cover-letter generation failed for row %s: %s",
                target.row_number,
                exc,
            )
            target_cover_letter = None
        if not target_cover_letter:
            logger.error(
                "Mandatory personalized cover-letter generation failed for %s; "
                "submission will not be attempted.",
                target.url,
            )
            return self._completion(
                target,
                ApplicationDetails(
                    engine=resolved.engine_label,
                    resume=target_resume.name,
                    cover_letter="",
                    email=self._operations.mask_email(context.email),
                    outcome=EngineOutcome(
                        success=False,
                        status="PERSONALIZED_COVER_LETTER_FAILED",
                        submitted=False,
                        confirmed=False,
                    ),
                ),
            )

        logger.info(
            "[%d/%d] row=%s ats=%s company=%s role=%s email=%s",
            context.ordinal,
            context.total,
            target.row_number,
            target.ats,
            target.company,
            target.role,
            self._operations.mask_email(context.email),
        )
        return PreparedApplication(
            resolved=resolved,
            resume_path=target_resume,
            cover_letter_path=target_cover_letter,
            current_title=current_title,
        )

    def _execute(self, prepared: PreparedApplication) -> ExecutedApplication:
        context = prepared.resolved.context
        target = context.target
        screenshot_dir: Path | None = None
        try:
            command = self._operations.build_engine_command(
                prepared.resolved.engine_path,
                target,
                prepared.resume_path,
                prepared.cover_letter_path,
                context.email,
                self._config.mode,
                self._config.headed,
            )
            inherited_screenshot_dir = os.environ.get(APPLICATION_SCREENSHOT_DIR_ENV, "")
            screenshot_dir = self._operations.create_screenshot_directory(
                inherited_screenshot_dir or None
            )
            engine_env = dict(os.environ)
            engine_env[ORCHESTRATOR_INVOCATION_ENV] = "1"
            engine_env[ORCHESTRATOR_CONFIG_ENV] = str(self._config.config_path)
            engine_env[ORCHESTRATOR_CURRENT_TITLE_ENV] = prepared.current_title
            engine_env[APPLICATION_SCREENSHOT_DIR_ENV] = str(screenshot_dir)
            command_result = self._operations.run_process(
                command,
                ProcessSettings(
                    timeout_seconds=self._config.timeout_seconds,
                    environment=engine_env,
                ),
            )
            process_result = ProcessResult(
                command_result.returncode,
                command_result.stdout,
                command_result.stderr,
            )
            outcome = self._operations.parse_engine_result(
                process_result,
                self._config.mode,
                target.ats,
            )
            if not outcome.success:
                logger.error(
                    "Engine diagnostics:\n%s",
                    (process_result.stdout + "\n" + process_result.stderr)[-8000:],
                )
        except ProcessTimeoutError as exc:
            outcome = EngineOutcome(
                success=False,
                status="TIMED_OUT",
                timeout_seconds=exc.timeout,
            )
        except Exception as exc:
            logger.error("Engine execution failed: %s", exc)
            outcome = EngineOutcome(
                success=False,
                status=EngineStatus.ENGINE_EXECUTION_ERROR.value,
                detail=str(exc),
            )
        finally:
            if screenshot_dir is not None:
                try:
                    files_deleted, bytes_deleted = self._operations.cleanup_screenshot_directory(
                        screenshot_dir
                    )
                    logger.info(
                        "Application screenshots cleaned: files=%d bytes=%d directory=%s",
                        files_deleted,
                        bytes_deleted,
                        screenshot_dir,
                    )
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Could not clean application screenshot directory %s: %s",
                        screenshot_dir,
                        exc,
                    )
        return ExecutedApplication(prepared=prepared, outcome=outcome)

    def _reconcile_confirmation(self, executed: ExecutedApplication) -> PipelineCompletion:
        prepared = executed.prepared
        context = prepared.resolved.context
        target = context.target
        outcome = executed.outcome
        stop_after_ledger_failure = False
        engine_result: EngineOutcome | None = None
        ledger_persisted: bool | None = None
        manual_review_required: bool | None = None
        retry_safe: bool | None = None
        quarantine_persisted: bool | None = None
        quarantine_path = ""
        ledger_error = ""
        quarantine_error = ""
        if outcome.is_confirmed_submission:
            persistence = self._operations.record_submission(
                self._submission_log,
                self._config.submission_log_path,
                target,
                context.email,
                prepared.resume_path,
                prepared.cover_letter_path,
                outcome.status,
            )
            if not persistence.persisted:
                engine_result = outcome
                outcome = EngineOutcome(
                    success=False,
                    status=LEDGER_PERSIST_FAILED_STATUS,
                    submitted=outcome.submitted,
                    confirmed=outcome.confirmed,
                    test_mode=outcome.test_mode,
                    detail=(
                        "The engine confirmed submission, but the confirmed ledger could not "
                        "be persisted. This job requires manual review and must not be retried."
                    ),
                )
                ledger_persisted = False
                manual_review_required = True
                retry_safe = False
                ledger_error = persistence.error
                quarantine_persisted = not bool(persistence.quarantine_error)
                quarantine_path = (
                    str(persistence.quarantine_path)
                    if persistence.quarantine_path is not None
                    else ""
                )
                quarantine_error = persistence.quarantine_error
                stop_after_ledger_failure = True

        return self._completion(
            target,
            ApplicationDetails(
                engine=prepared.resolved.engine_label,
                resume=prepared.resume_path.name,
                cover_letter=prepared.cover_letter_path.name,
                email=self._operations.mask_email(context.email),
                outcome=outcome,
                ledger_persisted=ledger_persisted,
                manual_review_required=manual_review_required,
                retry_safe=retry_safe,
                quarantine_persisted=quarantine_persisted,
                quarantine_path=quarantine_path,
                ledger_error=ledger_error,
                quarantine_error=quarantine_error,
                engine_result=engine_result,
            ),
            halt_pipeline=stop_after_ledger_failure,
        )

    def _run_application(self, context: ApplicationContext) -> PipelineCompletion:
        resolved = self._safety_gate(context)
        if isinstance(resolved, PipelineCompletion):
            return resolved
        prepared = self._prepare_documents(resolved)
        if isinstance(prepared, PipelineCompletion):
            return prepared
        return self._reconcile_confirmation(self._execute(prepared))

    def _checkpoint(self, completion: PipelineCompletion) -> None:
        self._results.append(completion.result.to_payload())
        self._operations.write_results(self._config.results_path, self._results)

    def run(self) -> list[dict[str, Any]]:
        """Run every target, checkpointing exactly once per terminal outcome."""
        if not self._targets:
            self._operations.write_results(self._config.results_path, [])
        for index, target in enumerate(self._targets, start=1):
            completion = self._run_application(
                ApplicationContext(
                    target=target,
                    ordinal=index,
                    total=len(self._targets),
                    email=self._emails[index - 1],
                    email_required=self._email_required[index - 1],
                )
            )
            self._checkpoint(completion)
            if completion.halt_pipeline:
                logger.error(
                    "Stopping orchestration after confirmed-ledger persistence failure for %s",
                    target.url,
                )
                break

        successful = sum(1 for result in self._results if result.get("success"))
        logger.info(
            "Orchestration complete: processed=%d successful=%d failed=%d results=%s",
            len(self._results),
            successful,
            len(self._results) - successful,
            self._config.results_path,
        )
        return self._results
