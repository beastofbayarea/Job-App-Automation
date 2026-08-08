"""Normalize verified-live application candidates at worker boundaries."""

from __future__ import annotations

# Consolidated sections retain their local imports to keep each former module
# readable and mechanically comparable during the compatibility migration.
# ruff: noqa: E402

from collections.abc import Mapping
from typing import Any

from .foundation import ATS_HOST_MARKERS, detect_ats_job_url
from .foundation import canonical_job_url


SUPPORTED_PLATFORMS = frozenset(ATS_HOST_MARKERS)


def normalize_application_candidate(
    value: Mapping[str, Any],
    *,
    expected_platform: str | None = None,
    require_declared_platform: bool = False,
) -> dict[str, Any] | None:
    """Validate one source record and return its canonical application route.

    ``apply_url`` takes precedence when it points to a supported job form. The
    declared provider is treated as a consistency assertion, while generic
    discovery records such as ``platform=web`` may still route through a valid
    provider-owned apply URL.
    """
    if str(value.get("live_status", "")).strip().lower() != "live":
        return None
    if not all(
        str(value.get(key, "")).strip() for key in ("job_url", "company", "title", "description")
    ):
        return None

    declared_platform = str(value.get("platform", "")).strip().lower()
    if require_declared_platform and declared_platform != expected_platform:
        return None

    application_url = next(
        (
            candidate_url
            for candidate_url in (
                str(value.get("apply_url", "")).strip(),
                str(value.get("job_url", "")).strip(),
            )
            if detect_ats_job_url(candidate_url)
        ),
        "",
    )
    detected_platform = detect_ats_job_url(application_url) if application_url else None
    if detected_platform not in SUPPORTED_PLATFORMS:
        return None
    if expected_platform is not None and detected_platform != expected_platform:
        return None
    if declared_platform in SUPPORTED_PLATFORMS and declared_platform != detected_platform:
        return None

    try:
        canonical_url = canonical_job_url(application_url)
    except ValueError:
        return None

    job = dict(value)
    job["platform"] = detected_platform
    job["_application_url"] = application_url
    job["_canonical_url"] = canonical_url
    return job


def eligible_application_jobs(
    payload: Any,
    *,
    expected_platform: str | None = None,
    require_declared_platform: bool = False,
    input_label: str = "application input",
) -> list[dict[str, Any]]:
    """Return complete, live, provider-consistent candidates without duplicates."""
    if not isinstance(payload, list):
        raise ValueError(f"{input_label} must be a JSON array")
    eligible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in payload:
        if not isinstance(value, Mapping):
            continue
        job = normalize_application_candidate(
            value,
            expected_platform=expected_platform,
            require_declared_platform=require_declared_platform,
        )
        if job is None:
            continue
        canonical_url = str(job["_canonical_url"])
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        eligible.append(job)
    return eligible


def application_url(job: Mapping[str, Any]) -> str:
    """Return the validated provider application URL on a normalized record."""
    return str(job.get("_application_url") or job.get("job_url") or "").strip()


"""Candidate-state selection and confirmed-ledger indexes for ATS workers."""


from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from .foundation import read_json


EXACT_CONFIRMED_STATUS = "SUBMITTED & CONFIRMED"
RESUMABLE_STATUSES = frozenset({"preparing", "documents_ready"})

CandidateT = TypeVar("CandidateT", bound=Mapping[str, Any])
CandidateKey = Callable[[CandidateT], str]
JobIdentity = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class ConfirmedSubmission:
    """One exact-confirmed ledger record normalized for worker coordination."""

    identity: str
    canonical_url: str
    job_url: str
    company: str
    title: str
    applied_at: str


@dataclass(frozen=True, slots=True)
class ExactConfirmedLedgerIndex:
    """Provider-scoped exact-confirmed records keyed by a caller-defined identity."""

    records: Mapping[str, ConfirmedSubmission]

    @property
    def identities(self) -> frozenset[str]:
        return frozenset(self.records)

    def contains(self, identity: str) -> bool:
        return identity in self.records


@dataclass(frozen=True, slots=True)
class CandidateStatePools(Generic[CandidateT]):
    """Resumable candidates first, followed by never-attempted candidates."""

    resumable: tuple[CandidateT, ...]
    fresh: tuple[CandidateT, ...]


