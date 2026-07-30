"""Expanded unit and mock integration tests for smartrecruiters.py ATS engine."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from job_application_automation.engines.smartrecruiters import (
    _parser,
    main,
    run,
)


def test_smartrecruiters_parser_and_main_help() -> None:
    parser = _parser()
    assert parser.prog is not None
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_smartrecruiters_run_mocked(tmp_path: Path) -> None:
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"dummy pdf content")

    config = {
        "candidate": {
            "identity": {"first_name": "Jane", "last_name": "Doe"},
            "contact": {"phone": "+1234567890"},
        }
    }

    mock_session = MagicMock()
    mock_page = MagicMock()
    mock_session.page = mock_page
    mock_session.close_browser_on_exit = True

    mock_file_input = MagicMock()
    mock_file_input.count.return_value = 1
    mock_page.locator.return_value.first = mock_file_input

    with patch("job_application_automation.engines.smartrecruiters.open_chrome_session", return_value=mock_session), \
         patch("job_application_automation.engines.smartrecruiters.resolve_candidate_email", return_value="jane@example.com"), \
         patch("job_application_automation.engines.smartrecruiters.fill_required_consent", return_value={}), \
         patch("job_application_automation.engines.smartrecruiters.page_has_captcha", return_value=False), \
         patch("job_application_automation.engines.smartrecruiters.capture_screenshot", return_value="screenshot.png"):

        res = run(
            url="https://jobs.smartrecruiters.com/Example/123-role",
            resume=resume_file,
            config=config,
            company="Example",
            role="AI Engineer",
            live_submit=False,
            screenshot_dir=tmp_path,
        )

        assert res["success"] is True
        assert res["status"] == "PREFILLED_ONLY"
        assert res["ats"] == "smartrecruiters"
        assert res["filled_fields"]["first_name"] == "Jane"
        assert mock_session.browser.close.called
