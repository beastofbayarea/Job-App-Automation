from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from job_application_automation.engines import _browser_form as browser_form
from job_application_automation.engines import workable


def _profile() -> dict[str, object]:
    return {
        "candidate": {
            "identity": {"first_name": "Jane", "last_name": "Doe"},
            "contact": {
                "phone": "+1 555 0100",
                "fallback_email": "jane@example.com",
            },
            "address": {
                "street_address": "1 Main Street",
                "city": "San Francisco",
                "zip_code": "94105",
                "country": "United States",
            },
        }
    }


def _resume(tmp_path: Path) -> Path:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 test resume")
    return path


def _session() -> tuple[SimpleNamespace, MagicMock]:
    page = MagicMock()
    browser = MagicMock()
    session = SimpleNamespace(page=page, browser=browser, close_browser_on_exit=True)
    return session, page


def _successful_fill() -> tuple[dict[str, bool], list[str]]:
    return (
        {
            "first_name": True,
            "last_name": True,
            "full_name": False,
            "email": True,
            "phone": True,
            "headline": False,
            "linkedin": False,
            "github": False,
            "portfolio": False,
            "resume": True,
            "cover_letter": False,
        },
        [],
    )


def _run_with_browser_mocks(
    tmp_path: Path,
    *,
    live_submit: bool,
    missing_required: list[str] | None = None,
    captcha_values: list[bool] | None = None,
    submit_button: object | None = None,
    confirmed: bool = False,
    preexisting_confirmation: bool = False,
) -> tuple[dict[str, object], SimpleNamespace, MagicMock]:
    session, page = _session()
    apply_and_submit = [None, None, submit_button]
    with (
        patch.object(browser_form, "sync_playwright", return_value=nullcontext(MagicMock())),
        patch.object(
            browser_form,
            "open_chrome_session",
            return_value=session,
        ) as open_session,
        patch.object(browser_form, "navigate_reusing_tab"),
        patch.object(browser_form, "_dismiss_cookie_banner"),
        patch.object(browser_form, "_wait_for_application_entry"),
        patch.object(browser_form, "_closed_job_reason", return_value=""),
        patch.object(browser_form, "_first_visible_for", side_effect=apply_and_submit),
        patch.object(browser_form, "_fill_standard_fields", return_value=_successful_fill()),
        patch.object(browser_form, "_fill_custom_questions", return_value={}),
        patch.object(browser_form, "_stabilize_email_fields"),
        patch.object(browser_form, "_repair_forbidden_text_characters", return_value=[]),
        patch.object(browser_form, "fill_required_consent", return_value=[]),
        patch.object(
            browser_form,
            "page_has_captcha",
            side_effect=captcha_values or [False],
        ),
        patch.object(
            browser_form,
            "validate_required_fields",
            return_value=missing_required or [],
        ),
        patch.object(browser_form, "capture_screenshot", return_value="proof.png"),
        patch.object(
            browser_form,
            "confirmation_visible",
            side_effect=[preexisting_confirmation] + [confirmed] * 20,
        ),
    ):
        result = browser_form.run_browser_form_engine(
            workable.SPEC,
            url="https://apply.workable.com/example/j/ABC123/",
            resume=_resume(tmp_path),
            config=_profile(),
            live_submit=live_submit,
            screenshot_dir=tmp_path,
        )
    assert open_session.call_args.kwargs["headless"] is True
    return result, session, page


def test_candidate_fields_uses_profile_fallback_email() -> None:
    candidate = browser_form.candidate_fields(_profile(), None)
    assert candidate.first_name == "Jane"
    assert candidate.last_name == "Doe"
    assert candidate.email == "jane@example.com"
    assert candidate.phone == "+1 555 0100"
    assert candidate.street_address == "1 Main Street"
    assert candidate.city == "San Francisco"
    assert candidate.postcode == "94105"
    assert candidate.country == "United States"


