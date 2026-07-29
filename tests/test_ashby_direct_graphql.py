"""Unit tests for Ashby Direct GraphQL API submission."""

from pathlib import Path
from job_application_automation.engines.ashby import submit_ashby_graphql_direct


def test_ashby_direct_graphql_fill_only(tmp_path):
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"%PDF-1.4 test")

    cfg = {
        "candidate": {
            "first_name": "Test",
            "last_name": "Applicant",
            "fallback_email": "test@example.com",
        }
    }

    status = submit_ashby_graphql_direct(
        url="https://jobs.ashbyhq.com/testcompany/12345",
        resume_path=resume_file,
        company="TestCompany",
        role="AI Engineer",
        live=False,
        cfg=cfg,
    )

    assert status == "PREFILLED_ONLY"
