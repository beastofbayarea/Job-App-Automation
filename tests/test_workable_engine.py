"""Expanded unit and mock integration tests for workable.py ATS engine."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from job_application_automation.engines.workable import (
    _extract_workable_ids,
    _parser,
    main,
    run,
)


def test_extract_workable_ids() -> None:
    url = "https://apply.workable.com/example-company/j/ABC123XYZ/"
    company, job_id = _extract_workable_ids(url)
    assert company == "example-company"
    assert job_id == "ABC123XYZ"


def test_workable_parser_and_main_help() -> None:
    parser = _parser()
    assert parser.prog is not None
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_workable_run_mocked(tmp_path: Path) -> None:
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"dummy pdf content")

    config = {
        "candidate": {
            "identity": {"first_name": "Jane", "last_name": "Doe", "headline": "Staff AI Engineer"},
            "contact": {
                "phone": "+1234567890",
                "linkedin": "https://linkedin.com/in/janedoe",
                "github": "https://github.com/janedoe",
            },
        }
    }

    mock_session = MagicMock()
    mock_page = MagicMock()
    mock_session.page = mock_page
    mock_session.close_browser_on_exit = True

    mock_file_input = MagicMock()
    mock_file_input.count.return_value = 1
    mock_page.locator.return_value.first = mock_file_input

    mock_playwright = MagicMock()
    with patch("job_application_automation.engines.workable.sync_playwright", return_value=MagicMock(__enter__=MagicMock(return_value=mock_playwright))), \
         patch("job_application_automation.engines.workable.open_chrome_session", return_value=mock_session), \
         patch("job_application_automation.engines.workable.resolve_candidate_email", return_value="jane@example.com"), \
         patch("job_application_automation.engines.workable.fill_required_consent", return_value={}), \
         patch("job_application_automation.engines.workable.page_has_captcha", return_value=False), \
         patch("job_application_automation.engines.workable.capture_screenshot", return_value="screenshot.png"):

        res = run(
            url="https://apply.workable.com/example/j/123/",
            resume=resume_file,
            config=config,
            company="Example",
            role="AI Engineer",
            live_submit=False,
            screenshot_dir=tmp_path,
        )

        assert res["success"] is True
        assert res["status"] == "PREFILLED_ONLY"
        assert res["ats"] == "workable"
        assert res["filled_fields"]["first_name"] == "Jane"
        assert res["filled_fields"]["email"] == "jane@example.com"
        assert mock_session.browser.close.called
