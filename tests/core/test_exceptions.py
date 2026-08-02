from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from job_application_automation.core import orchestrator
from job_application_automation.core.artifacts import atomic_write_text, read_json
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


def test_invalid_utf8_json_is_an_artifact_error_with_the_decode_cause(tmp_path: Path) -> None:
    target = tmp_path / "invalid.json"
    target.write_bytes(b'{"value": \xff}')

    with pytest.raises(ArtifactError, match="could not read JSON artifact") as captured:
        read_json(target)

    assert isinstance(captured.value.__cause__, UnicodeDecodeError)


def test_unencodable_text_is_an_artifact_error_and_cleans_the_temporary_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ascii-only.txt"

    with pytest.raises(ArtifactError, match="could not write artifact") as captured:
        atomic_write_text(target, "München", encoding="ascii")

    assert isinstance(captured.value.__cause__, UnicodeEncodeError)
    assert list(tmp_path.iterdir()) == []


def test_raw_cdp_url_failure_is_a_browser_error_with_the_transport_cause() -> None:
    transport_error = OSError("endpoint unavailable")

    with (
        patch.object(browser_runtime.urllib.request, "urlopen", side_effect=transport_error),
        pytest.raises(BrowserAutomationError, match="CDP transport failed") as captured,
    ):
        browser_runtime._raw_browser_cdp_command("http://localhost:9222", "Browser.getVersion", {})

    assert captured.value.__cause__ is transport_error


def test_raw_cdp_version_json_failure_is_a_browser_error_with_the_decode_cause() -> None:
    with (
        patch.object(
            browser_runtime.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b"{not-json"),
        ),
        pytest.raises(BrowserAutomationError, match="CDP transport failed") as captured,
    ):
        browser_runtime._raw_browser_cdp_command("http://localhost:9222", "Browser.getVersion", {})

    assert isinstance(captured.value.__cause__, json.JSONDecodeError)


def test_raw_cdp_websocket_failure_is_a_browser_error_with_the_transport_cause() -> None:
    version = io.BytesIO(
        json.dumps({"webSocketDebuggerUrl": "ws://localhost/devtools/browser/1"}).encode()
    )
    transport_error = OSError("websocket unavailable")

    with (
        patch.object(browser_runtime.urllib.request, "urlopen", return_value=version),
        patch("websockets.sync.client.connect", side_effect=transport_error),
        pytest.raises(BrowserAutomationError, match="CDP transport failed") as captured,
    ):
        browser_runtime._raw_browser_cdp_command("http://localhost:9222", "Browser.getVersion", {})

    assert captured.value.__cause__ is transport_error


class _InvalidJsonSocket:
    def __enter__(self) -> _InvalidJsonSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def send(self, _payload: str) -> None:
        return None

    def recv(self, *, timeout: int) -> str:
        assert timeout == 5
        return "{not-json"


def test_raw_cdp_websocket_json_failure_is_a_browser_error_with_the_decode_cause() -> None:
    version = io.BytesIO(
        json.dumps({"webSocketDebuggerUrl": "ws://localhost/devtools/browser/1"}).encode()
    )

    with (
        patch.object(browser_runtime.urllib.request, "urlopen", return_value=version),
        patch("websockets.sync.client.connect", return_value=_InvalidJsonSocket()),
        pytest.raises(BrowserAutomationError, match="CDP transport failed") as captured,
    ):
        browser_runtime._raw_browser_cdp_command("http://localhost:9222", "Browser.getVersion", {})

    assert isinstance(captured.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"require_tracker": True, "tracker_path": None}, "Tracker path is required"),
        ({"timeout_seconds": 0}, "Engine timeout must be greater than zero"),
        ({"resume_timeout_seconds": 0}, "Resume timeout must be greater than zero"),
    ],
)
def test_orchestrator_file_and_timeout_contracts_raise_specific_input_errors(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume")
    arguments: dict[str, object] = {
        "tracker_path": None,
        "require_tracker": False,
        "resume_path": resume,
        "prepared_resume_path": None,
        "cover_letter_path": None,
        "config_path": None,
        "timeout_seconds": 30,
        "resume_timeout_seconds": 30,
    }
    arguments.update(overrides)

    with pytest.raises(InputContractError, match=message):
        orchestrator._validate_orchestrator_inputs(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("personalization", "personalization is mandatory"),
        ("config", "profile configuration is required"),
        ("prepared", "prepared-resume can only be used"),
        ("cover_letter", "cover-letter requires"),
        ("email", "Email override must contain"),
    ],
)
def test_run_orchestrator_rejects_public_argument_contracts_with_specific_errors(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    resume = tmp_path / "resume.pdf"
    prepared_resume = tmp_path / "prepared.pdf"
    cover_letter = tmp_path / "cover.pdf"
    tracker = tmp_path / "tracker.xlsx"
    config = tmp_path / "profile.json"
    for path in (resume, prepared_resume, cover_letter, tracker, config):
        path.write_bytes(b"fixture")
    arguments: dict[str, object] = {
        "engine_paths": {},
        "tracker_path": None,
        "resume_path": resume,
        "config_path": config,
        "results_path": tmp_path / "results.json",
        "direct_url": "https://jobs.lever.co/example/123",
    }
    if case == "personalization":
        arguments["personalize_resume"] = False
    elif case == "config":
        arguments["config_path"] = None
    elif case == "prepared":
        arguments.update(
            prepared_resume_path=prepared_resume,
            direct_url=None,
            tracker_path=tracker,
        )
    elif case == "cover_letter":
        arguments["cover_letter_path"] = cover_letter
    else:
        arguments["email_override"] = "@invalid"

    with pytest.raises(InputContractError, match=message):
        orchestrator.run_orchestrator(**arguments)  # type: ignore[arg-type]


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
