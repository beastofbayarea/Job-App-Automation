"""Shared provider contracts, utilities, registry, and adapter exports."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import requests

from .. import support as _search_jsonld
from ..support import Board, Job, SearchCandidate, clean_whitespace

"""Typed contracts shared by public job-board provider adapters."""


@dataclass(frozen=True)
class ProviderUrl:
    """Normalized URL components consumed by provider recognition rules."""

    raw_url: str
    host: str
    parts: tuple[str, ...]
    query: Mapping[str, tuple[str, ...]]

    @classmethod
    def parse(cls, raw_url: str) -> ProviderUrl:
        parsed = urlparse(raw_url)
        return cls(
            raw_url=raw_url,
            host=parsed.netloc.lower().split(":", 1)[0],
            parts=tuple(unquote(part) for part in parsed.path.split("/") if part),
            query={key: tuple(values) for key, values in parse_qs(parsed.query).items()},
        )


class JobCriteria(Protocol):
    """Filtering behavior required by provider feed normalizers."""

    @property
    def role_terms(self) -> tuple[str, ...]: ...

    @property
    def ai_terms(self) -> tuple[str, ...]: ...

    @property
    def exclude_terms(self) -> tuple[str, ...]: ...

    @property
    def location_terms(self) -> tuple[str, ...]: ...

    @property
    def days(self) -> int: ...

    @property
    def include_unknown_dates(self) -> bool: ...

    @property
    def posted_since(self) -> date | None: ...

    @property
    def posted_until(self) -> date | None: ...

    @property
    def match_mode(self) -> str: ...

    def matches_job(
        self,
        *,
        title: str,
        description: str,
        location: str,
        workplace_type: str = "",
    ) -> str | None: ...

    def includes_posted_at(self, posted_at: datetime | None, *, now: datetime) -> bool: ...


@dataclass(frozen=True)
class FetchContext:
    """Transport and filtering inputs shared by every provider adapter."""

    criteria: JobCriteria
    now: datetime
    timeout: float
    delay: float
    page_limits: Mapping[str, int]

    def __post_init__(self) -> None:
        normalized: dict[str, int] = {}
        for platform, maximum in self.page_limits.items():
            if (
                not platform
                or not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or maximum < 0
            ):
                raise ValueError("provider page limits must be non-negative integers")
            normalized[platform.lower()] = maximum
        object.__setattr__(self, "page_limits", MappingProxyType(normalized))

    def max_pages_for(self, platform: str) -> int:
        """Return a provider's optional pagination cap; zero means unlimited."""
        return self.page_limits.get(platform.lower(), 0)


