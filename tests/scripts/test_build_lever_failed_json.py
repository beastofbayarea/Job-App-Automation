from scripts.build_lever_failed_json import canonical_lever_url, merge_records


def test_canonical_lever_url_handles_global_eu_and_apply_suffix() -> None:
    assert canonical_lever_url("https://jobs.lever.co/acme/123/apply?source=x") == (
        "https://jobs.lever.co/acme/123"
    )
    assert canonical_lever_url("https://jobs.eu.lever.co/acme/123") == (
        "https://jobs.eu.lever.co/acme/123"
    )
    assert canonical_lever_url("https://example.com/acme/123") is None


def test_merge_records_overrides_workbook_placeholder() -> None:
    base = [{"job_url": "https://jobs.lever.co/acme/123", "status": "NOT_ATTEMPTED"}]
    incoming = [
        {
            "job_url": "https://jobs.lever.co/acme/123/apply",
            "status": "REQUIRED_FIELDS_NOT_FILLED",
        }
    ]
    records = merge_records(base, incoming)
    assert len(records) == 1
    assert records[0]["status"] == "REQUIRED_FIELDS_NOT_FILLED"
