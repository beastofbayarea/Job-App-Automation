from __future__ import annotations

from job_application_automation.search.liveness import page_response_decision


def test_network_fault_http_500_server_error() -> None:
    dec = page_response_decision(
        status_code=500,
        response_url="https://example.com/job/1",
        html_text="500 Internal Server Error",
        job_title="Software Engineer",
        job_urls=["https://example.com/job/1"],
        existing_status="listed",
        dead_role_markers=["this job is no longer available"],
        canonical_url=lambda u: u.lower(),
        clean_text=lambda s: str(s or "").strip(),
        normalize_text=lambda s: str(s or "").lower(),
        extract_jsonld_objects=lambda h: [],
        is_jobposting_object=lambda o: True,
        is_not_expired=lambda vt, n: True,
        now="2026-07-30",
    )
    # 500 Server Error should preserve existing listed status conservatively
    assert dec.status is None
    assert dec.preserve_listing is True
    assert dec.reason == "http_500"


def test_network_fault_http_429_rate_limit() -> None:
    dec = page_response_decision(
        status_code=429,
        response_url="https://example.com/job/1",
        html_text="429 Too Many Requests",
        job_title="Software Engineer",
        job_urls=["https://example.com/job/1"],
        existing_status="listed",
        dead_role_markers=[],
        canonical_url=lambda u: u.lower(),
        clean_text=lambda s: str(s or "").strip(),
        normalize_text=lambda s: str(s or "").lower(),
        extract_jsonld_objects=lambda h: [],
        is_jobposting_object=lambda o: True,
        is_not_expired=lambda vt, n: True,
        now="2026-07-30",
    )
    assert dec.status is None
    assert dec.preserve_listing is True
    assert dec.reason == "http_429"


def test_network_fault_http_410_gone() -> None:
    dec = page_response_decision(
        status_code=410,
        response_url="https://example.com/job/1",
        html_text="410 Gone",
        job_title="Software Engineer",
        job_urls=["https://example.com/job/1"],
        existing_status="listed",
        dead_role_markers=[],
        canonical_url=lambda u: u.lower(),
        clean_text=lambda s: str(s or "").strip(),
        normalize_text=lambda s: str(s or "").lower(),
        extract_jsonld_objects=lambda h: [],
        is_jobposting_object=lambda o: True,
        is_not_expired=lambda vt, n: True,
        now="2026-07-30",
    )
    assert dec.status == "closed"
    assert dec.reason == "http_410"
