from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
import pytest


from job_application_automation.core.adapters import (
    BrowserFactory,
    BrowserPage,
    BrowserSession,
    BrowserSettings,
    CommandResult,
    LLMClient,
    LLMSettings,
    ProcessRunner,
    ProcessSettings,
)


def test_process_settings_valid() -> None:
    settings = ProcessSettings(
        timeout_seconds=60,
        cwd=Path("/tmp"),
        environment={"KEY": "VAL"},
    )
    assert settings.timeout_seconds == 60
    assert settings.cwd == Path("/tmp")
    assert settings.environment["KEY"] == "VAL"
    with pytest.raises(TypeError):
        settings.environment["NEW"] = "VAL"  # Immutable MappingProxyType


def test_process_settings_invalid_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be an integer"):
        ProcessSettings(timeout_seconds=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="timeout_seconds must be an integer"):
        ProcessSettings(timeout_seconds="60")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="timeout_seconds must be greater than zero"):
        ProcessSettings(timeout_seconds=0)

    with pytest.raises(ValueError, match="timeout_seconds must be greater than zero"):
        ProcessSettings(timeout_seconds=-10)


def test_process_settings_invalid_environment() -> None:
    with pytest.raises(ValueError, match="environment must be a mapping"):
        ProcessSettings(environment="not a dict")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="environment keys must be non-empty strings"):
        ProcessSettings(environment={"": "value"})

    with pytest.raises(ValueError, match="environment keys must be non-empty strings"):
        ProcessSettings(environment={123: "value"})  # type: ignore[dict-item]

    with pytest.raises(ValueError, match="environment values must be strings"):
        ProcessSettings(environment={"key": 123})  # type: ignore[dict-item]


def test_command_result_valid() -> None:
    res = CommandResult(returncode=0, stdout="out", stderr="err")
    assert res.returncode == 0
    assert res.stdout == "out"
    assert res.stderr == "err"


def test_command_result_invalid() -> None:
    with pytest.raises(ValueError, match="returncode must be an integer"):
        CommandResult(returncode=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="returncode must be an integer"):
        CommandResult(returncode="0")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="stdout and stderr must be strings"):
        CommandResult(returncode=0, stdout=123)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="stdout and stderr must be strings"):
        CommandResult(returncode=0, stderr=None)  # type: ignore[arg-type]


def test_llm_settings_valid() -> None:
    llm = LLMSettings(
        model="  gemini-pro  ", temperature=0.7, max_attempts=5, retry_delay_seconds=1.5
    )
    assert llm.model == "gemini-pro"
    assert llm.temperature == 0.7
    assert llm.max_attempts == 5
    assert llm.retry_delay_seconds == 1.5


def test_llm_settings_invalid() -> None:
    with pytest.raises(ValueError, match="model cannot be empty"):
        LLMSettings(model="")

    with pytest.raises(ValueError, match="model cannot be empty"):
        LLMSettings(model="   ")

    with pytest.raises(ValueError, match="model cannot be empty"):
        LLMSettings(model=None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="temperature must be a number"):
        LLMSettings(model="m", temperature=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="temperature must be between 0 and 2"):
        LLMSettings(model="m", temperature=-0.1)

    with pytest.raises(ValueError, match="temperature must be between 0 and 2"):
        LLMSettings(model="m", temperature=2.5)

    with pytest.raises(ValueError, match="max_attempts must be an integer"):
        LLMSettings(model="m", max_attempts=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_attempts must be at least 1"):
        LLMSettings(model="m", max_attempts=0)

    with pytest.raises(ValueError, match="retry_delay_seconds must be a number"):
        LLMSettings(model="m", retry_delay_seconds=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="retry_delay_seconds cannot be negative"):
        LLMSettings(model="m", retry_delay_seconds=-1.0)


def test_browser_settings_valid() -> None:
    bs = BrowserSettings(headed=True, timeout_ms=5000, cdp_endpoint="  ws://localhost:9222  ")
    assert bs.headed is True
    assert bs.timeout_ms == 5000
    assert bs.cdp_endpoint == "ws://localhost:9222"


def test_browser_settings_invalid() -> None:
    with pytest.raises(ValueError, match="headed must be a boolean"):
        BrowserSettings(headed="true")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="timeout_ms must be an integer"):
        BrowserSettings(timeout_ms=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="timeout_ms must be greater than zero"):
        BrowserSettings(timeout_ms=0)

    with pytest.raises(ValueError, match="cdp_endpoint must be a non-empty string or None"):
        BrowserSettings(cdp_endpoint="")

    with pytest.raises(ValueError, match="cdp_endpoint must be a non-empty string or None"):
        BrowserSettings(cdp_endpoint="   ")


def test_protocols_runtime_checkable() -> None:
    class DummyRunner:
        def run(self, command: Sequence[str], settings: ProcessSettings) -> CommandResult:
            return CommandResult(0)

    assert isinstance(DummyRunner(), ProcessRunner)

    class DummyLLM:
        def generate(
            self, prompt: str, *, system: str, settings: LLMSettings, json_mode: bool = False
        ) -> str:
            return "response"

    assert isinstance(DummyLLM(), LLMClient)

    class DummyPage:
        def goto(self, url: str, *, timeout: int | None = None) -> object:
            return None

        def screenshot(self, path: str | Path, *, full_page: bool = True) -> object:
            return None

    assert isinstance(DummyPage(), BrowserPage)

    class DummySession:
        @property
        def page(self) -> BrowserPage:
            return DummyPage()

        def close(self) -> None:
            pass

    assert isinstance(DummySession(), BrowserSession)

    class DummyFactory:
        def open(self, settings: BrowserSettings) -> BrowserSession:
            return DummySession()

    assert isinstance(DummyFactory(), BrowserFactory)
