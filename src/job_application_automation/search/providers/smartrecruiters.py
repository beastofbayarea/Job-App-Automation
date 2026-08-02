"""SmartRecruiters public Posting API adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any
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
    prettify_slug,
    strip_html,
)
from .contracts import FetchContext, FetchServices, LivenessServices, ProviderUrl


SMARTRECRUITERS_HOSTS = frozenset(
    {
        "jobs.smartrecruiters.com",
        "www.smartrecruiters.com",
        "careers.smartrecruiters.com",
    }
)


def matches_url(url: ProviderUrl) -> bool:
    return url.host in SMARTRECRUITERS_HOSTS


def board_from_url(url: ProviderUrl) -> Board | None:
    if not matches_url(url):
        return None
    lowered_parts = tuple(part.lower() for part in url.parts)
    if (
        len(url.parts) >= 5
        and lowered_parts[:2] == ("oneclick-ui", "company")
        and lowered_parts[3] == "publication"
    ):
        return Board("smartrecruiters", url.parts[2], "global")
    if url.parts:
        return Board("smartrecruiters", url.parts[0], "global")
    return None


def looks_like_job_url(url: ProviderUrl) -> bool:
    if url.host not in {"jobs.smartrecruiters.com", "www.smartrecruiters.com"}:
        return False
    lowered_parts = tuple(part.lower() for part in url.parts)
    return len(url.parts) >= 2 and (
        lowered_parts[0] != "oneclick-ui"
        or (
            len(url.parts) >= 5
            and lowered_parts[:2] == ("oneclick-ui", "company")
            and lowered_parts[3] == "publication"
        )
    )


def api_base(board: Board) -> str:
    return str(PROVIDER_API_URLS["smartrecruiters"]).format(token=quote(board.token, safe=""))


def description(value: Any) -> str:
    sections = value.get("sections", {}) if isinstance(value, dict) else {}
    if not isinstance(sections, dict):
        return ""
    return clean_whitespace(
        " ".join(
            strip_html(section.get("text"))
            for section in sections.values()
            if isinstance(section, dict) and section.get("text")
        )
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
    delay = context.delay
    base = api_base(board)
    page_size = 100
    offset = 0
    all_items: list[dict[str, Any]] = []

    while True:
        payload = services.get_json(
            session,
            base,
            params={"limit": page_size, "offset": offset},
            timeout=timeout,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected SmartRecruiters response for {board.token}")
        page = mapping_items(payload.get("content"))
        all_items.extend(page)
        offset += len(page)
        try:
            total_found = int(payload.get("totalFound", offset))
        except (TypeError, ValueError):
            total_found = offset
        if not page or len(page) < page_size or offset >= total_found:
            break
        if delay > 0:
            services.sleep(delay)

    normalized: list[Job] = []
    for item in all_items:
        title = clean_whitespace(item.get("name"))
        if not matching_terms(title, criteria.role_terms, match_mode=criteria.match_mode):
            continue
        if matching_terms(title, criteria.exclude_terms, match_mode=criteria.match_mode):
            continue

        item_id = clean_whitespace(item.get("id"))
        if not item_id:
            continue
        detail = services.get_json(
            session,
            f"{base}/{quote(item_id, safe='')}",
            timeout=timeout,
        )
        if not isinstance(detail, dict):
            services.logger.warning(
                "SmartRecruiters detail was not an object for %s job %s",
                board.token,
                item_id,
            )
            continue
        if detail.get("active") is False or str(detail.get("visibility", "")).upper() not in {
            "",
            "PUBLIC",
        }:
            continue
        if delay > 0:
            services.sleep(delay)

        job_description = description(detail.get("jobAd"))
        raw_location = detail.get("location")
        location_value: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
        location = clean_whitespace(location_value.get("fullLocation"))
        if not location:
            location = " | ".join(
                dict.fromkeys(
                    clean_whitespace(location_value.get(key))
                    for key in ("city", "region", "country")
                    if clean_whitespace(location_value.get(key))
                )
            )
        workplace_type = (
            "Remote"
            if location_value.get("remote") is True
            else "Hybrid"
            if location_value.get("hybrid") is True
            else ""
        )
        reason = criteria.matches_job(
            title=title,
            description=job_description,
            location=location,
            workplace_type=workplace_type,
        )
        if reason is None:
            continue

        posted_dt = parse_datetime(detail.get("releasedDate") or item.get("releasedDate"))
        if not criteria.includes_posted_at(posted_dt, now=now):
            continue
        company = mapping_text(detail.get("company")) or prettify_slug(board.token)
        job_url = clean_whitespace(detail.get("postingUrl"))
        apply_url = clean_whitespace(detail.get("applyUrl")) or job_url
        employment = mapping_text(detail.get("typeOfEmployment"), "label")
        department = mapping_text(detail.get("department"), "label")
        function = mapping_text(detail.get("function"), "label")

        normalized.append(
            Job(
                platform="smartrecruiters",
                company=company,
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location,
                workplace_type=workplace_type,
                employment_type=employment,
                department=department,
                team=function,
                salary="",
                job_url=job_url,
                apply_url=apply_url,
                board_token=board.token,
                date_source="releasedDate",
                match_reason=reason,
                description=job_description,
                platform_job_id=item_id,
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="smartrecruiters_public_posting_api",
                live_check_reason="job_present_in_current_company_postings",
                unique_id=f"smartrecruiters:{board.token}:{item_id}",
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
    if not job.provider_id_trusted or not job.platform_job_id or not job.board_token:
        services.set_live_status(
            job,
            status="unknown",
            source="smartrecruiters_posting_api",
            now=now,
            reason="untrusted_or_missing_company_or_posting_id",
        )
        return
    board = Board("smartrecruiters", job.board_token, job.board_region)
    url = f"{api_base(board)}/{quote(job.platform_job_id, safe='')}"
    response = services.response_or_none(session, url, timeout=timeout)
    if response is None:
        services.set_live_status(
            job,
            status="unknown",
            source="smartrecruiters_posting_api",
            now=now,
            reason="request_failed",
        )
        return
    if response.status_code in {404, 410}:
        services.set_live_status(
            job,
            status="closed",
            source="smartrecruiters_posting_api",
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
            source="smartrecruiters_posting_api",
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
    if not isinstance(payload, dict) or clean_whitespace(payload.get("id")) != str(
        job.platform_job_id
    ):
        services.set_live_status(
            job,
            status="unknown",
            source="smartrecruiters_posting_api",
            now=now,
            reason="unexpected_posting_response",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    if payload.get("active") is False or str(payload.get("visibility", "")).upper() not in {
        "",
        "PUBLIC",
    }:
        services.set_live_status(
            job,
            status="closed",
            source="smartrecruiters_posting_api",
            now=now,
            reason="posting_not_active_or_public",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    services.set_live_status(
        job,
        status="live",
        source="smartrecruiters_posting_api",
        now=now,
        reason="active_public_posting_present",
        http_status=response.status_code,
        final_url=response.url,
    )
