"""Typed registry and dispatch helpers for public ATS provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import requests

from ..models import Board, Job
from . import ashby, greenhouse, lever, smartrecruiters, workable
from .contracts import FetchContext, FetchServices, LivenessServices, ProviderUrl


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
