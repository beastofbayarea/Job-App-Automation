"""Greenhouse public Job Board API adapter."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import requests

from ..config import PROVIDER_API_URLS
from ..models import Board, Job
from ..terms import clean_whitespace, matching_terms
from .common import (
    days_old,
    iso_or_blank,
    mapping_items,
    mapping_text,
    parse_datetime,
    parse_expiry_datetime,
    prettify_slug,
    strip_html,
)
from .contracts import FetchContext, FetchServices, LivenessServices


def base_url(board: Board) -> str:
    """Return Greenhouse's documented public Job Board API base URL."""
    return str(PROVIDER_API_URLS["greenhouse"]).format(token=quote(board.token, safe=""))


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
    delay = context.delay
    api_base = base_url(board)
    payload = services.get_json(
        session,
        f"{api_base}/jobs",
        params={"content": "true"},
        timeout=timeout,
    )
    jobs_raw = payload.get("jobs", []) if isinstance(payload, dict) else []
    normalized: list[Job] = []

    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        title = clean_whitespace(item.get("title"))
        description = strip_html(item.get("content"))
        location = mapping_text(item.get("location"))
        if not matching_terms(title, criteria.role_terms, match_mode=criteria.match_mode):
            continue
        if matching_terms(
            clean_whitespace(f"{title} {description}"),
            criteria.exclude_terms,
            match_mode=criteria.match_mode,
        ):
            continue

        detail = item
        date_source = "updated_at_fallback"
        job_id = item.get("id")
        if job_id is not None:
            try:
                detail_payload = services.get_json(
                    session,
                    f"{api_base}/jobs/{job_id}",
                    timeout=timeout,
                )
                if isinstance(detail_payload, dict):
                    detail = {**item, **detail_payload}
                    date_source = "first_published"
            except Exception as exc:
                services.logger.warning(
                    "Greenhouse detail failed for %s job %s: %s",
                    board.token,
                    job_id,
                    exc,
                )
            if delay > 0:
                services.sleep(delay)

        description = strip_html(detail.get("content") or item.get("content"))
        deadline = parse_expiry_datetime(detail.get("application_deadline"))
        if deadline is not None and deadline <= now:
            continue

        posted_dt = parse_datetime(detail.get("first_published"))
        if posted_dt is None:
            posted_dt = parse_datetime(detail.get("updated_at"))
            date_source = "updated_at_fallback"
        if not criteria.includes_posted_at(posted_dt, now=now):
            continue

        departments = " | ".join(
            clean_whitespace(department.get("name"))
            for department in mapping_items(detail.get("departments"))
            if department.get("name")
        )
        offices = " | ".join(
            clean_whitespace(office.get("name"))
            for office in mapping_items(detail.get("offices"))
            if office.get("name")
        )
        detail_location = mapping_text(detail.get("location"))
        location_full = " | ".join(
            dict.fromkeys(part for part in (location, detail_location, offices) if part)
        )

        reason = criteria.matches_job(
            title=title,
            description=description,
            location=location_full,
        )
        if reason is None:
            continue

        company = clean_whitespace(detail.get("company_name")) or prettify_slug(board.token)
        job_url = clean_whitespace(detail.get("absolute_url") or item.get("absolute_url"))
        unique = (
            f"greenhouse:{board.region}:{board.token}:{job_id}"
            if job_id is not None
            else services.canonical_url(job_url)
        )

        normalized.append(
            Job(
                platform="greenhouse",
                company=company,
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location_full,
                workplace_type="",
                employment_type="",
                department=departments,
                team="",
                salary="",
                job_url=job_url,
                apply_url=job_url,
                board_token=board.token,
                date_source=date_source,
                match_reason=reason,
                description=description,
                platform_job_id=clean_whitespace(job_id),
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="greenhouse_public_feed",
                live_check_reason="job_present_in_current_board_feed",
                unique_id=unique,
            )
        )

    return normalized


def verify_job_live(
    session: requests.Session,
    job: Job,
    *,
    timeout: float,
    now: datetime,
    services: LivenessServices,
) -> None:
    board = Board("greenhouse", job.board_token, job.board_region)
    if not job.provider_id_trusted or not job.platform_job_id or not job.board_token:
        services.set_live_status(
            job,
            status="unknown",
            source="greenhouse_job_api",
            now=now,
            reason="untrusted_or_missing_board_or_job_id",
        )
        return
    url = f"{base_url(board)}/jobs/{quote(job.platform_job_id, safe='')}"
    response = services.response_or_none(session, url, timeout=timeout)
    if response is None:
        services.set_live_status(
            job,
            status="unknown",
            source="greenhouse_job_api",
            now=now,
            reason="request_failed",
        )
        return
    if response.status_code in {404, 410}:
        services.set_live_status(
            job,
            status="closed",
            source="greenhouse_job_api",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    if response.status_code >= 400:
        services.set_live_status(
            job,
            status="unknown",
            source="greenhouse_job_api",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or str(payload.get("id", "")) != str(job.platform_job_id):
        services.set_live_status(
            job,
            status="unknown",
            source="greenhouse_job_api",
            now=now,
            reason="unexpected_job_response",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    deadline = parse_expiry_datetime(payload.get("application_deadline"))
    if deadline is not None and deadline <= now:
        services.set_live_status(
            job,
            status="closed",
            source="greenhouse_job_api",
            now=now,
            reason="application_deadline_elapsed",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    services.set_live_status(
        job,
        status="live",
        source="greenhouse_job_api",
        now=now,
        reason="job_present_and_deadline_open",
        http_status=response.status_code,
        final_url=response.url,
    )