def test_application_entry_waits_when_initial_apply_control_is_late(tmp_path: Path) -> None:
    session, _ = _session()
    session.page.url = "https://apply.workable.com/example/j/ABC123/"
    apply_button = MagicMock()
    apply_button.get_attribute.return_value = "/example/j/ABC123/apply/"
    with (
        patch.object(browser_form, "sync_playwright", return_value=nullcontext(MagicMock())),
        patch.object(browser_form, "open_chrome_session", return_value=session),
        patch.object(browser_form, "navigate_reusing_tab") as navigate,
        patch.object(browser_form, "_dismiss_cookie_banner"),
        patch.object(browser_form, "_wait_for_application_entry") as wait_for_entry,
        patch.object(browser_form, "_wait_for_form"),
        patch.object(browser_form, "_closed_job_reason", return_value=""),
        patch.object(
            browser_form,
            "_first_visible_for",
            side_effect=[None, apply_button, None],
        ),
        patch.object(browser_form, "_fill_standard_fields", return_value=_successful_fill()),
        patch.object(browser_form, "_fill_custom_questions", return_value={}),
        patch.object(browser_form, "_stabilize_email_fields"),
        patch.object(browser_form, "_repair_forbidden_text_characters", return_value=[]),
        patch.object(browser_form, "fill_required_consent", return_value=[]),
        patch.object(browser_form, "page_has_captcha", return_value=False),
        patch.object(browser_form, "validate_required_fields", return_value=[]),
        patch.object(browser_form, "capture_screenshot", return_value="proof.png"),
    ):
        result = browser_form.run_browser_form_engine(
            workable.SPEC,
            url="https://apply.workable.com/example/j/ABC123/",
            resume=_resume(tmp_path),
            config=_profile(),
            live_submit=False,
            screenshot_dir=tmp_path,
        )

    wait_for_entry.assert_called_once_with(session.page, workable.SPEC, 30000)
    assert navigate.call_count == 2
    assert result["status"] == "PREFILLED_ONLY"


def test_maximum_option_ranking_rejects_no_experience_even_when_list_is_reversed() -> None:
    assert browser_form._option_strength(
        "Extensive experience with multi-component AI systems in production"
    ) > browser_form._option_strength("No experience with multi-component AI systems")


def test_smartrecruiters_combobox_selects_custom_spl_option() -> None:
    page = MagicMock()
    control = MagicMock()
    control.evaluate.return_value = "input"
    attributes = {
        "readonly": None,
        "aria-autocomplete": "list",
        "aria-controls": "menu-city",
        "aria-owns": None,
        "aria-invalid": "false",
    }
    control.get_attribute.side_effect = attributes.get
    control.locator.return_value.count.return_value = 0
    control.input_value.return_value = "San Francisco, CA, US"
    options = MagicMock()
    options.count.return_value = 1
    option = options.nth.return_value
    option.inner_text.return_value = "San Francisco, CA, US"
    page.locator.return_value = options

    selected = browser_form._select_combobox(page, control, ("San Francisco",))

    assert selected is True
    assert "spl-select-option" in page.locator.call_args.args[0]
    option.click.assert_called_once()


def test_smartrecruiters_custom_radio_group_selects_configured_answer() -> None:
    page = MagicMock()
    controls = MagicMock()
    yes = MagicMock()
    no = MagicMock()
    controls.count.return_value = 2
    controls.nth.side_effect = lambda index: yes if index == 0 else no
    yes.get_attribute.side_effect = lambda name: (
        "radio" if name == "role" else "true" if name == "aria-checked" else None
    )
    no.get_attribute.side_effect = lambda name: (
        "radio" if name == "role" else "false" if name == "aria-checked" else None
    )
    with (
        patch.object(browser_form, "_group_controls", return_value=controls),
        patch.object(browser_form, "_question_label", return_value="Valid work permit?"),
        patch.object(
            browser_form,
            "_option_label",
            side_effect=lambda item: "Yes" if item is yes else "No",
        ),
    ):
        selected = browser_form._choose_group_options(
            page,
            yes,
            "radio",
            ("Yes",),
        )

    assert selected is True
    yes.click.assert_called_once()
    no.click.assert_not_called()


def test_smartrecruiters_uses_the_background_cdp_fallback() -> None:
    from job_application_automation.engines import smartrecruiters

    assert smartrecruiters.SPEC.background_cdp is True
    assert smartrecruiters.SPEC.first_name_selectors[0].startswith("input#")
    assert smartrecruiters.SPEC.cover_letter_text_selectors[0].startswith("textarea#")
    assert 'spl-button:has-text("Submit")' in smartrecruiters.SPEC.submit_selectors
    assert smartrecruiters.SPEC.next_selectors == ('spl-button:has-text("Next")',)
    assert workable.SPEC.background_cdp is False


def test_stabilize_email_fields_refills_confirmation_after_dynamic_rerender() -> None:
    page = MagicMock()
    email = MagicMock()
    confirmation = MagicMock()
    email.input_value.return_value = "jane@example.com"
    confirmation.input_value.return_value = "jane@example.com"
    filled = {"email": False, "email_confirmation": False}
    spec = replace(
        workable.SPEC,
        email_confirmation_selectors=("#confirm-email",),
    )
    with patch.object(
        browser_form,
        "_first_visible_for",
        side_effect=[email, confirmation],
    ):
        browser_form._stabilize_email_fields(
            page,
            spec,
            browser_form.CandidateFields(
                first_name="Jane",
                last_name="Doe",
                email="jane@example.com",
                phone="",
                headline="",
                linkedin="",
                github="",
                portfolio="",
                street_address="",
                city="",
                postcode="",
                country="",
            ),
            filled,
        )
    email.fill.assert_called_once_with("jane@example.com")
    confirmation.fill.assert_called_once_with("jane@example.com")
    email.blur.assert_called_once()
    confirmation.blur.assert_called_once()
    assert filled == {"email": True, "email_confirmation": True}


