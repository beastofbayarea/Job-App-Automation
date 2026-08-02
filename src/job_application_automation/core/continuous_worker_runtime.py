"""Reusable supervision loop for continuous application workers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from .continuous_worker_models import CycleStatus, OnceExitPolicy


EventLevel: TypeAlias = Literal["info", "warning", "error"]


class WorkerTelemetry(Protocol):
    """Telemetry operations required by the worker supervisor."""

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
    ) -> None: ...

    def flush(self, timeout: float = 2.0) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Callbacks and policies needed to supervise one worker implementation."""

    provider: str
    cycle_stage: str
    once: bool
    once_exit_policy: OnceExitPolicy
    telemetry: WorkerTelemetry
    run_cycle: Callable[[], CycleStatus]
    delay_for: Callable[[CycleStatus], int]
    announce_sleep: Callable[[int, CycleStatus], None]
    sleep: Callable[[int], bool]
    report_interrupt: Callable[[], None]
    report_exception: Callable[[Exception], None]


def cycle_event_level(status: CycleStatus) -> EventLevel:
    """Map stable worker outcomes to their established telemetry severity."""
    if status in {"confirmed", "no_work", "refreshed"}:
        return "info"
    if status in {
        "application_rate_limit",
        "captcha_cooldown",
        "manual_review",
        "possible_spam_cooldown",
    }:
        return "warning"
    return "error"


def run_worker(runtime: WorkerRuntime) -> int:
    """Run cycles until ``--once`` completes or supervision is interrupted."""
    while True:
        try:
            cycle_status = runtime.run_cycle()
        except KeyboardInterrupt:
            runtime.report_interrupt()
            runtime.telemetry.flush()
            return 130
        except Exception as exc:
            runtime.report_exception(exc)
            runtime.telemetry.emit(
                "worker_cycle_exception",
                provider=runtime.provider,
                stage=runtime.cycle_stage,
                cycle_status="exception",
                error_type=type(exc),
            )
            cycle_status = "exception"

        runtime.telemetry.emit(
            "worker_cycle_complete",
            level=cycle_event_level(cycle_status),
            provider=runtime.provider,
            stage=runtime.cycle_stage,
            cycle_status=cycle_status,
        )
        if runtime.once:
            runtime.telemetry.flush()
            return runtime.once_exit_policy.exit_code(cycle_status)

        delay = runtime.delay_for(cycle_status)
        runtime.announce_sleep(delay, cycle_status)
        if not runtime.sleep(delay):
            runtime.telemetry.flush()
            return 130