class JsonGetter(Protocol):
    def __call__(
        self,
        session: requests.Session,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> Any: ...


class JsonLdJobScraper(Protocol):
    def __call__(
        self,
        session: requests.Session,
        candidate: SearchCandidate,
        *,
        timeout: float,
        now: datetime,
        criteria: JobCriteria,
    ) -> list[Job]: ...


@dataclass(frozen=True)
class FetchServices:
    """Patchable facade services used by extracted feed implementations."""

    get_json: JsonGetter
    scrape_jsonld_jobs: JsonLdJobScraper
    canonical_url: Callable[[str], str]
    sleep: Callable[[float], None]
    logger: logging.Logger


class ResponseGetter(Protocol):
    def __call__(
        self,
        session: requests.Session,
        url: str,
        *,
        timeout: float,
        accept: str = "application/json,text/html;q=0.9,*/*;q=0.8",
    ) -> requests.Response | None: ...


class LiveStatusSetter(Protocol):
    def __call__(
        self,
        job: Job,
        *,
        status: str,
        source: str,
        now: datetime,
        reason: str,
        http_status: int | str = "",
        final_url: str = "",
    ) -> None: ...


@dataclass(frozen=True)
class LivenessServices:
    """Patchable facade services used by provider liveness checks."""

    response_or_none: ResponseGetter
    set_live_status: LiveStatusSetter


ProviderFetcher = Callable[[requests.Session, Board, FetchContext], list[Job]]


"""Provider-neutral normalization, transport, and liveness helpers."""


UTC = timezone.utc


def strip_html(value: Any) -> str:
    """Convert an optional HTML fragment to normalized plain text."""
    return _search_jsonld.strip_html(value)


def mapping_text(value: Any, key: str = "name") -> str:
    """Safely read a text field from an optional provider response object."""
    return clean_whitespace(value.get(key)) if isinstance(value, dict) else ""


def mapping_items(value: Any) -> list[dict[str, Any]]:
    """Return only mappings from an optional list-like API field."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def prettify_slug(slug: str) -> str:
    value = unquote(slug).replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in value.split()) or slug


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float,
) -> Any:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        content_type = response.headers.get("Content-Type", "unknown")
        raise RuntimeError(f"Expected JSON from {response.url}, received {content_type}") from exc


def parse_datetime(value: Any) -> datetime | None:
    """Parse an epoch number, ISO 8601, or RFC 2822 timestamp into UTC."""
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if abs(number) > 10_000_000_000:
            number /= 1000.0
        try:
            dt = datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            try:
                return parse_datetime(float(raw))
            except ValueError:
                return None

        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_expiry_datetime(value: Any) -> datetime | None:
    """Parse an expiration value, treating a calendar date as inclusive."""
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def iso_or_blank(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def days_old(dt: datetime | None, now: datetime) -> int | str:
    if dt is None:
        return ""
    seconds = max(0.0, (now - dt).total_seconds())
    return int(seconds // 86400)


def canonical_url(url: str) -> str:
    """Build the established final-result URL identity key."""
    parsed = urlparse(url)
    path = re.sub(r"/(?:apply|application)/?$", "", parsed.path, flags=re.IGNORECASE)
    path = path.rstrip("/") or "/"
    query = parsed.query if "greenhouse.io/embed/" in url.lower() else ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def set_live_status(
    job: Job,
    *,
    status: str,
    source: str,
    now: datetime,
    reason: str,
    http_status: int | str = "",
    final_url: str = "",
) -> None:
    job.live_status = status
    job.live_checked_at = iso_or_blank(now)
    job.live_check_source = source
    job.live_check_reason = reason
    job.live_check_http_status = http_status
    job.live_check_final_url = final_url


def response_or_none(
    session: requests.Session,
    url: str,
    *,
    timeout: float,
    accept: str = "application/json,text/html;q=0.9,*/*;q=0.8",
) -> requests.Response | None:
    try:
        return session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"Accept": accept},
        )
    except requests.RequestException:
        return None


"""Typed registry and dispatch helpers for public ATS provider adapters."""


from . import ashby, greenhouse, lever, smartrecruiters, workable  # noqa: E402


class FetchImplementation(Protocol):
    def __call__(
        self,
        session: requests.Session,
        board: Board,
        context: FetchContext,
        *,
        services: FetchServices,
    ) -> list[Job]: ...


class SingleVerifier(Protocol):
    def __call__(
        self,
        session: requests.Session,
        job: Job,
        *,
        timeout: float,
        now: datetime,
        services: LivenessServices,
    ) -> None: ...


class BatchVerifier(Protocol):
    def __call__(
        self,
        session: requests.Session,
        jobs: Sequence[Job],
        *,
        timeout: float,
        now: datetime,
        services: LivenessServices,
    ) -> None: ...


class UrlMatcher(Protocol):
    def __call__(self, url: ProviderUrl) -> bool: ...


class BoardRecognizer(Protocol):
    def __call__(self, url: ProviderUrl) -> Board | None: ...


@dataclass(frozen=True)
class ProviderAdapter:
    """One provider's URL, feed, and liveness behavior."""

    platform: str
    matches_url: UrlMatcher
    board_from_url: BoardRecognizer
    looks_like_job_url: UrlMatcher
    fetch: FetchImplementation
    verify_one: SingleVerifier | None = None
    verify_many: BatchVerifier | None = None


PROVIDER_ADAPTERS: dict[str, ProviderAdapter] = {
    "greenhouse": ProviderAdapter(
        platform="greenhouse",
        matches_url=greenhouse.matches_url,
        board_from_url=greenhouse.board_from_url,
        looks_like_job_url=greenhouse.looks_like_job_url,
        fetch=greenhouse.fetch_jobs,
        verify_one=greenhouse.verify_job_live,
    ),
    "lever": ProviderAdapter(
        platform="lever",
        matches_url=lever.matches_url,
        board_from_url=lever.board_from_url,
        looks_like_job_url=lever.looks_like_job_url,
        fetch=lever.fetch_jobs,
        verify_one=lever.verify_job_live,
    ),
    "ashby": ProviderAdapter(
        platform="ashby",
        matches_url=ashby.matches_url,
        board_from_url=ashby.board_from_url,
        looks_like_job_url=ashby.looks_like_job_url,
        fetch=ashby.fetch_jobs,
        verify_many=ashby.verify_jobs_live,
    ),
    "smartrecruiters": ProviderAdapter(
        platform="smartrecruiters",
        matches_url=smartrecruiters.matches_url,
        board_from_url=smartrecruiters.board_from_url,
        looks_like_job_url=smartrecruiters.looks_like_job_url,
        fetch=smartrecruiters.fetch_jobs,
        verify_one=smartrecruiters.verify_job_live,
    ),
    "workable": ProviderAdapter(
        platform="workable",
        matches_url=workable.matches_url,
        board_from_url=workable.board_from_url,
        looks_like_job_url=workable.looks_like_job_url,
        fetch=workable.fetch_jobs,
        verify_many=workable.verify_jobs_live,
    ),
}


def _has_host_suffix(host: str, suffixes: Sequence[str]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def board_from_url(raw_url: str, *, generic_host_suffixes: Sequence[str]) -> Board | None:
    """Resolve a URL through provider-owned recognition rules."""
    url = ProviderUrl.parse(raw_url)
    for adapter in PROVIDER_ADAPTERS.values():
        if adapter.matches_url(url):
            return adapter.board_from_url(url)
    if _has_host_suffix(url.host, generic_host_suffixes):
        return Board("web", url.host, "global")
    return None


def looks_like_job_url(raw_url: str, *, generic_host_suffixes: Sequence[str]) -> bool:
    """Resolve provider-specific job-path rules from the same adapter registry."""
    url = ProviderUrl.parse(raw_url)
    for adapter in PROVIDER_ADAPTERS.values():
        if adapter.matches_url(url):
            return adapter.looks_like_job_url(url)
    return _has_host_suffix(url.host, generic_host_suffixes) and len(url.parts) >= 2


def fetch_board_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
    *,
    services: FetchServices,
    is_restricted_board: Callable[[Board], bool],
) -> list[Job]:
    """Dispatch through the registered provider while enforcing board policy."""
    if is_restricted_board(board):
        return []
    adapter = PROVIDER_ADAPTERS.get(board.platform)
    if adapter is not None:
        return adapter.fetch(session, board, context, services=services)
    if board.platform == "web":
        return []
    raise ValueError(f"Unsupported platform: {board.platform}")


def verify_jobs_live(
    session: requests.Session,
    jobs: Sequence[Job],
    *,
    timeout: float,
    delay: float,
    now: datetime,
    services: LivenessServices,
    sleep: Callable[[float], None],
) -> None:
    """Run registered single-record and batch liveness strategies."""
    for job in jobs:
        adapter = PROVIDER_ADAPTERS.get(job.platform)
        if adapter is None or adapter.verify_one is None:
            continue
        adapter.verify_one(session, job, timeout=timeout, now=now, services=services)
        if delay > 0:
            sleep(delay)

    for adapter in PROVIDER_ADAPTERS.values():
        if adapter.verify_many is None:
            continue
        provider_jobs = [job for job in jobs if job.platform == adapter.platform]
        if not provider_jobs:
            continue
        adapter.verify_many(
            session,
            provider_jobs,
            timeout=timeout,
            now=now,
            services=services,
        )
        if delay > 0:
            sleep(delay)