def test_repair_forbidden_text_characters_replaces_reported_semicolon() -> None:
    page = MagicMock()
    control = MagicMock()
    control.is_visible.return_value = True
    control.input_value.return_value = "A strong fit; ready to contribute."
    control.get_attribute.side_effect = lambda name: (
        "hiring-manager-message-input" if name == "id" else ""
    )
    page.locator.return_value.count.return_value = 1
    page.locator.return_value.nth.return_value = control
    with patch.object(browser_form, "_forbidden_characters", return_value={";"}):
        repaired = browser_form._repair_forbidden_text_characters(page)
    control.fill.assert_called_once_with("A strong fit, ready to contribute.")
    control.blur.assert_called_once()
    assert repaired == ["hiring-manager-message-input"]


def test_positive_checkbox_policy_is_not_applied_to_document_declarations() -> None:
    rules = {"interest_checkbox_selection": "all"}
    assert not browser_form._select_all_positive_checkbox_answers(
        "Please attach a copy of your academic degree and work references.",
        rules,
    )
    assert browser_form._select_all_positive_checkbox_answers(
        "Which areas of product management interest you?",
        rules,
    )
    assert browser_form._select_all_positive_checkbox_answers(
        "How do you use AI tools in your workflows? Select all that apply.",
        rules,
    )


def test_attention_check_uses_exact_option_disclosed_in_job_description() -> None:
    assert (
        browser_form._answer_from_job_context(
            "What is Andrew's favorite ice cream flavor?",
            ("Salted caramel", "Salted caramel", "Cookies & Cream", "Pistachio", "Pistachio"),
            "Ship the right problems while knowing Andrew's favorite flavor is pistachio.",
        )
        == "Pistachio"
    )


def test_regular_screening_question_does_not_infer_from_job_description() -> None:
    assert (
        browser_form._answer_from_job_context(
            "Which product area have you worked in?",
            ("Security", "Infrastructure"),
            "This role owns our security and infrastructure roadmap.",
        )
        == ""
    )


def test_does_your_language_binary_question_uses_professional_policy() -> None:
    assert browser_form._is_professional_binary_question(
        "Does your level of Polish and English allow fluent work communication?"
    )


def test_repeatable_language_fields_use_profile_language_and_proficiency() -> None:
    profile = {"languages": ["English", "French"]}
    rules = {"language_proficiency": "C1-C2 or native"}

    assert (
        browser_form._repeatable_language_answer(
            "Language input for entry 1",
            profile,
            rules,
        )
        == "English"
    )
    assert (
        browser_form._repeatable_language_answer(
            "Level for , language entry 1",
            profile,
            rules,
        )
        == "C1-C2 or native"
    )


def test_technical_skills_prompt_is_answered_from_candidate_evidence() -> None:
    assert browser_form.is_essay_question("What are your main technical skills?")


@pytest.mark.parametrize(
    ("configured", "expected"),
    (
        ("more than 8 years", "YES"),
        ("C1-C2 or native", "YES"),
        ("No sponsorship required", "NO"),
        ("Prefer not to disclose", ""),
    ),
)
def test_configured_answers_are_mapped_to_explicit_binary_options(
    configured: str,
    expected: str,
) -> None:
    assert browser_form._answer_for_binary_options(configured, ("YES", "NO")) == expected


def test_load_orchestrated_config_normalizes_grouped_profile() -> None:
    raw = {
        "schema_version": 2,
        "candidate": {
            "identity": {"first_name": "Jane", "last_name": "Doe"},
            "contact": {"fallback_email": "jane@example.com"},
        },
    }
    with (
        patch.object(browser_form, "orchestrated_config_path", return_value=Path("profile.json")),
        patch.object(browser_form, "load_json_config", return_value=raw),
    ):
        normalized = browser_form.load_orchestrated_config()
    assert normalized["candidate"]["first_name"] == "Jane"
    assert normalized["candidate"]["fallback_email"] == "jane@example.com"


@pytest.mark.parametrize(
    "url",
    (
        "https://apply.workable.com/example/",
        "https://example.com/example/j/ABC123/",
        "http://apply.workable.com/example/j/ABC123/",
    ),
)
def test_engine_rejects_board_root_and_wrong_hosts(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="job-specific Workable"):
        browser_form.run_browser_form_engine(
            workable.SPEC,
            url=url,
            resume=_resume(tmp_path),
            config=_profile(),
        )


