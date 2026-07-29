"""Expanded unit and mock integration tests for lever.py ATS engine."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from job_application_automation.engines.lever import (
    _lever_semantic_answer,
    _option_matches_variant,
    _upload_resume,
    _fill_location,
    _select_option,
    _required_issues,
    _captcha_present,
    _parser,
    main,
)


def test_numbered_language_questions_use_candidate_languages_in_order() -> None:
    profile = {"languages": ["English", "French", "Hindi"]}

    assert _lever_semantic_answer("Language 1", profile, {}) == "English"
    assert _lever_semantic_answer("Language 2", profile, {}) == "French"
    assert _lever_semantic_answer("Language 3", profile, {}) == "Hindi"
    assert _lever_semantic_answer("Language 4", profile, {}) is None


def test_nationality_question_uses_candidate_nationality() -> None:
    assert (
        _lever_semantic_answer("What is your nationality?", {"nationality": "Indian"}, {})
        == "Indian"
    )


def test_expected_compensation_range_uses_salary_policy() -> None:
    assert (
        _lever_semantic_answer(
            "What is your expected compensation range?",
            {},
            {"salary_expectation": "Negotiable"},
        )
        == "Negotiable"
    )


def test_country_dropdown_accepts_exact_demonym_stem_only() -> None:
    assert _option_matches_variant("India", "Indian")
    assert _option_matches_variant("Canada", "Canadian")
    assert not _option_matches_variant("Indonesia", "Indian")


def test_notice_period_matches_equivalent_day_option() -> None:
    assert _option_matches_variant("15 Days", "2 weeks")
    assert not _option_matches_variant("30 Days", "2 weeks")


def test_ctc_and_joining_questions_use_configured_policies() -> None:
    profile = {"available_start_date": "2026-08-12"}
    rules = {"current_salary": "Prefer not to disclose", "salary_expectation": "Negotiable"}

    assert _lever_semantic_answer("How soon are you able to join if selected?", profile, rules)
    assert (
        _lever_semantic_answer(
            "What is your current CTC (Current Cost to Company)?", profile, rules
        )
        == "Prefer not to disclose"
    )
    assert (
        _lever_semantic_answer(
            "What is your expected CTC (Expected Cost to Company)?", profile, rules
        )
        == "Negotiable"
    )


def test_upload_resume_mocked(tmp_path: Path) -> None:
    page = MagicMock()
    file_input = MagicMock()
    file_input.count.return_value = 1
    page.locator.return_value.first = file_input
    
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"content")

    assert _upload_resume(page, resume_file) is True
    file_input.set_input_files.assert_called_with(str(resume_file))


def test_fill_location_mocked() -> None:
    page = MagicMock()
    loc_input = MagicMock()
    loc_input.input_value.return_value = "San Francisco, CA"

    with patch("job_application_automation.engines.lever.first_visible", side_effect=[loc_input, None]):
        assert _fill_location(page, "San Francisco, CA") is True


def test_select_option_mocked() -> None:
    control = MagicMock()
    options = MagicMock()
    options.count.return_value = 1

    option = MagicMock()
    option.inner_text.return_value = "Yes"
    options.nth.return_value = option

    control.locator.return_value = options
    control.input_value.return_value = "Yes"

    assert _select_option(control, "Do you need visa sponsorship?", "Yes", {}) is True


def test_required_issues_and_captcha_mocked() -> None:
    page = MagicMock()
    page.locator.return_value.all.return_value = []
    assert _required_issues(page) == []

    captcha_loc = MagicMock()
    captcha_loc.count.return_value = 0
    page.locator.return_value = captcha_loc
    assert _captcha_present(page) is False


def test_lever_parser_and_main_help() -> None:
    parser = _parser()
    assert parser.prog is not None

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
