"""Typed contracts shared by public job-board provider adapters."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

import requests

from ..models import Board, Job, SearchCandidate


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
