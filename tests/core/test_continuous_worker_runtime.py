from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from job_application_automation.core.continuous_worker_models import (
    DIRECT_ONCE_EXIT_POLICY,
    SOURCE_ONCE_EXIT_POLICY,
    CycleStatus,
    OnceExitPolicy,
)
from job_application_automation.core.continuous_worker_runtime import (
    WorkerRuntime,
    cycle_event_level,
    run_worker,
)


@dataclass
class RecordingTelemetry:
    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    flush_count: int = 0

    def emit(self, event_name: str, **metadata: object) -> None:
        self.events.append((event_name, metadata))

    def flush(self, timeout: float = 2.0) -> None:
        del timeout
        self.flush_count += 1


def _once_runtime(
    status: CycleStatus,
    policy: OnceExitPolicy,
    telemetry: RecordingTelemetry,
) -> WorkerRuntime:
    return WorkerRuntime(
        provider="greenhouse",
        cycle_stage="test_cycle",
        once=True,
        once_exit_policy=policy,
        telemetry=telemetry,
        run_cycle=lambda: status,
        delay_for=lambda _status: pytest.fail("once mode must not calculate pacing"),
        announce_sleep=lambda _delay, _status: pytest.fail("once mode must not announce sleep"),
        sleep=lambda _delay: pytest.fail("once mode must not sleep"),
        report_interrupt=lambda: pytest.fail("cycle did not interrupt"),
        report_exception=lambda _exc: pytest.fail("cycle did not fail"),
    )


@pytest.mark.parametrize(
    ("policy", "status", "expected_exit"),
    [
        (DIRECT_ONCE_EXIT_POLICY, "confirmed", 0),
        (DIRECT_ONCE_EXIT_POLICY, "refreshed", 0),
        (DIRECT_ONCE_EXIT_POLICY, "no_work", 1),
        (DIRECT_ONCE_EXIT_POLICY, "manual_review", 1),
        (SOURCE_ONCE_EXIT_POLICY, "confirmed", 0),
        (SOURCE_ONCE_EXIT_POLICY, "no_work", 0),
        (SOURCE_ONCE_EXIT_POLICY, "refreshed", 1),
        (SOURCE_ONCE_EXIT_POLICY, "failed", 1),
    ],
)
def test_once_exit_policies_preserve_each_worker_contract(
    policy: OnceExitPolicy,
    status: CycleStatus,
    expected_exit: int,
) -> None:
    telemetry = RecordingTelemetry()

    assert run_worker(_once_runtime(status, policy, telemetry)) == expected_exit
    assert telemetry.flush_count == 1
    assert telemetry.events == [
        (
            "worker_cycle_complete",
            {
                "level": cycle_event_level(status),
                "provider": "greenhouse",
                "stage": "test_cycle",
                "cycle_status": status,
            },
        )
    ]


def test_runtime_converts_cycle_exception_to_failed_once_result() -> None:
    telemetry = RecordingTelemetry()
    reported: list[Exception] = []

    def fail_cycle() -> CycleStatus:
        raise ValueError("bad source")

    runtime = _once_runtime("confirmed", DIRECT_ONCE_EXIT_POLICY, telemetry)
    runtime = WorkerRuntime(
        provider=runtime.provider,
        cycle_stage=runtime.cycle_stage,
        once=runtime.once,
        once_exit_policy=runtime.once_exit_policy,
        telemetry=runtime.telemetry,
        run_cycle=fail_cycle,
        delay_for=runtime.delay_for,
        announce_sleep=runtime.announce_sleep,
        sleep=runtime.sleep,
        report_interrupt=runtime.report_interrupt,
        report_exception=reported.append,
    )

    assert run_worker(runtime) == 1
    assert len(reported) == 1
    assert isinstance(reported[0], ValueError)
    assert telemetry.events[0][0] == "worker_cycle_exception"
    assert telemetry.events[0][1]["error_type"] is ValueError
    assert telemetry.events[1][0] == "worker_cycle_complete"
    assert telemetry.events[1][1]["cycle_status"] == "exception"
    assert telemetry.events[1][1]["level"] == "error"
    assert telemetry.flush_count == 1


def test_runtime_flushes_and_returns_signal_exit_on_cycle_interrupt() -> None:
    telemetry = RecordingTelemetry()
    interrupts: list[bool] = []

    def interrupt_cycle() -> CycleStatus:
        raise KeyboardInterrupt

    runtime = _once_runtime("confirmed", DIRECT_ONCE_EXIT_POLICY, telemetry)
    runtime = WorkerRuntime(
        provider=runtime.provider,
        cycle_stage=runtime.cycle_stage,
        once=runtime.once,
        once_exit_policy=runtime.once_exit_policy,
        telemetry=runtime.telemetry,
        run_cycle=interrupt_cycle,
        delay_for=runtime.delay_for,
        announce_sleep=runtime.announce_sleep,
        sleep=runtime.sleep,
        report_interrupt=lambda: interrupts.append(True),
        report_exception=runtime.report_exception,
    )

    assert run_worker(runtime) == 130
    assert interrupts == [True]
    assert telemetry.events == []
    assert telemetry.flush_count == 1


def test_runtime_delegates_non_once_pacing_and_stop_behavior() -> None:
    telemetry = RecordingTelemetry()
    delays: list[CycleStatus] = []
    announcements: list[tuple[int, CycleStatus]] = []
    sleeps: list[int] = []

    def delay_for(status: CycleStatus) -> int:
        delays.append(status)
        return 17

    def sleep(delay: int) -> bool:
        sleeps.append(delay)
        return False

    runtime = WorkerRuntime(
        provider="ashby",
        cycle_stage="worker_cycle",
        once=False,
        once_exit_policy=DIRECT_ONCE_EXIT_POLICY,
        telemetry=telemetry,
        run_cycle=lambda: "manual_review",
        delay_for=delay_for,
        announce_sleep=lambda delay, status: announcements.append((delay, status)),
        sleep=sleep,
        report_interrupt=lambda: pytest.fail("cycle did not interrupt"),
        report_exception=lambda _exc: pytest.fail("cycle did not fail"),
    )

    assert run_worker(runtime) == 130
    assert delays == ["manual_review"]
    assert announcements == [(17, "manual_review")]
    assert sleeps == [17]
    assert telemetry.events[0][1]["level"] == "warning"
    assert telemetry.flush_count == 1
