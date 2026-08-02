"""Focused tests for the typed per-application pipeline stages."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

import pytest

from job_application_automation.core.adapters import CommandResult, ProcessSettings
from job_application_automation.core.application_pipeline import (
    LEDGER_PERSIST_FAILED_STATUS,
    ApplicationPipeline,
    ApplicationTarget,
    EngineOutcome,
    PipelineConfig,
    PipelineOperations,
    ProcessResult,
    ProcessTimeoutError,
    SubmissionPersistence,
)
from job_application_automation.core.contracts import EngineMode, EngineStatus
from job_application_automation.core.engine_shared import (
    ORCHESTRATOR_CONFIG_ENV,
    ORCHESTRATOR_CURRENT_TITLE_ENV,
    ORCHESTRATOR_INVOCATION_ENV,
)
from job_application_automation.core.screenshots import APPLICATION_SCREENSHOT_DIR_ENV
from job_application_automation.core.submission_log import SubmissionLog, SubmissionRecord


class FakeOperations:
    """Stateful side-effect fake used to assert stage ordering and isolation."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.generated_resume: Path | None = root / "generated-resume.pdf"
        self.generated_cover_letter: Path | None = root / "generated-cover-letter.pdf"
        self.generate_resume_error: Exception | None = None
        self.generate_cover_letter_error: Exception | None = None
        self.resume_email = "candidate@example.test"
        self.current_title = "Senior Product Manager"
        self.process_error: Exception | None = None
        self.build_command_error: Exception | None = None
        self.process_result = CommandResult(returncode=0, stdout="provider output", stderr="")
        self.parsed_result = EngineOutcome(
            success=False,
            status="FAILED",
            ats="workable",
            submitted=False,
            confirmed=False,
            test_mode=True,
        )
        self.persistence = SubmissionPersistence(persisted=True)
        self.events: list[str] = []
        self.process_settings: list[ProcessSettings] = []
        self.snapshots: list[list[dict[str, Any]]] = []
        self.recorded_targets: list[ApplicationTarget] = []
        self.cleanup_paths: list[Path] = []

    def generate_resume(self, _target: ApplicationTarget, _email: str) -> Path | None:
        self.events.append("generate_resume")
        if self.generate_resume_error is not None:
            raise self.generate_resume_error
        return self.generated_resume

    def generate_cover_letter(self, _target: ApplicationTarget, _email: str) -> Path | None:
        self.events.append("generate_cover_letter")
        if self.generate_cover_letter_error is not None:
            raise self.generate_cover_letter_error
        return self.generated_cover_letter

    def read_resume_email(self, _path: Path, _fallback: str) -> str:
        self.events.append("read_resume_email")
        return self.resume_email

    def read_current_title(self, _path: Path) -> str:
        self.events.append("read_current_title")
        return self.current_title

    @staticmethod
    def engine_label(_path: Path, ats: str) -> str:
        return f"internal:{ats}"

    def build_engine_command(
        self,
        _engine_path: Path,
        _target: ApplicationTarget,
        _resume_path: Path,
        _cover_letter_path: Path,
        _email: str,
        _mode: EngineMode,
        _headed: bool,
    ) -> Sequence[str]:
        self.events.append("build_command")
        if self.build_command_error is not None:
            raise self.build_command_error
        return ("engine", "--run")

    def run_process(
        self,
        _command: Sequence[str],
        settings: ProcessSettings,
    ) -> CommandResult:
        self.events.append("run_process")
        self.process_settings.append(settings)
        if self.process_error is not None:
            raise self.process_error
        return self.process_result

    def parse_engine_result(
        self,
        _result: ProcessResult,
        _mode: EngineMode,
        _expected_ats: str,
    ) -> EngineOutcome:
        self.events.append("parse_result")
        return self.parsed_result

    def create_screenshot_directory(self, _inherited: str | Path | None) -> Path:
        self.events.append("create_screenshot")
        path = self.root / f"screenshots-{self.events.count('create_screenshot')}"
        path.mkdir(exist_ok=True)
        return path

    def cleanup_screenshot_directory(self, path: Path) -> tuple[int, int]:
        self.events.append("cleanup_screenshot")
        self.cleanup_paths.append(path)
        return 1, 128

    @staticmethod
    def mask_email(email: str) -> str:
        return f"masked:{email}"

    @staticmethod
    def is_confirmed_submission(payload: Mapping[str, object]) -> bool:
        return bool(
            payload.get("success") is True
            and payload.get("status") == EngineStatus.SUBMITTED_CONFIRMED.value
            and payload.get("submitted") is True
            and payload.get("confirmed") is True
            and payload.get("test_mode") is False
        )

    def record_submission(
        self,
        _submission_log: SubmissionLog,
        _submission_log_path: Path,
        target: ApplicationTarget,
        _email: str,
        _resume_path: Path,
        _cover_letter_path: Path,
        _status: str,
    ) -> SubmissionPersistence:
        self.events.append("record_submission")
        self.recorded_targets.append(target)
        return self.persistence

    def write_results(
        self,
        _path: Path,
        results: Sequence[Mapping[str, Any]],
    ) -> None:
        self.events.append("write_results")
        self.snapshots.append([dict(result) for result in results])

    def bundle(self) -> PipelineOperations:
        return PipelineOperations(
            generate_resume=self.generate_resume,
            generate_cover_letter=self.generate_cover_letter,
            read_resume_email=self.read_resume_email,
            read_current_title=self.read_current_title,
            engine_label=self.engine_label,
            build_engine_command=self.build_engine_command,
            run_process=self.run_process,
            parse_engine_result=self.parse_engine_result,
            create_screenshot_directory=self.create_screenshot_directory,
            cleanup_screenshot_directory=self.cleanup_screenshot_directory,
            mask_email=self.mask_email,
            record_submission=self.record_submission,
            write_results=self.write_results,
        )