def load_exact_confirmed_ledger_index(
    path: Path,
    ats_platform: str,
    *,
    identity_for_url: JobIdentity = canonical_job_url,
) -> ExactConfirmedLedgerIndex:
    """Index only canonical ``SUBMITTED & CONFIRMED`` records for one provider."""
    if not path.is_file():
        return ExactConfirmedLedgerIndex(records={})
    payload: object = read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError("submission log root must be an object")

    records: dict[str, ConfirmedSubmission] = {}
    for value in payload.values():
        if not isinstance(value, Mapping):
            continue
        if str(value.get("status", "")).strip() != EXACT_CONFIRMED_STATUS:
            continue
        if str(value.get("ats", "")).strip().lower() != ats_platform:
            continue
        job_url = str(value.get("job_url", "")).strip()
        try:
            canonical_url = canonical_job_url(job_url)
            identity = identity_for_url(job_url)
        except ValueError:
            continue
        records[identity] = ConfirmedSubmission(
            identity=identity,
            canonical_url=canonical_url,
            job_url=job_url,
            company=str(value.get("company") or ""),
            title=str(value.get("role") or ""),
            applied_at=str(value.get("applied_at") or ""),
        )
    return ExactConfirmedLedgerIndex(records=records)


def partition_candidate_state(
    candidates: Sequence[CandidateT],
    state_records: Mapping[str, object],
    *,
    state_key: CandidateKey[CandidateT],
    identity: CandidateKey[CandidateT],
    confirmed_identities: Collection[str] = (),
    blocked_identities: Collection[str] = (),
) -> CandidateStatePools[CandidateT]:
    """Classify resumable and fresh work while excluding confirmed or peer-owned jobs."""
    confirmed = set(confirmed_identities)
    blocked = set(blocked_identities)
    by_state_key: dict[str, CandidateT] = {}
    for candidate in candidates:
        try:
            candidate_state_key = state_key(candidate)
            candidate_identity = identity(candidate)
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not candidate_state_key
            or candidate_identity in confirmed
            or candidate_identity in blocked
        ):
            continue
        by_state_key[candidate_state_key] = candidate

    resumable: list[CandidateT] = []
    for candidate_state_key, record in state_records.items():
        resumable_candidate = by_state_key.get(candidate_state_key)
        if resumable_candidate is None or not isinstance(record, Mapping):
            continue
        if record.get("status") not in RESUMABLE_STATUSES:
            continue
        resumable.append(resumable_candidate)

    fresh = tuple(
        candidate
        for candidate_state_key, candidate in by_state_key.items()
        if candidate_state_key not in state_records
    )
    return CandidateStatePools(resumable=tuple(resumable), fresh=fresh)


def choose_resumable_or_fresh(
    pools: CandidateStatePools[CandidateT],
    *,
    choice: Callable[[Sequence[CandidateT]], CandidateT],
) -> CandidateT | None:
    """Resume deterministic in-progress work before sampling a fresh candidate."""
    if pools.resumable:
        return pools.resumable[0]
    return choice(pools.fresh) if pools.fresh else None


"""Typed models shared by continuous application workers.

The continuous worker JSON files intentionally remain dictionaries on disk so
older deployments and operational tooling can keep reading them. These types
describe that established wire format without introducing a migration.
"""


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


"""Reusable supervision loop for continuous application workers."""


from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol


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
    stop_after_cycle: Callable[[CycleStatus], bool] | None = None


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
        if runtime.stop_after_cycle is not None and runtime.stop_after_cycle(cycle_status):
            runtime.telemetry.flush()
            return 0

        delay = runtime.delay_for(cycle_status)
        runtime.announce_sleep(delay, cycle_status)
        if not runtime.sleep(delay):
            runtime.telemetry.flush()
            return 130


"""Typed source strategies shared by continuous application workers."""


import importlib.util
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


ATS_PLATFORM_PATTERN = re.compile(r"^[a-z][a-z0-9]*$")


class JsonReader(Protocol):
    def __call__(self, path: Path) -> object: ...


class TrackerReader(Protocol):
    def __call__(self, path: Path) -> Sequence[Mapping[str, Any]]: ...


class AtsDetector(Protocol):
    def __call__(self, url: str) -> str | None: ...


class ModuleFinder(Protocol):
    def __call__(self, module_name: str) -> object | None: ...


@dataclass(frozen=True, slots=True)
class SourceServices:
    """Patchable file and provider operations used by source strategies."""

    read_json: JsonReader
    read_tracker: TrackerReader
    detect_ats: AtsDetector


def eligible_provider_jobs(payload: object, ats_platform: str) -> list[dict[str, Any]]:
    """Normalize one provider's live search records for continuous work."""
    return eligible_application_jobs(
        payload,
        expected_platform=ats_platform,
        require_declared_platform=True,
        input_label=f"continuous {ats_platform} input",
    )


