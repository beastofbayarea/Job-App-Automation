"""Workable public account jobs API adapter."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from ..config import PROVIDER_API_URLS, WORKABLE_SHORT_LINK_BOARD
from ..models import Board, Job
from ..terms import clean_whitespace
from .common import (
    days_old,
    iso_or_blank,
    mapping_items,
    parse_datetime,
    prettify_slug,
    strip_html,
)
from .contracts import FetchContext, FetchServices, LivenessServices


def api_url(board: Board) -> str:
    return str(PROVIDER_API_URLS["workable"]).format(token=quote(board.token, safe=""))


def location(item: dict[str, Any]) -> str:
    locations: list[str] = []
    for location_value in mapping_items(item.get("locations")):
        rendered = ", ".join(
            dict.fromkeys(
                clean_whitespace(location_value.get(key))
                for key in ("city", "region", "country")
                if clean_whitespace(location_value.get(key))
            )
        )
        if rendered:
            locations.append(rendered)
    if not locations:
        fallback = ", ".join(
            dict.fromkeys(
                clean_whitespace(item.get(key))
                for key in ("city", "state", "country")
                if clean_whitespace(item.get(key))
            )
        )
        if fallback:
            locations.append(fallback)
    return " | ".join(dict.fromkeys(locations))


def fetch_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
    *,
    services: FetchServices,
) -> list[Job]:
    if board.token == WORKABLE_SHORT_LINK_BOARD:
        return []

    payload = services.get_json(session, api_url(board), timeout=context.timeout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected Workable response for {board.token}")
    company = clean_whitespace(payload.get("name")) or prettify_slug(board.token)
    normalized: list[Job] = []

    for item in mapping_items(payload.get("jobs")):
        title = clean_whitespace(item.get("title"))
        job_description = strip_html(item.get("description"))
        job_location = location(item)
        workplace_type = "Remote" if item.get("telecommuting") is True else ""
        reason = context.criteria.matches_job(
            title=title,
            description=job_description,
            location=job_location,
            workplace_type=workplace_type,
        )
        if reason is None:
            continue

        published_on = item.get("published_on")
        posted_dt = parse_datetime(published_on or item.get("created_at"))
        if not context.criteria.includes_posted_at(posted_dt, now=context.now):
            continue
        item_id = clean_whitespace(item.get("shortcode"))
        job_url = clean_whitespace(item.get("shortlink") or item.get("url"))
        apply_url = clean_whitespace(item.get("application_url")) or job_url
        if not item_id or not job_url:
            continue

        normalized.append(
            Job(
                platform="workable",
                company=company,
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, context.now),
                location=job_location,
                workplace_type=workplace_type,
                employment_type=clean_whitespace(item.get("employment_type")),
                department=clean_whitespace(item.get("department")),
                team=clean_whitespace(item.get("function")),
                salary="",
                job_url=job_url,
                apply_url=apply_url,
                board_token=board.token,
                date_source="published_on" if published_on else "created_at",
                match_reason=reason,
                description=job_description,
                platform_job_id=item_id,
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(context.now),
                live_check_source="workable_public_account_api",
                live_check_reason="job_present_in_current_account_response",
                unique_id=f"workable:{board.token}:{item_id}",
            )
        )

    return normalized


def verify_jobs_live(
    session: requests.Session,
    jobs: Sequence[Job],
    *,
    timeout: float,
    now: datetime,
    services: LivenessServices,
) -> None:
    by_board: dict[tuple[str, str], list[Job]] = {}
    for job in jobs:
        if (
            not job.provider_id_trusted
            or not job.platform_job_id
            or not job.board_token
            or job.board_token == WORKABLE_SHORT_LINK_BOARD
        ):
            services.set_live_status(
                job,
                status="unknown",
                source="workable_account_api",
                now=now,
                reason="untrusted_or_missing_account_or_shortcode",
            )
            continue
        by_board.setdefault((job.board_region, job.board_token), []).append(job)

    for (board_region, board_token), board_jobs in by_board.items():
        board = Board("workable", board_token, board_region)
        response = services.response_or_none(session, api_url(board), timeout=timeout)
        if response is None:
            for job in board_jobs:
                services.set_live_status(
                    job,
                    status="unknown",
                    source="workable_account_api",
                    now=now,
                    reason="request_failed",
                )
            continue
        if response.status_code in {404, 410}:
            for job in board_jobs:
                services.set_live_status(
                    job,
                    status="closed",
                    source="workable_account_api",
                    now=now,
                    reason=f"http_{response.status_code}",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            continue
        if response.status_code >= 400:
            for job in board_jobs:
                services.set_live_status(
                    job,
                    status="unknown",
                    source="workable_account_api",
                    now=now,
                    reason=f"http_{response.status_code}",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            for job in board_jobs:
                services.set_live_status(
                    job,
                    status="unknown",
                    source="workable_account_api",
                    now=now,
                    reason="unexpected_account_response",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            continue
        active_ids = {
            clean_whitespace(item.get("shortcode"))
            for item in mapping_items(payload.get("jobs"))
            if item.get("shortcode")
        }
        for job in board_jobs:
            if job.platform_job_id in active_ids:
                services.set_live_status(
                    job,
                    status="live",
                    source="workable_account_api",
                    now=now,
                    reason="job_present_in_current_account_response",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            else:
                services.set_live_status(
                    job,
                    status="closed",
                    source="workable_account_api",
                    now=now,
                    reason="job_missing_from_current_account_response",
                    http_status=response.status_code,
                    final_url=response.url,
                )