def _target(row: int = 1) -> ApplicationTarget:
    return ApplicationTarget(
        row_number=row,
        company=f"Acme {row}",
        role="Product Manager",
        url=f"https://apply.workable.com/acme/j/ABC{row}/",
        ats="workable",
    )


def _config(root: Path, engine: Path) -> PipelineConfig:
    profile = root / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    resume = root / "prepared-resume.pdf"
    resume.write_bytes(b"resume")
    cover_letter = root / "prepared-cover-letter.pdf"
    cover_letter.write_bytes(b"cover")
    return PipelineConfig(
        engine_paths={"workable": engine},
        results_path=root / "results.json",
        submission_log_path=root / "submission-log.json",
        submission_quarantine_path=root / "submission-log_quarantine.json",
        config_path=profile,
        prepared_resume_path=resume,
        prepared_cover_letter_path=cover_letter,
        mode=EngineMode.DRY_RUN,
        headed=False,
        timeout_seconds=30,
        fallback_email="fallback@example.test",
    )


def _run(
    *,
    targets: Sequence[ApplicationTarget],
    config: PipelineConfig,
    operations: FakeOperations,
    submission_log: SubmissionLog | None = None,
    quarantine: SubmissionLog | None = None,
) -> list[dict[str, Any]]:
    return ApplicationPipeline(
        targets=targets,
        emails=["candidate@example.test"] * len(targets),
        config=config,
        submission_log=submission_log or SubmissionLog(),
        submission_quarantine=quarantine or SubmissionLog(),
        operations=operations.bundle(),
    ).run()


def test_missing_engine_checkpoints_the_exact_sparse_payload(tmp_path: Path) -> None:
    missing_engine = tmp_path / "missing.py"
    operations = FakeOperations(tmp_path)
    config = _config(tmp_path, missing_engine)

    results = _run(targets=[_target()], config=config, operations=operations)

    assert results == [
        {
            "row": 1,
            "company": "Acme 1",
            "role": "Product Manager",
            "url": "https://apply.workable.com/acme/j/ABC1/",
            "ats": "workable",
            "status": "ENGINE_NOT_FOUND",
            "success": False,
        }
    ]
    assert operations.events == ["write_results"]
    assert operations.snapshots == [results]


