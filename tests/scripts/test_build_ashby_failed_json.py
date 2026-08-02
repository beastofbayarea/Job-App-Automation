from pathlib import Path

from openpyxl import Workbook

from scripts.build_ashby_failed_json import build_records, canonical_ashby_url, merge_records


def test_canonical_ashby_url_removes_tracking_and_rejects_non_ashby() -> None:
    assert canonical_ashby_url("https://jobs.ashbyhq.com/acme/123/?utm_source=x") == (
        "https://jobs.ashbyhq.com/acme/123"
    )
    assert canonical_ashby_url("https://example.com/acme/123") is None


def test_build_records_keeps_unique_valid_jobs_in_workbook_order(tmp_path: Path) -> None:
    path = tmp_path / "jobs.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Posting Date", "Company", "Job Title", "Location", "Application URL"])
    sheet.append(["2026-08-01", "Acme", "PM", "Remote", "https://jobs.ashbyhq.com/acme/123"])
    sheet.append(["2026-08-01", "Acme", "PM", "Remote", "https://jobs.ashbyhq.com/acme/123?ref=x"])
    sheet.append(["2026-08-01", "Other", "PM", "Remote", "https://example.com/jobs/456"])
    workbook.save(path)

    records = build_records(path)

    assert len(records) == 1
    assert records[0]["job_url"] == "https://jobs.ashbyhq.com/acme/123"
    assert records[0]["status"] == "NOT_ATTEMPTED"


def test_merge_records_overrides_placeholder_and_appends_vps_only_job() -> None:
    workbook = [
        {
            "company": "Acme",
            "role": "PM",
            "job_url": "https://jobs.ashbyhq.com/acme/123",
            "status": "NOT_ATTEMPTED",
            "failure_reason": "Imported",
            "location": "Remote",
        }
    ]
    vps = [
        {
            "company": "Acme",
            "role": "PM",
            "job_url": "https://jobs.ashbyhq.com/acme/123?ref=x",
            "status": "REQUIRED_FIELDS_NOT_FILLED",
            "failure_reason": "Required fields: Work authorization",
        },
        {
            "company": "Other",
            "role": "Lead PM",
            "job_url": "https://jobs.ashbyhq.com/other/456",
            "status": "SUBMISSION_UNCONFIRMED",
        },
    ]

    records = merge_records(workbook, vps)

    assert len(records) == 2
    assert records[0]["status"] == "REQUIRED_FIELDS_NOT_FILLED"
    assert records[0]["location"] == "Remote"
    assert records[1]["job_url"] == "https://jobs.ashbyhq.com/other/456"
