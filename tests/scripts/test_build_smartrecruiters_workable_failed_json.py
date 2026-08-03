from scripts.build_smartrecruiters_workable_failed_json import canonical_url, merge_records


def test_provider_url_canonicalization() -> None:
    assert canonical_url(
        "https://jobs.smartrecruiters.com/Acme/123-role?ref=x", "smartrecruiters"
    ) == "https://jobs.smartrecruiters.com/Acme/123-role"
    assert canonical_url(
        "https://apply.workable.com/acme/j/ABC123/?source=x", "workable"
    ) == "https://apply.workable.com/acme/j/ABC123"


def test_merge_prefers_real_vps_outcome() -> None:
    base = [{"job_url": "https://apply.workable.com/acme/j/ABC123", "status": "NOT_ATTEMPTED"}]
    incoming = [{"job_url": "https://apply.workable.com/acme/j/ABC123/", "status": "CAPTCHA_REQUIRED"}]
    result = merge_records(base, incoming, "workable")
    assert len(result) == 1
    assert result[0]["status"] == "CAPTCHA_REQUIRED"
