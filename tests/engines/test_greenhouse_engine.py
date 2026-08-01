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
    _fill_custom_questions,
    _fill_pre_submit_security_challenge,
    _fill_source_checkbox,
    _greenhouse_semantic_answer,
    _option_text_matches,
    _required_empty_fields,
    _submit_control_enabled,
    _upload_cover_letter,
    _upload_resume,
    _parser,
    main,
)


def test_pre_submit_security_challenge_uses_newest_gmail_code_only_in_live_mode() -> None:
    page = MagicMock()
    page.locator.return_value.count.return_value = 0
    with (
        patch(
            "job_application_automation.engines.greenhouse._security_challenge_visible",
            return_value=True,
        ),
        patch(
            "job_application_automation.engines.greenhouse._fill_security_code_from_gmail",
            return_value=True,
        ) as fill_code,
    ):
        assert _fill_pre_submit_security_challenge(
            page,
            "Example Company",
            live_submit=True,
        )
        fill_code.assert_called_once_with(page, "Example Company")
        assert not _fill_pre_submit_security_challenge(
            page,
            "Example Company",
            live_submit=False,
        )
        fill_code.assert_called_once()


def test_pre_submit_security_challenge_accepts_an_already_filled_code() -> None:
    page = MagicMock()
    inputs = MagicMock()
    inputs.count.return_value = 8
    inputs.nth.side_effect = [
        MagicMock(input_value=MagicMock(return_value=character)) for character in "A1B2C3D4"
    ]
    page.locator.return_value = inputs
    with (
        patch(
            "job_application_automation.engines.greenhouse._security_challenge_visible",
            return_value=True,
        ),
        patch(
            "job_application_automation.engines.greenhouse._fill_security_code_from_gmail"
        ) as fill_code,
    ):
        assert _fill_pre_submit_security_challenge(
            page,
            "Example Company",
            live_submit=True,
        )
    fill_code.assert_not_called()


def test_submit_control_must_be_enabled_and_not_aria_disabled() -> None:
    submit = MagicMock()
    submit.is_enabled.return_value = True
    submit.get_attribute.return_value = None
    assert _submit_control_enabled(submit)

    submit.get_attribute.return_value = "true"
    assert not _submit_control_enabled(submit)

    submit.is_enabled.return_value = False
    submit.get_attribute.return_value = None
    assert not _submit_control_enabled(submit)


def test_custom_text_question_blurs_to_commit_greenhouse_validation_state() -> None:
    page = MagicMock()
    body = MagicMock()
    body.inner_text.return_value = ""
    controls = MagicMock()
    controls.count.return_value = 1
    control = MagicMock()
    controls.nth.return_value = control
    control.is_visible.return_value = True
    control.get_attribute.side_effect = lambda name: {
        "id": "country-of-birth",
        "type": "text",
        "name": "country_of_birth",
        "role": "",
        "aria-required": "true",
    }.get(name)
    control.evaluate.return_value = "input"
    control.input_value.return_value = "India"
    page.locator.side_effect = lambda selector: body if selector == "body" else controls

    with (
        patch(
            "job_application_automation.engines.greenhouse._label_for",
            return_value="What is your country of birth?",
        ),
        patch(
            "job_application_automation.engines.greenhouse._configured_answer",
            return_value="India",
        ),
    ):
        result = _fill_custom_questions(
            page,
            {"country_of_birth": "India"},
            {},
            {},
            {},
            {},
            "Example",
            "Product Manager",
            "",
        )

    assert result == {"What is your country of birth?": True}
    control.fill.assert_called_once_with("India")
    control.blur.assert_called_once()


def test_greenhouse_semantic_answers_prevent_observed_matcher_collisions() -> None:
    profile = {
        "current_company": "Current Company",
        "portfolio": "https://example.test/portfolio",
    }
    rules = {"notice_period": "2 weeks"}

    assert (
        _greenhouse_semantic_answer(
            "What is your notice period to your current employer?",
            profile,
            rules,
        )
        == "2 weeks"
    )
    assert (
        _greenhouse_semantic_answer(
            "Where are you currently employed or where were you last employed?",
            profile,
            rules,
        )
        == "Current Company"
    )
    assert (
        _greenhouse_semantic_answer(
            "Please share some samples of your work",
            profile,
            rules,
        )
        == "https://example.test/portfolio"
    )


def test_greenhouse_option_matching_does_not_treat_no_as_none() -> None:
    assert _option_text_matches("No", "No")
    assert _option_text_matches("No", "No, I do not require sponsorship")
    assert not _option_text_matches("No", "None of the above")
    assert _option_text_matches("LinkedIn", "LinkedIn profile")


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
            "summary": "Senior Software Engineer",
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

    with patch(
        "job_application_automation.engines.greenhouse._label_for",
        return_value="I agree to privacy policy",
    ):
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
    with (
        patch("job_application_automation.engines.greenhouse._first_visible", return_value=ctrl),
        patch(
            "job_application_automation.engines.greenhouse._label_for",
            return_value="None of the above",
        ),
    ):
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

    with patch(
        "job_application_automation.engines.greenhouse._label_for", return_value="First Name *"
    ):
        empty_labels = _required_empty_fields(page)
        assert "First Name *" in empty_labels


def test_required_empty_fields_accepts_selected_checkbox_question_group() -> None:
    page = MagicMock()
    controls = MagicMock()
    controls.count.return_value = 2
    first = MagicMock()
    second = MagicMock()
    for control in (first, second):
        control.is_visible.return_value = True
        control.is_checked.return_value = False
        control.get_attribute.side_effect = lambda attr: {
            "type": "checkbox",
            "name": "distinct-option-name",
        }.get(attr)
        control.evaluate.return_value = True
    controls.nth.side_effect = [first, second]
    page.locator.side_effect = lambda selector: (
        MagicMock(count=MagicMock(return_value=0))
        if selector == 'input[name="distinct-option-name"]:checked'
        else controls
    )

    with patch(
        "job_application_automation.engines.greenhouse._label_for",
        side_effect=["LinkedIn", "Indeed"],
    ):
        assert _required_empty_fields(page) == []


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
