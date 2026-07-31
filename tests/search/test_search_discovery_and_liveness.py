from __future__ import annotations

from job_application_automation.search.liveness import (
    page_jsonld_decision,
    page_response_decision,
    page_uncertainty,
)


def test_liveness_uncertainty() -> None:
    dec = page_uncertainty(
        existing_status="listed",
        reason="http_500",
        http_status=500,
        final_url="https://example.com/job/1",
    )
    assert dec.status is None
    assert dec.preserve_listing is True

    dec_unlisted = page_uncertainty(
        existing_status="unknown",
        reason="http_500",
        http_status=500,
    )
    assert dec_unlisted.status == "unknown"
    assert dec_unlisted.preserve_listing is False


def test_page_jsonld_decision() -> None:
    now = "2026-07-30T10:00:00Z"
    dec = page_jsonld_decision(
        {"validThrough": "2026-12-31T23:59:59Z"},
        now=now,
        is_not_expired=lambda vt, n: True,
        http_status=200,
        final_url="https://example.com/job/1",
    )
    assert dec.status == "live"
    assert dec.reason == "matching_unexpired_jobposting"

    dec_expired = page_jsonld_decision(
        {"validThrough": "2020-01-01T00:00:00Z"},
        now=now,
        is_not_expired=lambda vt, n: False,
        http_status=200,
        final_url="https://example.com/job/1",
    )
    assert dec_expired.status == "closed"
    assert dec_expired.reason == "valid_through_elapsed"


def test_page_response_decision_404() -> None:
    dec = page_response_decision(
        status_code=404,
        response_url="https://example.com/job/1",
        html_text="Not found",
        job_title="Software Engineer",
        job_urls=["https://example.com/job/1"],
        existing_status="live",
        dead_role_markers=["this job is no longer available"],
        canonical_url=lambda u: u.lower(),
        clean_text=lambda s: str(s or "").strip(),
        normalize_text=lambda s: str(s or "").lower(),
        extract_jsonld_objects=lambda h: [],
        is_jobposting_object=lambda o: True,
        is_not_expired=lambda vt, n: True,
        now="2026-07-30",
    )
    assert dec.status == "closed"
    assert dec.reason == "http_404"
