"""Unit and mock integration tests for greenhouse.py ATS engine."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from job_application_automation.engines.greenhouse import (
    _valid_greenhouse_url,
    _fill_all_visible,
    _load_candidate_evidence,
    _fill_explicit_required_consents,
    _fill_export_control_questions,
    _fill_source_checkbox,
    _required_empty_fields,
    _upload_cover_letter,
    _upload_resume,
    _parser,
    main,
)


def test_fill_all_visible_populates_duplicate_standard_fields() -> None:
    page = MagicMock()
    controls = MagicMock()
    controls.count.return_value = 3
    first = MagicMock()
    first.is_visible.return_value = True
    first.input_value.return_value = "Candidate"
    duplicate = MagicMock()
    duplicate.is_visible.return_value = True
    duplicate.input_value.return_value = "Candidate"
    hidden = MagicMock()
    hidden.is_visible.return_value = False
    controls.nth.side_effect = [first, duplicate, hidden]
    page.locator.return_value = controls

    assert _fill_all_visible(page, ('input[name="last_name"]',), "Candidate") is True
    first.fill.assert_called_once_with("Candidate")
    duplicate.fill.assert_called_once_with("Candidate")
    hidden.fill.assert_not_called()


def test_valid_greenhouse_url() -> None:
    assert _valid_greenhouse_url("https://boards.greenhouse.io/company/jobs/12345") is True
    assert _valid_greenhouse_url("https://job-boards.greenhouse.io/company/jobs/12345") is True
    assert _valid_greenhouse_url("https://example.com/job/123") is False
    assert _valid_greenhouse_url("") is False


def test_load_candidate_evidence() -> None:
    config = {
        "candidate": {
            "evidence": ["Built high throughput API"],
            "summary": "Senior Software Engineer"
        }
    }
    evidence_text = _load_candidate_evidence(config)
    assert isinstance(evidence_text, str)
    assert len(evidence_text) > 0


def test_fill_explicit_required_consents_mocked() -> None:
    page = MagicMock()
    controls = MagicMock()
    controls.count.return_value = 1
    
    control = MagicMock()
    control.is_visible.return_value = True
    control.is_checked.side_effect = [False, True]
    controls.nth.return_value = control
    page.locator.return_value = controls

    with patch("job_application_automation.engines.greenhouse._label_for", return_value="I agree to privacy policy"):
        modified = _fill_explicit_required_consents(page)
        assert len(modified) == 1
        assert "I agree to privacy policy" in modified[0]


def test_fill_export_control_questions_mocked() -> None:
    page = MagicMock()
    body_loc = MagicMock()
    body_loc.inner_text.return_value = "U.S. sanctions and export controls terms..."
    page.locator.return_value = body_loc

    ctrl = MagicMock()
    ctrl.is_checked.return_value = True
    with patch("job_application_automation.engines.greenhouse._first_visible", return_value=ctrl), \
         patch("job_application_automation.engines.greenhouse._label_for", return_value="None of the above"):
        res = _fill_export_control_questions(page)
        assert len(res) >= 1


def test_fill_source_checkbox_mocked() -> None:
    page = MagicMock()
    body_loc = MagicMock()
    body_loc.inner_text.return_value = "How did you hear about Twilio?"
    page.locator.return_value = body_loc

    ctrl = MagicMock()
    ctrl.get_attribute.return_value = "checkbox"
    ctrl.is_checked.return_value = True
    with patch("job_application_automation.engines.greenhouse._first_visible", return_value=ctrl):
        res = _fill_source_checkbox(page)
        assert "How did you hear about this job? LinkedIn" in res


def test_required_empty_fields_mocked() -> None:
    page = MagicMock()
    controls = MagicMock()
    controls.count.return_value = 1
    
    control = MagicMock()
    control.is_visible.return_value = True
    control.get_attribute.side_effect = lambda attr: "text" if attr == "type" else None
    control.input_value.return_value = ""
    control.evaluate.return_value = ""
    controls.nth.return_value = control
    page.locator.return_value = controls

    with patch("job_application_automation.engines.greenhouse._label_for", return_value="First Name *"):
        empty_labels = _required_empty_fields(page)
        assert "First Name *" in empty_labels


def test_upload_resume_valid_and_invalid(tmp_path: Path) -> None:
    page = MagicMock()
    missing_file = tmp_path / "missing.pdf"
    assert _upload_resume(page, missing_file) is False

    valid_file = tmp_path / "valid.pdf"
    valid_file.write_bytes(b"%PDF-1.4 mock content")
    file_input = MagicMock()
    file_input.count.return_value = 1
    page.locator.return_value = file_input
    assert _upload_resume(page, valid_file) is True


def test_upload_cover_letter_only_targets_the_matching_file_field(tmp_path: Path) -> None:
    cover_letter = tmp_path / "cover_letter.pdf"
    cover_letter.write_bytes(b"%PDF-1.4 mock content")
    page = MagicMock()
    inputs = MagicMock()
    inputs.count.return_value = 2
    resume_input = MagicMock()
    resume_input.evaluate.return_value = "Resume upload"
    cover_input = MagicMock()
    cover_input.evaluate.return_value = "Cover Letter upload"
    inputs.nth.side_effect = [resume_input, cover_input]
    page.locator.return_value = inputs

    assert _upload_cover_letter(page, cover_letter) is True
    resume_input.set_input_files.assert_not_called()
    cover_input.set_input_files.assert_called_once_with(str(cover_letter))


def test_greenhouse_parser_and_main_help() -> None:
    parser = _parser()
    assert parser.prog is not None
    
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
