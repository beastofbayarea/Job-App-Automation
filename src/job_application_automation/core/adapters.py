"""Small protocol interfaces for dependencies that perform external work.

Production modules can adapt subprocess, a chosen LLM SDK, or Playwright to
these interfaces.  Tests can inject fakes without importing those packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from collections.abc import Mapping, Sequence

from .exceptions import InputContractError


def _immutable_environment(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise InputContractError("environment must be a mapping")
    copied: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise InputContractError("environment keys must be non-empty strings")
        if not isinstance(item, str):
            raise InputContractError("environment values must be strings")
        copied[key] = item
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class ProcessSettings:
    """Runtime settings passed to an injected process runner."""

    timeout_seconds: int = 300
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int):
            raise InputContractError("timeout_seconds must be an integer")
        if self.timeout_seconds <= 0:
            raise InputContractError("timeout_seconds must be greater than zero")
        if self.cwd is not None:
            object.__setattr__(self, "cwd", Path(self.cwd).expanduser())
        object.__setattr__(self, "environment", _immutable_environment(self.environment))


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured completion data from a child process."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.returncode, bool) or not isinstance(self.returncode, int):
            raise InputContractError("returncode must be an integer")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise InputContractError("stdout and stderr must be strings")


@runtime_checkable
class ProcessRunner(Protocol):
    """Runs a command under supplied, testable settings."""

    def run(self, command: Sequence[str], settings: ProcessSettings) -> CommandResult:
        """Run *command* and capture its result."""


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Provider-neutral controls for text generation calls."""

    model: str
    temperature: float = 0.2
    max_attempts: int = 3
    retry_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise InputContractError("model cannot be empty")
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
            raise InputContractError("temperature must be a number")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise InputContractError("temperature must be between 0 and 2")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise InputContractError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise InputContractError("max_attempts must be at least 1")
        if isinstance(self.retry_delay_seconds, bool) or not isinstance(
            self.retry_delay_seconds, (int, float)
        ):
            raise InputContractError("retry_delay_seconds must be a number")
        if self.retry_delay_seconds < 0:
            raise InputContractError("retry_delay_seconds cannot be negative")
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "temperature", float(self.temperature))
        object.__setattr__(self, "retry_delay_seconds", float(self.retry_delay_seconds))


@runtime_checkable
class LLMClient(Protocol):
    """Generates text without binding callers to a particular SDK."""

    def generate(
        self,
        prompt: str,
        *,
        system: str,
        settings: LLMSettings,
        json_mode: bool = False,
    ) -> str:
        """Generate one response for the supplied prompt."""


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    """Browser configuration shared by live and deterministic fake sessions."""

    headed: bool = False
    timeout_ms: int = 30_000
    cdp_endpoint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.headed, bool):
            raise InputContractError("headed must be a boolean")
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int):
            raise InputContractError("timeout_ms must be an integer")
        if self.timeout_ms <= 0:
            raise InputContractError("timeout_ms must be greater than zero")
        if self.cdp_endpoint is not None:
            if not isinstance(self.cdp_endpoint, str) or not self.cdp_endpoint.strip():
                raise InputContractError("cdp_endpoint must be a non-empty string or None")
            object.__setattr__(self, "cdp_endpoint", self.cdp_endpoint.strip())


@runtime_checkable
class BrowserPage(Protocol):
    """Minimal page capability required by provider adapters."""

    def goto(self, url: str, *, timeout: int | None = None) -> object:
        """Navigate to *url*."""

    def screenshot(self, path: str | Path, *, full_page: bool = True) -> object:
        """Capture a screenshot at *path*."""


@runtime_checkable
class BrowserSession(Protocol):
    """An owned browser page with explicit cleanup."""

    @property
    def page(self) -> BrowserPage:
        """The active page."""

    def close(self) -> None:
        """Release browser resources."""


@runtime_checkable
class BrowserFactory(Protocol):
    """Creates an injectable browser session for a provider adapter."""

    def open(self, settings: BrowserSettings) -> BrowserSession:
        """Open a configured browser session."""