def test_live_safety_gate_skips_confirmed_job_before_documents(tmp_path: Path) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)
    config = replace(_config(tmp_path, engine), mode=EngineMode.LIVE_SUBMIT)
    submission_log = SubmissionLog()
    submission_log.record(
        SubmissionRecord(
            company="Acme 1",
            role="Product Manager",
            job_url=_target().url,
            ats="workable",
            status=EngineStatus.SUBMITTED_CONFIRMED.value,
            email_used="existing@example.test",
            resume_filename="existing-resume.pdf",
            cover_letter_filename="existing-cover.pdf",
        )
    )

    results = _run(
        targets=[_target()],
        config=config,
        operations=operations,
        submission_log=submission_log,
    )

    assert results[0] == {
        **_target().base_result(),
        "engine": "internal:workable",
        "resume": "existing-resume.pdf",
        "cover_letter": "existing-cover.pdf",
        "email": "masked:existing@example.test",
        "status": "ALREADY_SUBMITTED",
        "success": True,
        "submitted": False,
        "confirmed": True,
        "test_mode": False,
        "already_submitted": True,
    }
    assert operations.events == ["write_results"]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            "resume_exception",
            {"status": "RESUME_IDENTITY_EXTRACTION_FAILED", "success": False},
        ),
        (
            "resume_missing",
            {
                "engine": "internal:workable",
                "resume": "",
                "email": "masked:candidate@example.test",
                "confirmed": False,
                "submitted": False,
                "success": False,
                "status": "PERSONALIZED_RESUME_FAILED",
            },
        ),
        (
            "resume_identity",
            {
                "engine": "internal:workable",
                "resume": "prepared-resume.pdf",
                "email": "masked:candidate@example.test",
                "status": "GENERATED_RESUME_IDENTITY_INVALID",
                "success": False,
            },
        ),
        (
            "cover_letter_missing",
            {
                "engine": "internal:workable",
                "resume": "prepared-resume.pdf",
                "cover_letter": "",
                "email": "masked:candidate@example.test",
                "confirmed": False,
                "submitted": False,
                "success": False,
                "status": "PERSONALIZED_COVER_LETTER_FAILED",
            },
        ),
    ],
)
def test_document_stage_preserves_each_terminal_payload(
    tmp_path: Path,
    case: str,
    expected: dict[str, Any],
) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)
    config = _config(tmp_path, engine)
    if case in {"resume_exception", "resume_missing"}:
        config = replace(config, prepared_resume_path=None)
    if case == "resume_exception":
        operations.generate_resume_error = RuntimeError("identity extraction failed")
    elif case == "resume_missing":
        operations.generated_resume = None
    elif case == "resume_identity":
        operations.resume_email = "different@example.test"
    elif case == "cover_letter_missing":
        config = replace(config, prepared_cover_letter_path=None)
        operations.generated_cover_letter = None

    results = _run(targets=[_target()], config=config, operations=operations)

    assert results == [{**_target().base_result(), **expected}]
    assert "run_process" not in operations.events
    assert operations.events[-1] == "write_results"


@pytest.mark.parametrize(
    ("stage", "expected_status"),
    [
        ("cover_letter", "PERSONALIZED_COVER_LETTER_FAILED"),
        ("command", "ENGINE_EXECUTION_ERROR"),
    ],
)
def test_stage_exceptions_checkpoint_each_target_and_continue(
    tmp_path: Path,
    stage: str,
    expected_status: str,
) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)
    config = _config(tmp_path, engine)
    if stage == "cover_letter":
        config = replace(config, prepared_cover_letter_path=None)
        operations.generate_cover_letter_error = RuntimeError("cover service unavailable")
    else:
        operations.build_command_error = RuntimeError("invalid engine command")

    results = _run(
        targets=[_target(1), _target(2)],
        config=config,
        operations=operations,
    )

    assert [result["status"] for result in results] == [expected_status, expected_status]
    assert [len(snapshot) for snapshot in operations.snapshots] == [1, 2]
    assert operations.events.count("write_results") == 2


def test_pipeline_derives_live_ledger_policy_from_engine_mode(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "engine.py")

    assert config.live_submit is False
    assert replace(config, mode=EngineMode.LIVE_SUBMIT).live_submit is True


def test_execution_propagates_environment_preserves_extras_and_cleans(
    tmp_path: Path,
) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)
    operations.parsed_result = EngineOutcome(
        success=False,
        status="REQUIRED_FIELDS_NOT_FILLED",
        ats="workable",
        submitted=False,
        confirmed=False,
        test_mode=True,
        engine_details={"provider_fields": ["location", "salary"]},
    )
    config = _config(tmp_path, engine)

    results = _run(targets=[_target()], config=config, operations=operations)

    assert results[0]["engine_details"] == {"provider_fields": ["location", "salary"]}
    assert operations.cleanup_paths == [tmp_path / "screenshots-1"]
    settings = operations.process_settings[0]
    assert settings.environment[ORCHESTRATOR_INVOCATION_ENV] == "1"
    assert settings.environment[ORCHESTRATOR_CONFIG_ENV] == str(config.config_path)
    assert settings.environment[ORCHESTRATOR_CURRENT_TITLE_ENV] == operations.current_title
    assert settings.environment[APPLICATION_SCREENSHOT_DIR_ENV] == str(tmp_path / "screenshots-1")
    assert operations.events.index("cleanup_screenshot") < operations.events.index("write_results")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ProcessTimeoutError(17),
            {"success": False, "status": "TIMED_OUT", "timeout_seconds": 17},
        ),
        (
            RuntimeError("browser crashed"),
            {
                "success": False,
                "status": "ENGINE_EXECUTION_ERROR",
                "detail": "browser crashed",
            },
        ),
    ],
)
def test_execution_cleans_screenshots_for_timeout_and_error(
    tmp_path: Path,
    error: Exception,
    expected: dict[str, Any],
) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)
    operations.process_error = error

    results = _run(targets=[_target()], config=_config(tmp_path, engine), operations=operations)

    assert {key: results[0][key] for key in expected} == expected
    assert operations.cleanup_paths == [tmp_path / "screenshots-1"]
    assert operations.events[-1] == "write_results"


