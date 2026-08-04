"""Unit and mock integration tests for greenhouse.py ATS engine."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from job_application_automation.engines.greenhouse import (
    CUSTOM_QUESTION_CONTROL_SELECTOR,
    _FormWorkBudget,
    _valid_greenhouse_url,
    _fill_all_visible,
    _fill_all_labeled,
    _load_candidate_evidence,
    _load_personalized_resume_evidence,
    _fill_explicit_required_consents,
    _fill_export_control_questions,
    _fill_custom_questions,
    _fill_pre_submit_security_challenge,
    _fill_security_code_from_gmail,
    _fill_source_checkbox,
    _effective_action_timeout_ms,
    _effective_form_work_timeout_ms,
    _greenhouse_semantic_answer,
    _job_unavailable_after_navigation,
    _option_text_matches,
    _resume_employer_answer,
    _select_first_greenhouse_combobox,
    _select_greenhouse_combobox,
    _select_native_control,
    _required_empty_fields,
    _repair_missing_required_controls,
    _skip_application_topic,
    _submit_control_enabled,
    _upload_cover_letter,
    _upload_resume,
    _parser,
    main,
)


def test_required_combobox_fallback_clicks_first_visible_option() -> None:
    page = MagicMock()
    control = MagicMock()
    options = MagicMock()
    first = MagicMock()
    second = MagicMock()
    options.count.return_value = 2
    options.nth.side_effect = [first, second]
    first.is_visible.return_value = True
    first.inner_text.return_value = "First choice"
    page.locator.return_value = options

    assert _select_first_greenhouse_combobox(page, control)

    first.click.assert_called_once_with()
    second.click.assert_not_called()


def test_combobox_fallback_clears_failed_search_before_selecting_first_option() -> None:
    page = MagicMock()
    control = MagicMock()
    filtered = MagicMock()
    filtered.count.return_value = 1
    filtered.first.wait_for.return_value = None
    full_menu = MagicMock()
    first = MagicMock()
    full_menu.count.return_value = 1
    full_menu.nth.return_value = first
    first.is_visible.return_value = True
    first.inner_text.return_value = "First available choice"
    page.locator.side_effect = [filtered, filtered, filtered, full_menu]

    assert _select_greenhouse_combobox(page, control, ())

    control.fill.assert_called_once_with("")
    first.click.assert_called_once_with()


def test_combobox_fallback_uses_react_select_shell_and_page_keyboard() -> None:
    page = MagicMock()
    control = MagicMock()
    no_options = MagicMock()
    no_options.count.return_value = 0
    no_options.first.wait_for.side_effect = RuntimeError("not mounted")
    page.locator.return_value = no_options
    shell = MagicMock()
    shell.count.return_value = 1
    shell.first.inner_text.return_value = "Yes"
    control.locator.return_value = shell
    control.evaluate.return_value = ""
    control.get_attribute.return_value = None

    assert _select_greenhouse_combobox(page, control, ())

    shell.first.click.assert_called_once_with(force=True)
    page.keyboard.press.assert_any_call("ArrowDown")
    page.keyboard.press.assert_any_call("Enter")


def test_missing_required_repair_reacquires_exact_labeled_combobox() -> None:
    page = MagicMock()
    controls = MagicMock()
    control = MagicMock()
    controls.count.return_value = 1
    controls.nth.return_value = control
    control.is_visible.return_value = True
    control.get_attribute.side_effect = lambda name: {
        "role": "combobox",
    }.get(name)
    control.evaluate.return_value = "input"
    page.get_by_label.return_value = controls

    with (
        patch(
            "job_application_automation.engines.greenhouse._configured_answer",
            return_value="India",
        ),
        patch(
            "job_application_automation.engines.greenhouse._select_greenhouse_combobox",
            return_value=True,
        ) as select,
    ):
        result = _repair_missing_required_controls(
            page,
            ["What is the country of your birth?"],
            {"country_of_birth": "India"},
            {},
            {},
            {},
            {},
        )

    assert result == {"What is the country of your birth?": True}
    page.get_by_label.assert_called_once_with(
        "What is the country of your birth?", exact=True
    )
    select.assert_called_once_with(page, control, ("India",), budget=None)
    control.blur.assert_called_once_with()


def test_missing_required_repair_reuses_discovered_question_mapping() -> None:
    page = MagicMock()
    semantic_controls = MagicMock()
    semantic_controls.count.return_value = 0
    discovered = MagicMock()
    control = MagicMock()
    discovered.count.return_value = 1
    discovered.nth.return_value = control
    control.is_visible.return_value = True
    control.get_attribute.side_effect = lambda name: {
        "role": "combobox",
        "id": "question_country_of_birth",
    }.get(name)
    control.evaluate.return_value = "input"
    page.get_by_label.return_value = semantic_controls
    page.locator.return_value = discovered

    with (
        patch(
            "job_application_automation.engines.greenhouse._label_for",
            return_value="What is the country of your birth? *",
        ),
        patch(
            "job_application_automation.engines.greenhouse._configured_answer",
            return_value="India",
        ),
        patch(
            "job_application_automation.engines.greenhouse._select_greenhouse_combobox",
            return_value=True,
        ) as select,
    ):
        result = _repair_missing_required_controls(
            page,
            ["What is the country of your birth?"],
            {"country_of_birth": "India"},
            {},
            {},
            {},
            {},
        )

    assert result == {"What is the country of your birth?": True}
    page.locator.assert_called_once_with(CUSTOM_QUESTION_CONTROL_SELECTOR)
    select.assert_called_once_with(page, control, ("India",), budget=None)
    control.blur.assert_called_once_with()


def test_missing_required_repair_refills_runtime_email() -> None:
    page = MagicMock()
    controls = MagicMock()
    control = MagicMock()
    controls.count.return_value = 1
    controls.nth.return_value = control
    control.is_visible.return_value = True
    control.get_attribute.side_effect = lambda name: {"type": "text"}.get(name)
    control.evaluate.return_value = "input"
    control.input_value.return_value = "candidate@example.com"
    page.get_by_label.return_value = controls

    result = _repair_missing_required_controls(
        page,
        ["Email"],
        {},
        {},
        {},
        {},
        {},
        "candidate@example.com",
    )

    assert result == {"Email": True}
    control.fill.assert_called_once_with("candidate@example.com")
    control.blur.assert_called_once_with()


def test_greenhouse_action_timeout_is_bounded_per_control() -> None:
    assert _effective_action_timeout_ms(30_000) == 20_000
    assert _effective_action_timeout_ms(14_000) == 14_000
    assert _effective_action_timeout_ms(2_500) == 2_500
    assert _effective_action_timeout_ms(100) == 1_000


def test_greenhouse_form_work_budget_is_bounded_and_expires() -> None:
    timestamps = iter((10.0, 10.01, 10.051))
    budget = _FormWorkBudget(50, clock=lambda: next(timestamps))

    assert budget.available("initial-fill")
    assert not budget.available("problem-control")
    assert _effective_form_work_timeout_ms(1) == 30_000
    assert _effective_form_work_timeout_ms(240_000) == 240_000
    assert _effective_form_work_timeout_ms(999_000) == 300_000


def test_greenhouse_custom_question_loop_stops_at_shared_budget() -> None:
    class StepClock:
        value = 0.0

        def __call__(self) -> float:
            self.value += 0.02
            return self.value

    page = MagicMock()
    body = MagicMock()
    body.inner_text.return_value = ""
    controls = MagicMock()
    controls.count.return_value = 100
    control = MagicMock()
    control.is_visible.return_value = False
    controls.nth.return_value = control
    page.locator.side_effect = lambda selector: body if selector == "body" else controls
    budget = _FormWorkBudget(50, clock=StepClock())

    result = _fill_custom_questions(
        page,
        {},
        {},
        {},
        {},
        {},
        "Example",
        "Product Manager",
        "",
        budget=budget,
    )

    assert result == {}
    assert controls.nth.call_count == 2


def test_country_of_your_birth_uses_dedicated_profile_value() -> None:
    assert (
        _greenhouse_semantic_answer(
            "What is the country of your birth?",
            {"country_of_birth": "India", "country": "United States"},
            {},
        )
        == "India"
    )


def test_current_location_text_prompt_uses_full_profile_location() -> None:
    assert (
        _greenhouse_semantic_answer(
            "Where are you currently located?",
            {
                "location": "San Francisco, California, United States",
                "city": "San Francisco",
                "country": "United States",
            },
            {},
        )
        == "San Francisco, California, United States"
    )


def test_native_select_falls_back_to_first_nonempty_option() -> None:
    control = MagicMock()
    options = MagicMock()
    placeholder = MagicMock()
    first = MagicMock()
    options.count.return_value = 2
    options.nth.side_effect = [placeholder, first]
    placeholder.get_attribute.return_value = ""
    placeholder.inner_text.return_value = "Select..."
    first.get_attribute.return_value = "yes"
    first.inner_text.return_value = "Yes"
    control.locator.return_value = options

    assert _select_native_control(control, ("Unknown",), fallback_first=True)
    control.select_option.assert_called_once_with(value="yes")


def test_fill_all_labeled_populates_duplicate_identity_controls() -> None:
    page = MagicMock()
    controls = MagicMock()
    first = MagicMock()
    second = MagicMock()
    controls.count.return_value = 2
    controls.nth.side_effect = [first, second]
    first.is_visible.return_value = True
    second.is_visible.return_value = True
    first.input_value.return_value = "Candidate"
    second.input_value.return_value = "Candidate"
    page.get_by_label.return_value = controls

    assert _fill_all_labeled(page, r"^first name$", "Candidate")
    first.fill.assert_called_once_with("Candidate")
    second.fill.assert_called_once_with("Candidate")


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
        fill_code.assert_called_once_with(page, "Example Company", budget=None)
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


def test_security_code_poll_is_capped_by_the_shared_form_budget() -> None:
    page = MagicMock()
    code_inputs = MagicMock()
    code_inputs.count.return_value = 8
    page.locator.return_value = code_inputs
    budget = MagicMock(spec=_FormWorkBudget)
    budget.available.return_value = True
    budget.remaining_ms.side_effect = [5_000, 4_000]

    with (
        patch(
            "job_application_automation.engines.greenhouse.get_gmail_read_service",
            return_value=MagicMock(),
        ),
        patch(
            "job_application_automation.engines.greenhouse.load_used_verification_message_ids",
            return_value=set(),
        ),
        patch(
            "job_application_automation.engines.greenhouse.poll_for_verification_code",
            return_value=None,
        ) as poll,
    ):
        assert not _fill_security_code_from_gmail(
            page,
            "Example Company",
            budget=budget,
        )

    page.wait_for_timeout.assert_called_once_with(5_000)
    assert poll.call_args.kwargs["timeout_seconds"] == 4


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


def test_greenhouse_essay_uses_personalized_resume_instead_of_short_default() -> None:
    page = MagicMock()
    body = MagicMock()
    body.inner_text.return_value = "Role description"
    controls = MagicMock()
    controls.count.return_value = 1
    control = MagicMock()
    controls.nth.return_value = control
    control.is_visible.return_value = True
    control.get_attribute.side_effect = lambda name: {
        "id": "question_essay",
        "type": "text",
        "name": "question_essay",
        "role": "",
        "placeholder": "Type here",
        "aria-required": "true",
    }.get(name)
    control.evaluate.return_value = "textarea"
    control.input_value.return_value = "Resume-grounded narrative"
    page.locator.side_effect = lambda selector: body if selector == "body" else controls

    with (
        patch(
            "job_application_automation.engines.greenhouse._label_for",
            return_value="Describe your relevant product experience",
        ),
        patch(
            "job_application_automation.engines.greenhouse._configured_answer",
            return_value="Yes",
        ),
        patch(
            "job_application_automation.engines.greenhouse._generate_essay",
            return_value="Resume-grounded narrative",
        ) as generate,
    ):
        result = _fill_custom_questions(
            page,
            {},
            {},
            {},
            {},
            {},
            "Example",
            "Product Manager",
            "Personalized resume evidence",
        )

    assert result == {"Describe your relevant product experience": True}
    generate.assert_called_once_with(
        "Describe your relevant product experience",
        "Role description",
        "Example",
        "Product Manager",
        "Personalized resume evidence",
    )


def test_load_personalized_resume_evidence_extracts_attached_pdf(tmp_path: Path) -> None:
    import pymupdf

    resume = tmp_path / "personalized.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Personalized Product Manager evidence")
    document.save(resume)
    document.close()

    assert "Personalized Product Manager evidence" in _load_personalized_resume_evidence(
        resume,
        {"candidate_evidence_file": "missing.txt"},
    )


def test_greenhouse_semantic_answers_prevent_observed_matcher_collisions() -> None:
    profile = {
        "current_company": "Current Company",
        "portfolio": "https://example.test/portfolio",
        "education_history": {
            "school": "Example University",
            "field_of_study": "Computer Science",
        },
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
    assert _greenhouse_semantic_answer("School", profile, rules) == "Example University"
    assert _greenhouse_semantic_answer("Discipline", profile, rules) == "Computer Science"


def test_greenhouse_option_matching_does_not_treat_no_as_none() -> None:
    assert _option_text_matches("No", "No")
    assert _option_text_matches("No", "No, I do not require sponsorship")
    assert not _option_text_matches("No", "None of the above")
    assert _option_text_matches("LinkedIn", "LinkedIn profile")


def test_greenhouse_policy_answers_cover_user_supplied_screening_defaults() -> None:
    rules = {
        "security_clearance": "No",
        "government_relationship": "No",
        "conflict_of_interest": "No",
        "outside_activities": "No",
        "employment_restrictions": "No",
        "hourly_rate": "$50/hour",
        "referral_default": "N/A",
        "consent_default": "Yes",
        "relocation": "Yes",
        "permit_status": "Yes",
        "visa_sponsorship": "No",
        "visa_type_not_applicable": "N/A",
        "target_country_work_authorization": "Yes",
        "relocation_support": "No",
        "additional_permanent_residencies": "N/A",
        "export_control_eligibility": "No",
        "work_country_timezone": "USA, ET",
        "city_availability_selection": "first_option",
        "financial_content_experience": "Yes",
        "dealer_partner_supplier_relationship": "No",
        "employment_start_month": "June",
        "employment_start_year": "2022",
        "employment_end_month": "Present",
        "employment_end_year": "Present",
        "language_answers": {"English": "Yes", "German": "No", "Korean": "No"},
    }
    assert _greenhouse_semantic_answer("Do you hold a security clearance?", {}, rules) == "No"
    assert (
        _greenhouse_semantic_answer("What is your expected hourly rate?", {}, rules) == "$50/hour"
    )
    assert _greenhouse_semantic_answer("Will you relocate to London?", {}, rules) == "Yes"
    assert _greenhouse_semantic_answer("Will you require relocation support?", {}, rules) == "No"
    assert _greenhouse_semantic_answer("Are you authorized to work in France?", {}, rules) == "Yes"
    assert _greenhouse_semantic_answer("Do you require visa sponsorship?", {}, rules) == "No"
    assert _greenhouse_semantic_answer("If yes, what type of visa are you on?", {}, rules) == "N/A"
    assert (
        _greenhouse_semantic_answer("Do you reside within the United States?", {}, rules) == "Yes"
    )
    assert (
        _greenhouse_semantic_answer(
            "Please list any additional countries of which you are a lawful permanent resident.",
            {},
            rules,
        )
        == "N/A"
    )
    assert _greenhouse_semantic_answer("EXPORT CONTROLS", {}, rules) == "No"
    assert (
        _greenhouse_semantic_answer("What country and time zone are you based in?", {}, rules)
        == "USA, ET"
    )
    assert (
        _greenhouse_semantic_answer("In what cities are you available to work?", {}, rules)
        == "__FIRST_OPTION__"
    )
    assert (
        _greenhouse_semantic_answer(
            "Have you worked with earnings call transcripts, event transcripts, or financial content/data products?",
            {},
            rules,
        )
        == "Yes"
    )
    assert (
        _greenhouse_semantic_answer(
            "Do you currently, or in the past year, work for or with a dealer, partner or supplier?",
            {},
            rules,
        )
        == "No"
    )
    assert _greenhouse_semantic_answer("Start date month", {}, rules) == "June"
    assert _greenhouse_semantic_answer("Start date year", {}, rules) == "2022"
    assert _greenhouse_semantic_answer("End date month", {}, rules) == "Present"
    assert _greenhouse_semantic_answer("End date year", {}, rules) == "Present"
    assert _greenhouse_semantic_answer("English", {}, rules) == "Yes"
    assert _greenhouse_semantic_answer("What is your level of German?", {}, rules) == "No"
    assert _greenhouse_semantic_answer("Korean", {}, rules) == "No"


def test_resume_employer_answer_uses_generated_resume_companies() -> None:
    evidence = "[COMPANY] AWS\n[COMPANY] Microsoft\n"
    assert _resume_employer_answer("Have you worked at Microsoft before?", evidence) == "Yes"
    assert _resume_employer_answer("Have you worked at MongoDB before?", evidence) == "No"


def test_skip_application_topic_matches_configured_high_school_policy() -> None:
    page = MagicMock()
    page.locator.return_value.inner_text.return_value = (
        "How did you perform in mathematics at high school?"
    )
    assert (
        _skip_application_topic(
            page,
            {"skip_application_question_topics": ["mathematics at high school"]},
        )
        == "mathematics at high school"
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


def test_job_unavailable_detects_greenhouse_board_error_redirect() -> None:
    page = MagicMock()
    page.url = "https://job-boards.greenhouse.io/trgscreen?error=true"

    assert _job_unavailable_after_navigation(page) is True

    page.url = "https://job-boards.greenhouse.io/trgscreen/jobs/4819740101"
    assert _job_unavailable_after_navigation(page) is False


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
