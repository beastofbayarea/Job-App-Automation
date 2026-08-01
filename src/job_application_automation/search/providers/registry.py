"""Typed registry and dispatch helpers for public ATS provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import requests

from ..models import Board, Job
from . import ashby, greenhouse, lever, smartrecruiters, workable
from .contracts import FetchContext, FetchServices, LivenessServices, ProviderFetcher


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


@dataclass(frozen=True)
class ProviderAdapter:
    """One provider's feed implementation and optional liveness strategy."""

    platform: str
    fetch: FetchImplementation
    verify_one: SingleVerifier | None = None
    verify_many: BatchVerifier | None = None


PROVIDER_ADAPTERS: dict[str, ProviderAdapter] = {
    "greenhouse": ProviderAdapter(
        platform="greenhouse",
        fetch=greenhouse.fetch_jobs,
        verify_one=greenhouse.verify_job_live,
    ),
    "lever": ProviderAdapter(
        platform="lever",
        fetch=lever.fetch_jobs,
        verify_one=lever.verify_job_live,
    ),
    "ashby": ProviderAdapter(
        platform="ashby",
        fetch=ashby.fetch_jobs,
        verify_many=ashby.verify_jobs_live,
    ),
    "smartrecruiters": ProviderAdapter(
        platform="smartrecruiters",
        fetch=smartrecruiters.fetch_jobs,
        verify_one=smartrecruiters.verify_job_live,
    ),
    "workable": ProviderAdapter(
        platform="workable",
        fetch=workable.fetch_jobs,
        verify_many=workable.verify_jobs_live,
    ),
}


def fetch_board_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
    *,
    fetchers: Mapping[str, ProviderFetcher],
    is_restricted_board: Callable[[Board], bool],
) -> list[Job]:
    """Dispatch through compatibility fetchers while enforcing board policy."""
    if is_restricted_board(board):
        return []
    fetcher = fetchers.get(board.platform)
    if fetcher is not None:
        return fetcher(session, board, context)
    if board.platform == "web":
        return []
    raise ValueError(f"Unsupported platform: {board.platform}")
