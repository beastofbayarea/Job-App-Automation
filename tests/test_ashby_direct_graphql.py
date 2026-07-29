"""Unit tests for Ashby Direct GraphQL API submission."""

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


def test_ashby_direct_graphql_live_failure(tmp_path, monkeypatch):
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"%PDF-1.4 test")

    def mock_urlopen(*args, **kwargs):
        raise RuntimeError("Network error")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    status = submit_ashby_graphql_direct(
        url="https://jobs.ashbyhq.com/testcompany/12345",
        resume_path=resume_file,
        company="TestCompany",
        role="AI Engineer",
        live=True,
        cfg={},
    )

    assert status == "FAILED: DIRECT_GRAPHQL_API_ERROR"

