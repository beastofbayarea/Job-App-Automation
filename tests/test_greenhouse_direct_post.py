"""Unit tests for Greenhouse direct multipart POST submission."""

from pathlib import Path
from job_application_automation.engines.greenhouse import submit_greenhouse_direct_post


def test_greenhouse_direct_post_fill_only(tmp_path):
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"%PDF-1.4 test")

    result = submit_greenhouse_direct_post(
        url="https://boards.greenhouse.io/testcompany/jobs/12345",
        resume=resume_file,
        email_override="applicant@example.com",
        company="TestCompany",
        role="AI Engineer",
        live_submit=False,
    )

    assert result["success"] is True
    assert result["status"] == "PREFILLED_ONLY"
    assert result["ats"] == "greenhouse"
