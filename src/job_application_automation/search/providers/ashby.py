"""Ashby public job-board API adapter."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from ..config import PROVIDER_API_URLS
from ..models import Board, Job
from ..terms import clean_whitespace
from .common import days_old, iso_or_blank, mapping_items, parse_datetime, prettify_slug, strip_html
from .contracts import FetchContext, FetchServices, LivenessServices, ProviderUrl


def matches_url(url: ProviderUrl) -> bool:
    return url.host == "jobs.ashbyhq.com" or url.host.endswith(".jobs.ashbyhq.com")


def board_from_url(url: ProviderUrl) -> Board | None:
    if not matches_url(url) or not url.parts:
        return None
    return Board("ashby", url.parts[0], "global")


def looks_like_job_url(url: ProviderUrl) -> bool:
    return url.host == "jobs.ashbyhq.com" and len(url.parts) >= 2


def format_salary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return clean_whitespace(
        value.get("scrapeableCompensationSalarySummary")
        or value.get("compensationTierSummary")
        or ""
    )


def fetch_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
    *,
    services: FetchServices,
) -> list[Job]:
    criteria = context.criteria
    now = context.now
    timeout = context.timeout
    url = PROVIDER_API_URLS["ashby"].format(token=quote(board.token, safe=""))
    payload = services.get_json(
        session,
        url,
        params={"includeCompensation": "true"},
        timeout=timeout,
    )
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    normalized: list[Job] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("isListed") is False:
            continue

        title = clean_whitespace(item.get("title"))
        description = clean_whitespace(
            item.get("descriptionPlain") or strip_html(item.get("descriptionHtml"))
        )
        primary_location = clean_whitespace(item.get("location"))
        secondary = [
            clean_whitespace(location.get("location"))
            for location in mapping_items(item.get("secondaryLocations"))
            if location.get("location")
        ]
        location = " | ".join(
            dict.fromkeys(part for part in [primary_location, *secondary] if part)
        )
        workplace_type = clean_whitespace(item.get("workplaceType"))

        reason = criteria.matches_job(
            title=title,
            description=description,
            location=location,
            workplace_type=workplace_type,
        )
        if reason is None:
            continue

        posted_dt = parse_datetime(item.get("publishedAt"))
        if not criteria.includes_posted_at(posted_dt, now=now):
            continue

        job_url = clean_whitespace(item.get("jobUrl"))
        apply_url = clean_whitespace(item.get("applyUrl"))
        item_id = clean_whitespace(item.get("id"))
        unique = f"ashby:{board.token}:{item_id}" if item_id else services.canonical_url(job_url)

        normalized.append(
            Job(
                platform="ashby",
                company=prettify_slug(board.token),
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location,
                workplace_type=workplace_type,
                employment_type=clean_whitespace(item.get("employmentType")),
                department=clean_whitespace(item.get("department")),
                team=clean_whitespace(item.get("team")),
                salary=format_salary(item.get("compensation")),
                job_url=job_url,
                apply_url=apply_url,
                board_token=board.token,
                date_source="publishedAt",
                match_reason=reason,
                description=description,
                platform_job_id=item_id,
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="ashby_public_board",
                live_check_reason="job_present_in_current_board_response",
                unique_id=unique,
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
    """Verify Ashby roles per board to avoid one request per final job."""
    by_board: dict[tuple[str, str], list[Job]] = {}
    for job in jobs:
        if not job.provider_id_trusted or not job.platform_job_id:
            services.set_live_status(
                job,
                status="unknown",
                source="ashby_board_api",
                now=now,
                reason="untrusted_or_missing_job_id",
            )
            continue
        by_board.setdefault((job.board_region, job.board_token), []).append(job)
    for (_board_region, board_token), board_jobs in by_board.items():
        if not board_token:
            for job in board_jobs:
                services.set_live_status(
                    job,
                    status="unknown",
                    source="ashby_board_api",
                    now=now,
                    reason="missing_board",
                )
            continue
        url = PROVIDER_API_URLS["ashby"].format(token=quote(board_token, safe=""))
        response = services.response_or_none(session, url, timeout=timeout)
        if response is None:
            for job in board_jobs:
                services.set_live_status(
                    job,
                    status="unknown",
                    source="ashby_board_api",
                    now=now,
                    reason="request_failed",
                )
            continue
        if response.status_code in {404, 410}:
            for job in board_jobs:
                services.set_live_status(
                    job,
                    status="closed",
                    source="ashby_board_api",
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
                    source="ashby_board_api",
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
        jobs_payload = payload.get("jobs", []) if isinstance(payload, dict) else []
        active_ids = {
            clean_whitespace(item.get("id"))
            for item in jobs_payload
            if isinstance(item, dict) and item.get("id") and item.get("isListed") is not False
        }
        for job in board_jobs:
            if job.platform_job_id and job.platform_job_id in active_ids:
                services.set_live_status(
                    job,
                    status="live",
                    source="ashby_board_api",
                    now=now,
                    reason="job_present_in_current_board_response",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            else:
                services.set_live_status(
                    job,
                    status="closed",
                    source="ashby_board_api",
                    now=now,
                    reason="job_missing_from_current_board_response",
                    http_status=response.status_code,
                    final_url=response.url,
                )