def test_fill_only_success_requires_critical_and_required_fields(tmp_path: Path) -> None:
    result, session, _ = _run_with_browser_mocks(tmp_path, live_submit=False)
    assert result["success"] is True
    assert result["status"] == "PREFILLED_ONLY"
    assert result["filled_fields"]["email"] is True
    assert session.browser.close.called


def test_captcha_gate_takes_precedence_over_unrendered_required_fields(
    tmp_path: Path,
) -> None:
    result, _, _ = _run_with_browser_mocks(
        tmp_path,
        live_submit=False,
        missing_required=["Candidate name"],
        captcha_values=[True],
    )

    assert result["status"] == "CAPTCHA_REQUIRED"
    assert result["captcha_present"] is True
    assert result["missing_required"] == ["Candidate name"]
    assert result["submitted"] is False


def test_missing_required_fields_block_live_submission(tmp_path: Path) -> None:
    result, _, page = _run_with_browser_mocks(
        tmp_path,
        live_submit=True,
        missing_required=["Work authorization*"],
    )
    assert result["success"] is False
    assert result["status"] == "REQUIRED_FIELDS_NOT_FILLED"
    assert result["submitted"] is False
    page.click.assert_not_called()


def test_captcha_blocks_live_submission_before_click(tmp_path: Path) -> None:
    result, _, _ = _run_with_browser_mocks(
        tmp_path,
        live_submit=True,
        captcha_values=[True],
    )
    assert result["status"] == "CAPTCHA_REQUIRED"
    assert result["submitted"] is False


def test_missing_submit_button_is_explicit_failure(tmp_path: Path) -> None:
    result, _, _ = _run_with_browser_mocks(tmp_path, live_submit=True)
    assert result["status"] == "SUBMIT_BUTTON_NOT_FOUND"
    assert result["submitted"] is False


def test_confirmation_is_required_for_successful_submission(tmp_path: Path) -> None:
    submit = MagicMock()
    result, _, _ = _run_with_browser_mocks(
        tmp_path,
        live_submit=True,
        captcha_values=[False, False] * 20,
        submit_button=submit,
        confirmed=True,
    )
    submit.click.assert_called_once()
    assert result["status"] == "SUBMITTED & CONFIRMED"
    assert result["submitted"] is True
    assert result["confirmed"] is True
    assert result["success"] is True


def test_post_submit_captcha_is_not_reported_as_success(tmp_path: Path) -> None:
    result, _, _ = _run_with_browser_mocks(
        tmp_path,
        live_submit=True,
        captcha_values=[False, True],
        submit_button=MagicMock(),
        confirmed=False,
    )
    assert result["status"] == "CAPTCHA_REQUIRED"
    assert result["submitted"] is True
    assert result["confirmed"] is False
    assert result["success"] is False


def test_preexisting_confirmation_blocks_duplicate_submit(tmp_path: Path) -> None:
    submit = MagicMock()
    result, _, _ = _run_with_browser_mocks(
        tmp_path,
        live_submit=True,
        captcha_values=[False],
        submit_button=submit,
        preexisting_confirmation=True,
    )
    submit.click.assert_not_called()
    assert result["status"] == "CONFIRMATION_PRESENT_BEFORE_SUBMIT"
    assert result["preexisting_confirmation"] is True
    assert result["submitted"] is False


def test_required_field_inspection_fails_closed() -> None:
    page = MagicMock()
    page.locator.return_value.evaluate_all.side_effect = RuntimeError("DOM unavailable")
    assert browser_form._required_issues(page) == ["Required-field inspection failed"]


def test_resume_upload_is_always_a_critical_gate(tmp_path: Path) -> None:
    candidate = browser_form.candidate_fields(_profile(), None)
    with (
        patch.object(browser_form, "fill_first", return_value=True),
        patch.object(browser_form, "_upload_first", return_value=False),
    ):
        filled, missing = browser_form._fill_standard_fields(
            MagicMock(),
            workable.SPEC,
            candidate,
            _resume(tmp_path),
            None,
        )
    assert filled["resume"] is False
    assert "resume" in missing


def test_navigation_failures_are_not_swallowed(tmp_path: Path) -> None:
    session, _ = _session()
    with (
        patch.object(browser_form, "sync_playwright", return_value=nullcontext(MagicMock())),
        patch.object(browser_form, "open_chrome_session", return_value=session),
        patch.object(
            browser_form,
            "navigate_reusing_tab",
            side_effect=TimeoutError("navigation failed"),
        ),
    ):
        with pytest.raises(TimeoutError, match="navigation failed"):
            browser_form.run_browser_form_engine(
                workable.SPEC,
                url="https://apply.workable.com/example/j/ABC123/",
                resume=_resume(tmp_path),
                config=_profile(),
            )
    assert session.browser.close.called
