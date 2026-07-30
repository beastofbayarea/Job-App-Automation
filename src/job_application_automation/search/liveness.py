"""Pure liveness decisions for public ATS listings and job pages.

HTTP remains in the compatibility module; these functions only classify
already-fetched response data.  That keeps the cautious "listed is not
discarded on a bot-blocked page" rule independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Callable, Iterable, Sequence


@dataclass(frozen=True)
class LivenessDecision:
    """A side-effect-free update to apply to a search ``Job`` record."""

    status: str | None
    source: str
    reason: str
    http_status: int | str = ""
    final_url: str = ""
    preserve_listing: bool = False


def page_uncertainty(
    *,
    existing_status: str,
    reason: str,
    http_status: int | str = "",
    final_url: str = "",
) -> LivenessDecision:
    """Return a conservative decision when a page cannot prove job identity."""
    preserve_listing = existing_status in {"listed", "live"}
    return LivenessDecision(
        status=None if preserve_listing else "unknown",
        source="job_page",
        reason=reason,
        http_status=http_status,
        final_url=final_url,
        preserve_listing=preserve_listing,
    )


def page_jsonld_decision(
    value: dict[str, Any],
    *,
    now: Any,
    is_not_expired: Callable[[Any, Any], bool],
    http_status: int | str,
    final_url: str,
) -> LivenessDecision:
    """Classify a JSON-LD JobPosting once its URL/title identity was verified."""
    if not is_not_expired(value.get("validThrough"), now):
        return LivenessDecision(
            status="closed",
            source="job_page_jsonld",
            reason="valid_through_elapsed",
            http_status=http_status,
            final_url=final_url,
        )
    return LivenessDecision(
        status="live",
        source="job_page_jsonld",
        reason="matching_unexpired_jobposting",
        http_status=http_status,
        final_url=final_url,
    )


def page_response_decision(
    *,
    status_code: int,
    response_url: str,
    html_text: str,
    job_title: str,
    job_urls: Iterable[str],
    existing_status: str,
    dead_role_markers: Sequence[str],
    canonical_url: Callable[[str], str],
    clean_text: Callable[[Any], str],
    normalize_text: Callable[[Any], str],
    extract_jsonld_objects: Callable[[str], Iterable[dict[str, Any]]],
    is_jobposting_object: Callable[[dict[str, Any]], bool],
    is_not_expired: Callable[[Any, Any], bool],
    now: Any,
) -> LivenessDecision:
    """Classify a job-page response without performing requests or mutations."""
    if status_code in {404, 410}:
        return LivenessDecision(
            status="closed",
            source="job_page",
            reason=f"http_{status_code}",
            http_status=status_code,
            final_url=response_url,
        )
    if status_code >= 400:
        return page_uncertainty(
            existing_status=existing_status,
            reason=f"http_{status_code}",
            http_status=status_code,
            final_url=response_url,
        )
    if any(marker in html_text.casefold() for marker in dead_role_markers):
        return LivenessDecision(
            status="closed",
            source="job_page",
            reason="explicit_closed_message",
            http_status=status_code,
            final_url=response_url,
        )

    expected_urls = {canonical_url(value) for value in job_urls if clean_text(value)}
    response_targets_job = canonical_url(response_url) in expected_urls
    title_only_records: list[dict[str, Any]] = []
    for value in extract_jsonld_objects(html_text):
        if not is_jobposting_object(value):
            continue
        title = clean_text(value.get("title"))
        if not title or normalize_text(title) != normalize_text(job_title):
            continue
        record_url = clean_text(value.get("url"))
        if record_url:
            if canonical_url(record_url) not in expected_urls:
                # An index page can mention a similarly titled but different role.
                continue
            return page_jsonld_decision(
                value,
                now=now,
                is_not_expired=is_not_expired,
                http_status=status_code,
                final_url=response_url,
            )
        if response_targets_job:
            title_only_records.append(value)

    if len(title_only_records) == 1:
        return page_jsonld_decision(
            title_only_records[0],
            now=now,
            is_not_expired=is_not_expired,
            http_status=status_code,
            final_url=response_url,
        )
    return page_uncertainty(
        existing_status=existing_status,
        reason="no_positive_job_identity_evidence",
        http_status=status_code,
        final_url=response_url,
    )


def page_jobs_for_target(jobs: Sequence[Any], target: str) -> list[Any]:
    """Select records needing a page request after provider-listing checks."""
    if target in {"application", "both"}:
        return list(jobs)
    return [job for job in jobs if not job.provider_id_trusted]
