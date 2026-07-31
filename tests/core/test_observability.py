from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from job_application_automation.core import observability


class FakeScope:
    def __init__(self) -> None:
        self.tags: dict[str, str | int | bool] = {}

    def set_tag(self, key: str, value: str | int | bool) -> None:
        self.tags[key] = value


class FakeSDK:
    def __init__(self, *, fail_capture: bool = False) -> None:
        self.init_kwargs: dict[str, object] = {}
        self.messages: list[tuple[str, str, dict[str, str | int | bool]]] = []
        self.scope = FakeScope()
        self.fail_capture = fail_capture
        self.flush_calls: list[float | None] = []

    def init(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs

    @contextmanager
    def new_scope(self):
        self.scope = FakeScope()
        yield self.scope

    def capture_message(self, message: str, *, level: str) -> None:
        if self.fail_capture:
            raise RuntimeError("candidate@example.test https://secret.test token=secret")
        self.messages.append((message, level, dict(self.scope.tags)))

    def flush(self, timeout: float | None = None) -> None:
        self.flush_calls.append(timeout)


def test_disabled_observability_does_not_import_sdk(monkeypatch) -> None:
    monkeypatch.delenv(observability.SENTRY_DSN_ENV, raising=False)
    with patch.object(observability.importlib, "import_module") as importer:
        telemetry = observability.initialize_observability(worker_kind="continuous_ats")

    assert telemetry.enabled is False
    importer.assert_not_called()


def test_missing_sdk_fails_open(monkeypatch) -> None:
    monkeypatch.setenv(observability.SENTRY_DSN_ENV, "https://public@example.test/1")
    with patch.object(observability.importlib, "import_module", side_effect=ImportError):
        telemetry = observability.initialize_observability(worker_kind="continuous_ats")

    assert telemetry.enabled is False
    telemetry.emit("worker_cycle_exception", error_type=RuntimeError)


def test_initialization_and_events_use_only_privacy_safe_fields(monkeypatch) -> None:
    sdk = FakeSDK()
    monkeypatch.setenv(observability.SENTRY_DSN_ENV, "https://public@example.test/1")
    monkeypatch.setenv(observability.SENTRY_ENVIRONMENT_ENV, "prod / candidate@example.test")
    monkeypatch.setenv(observability.SENTRY_RELEASE_ENV, "release/abc")
    with patch.object(observability.importlib, "import_module", return_value=sdk):
        telemetry = observability.initialize_observability(
            worker_kind="continuous_ats",
            provider="greenhouse",
            worker_id="worker/search",
        )

    assert telemetry.enabled is True
    assert sdk.init_kwargs["send_default_pii"] is False
    assert sdk.init_kwargs["default_integrations"] is False
    assert sdk.init_kwargs["max_breadcrumbs"] == 0
    assert sdk.init_kwargs["traces_sample_rate"] == 0.0
    assert sdk.init_kwargs["profiles_sample_rate"] == 0.0
    assert sdk.init_kwargs["include_local_variables"] is False
    assert sdk.init_kwargs["include_source_context"] is False
    assert sdk.init_kwargs["environment"] == "production"
    assert sdk.init_kwargs["release"] is None

    telemetry.emit(
        "worker_cycle_exception",
        error_type=RuntimeError,
        stage="application",
        cycle_status="exception",
        exit_code=1,
        timed_out=False,
        failure_count=2,
    )
    assert sdk.messages == [
        (
            "worker_cycle_exception",
            "error",
            {
                "worker_kind": "continuous_ats",
                "provider": "greenhouse",
                "stage": "application",
                "cycle_status": "exception",
                "error_type": "RuntimeError",
                "exit_code": 1,
                "timed_out": False,
                "failure_count": 2,
            },
        )
    ]


def test_before_send_removes_sensitive_payloads() -> None:
    event = {
        "event_id": "abc",
        "message": "worker_cycle_exception",
        "level": "error",
        "environment": "prod / candidate@example.test",
        "release": "release/abc",
        "tags": {
            "provider": "greenhouse",
            "worker_kind": "continuous ats",
            "unsafe": "candidate@example.test",
        },
        "exception": {"values": [{"value": "token=secret"}]},
        "request": {"url": "https://secret.test/job"},
        "user": {"email": "candidate@example.test"},
        "contexts": {"runtime": {"name": "private"}},
        "breadcrumbs": [{"message": "private"}],
        "extra": {"path": "C:/private/resume.pdf"},
        "threads": {"values": []},
        "modules": {"secret": "1"},
        "server_name": "candidate-laptop",
    }

    sanitized = observability._sanitize_event(event, {})

    assert sanitized == {
        "event_id": "abc",
        "message": "worker_cycle_exception",
        "level": "error",
        "tags": {"provider": "greenhouse"},
    }


def test_unknown_events_and_transport_failures_are_ignored() -> None:
    sdk = FakeSDK(fail_capture=True)
    telemetry = observability.OperationalTelemetry(sdk)

    telemetry.emit("candidate@example.test")
    telemetry.emit("worker_cycle_exception", error_type=RuntimeError)
    telemetry.flush()

    assert sdk.messages == []
    assert sdk.flush_calls == [2.0]
