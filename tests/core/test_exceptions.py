from __future__ import annotations

from unittest.mock import patch

import pytest

from job_application_automation.core import orchestrator
from job_application_automation.core.application_pipeline import (
    ProcessTimeoutError,
    engine_outcome_from_exception,
)
from job_application_automation.core.contracts import EngineRequest, EngineStatus
from job_application_automation.core.exceptions import (
    ApplicationBlockedError,
    ArtifactError,
    BrowserAutomationError,
    ConfigurationError,
    ExternalServiceError,
    InputContractError,
    JobAutomationError,
    SubmissionOutcomeUnknown,
)
from job_application_automation.engines import browser_runtime


def test_taxonomy_preserves_migration_compatibility_bases() -> None:
    assert all(issubclass(ConfigurationError, base) for base in (JobAutomationError, ValueError))
    assert all(issubclass(InputContractError, base) for base in (JobAutomationError, ValueError))
    assert all(
        issubclass(ArtifactError, base) for base in (JobAutomationError, OSError, ValueError)
    )
    assert all(
        issubclass(ExternalServiceError, base) for base in (JobAutomationError, RuntimeError)
    )
    assert issubclass(BrowserAutomationError, ExternalServiceError)
    assert issubclass(ApplicationBlockedError, BrowserAutomationError)
    assert issubclass(SubmissionOutcomeUnknown, BrowserAutomationError)


def test_contract_parser_raises_specific_input_error() -> None:
    with pytest.raises(InputContractError, match="missing resume"):
        EngineRequest.from_payload({"ats": "lever", "url": "https://jobs.lever.co/example/123"})


def test_browser_control_boundary_raises_specific_automation_error() -> None:
    with pytest.raises(BrowserAutomationError, match="did not return an ID"):
        browser_runtime._create_background_target(
            "http://localhost:9222",
            raw_command=lambda *_args: {},
        )


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (BrowserAutomationError("browser crashed"), EngineStatus.ENGINE_EXECUTION_ERROR.value),
        (ApplicationBlockedError("manual challenge"), EngineStatus.FAILED.value),
        (
            SubmissionOutcomeUnknown("confirmation timed out"),
            EngineStatus.SUBMISSION_UNCONFIRMED.value,
        ),
    ],
)
def test_typed_execution_errors_map_to_existing_engine_statuses(
    error: Exception,
    expected_status: str,
) -> None:
    mapped = engine_outcome_from_exception(error)
    outcome = mapped.outcome

    assert outcome.success is False
    assert outcome.status == expected_status
    if isinstance(error, SubmissionOutcomeUnknown):
        assert outcome.submitted is True
        assert outcome.confirmed is False
        assert mapped.manual_review_required is True
        assert mapped.retry_safe is False
    elif isinstance(error, ApplicationBlockedError):
        assert mapped.manual_review_required is True
        assert mapped.retry_safe is False
    else:
        assert mapped.manual_review_required is None
        assert mapped.retry_safe is None


@pytest.mark.parametrize(
    "error",
    [
        ConfigurationError("bad config"),
        InputContractError("bad input"),
        ArtifactError("bad artifact"),
    ],
)
def test_orchestrator_startup_errors_keep_exit_code_two(error: Exception) -> None:
    with (
        patch.object(orchestrator, "_resolve_engine_paths", return_value={}),
        patch.object(orchestrator, "run_orchestrator", side_effect=error),
    ):
        assert orchestrator.main(["--url", "https://jobs.lever.co/example/123"]) == 2


def test_process_timeout_is_an_external_service_timeout() -> None:
    error = ProcessTimeoutError(17)

    assert isinstance(error, ExternalServiceError)
    assert isinstance(error, TimeoutError)
