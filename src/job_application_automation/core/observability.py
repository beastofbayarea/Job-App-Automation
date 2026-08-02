"""Optional, privacy-safe operational telemetry for unattended workers.

The adapter is disabled unless ``SENTRY_DSN`` is present. It deliberately sends
only fixed event names and allow-listed operational tags. Candidate data,
URLs, paths, exception messages, logs, breadcrumbs, and stack traces are never
included.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from .runtime_config import RUNTIME_CONFIG


SENTRY_DSN_ENV = "SENTRY_DSN"
SENTRY_ENVIRONMENT_ENV = "SENTRY_ENVIRONMENT"
SENTRY_RELEASE_ENV = "SENTRY_RELEASE"
DEFAULT_ENVIRONMENT = RUNTIME_CONFIG.observability.default_environment
DEFAULT_FLUSH_TIMEOUT_SECONDS = RUNTIME_CONFIG.observability.flush_timeout_seconds

EVENT_NAMES = frozenset(
    {
        "browser_session_failed",
        "document_archive_failed",
        "document_generation_failed",
        "ledger_persist_failed",
        "source_context_failed",
        "worker_cycle_complete",
        "worker_cycle_exception",
    }
)
TAG_KEYS = frozenset(
    {
        "cycle_status",
        "error_type",
        "exit_code",
        "failure_count",
        "provider",
        "stage",
        "timed_out",
        "worker_id",
        "worker_kind",
    }
)
LEVELS = frozenset({"debug", "info", "warning", "error", "fatal"})
_SAFE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.:-]+")
_MAX_TOKEN_LENGTH = 80


class _Scope(Protocol):
    def set_tag(self, key: str, value: str | int | bool) -> None: ...


class _SentrySDK(Protocol):
    def init(self, **kwargs: Any) -> Any: ...

    def new_scope(self) -> AbstractContextManager[_Scope]: ...

    def capture_message(self, message: str, *, level: str) -> Any: ...

    def flush(self, timeout: float | None = None) -> Any: ...


def _safe_token(value: object) -> str:
    """Accept a bounded operational token or reject the entire free-form value."""
    normalized = str(value).strip()
    if len(normalized) > _MAX_TOKEN_LENGTH or not _SAFE_TOKEN_PATTERN.fullmatch(normalized):
        return ""
    return normalized


def _safe_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _sanitize_event(event: Mapping[str, Any], _hint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reduce a Sentry event to fixed messages and allow-listed metadata."""
    raw_message = event.get("message")
    if isinstance(raw_message, Mapping):
        message = str(raw_message.get("formatted", ""))
    else:
        message = str(raw_message or "")
    if message not in EVENT_NAMES:
        return None

    sanitized: dict[str, Any] = {"message": message}
    for key in ("event_id", "timestamp", "platform", "sdk"):
        if key in event:
            sanitized[key] = event[key]

    level = str(event.get("level", "error")).lower()
    sanitized["level"] = level if level in LEVELS else "error"
    for key in ("environment", "release"):
        token = _safe_token(event.get(key, ""))
        if token:
            sanitized[key] = token

    raw_tags = event.get("tags")
    tags: dict[str, str | int | bool] = {}
    if isinstance(raw_tags, Mapping):
        for key in TAG_KEYS:
            value = raw_tags.get(key)
            if isinstance(value, bool):
                tags[key] = value
            elif isinstance(value, int):
                tags[key] = value
            elif value is not None:
                token = _safe_token(value)
                if token:
                    tags[key] = token
    if tags:
        sanitized["tags"] = tags
    return sanitized


class OperationalTelemetry:
    """Fail-open wrapper around the optional Sentry SDK."""

    def __init__(
        self,
        sdk: _SentrySDK | None = None,
        *,
        base_tags: Mapping[str, str] | None = None,
    ) -> None:
        self._sdk = sdk
        self._base_tags = dict(base_tags or {})

    @property
    def enabled(self) -> bool:
        return self._sdk is not None

    def emit(
        self,
        event_name: str,
        *,
        level: str = "error",
        provider: str | None = None,
        stage: str | None = None,
        cycle_status: str | None = None,
        error_type: type[BaseException] | str | None = None,
        exit_code: int | None = None,
        timed_out: bool | None = None,
        failure_count: int | None = None,
    ) -> None:
        if self._sdk is None or event_name not in EVENT_NAMES:
            return
        tags: dict[str, str | int | bool] = dict(self._base_tags)
        text_tags: tuple[tuple[str, str | None], ...] = (
            ("provider", provider),
            ("stage", stage),
            ("cycle_status", cycle_status),
        )
        for key, value in text_tags:
            token = _safe_token(value or "")
            if token:
                tags[key] = token
        if error_type is not None:
            error_name = error_type.__name__ if isinstance(error_type, type) else error_type
            token = _safe_token(error_name)
            if token:
                tags["error_type"] = token
        safe_exit_code = _safe_integer(exit_code)
        if safe_exit_code is not None:
            tags["exit_code"] = safe_exit_code
        if isinstance(timed_out, bool):
            tags["timed_out"] = timed_out
        safe_failure_count = _safe_integer(failure_count)
        if safe_failure_count is not None:
            tags["failure_count"] = max(0, safe_failure_count)
        safe_level = level if level in LEVELS else "error"
        try:
            with self._sdk.new_scope() as scope:
                for tag_key, tag_value in tags.items():
                    scope.set_tag(tag_key, tag_value)
                self._sdk.capture_message(event_name, level=safe_level)
        except Exception:
            # Telemetry must never alter worker behavior or exit status.
            return

    def flush(self, timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS) -> None:
        if self._sdk is None:
            return
        try:
            self._sdk.flush(timeout=timeout)
        except Exception:
            return


NOOP_TELEMETRY = OperationalTelemetry()


def initialize_observability(
    *,
    worker_kind: str,
    provider: str | None = None,
    worker_id: str | None = None,
) -> OperationalTelemetry:
    """Initialize opt-in Sentry telemetry without exposing application data."""
    dsn = os.environ.get(SENTRY_DSN_ENV, "").strip()
    if not dsn:
        return NOOP_TELEMETRY
    try:
        sdk = importlib.import_module("sentry_sdk")
    except (ImportError, OSError):
        return NOOP_TELEMETRY

    base_tags: dict[str, str] = {}
    for key, value in (
        ("worker_kind", worker_kind),
        ("provider", provider),
        ("worker_id", worker_id),
    ):
        token = _safe_token(value or "")
        if token:
            base_tags[key] = token
    environment = _safe_token(os.environ.get(SENTRY_ENVIRONMENT_ENV, DEFAULT_ENVIRONMENT))
    release = _safe_token(os.environ.get(SENTRY_RELEASE_ENV, ""))
    try:
        sdk.init(
            dsn=dsn,
            environment=environment or DEFAULT_ENVIRONMENT,
            release=release or None,
            send_default_pii=False,
            default_integrations=False,
            integrations=[],
            max_breadcrumbs=0,
            traces_sample_rate=0.0,
            profiles_sample_rate=0.0,
            include_local_variables=False,
            include_source_context=False,
            attach_stacktrace=False,
            before_send=_sanitize_event,
        )
    except Exception:
        return NOOP_TELEMETRY
    return OperationalTelemetry(sdk, base_tags=base_tags)