def test_confirmed_submission_is_recorded_before_result_checkpoint(tmp_path: Path) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)
    operations.parsed_result = EngineOutcome(
        success=True,
        status=EngineStatus.SUBMITTED_CONFIRMED.value,
        ats="workable",
        submitted=True,
        confirmed=True,
        test_mode=False,
    )
    config = replace(_config(tmp_path, engine), mode=EngineMode.LIVE_SUBMIT)

    results = _run(targets=[_target()], config=config, operations=operations)

    assert results[0]["status"] == EngineStatus.SUBMITTED_CONFIRMED.value
    assert operations.recorded_targets == [_target()]
    assert operations.events.index("record_submission") < operations.events.index("write_results")


def test_ledger_failure_checkpoints_once_and_halts_remaining_jobs(tmp_path: Path) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)
    operations.parsed_result = EngineOutcome(
        success=True,
        status=EngineStatus.SUBMITTED_CONFIRMED.value,
        ats="workable",
        submitted=True,
        confirmed=True,
        test_mode=False,
    )
    quarantine_path = tmp_path / "submission-log_quarantine.json"
    operations.persistence = SubmissionPersistence(
        persisted=False,
        error="OSError: disk full",
        quarantine_path=quarantine_path,
    )
    config = replace(_config(tmp_path, engine), mode=EngineMode.LIVE_SUBMIT)

    results = _run(
        targets=[_target(1), _target(2)],
        config=config,
        operations=operations,
    )

    assert len(results) == 1
    assert results[0]["status"] == LEDGER_PERSIST_FAILED_STATUS
    assert results[0]["success"] is False
    assert results[0]["submitted"] is True
    assert results[0]["confirmed"] is True
    assert results[0]["manual_review_required"] is True
    assert results[0]["retry_safe"] is False
    assert results[0]["quarantine_path"] == str(quarantine_path)
    assert results[0]["engine_result"]["status"] == EngineStatus.SUBMITTED_CONFIRMED.value
    assert operations.events.count("run_process") == 1
    assert len(operations.snapshots) == 1


def test_checkpoints_accumulate_once_per_terminal_job(tmp_path: Path) -> None:
    missing_engine = tmp_path / "missing.py"
    operations = FakeOperations(tmp_path)

    results = _run(
        targets=[_target(1), _target(2)],
        config=_config(tmp_path, missing_engine),
        operations=operations,
    )

    assert len(results) == 2
    assert [len(snapshot) for snapshot in operations.snapshots] == [1, 2]
    assert operations.snapshots[-1] == results
    assert operations.events == ["write_results", "write_results"]


def test_empty_pipeline_checkpoints_an_empty_snapshot(tmp_path: Path) -> None:
    operations = FakeOperations(tmp_path)

    results = _run(
        targets=[],
        config=_config(tmp_path, tmp_path / "missing.py"),
        operations=operations,
    )

    assert results == []
    assert operations.snapshots == [[]]
    assert operations.events == ["write_results"]


def test_pipeline_rejects_an_email_assignment_length_mismatch(tmp_path: Path) -> None:
    engine = tmp_path / "engine.py"
    engine.write_text("# engine", encoding="utf-8")
    operations = FakeOperations(tmp_path)

    with pytest.raises(ValueError, match="one assigned email"):
        ApplicationPipeline(
            targets=[_target()],
            emails=[],
            config=_config(tmp_path, engine),
            submission_log=SubmissionLog(),
            submission_quarantine=SubmissionLog(),
            operations=operations.bundle(),
        )

    with pytest.raises(ValueError, match="email-requirement decision"):
        ApplicationPipeline(
            targets=[_target()],
            emails=["candidate@example.test"],
            email_required=[],
            config=_config(tmp_path, engine),
            submission_log=SubmissionLog(),
            submission_quarantine=SubmissionLog(),
            operations=operations.bundle(),
        )
