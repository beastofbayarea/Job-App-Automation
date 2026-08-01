"""Typed contracts shared by public job-board provider adapters."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import requests

from ..models import Board, Job, SearchCandidate


class JobCriteria(Protocol):
    """Filtering behavior required by provider feed normalizers."""

    role_terms: tuple[str, ...]
    ai_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    location_terms: tuple[str, ...]
    days: int
    include_unknown_dates: bool
    posted_since: date | None
    posted_until: date | None
    match_mode: str

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
    max_lever_pages: int


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
