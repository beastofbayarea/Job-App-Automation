"""Persistent active-job backlog shared by search and application workers."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

from ..core.artifacts import interprocess_file_lock, read_json, write_json
from ..core.identity import canonical_job_url
from .models import Job


UTC = timezone.utc
BACKLOG_VERSION = 1
CONFIRMED_STATUS = "SUBMITTED & CONFIRMED"


@dataclass
class BacklogEntry:
    """One active job plus discovery timestamps retained across searches."""

    job: Job
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class BacklogUpdate:
    """Summary of one atomic backlog reconciliation."""

    loaded: int
    candidates: int
    removed_confirmed: int
    removed_closed: int
    retained: int


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _boolean(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def _scalar(value: object, default: int | str = "") -> int | str:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, str)):
        return value
    return default


def _ashby_url_proves_provider_identity(job: Job) -> bool:
    """Recognize an exact Ashby record even when its source omitted trust flags."""
    if job.platform.casefold() != "ashby" or not job.board_token or not job.platform_job_id:
        return False
    for value in (job.job_url, job.apply_url):
        parsed = urlparse(value)
        if (parsed.hostname or "").casefold() != "jobs.ashbyhq.com":
            continue
        segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 2
            and segments[0].casefold() == job.board_token.casefold()
            and segments[1].casefold() == job.platform_job_id.casefold()
        ):
            return True
    return False


def _normalize_exact_ashby_identity(job: Job) -> None:
    if not job.provider_id_trusted and _ashby_url_proves_provider_identity(job):
        job.provider_id_trusted = True
        job.url_is_record_specific = True


def _prefer_current_ashby_metadata(current: Job, incoming: Job) -> bool:
    """Keep authoritative Ashby API metadata over duplicate page-discovery rows."""
    if not (
        _ashby_url_proves_provider_identity(current)
        and _ashby_url_proves_provider_identity(incoming)
    ):
        return False
    source_rank = {
        # Both are provider-authoritative. Equal rank preserves the normal
        # incoming-wins refresh behavior between current feed observations.
        "ashby_board_api": 2,
        "ashby_public_board": 2,
    }
    current_rank = source_rank.get(
        current.live_check_source.casefold(),
        1 if current.provider_id_trusted else 0,
    )
    incoming_rank = source_rank.get(
        incoming.live_check_source.casefold(),
        1 if incoming.provider_id_trusted else 0,
    )
    return current_rank > incoming_rank


def job_from_mapping(value: Mapping[str, object]) -> Job:
    """Rebuild the public, liveness-capable portion of a serialized job."""
    required = {
        field_name: _text(value.get(field_name))
        for field_name in ("platform", "company", "title", "job_url")
    }
    missing = [field_name for field_name, field_value in required.items() if not field_value]
    if missing:
        raise ValueError(f"backlog job is missing required fields: {', '.join(missing)}")
    job = Job(
        platform=required["platform"].casefold(),
        company=required["company"],
        title=required["title"],
        posted_at=_text(value.get("posted_at")),
        days_old=_scalar(value.get("days_old")),
        location=_text(value.get("location")),
        workplace_type=_text(value.get("workplace_type")),
        employment_type=_text(value.get("employment_type")),
        department=_text(value.get("department")),
        team=_text(value.get("team")),
        salary=_text(value.get("salary")),
        job_url=required["job_url"],
        apply_url=_text(value.get("apply_url")) or required["job_url"],
        board_token=_text(value.get("board_token")),
        date_source=_text(value.get("date_source")),
        match_reason=_text(value.get("match_reason")),
        description="",
        platform_job_id=_text(value.get("platform_job_id")),
        board_region=_text(value.get("board_region"), "global") or "global",
        provider_id_trusted=_boolean(value.get("provider_id_trusted"), False),
        source_identity=_text(value.get("source_identity")),
        url_is_record_specific=_boolean(value.get("url_is_record_specific"), True),
        live_status=_text(value.get("live_status"), "unknown") or "unknown",
        live_checked_at=_text(value.get("live_checked_at")),
        live_check_source=_text(value.get("live_check_source")),
        live_check_http_status=_scalar(value.get("live_check_http_status")),
        live_check_final_url=_text(value.get("live_check_final_url")),
        live_check_reason=_text(value.get("live_check_reason")),
        unique_id=_text(value.get("unique_id")),
    )
    if not job.provider_id_trusted and _ashby_url_proves_provider_identity(job):
        # Older CSV/JSON-LD artifacts did not serialize provider trust. The
        # exact board-token/job-id URL is itself record-specific evidence and
        # lets the migrated row merge with the authoritative Ashby API row.
        _normalize_exact_ashby_identity(job)
    return job


def _entry_from_mapping(value: Mapping[str, object], *, fallback_seen_at: str) -> BacklogEntry:
    first_seen_at = _text(value.get("first_seen_at")) or fallback_seen_at
    last_seen_at = _text(value.get("last_seen_at")) or first_seen_at
    return BacklogEntry(
        job=job_from_mapping(value),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


def _entry_payload(entry: BacklogEntry) -> dict[str, object]:
    return {
        **entry.job.to_csv_row(),
        "board_region": entry.job.board_region,
        "provider_id_trusted": entry.job.provider_id_trusted,
        "source_identity": entry.job.source_identity,
        "url_is_record_specific": entry.job.url_is_record_specific,
        "unique_id": entry.job.unique_id,
        "first_seen_at": entry.first_seen_at,
        "last_seen_at": entry.last_seen_at,
    }


def canonical_job_aliases(job: Job) -> set[str]:
    """Return ledger-compatible identities for both listing and apply URLs."""
    if not (
        job.url_is_record_specific
        or job.provider_id_trusted
        or _ashby_url_proves_provider_identity(job)
    ):
        # A multi-record JSON-LD page can give several jobs the same generic
        # careers-page URL. Only its scoped source identity can distinguish
        # those records; a URL match must never merge or delete every sibling.
        return set()
    aliases: set[str] = set()
    for value in (job.job_url, job.apply_url):
        if not value:
            continue
        try:
            aliases.add(canonical_job_url(value))
        except ValueError:
            continue
    return aliases


def _provider_identity(job: Job) -> str:
    if not (
        (job.provider_id_trusted or _ashby_url_proves_provider_identity(job))
        and job.platform
        and job.board_token
        and job.platform_job_id
    ):
        return ""
    return (
        f"provider:{job.platform.casefold()}:{job.board_region.casefold()}:"
        f"{job.board_token.casefold()}:{job.platform_job_id}"
    )


def _identity_keys(job: Job) -> set[str]:
    keys = {f"url:{alias}" for alias in canonical_job_aliases(job)}
    provider_identity = _provider_identity(job)
    if provider_identity:
        keys.add(provider_identity)
    if job.source_identity:
        keys.add(f"source:{job.source_identity}")
    if job.unique_id and ":" in job.unique_id:
        keys.add(f"unique:{job.unique_id}")
    return keys


def _earliest(left: str, right: str) -> str:
    values = [value for value in (left, right) if value]
    return min(values) if values else ""


def _latest(left: str, right: str) -> str:
    values = [value for value in (left, right) if value]
    return max(values) if values else ""


def _timestamp(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _observation_time(entry: BacklogEntry) -> datetime:
    return max(
        _timestamp(entry.job.live_checked_at),
        _timestamp(entry.last_seen_at),
        _timestamp(entry.first_seen_at),
    )


def merge_entries(entries: Iterable[BacklogEntry]) -> list[BacklogEntry]:
    """Deduplicate entries while preferring the newest supplied job metadata."""
    merged: list[BacklogEntry | None] = []
    identity_index: dict[str, int] = {}
    for incoming in entries:
        keys = _identity_keys(incoming.job)
        matching = sorted({identity_index[key] for key in keys if key in identity_index})
        if not matching:
            index = len(merged)
            _normalize_exact_ashby_identity(incoming.job)
            merged.append(incoming)
        else:
            index = matching[0]
            current = merged[index]
            if current is None:
                raise RuntimeError("backlog identity index referenced an empty entry")
            selected_job = (
                current.job
                if _prefer_current_ashby_metadata(current.job, incoming.job)
                else incoming.job
            )
            _normalize_exact_ashby_identity(selected_job)
            incoming = BacklogEntry(
                job=selected_job,
                first_seen_at=_earliest(current.first_seen_at, incoming.first_seen_at),
                last_seen_at=_latest(current.last_seen_at, incoming.last_seen_at),
            )
            for duplicate_index in matching[1:]:
                duplicate = merged[duplicate_index]
                if duplicate is None:
                    continue
                incoming.first_seen_at = _earliest(incoming.first_seen_at, duplicate.first_seen_at)
                incoming.last_seen_at = _latest(incoming.last_seen_at, duplicate.last_seen_at)
                merged[duplicate_index] = None
                for identity, existing_index in list(identity_index.items()):
                    if existing_index == duplicate_index:
                        identity_index[identity] = index
            merged[index] = incoming
        for key in _identity_keys(incoming.job):
            identity_index[key] = index
    return [entry for entry in merged if entry is not None]


def load_backlog(path: Path) -> list[BacklogEntry]:
    """Load and validate the active backlog, returning an empty list if absent."""
    if not path.exists():
        return []
    payload = read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError("job backlog root must be an object")
    if payload.get("version") != BACKLOG_VERSION:
        raise ValueError(f"unsupported job backlog version: {payload.get('version')!r}")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("job backlog jobs must be an array")
    fallback_seen_at = _text(payload.get("updated_at"))
    entries: list[BacklogEntry] = []
    for index, value in enumerate(raw_jobs):
        if not isinstance(value, Mapping):
            raise ValueError(f"job backlog entry {index} must be an object")
        entries.append(_entry_from_mapping(value, fallback_seen_at=fallback_seen_at))
    return merge_entries(entries)


def _write_backlog(path: Path, entries: Sequence[BacklogEntry], *, updated_at: str) -> None:
    ordered = sorted(
        entries,
        key=lambda entry: (
            entry.job.platform.casefold(),
            entry.job.company.casefold(),
            entry.job.title.casefold(),
            min(canonical_job_aliases(entry.job), default=entry.job.job_url),
        ),
    )
    write_json(
        path,
        {
            "version": BACKLOG_VERSION,
            "updated_at": updated_at,
            "jobs": [_entry_payload(entry) for entry in ordered],
        },
        indent=2,
        sort_keys=False,
    )


def prepare_candidates(
    existing: Sequence[BacklogEntry],
    discovered: Sequence[Job],
    *,
    now: datetime,
) -> list[BacklogEntry]:
    """Merge newly discovered jobs into the persistent candidates to recheck."""
    seen_at = _now_iso(now)
    additions = [
        BacklogEntry(job=job, first_seen_at=seen_at, last_seen_at=seen_at) for job in discovered
    ]
    return merge_entries([*existing, *additions])


def load_confirmed_urls(paths: Iterable[Path]) -> set[str]:
    """Read only exact confirmed-submission evidence from permanent ledgers."""
    confirmed: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        payload = read_json(path)
        if not isinstance(payload, Mapping):
            raise ValueError(f"submission ledger root must be an object: {path}")
        raw_entries = payload.get("jobs", payload)
        entries: Iterable[object]
        if isinstance(raw_entries, Mapping):
            entries = raw_entries.values()
        elif isinstance(raw_entries, list):
            entries = raw_entries
        else:
            continue
        for value in entries:
            if not isinstance(value, Mapping):
                continue
            if _text(value.get("status")) != CONFIRMED_STATUS:
                continue
            job_url = _text(value.get("job_url") or value.get("url"))
            try:
                confirmed.add(canonical_job_url(job_url))
            except ValueError:
                continue
    return confirmed


def _matches_confirmed(job: Job, confirmed_urls: set[str]) -> bool:
    return bool(canonical_job_aliases(job).intersection(confirmed_urls))


def reconcile_backlog(
    path: Path,
    candidates: Sequence[BacklogEntry],
    *,
    admitted_jobs: Sequence[Job],
    submission_logs: Iterable[Path],
    now: datetime,
) -> BacklogUpdate:
    """Atomically merge, prune, and replace the active-only backlog.

    ``candidates`` can include a snapshot read before slow liveness checks.
    Only jobs discovered or migrated by this run may be admitted when they are
    no longer on disk. This prevents a stale concurrent worker from
    resurrecting a job another worker conclusively removed.
    """
    updated_at = _now_iso(now)
    with interprocess_file_lock(path):
        disk_entries = load_backlog(path)
        confirmed_urls = load_confirmed_urls(submission_logs)
        disk_identities = {
            identity for entry in disk_entries for identity in _identity_keys(entry.job)
        }
        disk_observations: dict[str, datetime] = {}
        for entry in disk_entries:
            observation = _observation_time(entry)
            for identity in _identity_keys(entry.job):
                disk_observations[identity] = max(
                    observation,
                    disk_observations.get(identity, datetime.min.replace(tzinfo=UTC)),
                )
        admitted_identities = {
            identity for job in admitted_jobs for identity in _identity_keys(job)
        }
        applicable_candidates: list[BacklogEntry] = []
        for entry in candidates:
            identities = _identity_keys(entry.job)
            matching_disk = identities.intersection(disk_identities)
            if matching_disk:
                current_observation = max(disk_observations[identity] for identity in matching_disk)
                if _observation_time(entry) < current_observation:
                    continue
                applicable_candidates.append(entry)
            elif identities.intersection(admitted_identities):
                applicable_candidates.append(entry)
        combined = merge_entries([*disk_entries, *applicable_candidates])
        retained: list[BacklogEntry] = []
        removed_confirmed = 0
        removed_closed = 0
        for entry in combined:
            if _matches_confirmed(entry.job, confirmed_urls):
                removed_confirmed += 1
                continue
            if entry.job.live_status == "closed":
                removed_closed += 1
                continue
            retained.append(entry)
        _write_backlog(path, retained, updated_at=updated_at)
    return BacklogUpdate(
        loaded=len(disk_entries),
        candidates=len(candidates),
        removed_confirmed=removed_confirmed,
        removed_closed=removed_closed,
        retained=len(retained),
    )


def remove_confirmed_job(path: Path, job_url: str) -> bool:
    """Remove one ledger-confirmed URL without racing another backlog writer."""
    if not path.exists():
        return False
    canonical = canonical_job_url(job_url)
    with interprocess_file_lock(path):
        entries = load_backlog(path)
        retained = [entry for entry in entries if canonical not in canonical_job_aliases(entry.job)]
        if len(retained) == len(entries):
            return False
        _write_backlog(path, retained, updated_at=_now_iso())
    return True


def load_legacy_jobs(path: Path) -> list[Job]:
    """Read safe job metadata from a former CSV, JSON array, or state object."""
    if not path.exists():
        return []
    if path.suffix.casefold() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            values: Iterable[object] = list(csv.DictReader(stream))
    else:
        payload = read_json(path)
        if isinstance(payload, list):
            values = payload
        elif isinstance(payload, Mapping) and isinstance(payload.get("jobs"), Mapping):
            values = payload["jobs"].values()
        else:
            return []
    jobs: list[Job] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        try:
            jobs.append(job_from_mapping(value))
        except ValueError:
            continue
    return jobs