def validate_worker_platform(
    ats_platform: str,
    *,
    find_module: ModuleFinder | None = None,
) -> str:
    """Return an installed provider name accepted by continuous workers."""
    normalized = str(ats_platform).strip().lower()
    if not ATS_PLATFORM_PATTERN.fullmatch(normalized):
        raise ValueError("continuous ATS platform must contain only lowercase letters and digits")
    engine_module = f"job_application_automation.engines.{normalized}"
    module_finder = find_module or (lambda name: importlib.util.find_spec(name))
    if module_finder(engine_module) is None:
        raise ValueError(f"continuous ATS engine is not installed: {normalized}")
    return normalized


def load_source_jobs(
    *,
    source: str,
    ats_platform: str,
    input_path: Path,
    tracker_path: Path | None,
    services: SourceServices,
) -> list[dict[str, Any]]:
    """Load provider-consistent jobs from search JSON or an Excel tracker."""
    if source == "search":
        if not input_path.is_file():
            return []
        return eligible_provider_jobs(services.read_json(input_path), ats_platform)
    if source == "failed-json":
        if not input_path.is_file():
            return []
        payload = services.read_json(input_path)
        if not isinstance(payload, list):
            raise ValueError("failed JSON source must contain a list")
        failed_jobs: list[dict[str, Any]] = []
        for record in payload:
            if not isinstance(record, Mapping):
                continue
            job_url = str(record.get("job_url", "")).strip()
            if services.detect_ats(job_url) != ats_platform:
                continue
            failed_jobs.append(
                {
                    "job_url": job_url,
                    "company": str(record.get("company", "")),
                    "title": str(record.get("role") or record.get("title") or ""),
                    "platform": ats_platform,
                    "prior_status": str(record.get("status", "")),
                    "prior_missing_required": str(record.get("missing_required", "")),
                }
            )
        return failed_jobs
    if source != "tracker":
        raise ValueError(f"unsupported continuous worker source: {source}")
    if tracker_path is None:
        raise ValueError("--tracker is required for tracker source workers")

    eligible: list[dict[str, Any]] = []
    for job in services.read_tracker(tracker_path):
        job_url = str(job.get("url", "")).strip()
        if str(job.get("ats", "")).strip().lower() != ats_platform:
            continue
        if services.detect_ats(job_url) != ats_platform:
            continue
        eligible.append(
            {
                "job_url": job_url,
                "company": str(job.get("company", "")),
                "title": str(job.get("role", "")),
                "platform": ats_platform,
                "tracker_row": int(job.get("row_number", 0)),
            }
        )
    return eligible


"""Persistence and recovery operations for continuous worker state."""


import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from .foundation import atomic_write_text


UTC = timezone.utc
STATE_VERSION = 1


def utc_now_iso() -> str:
    """Return the worker timestamp format used by existing state files."""
    return datetime.now(UTC).isoformat()


def load_worker_state(path: Path, ats_platform: str) -> WorkerState:
    """Load and validate a version-one provider worker state file."""
    if not path.exists():
        return {"version": STATE_VERSION, "jobs": {}}
    payload: Any = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid continuous {ats_platform} state: {path}")
    if payload.get("version") != STATE_VERSION:
        raise ValueError(f"unsupported continuous {ats_platform} state version: {path}")
    return cast(WorkerState, payload)


def save_worker_state(path: Path, state: WorkerState) -> None:
    """Atomically persist state without changing the established schema."""
    state["updated_at"] = utc_now_iso()
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_worker_state_records(path: Path) -> dict[str, Mapping[str, Any]]:
    """Read state records for cross-worker coordination.

    This deliberately accepts the historic unversioned shape used by the
    source-claim reader while the owning worker performs strict version checks.
    """
    if not path.is_file():
        return {}
    payload: Any = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid continuous ATS state: {path}")
    jobs = cast(dict[object, object], payload["jobs"])
    return {str(url): record for url, record in jobs.items() if isinstance(record, Mapping)}


def reconcile_interrupted_submissions(state: WorkerState) -> int:
    """Quarantine attempts that may have submitted before the process stopped."""
    records = state.get("jobs")
    if not isinstance(records, dict):
        raise ValueError("continuous worker state jobs must be an object")
    reconciled = 0
    for record in records.values():
        if not isinstance(record, dict) or record.get("status") != "application_started":
            continue
        record.update(
            {
                "status": "manual_review",
                "stage": "application",
                "result_status": "INTERRUPTED_AFTER_APPLICATION_START",
                "updated_at": utc_now_iso(),
            }
        )
        reconciled += 1
    return reconciled
