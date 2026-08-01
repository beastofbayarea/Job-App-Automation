"""Typed models shared by continuous application workers.

The continuous worker JSON files intentionally remain dictionaries on disk so
older deployments and operational tooling can keep reading them. These types
describe that established wire format without introducing a migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, TypedDict


CycleStatus: TypeAlias = Literal[
    "application_rate_limit",
    "captcha_cooldown",
    "confirmed",
    "exception",
    "failed",
    "manual_review",
    "no_work",
    "possible_spam_cooldown",
    "refresh_failed",
    "refreshed",
]

WorkerJob: TypeAlias = dict[str, Any]
WorkerJobRecord: TypeAlias = dict[str, Any]


class WorkerState(TypedDict, total=False):
    """Version-one worker state as serialized by existing deployments."""

    version: int
    jobs: dict[str, WorkerJobRecord]
    updated_at: str


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Captured result of a bounded child process invocation."""

    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class OnceExitPolicy:
    """Map a completed cycle to the long-standing ``--once`` exit contract."""

    successful_statuses: frozenset[CycleStatus]

    def exit_code(self, status: CycleStatus) -> int:
        """Return zero only for statuses declared successful by this worker."""
        return 0 if status in self.successful_statuses else 1


DIRECT_ONCE_EXIT_POLICY = OnceExitPolicy(
    successful_statuses=frozenset(("confirmed", "refreshed")),
)
SOURCE_ONCE_EXIT_POLICY = OnceExitPolicy(
    successful_statuses=frozenset(("confirmed", "no_work")),
)

