"""Lever public Postings API adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from ..config import PROVIDER_API_URLS
from ..models import Board, Job, SearchCandidate
from ..terms import clean_whitespace
from .common import days_old, iso_or_blank, mapping_items, parse_datetime, prettify_slug, strip_html
from .contracts import FetchContext, FetchServices, LivenessServices, ProviderUrl


LEVER_HOSTS = frozenset({"jobs.lever.co", "jobs.eu.lever.co"})


def matches_url(url: ProviderUrl) -> bool:
    return url.host in LEVER_HOSTS


def board_from_url(url: ProviderUrl) -> Board | None:
    if not matches_url(url) or not url.parts:
        return None
    region = "eu" if url.host == "jobs.eu.lever.co" else "global"
    return Board("lever", url.parts[0], region)


def looks_like_job_url(url: ProviderUrl) -> bool:
    return matches_url(url) and len(url.parts) >= 2 and url.parts[-1].lower() != "apply"


def api_base(board: Board) -> str:
    key = "lever_eu" if board.region == "eu" else "lever_global"
    return str(PROVIDER_API_URLS[key]).format(token=quote(board.token, safe=""))


def format_salary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    currency = clean_whitespace(value.get("currency", ""))
    minimum = value.get("min")
    maximum = value.get("max")
    interval = clean_whitespace(value.get("interval", ""))
    if minimum is not None and maximum is not None:
        amount = f"{minimum} - {maximum}"
    else:
        amount = str(minimum if minimum is not None else maximum or "")
    return clean_whitespace(" ".join(part for part in (currency, amount, interval) if part))


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
    max_pages = context.max_lever_pages
    base = api_base(board)
    page_size = 100
    skip = 0
    all_items: list[dict[str, Any]] = []

    page_number = 0
    while max_pages <= 0 or page_number < max_pages:
        payload = services.get_json(
            session,
            base,
            params={"mode": "json", "skip": skip, "limit": page_size},
            timeout=timeout,
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected Lever response for {board.token}")
        page = [item for item in payload if isinstance(item, dict)]
        all_items.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
        page_number += 1
        if delay > 0:
            services.sleep(delay)

    normalized: list[Job] = []
    for item in all_items:
        title = clean_whitespace(item.get("text"))
        lists_text = " ".join(
            f"{clean_whitespace(section.get('text'))} {strip_html(section.get('content'))}"
            for section in mapping_items(item.get("lists"))
        )
        description = clean_whitespace(
            " ".join(
                part
                for part in (
                    item.get("descriptionPlain"),
                    item.get("openingPlain"),
                    item.get("descriptionBodyPlain"),
                    item.get("additionalPlain"),
                    lists_text,
                )
                if part
            )
        )
        categories_value = item.get("categories")
        categories: dict[str, Any] = categories_value if isinstance(categories_value, dict) else {}
        locations = categories.get("allLocations") or []
        if not isinstance(locations, list):
            locations = [locations]
        location_parts = [clean_whitespace(part) for part in locations if part]
        primary_location = clean_whitespace(categories.get("location"))
        if primary_location and primary_location not in location_parts:
            location_parts.insert(0, primary_location)
        location = " | ".join(dict.fromkeys(location_parts))
        workplace_type = clean_whitespace(item.get("workplaceType"))

        reason = criteria.matches_job(
            title=title,
            description=description,
            location=location,
            workplace_type=workplace_type,
        )
        if reason is None:
            continue

        posted_dt = parse_datetime(item.get("createdAt"))
        date_source = "createdAt"
        if posted_dt is None:
            hosted_url = clean_whitespace(item.get("hostedUrl"))
            if hosted_url:
                candidate = SearchCandidate(url=hosted_url, board=board)
                try:
                    fallback_jobs = services.scrape_jsonld_jobs(
                        session,
                        candidate,
                        timeout=timeout,
                        now=now,
                        criteria=criteria,
                    )
                except Exception as exc:
                    services.logger.warning(
                        "Lever date fallback failed for %s: %s", hosted_url, exc
                    )
                    fallback_jobs = []
                fallback = fallback_jobs[0] if fallback_jobs else None
                if fallback is not None:
                    posted_dt = parse_datetime(fallback.posted_at)
                    date_source = "jsonld.datePosted"

        if not criteria.includes_posted_at(posted_dt, now=now):
            continue

        job_id = clean_whitespace(item.get("id"))
        job_url = clean_whitespace(item.get("hostedUrl"))
        apply_url = clean_whitespace(item.get("applyUrl"))
        unique = (
            f"lever:{board.region}:{board.token}:{job_id}"
            if job_id
            else services.canonical_url(job_url)
        )

        normalized.append(
            Job(
                platform="lever",
                company=prettify_slug(board.token),
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location,
                workplace_type=workplace_type,
                employment_type=clean_whitespace(categories.get("commitment")),
                department=clean_whitespace(categories.get("department")),
                team=clean_whitespace(categories.get("team")),
                salary=format_salary(item.get("salaryRange"))
                or strip_html(item.get("salaryDescriptionPlain") or item.get("salaryDescription")),
                job_url=job_url,
                apply_url=apply_url,
                board_token=board.token,
                date_source=date_source,
                match_reason=reason,
                description=description,
                platform_job_id=job_id,
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="lever_public_feed",
                live_check_reason="posting_present_in_current_feed",
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
    board = Board("lever", job.board_token, job.board_region)
    if not job.provider_id_trusted or not job.platform_job_id or not job.board_token:
        services.set_live_status(
            job,
            status="unknown",
            source="lever_posting_api",
            now=now,
            reason="untrusted_or_missing_board_or_job_id",
        )
        return
    url = f"{api_base(board)}/{quote(job.platform_job_id, safe='')}?mode=json"
    response = services.response_or_none(session, url, timeout=timeout)
    if response is None:
        services.set_live_status(
            job,
            status="unknown",
            source="lever_posting_api",
            now=now,
            reason="request_failed",
        )
        return
    if response.status_code in {404, 410}:
        services.set_live_status(
            job,
            status="closed",
            source="lever_posting_api",
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
            source="lever_posting_api",
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
            source="lever_posting_api",
            now=now,
            reason="unexpected_posting_response",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    services.set_live_status(
        job,
        status="live",
        source="lever_posting_api",
        now=now,
        reason="posting_present",
        http_status=response.status_code,
        final_url=response.url,
    )
