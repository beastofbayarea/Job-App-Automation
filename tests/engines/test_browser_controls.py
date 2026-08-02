"""Contracts for provider-neutral browser controls and legacy facades."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from job_application_automation.core import engine_shared
from job_application_automation.engines import _browser_form as browser_form
from job_application_automation.engines import ashby, browser_controls, greenhouse, lever


def test_first_visible_for_respects_selector_and_locator_order() -> None:
    page = MagicMock()
    first_group = MagicMock()
    second_group = MagicMock()
    page.locator.side_effect = [first_group, second_group]
    selected = MagicMock()
    resolver = MagicMock(side_effect=[None, selected])

    result = browser_controls.first_visible_for(
        page,
        (".primary", ".fallback"),
        visible_resolver=resolver,
    )

    assert result is selected
    assert [call.args[0] for call in page.locator.call_args_list] == [".primary", ".fallback"]
    assert [call.args[0] for call in resolver.call_args_list] == [first_group, second_group]


def test_optional_matching_upload_distinguishes_absent_failed_and_successful_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cover-letter.pdf"
    path.write_bytes(b"%PDF-1.4")
    page = MagicMock()
    inputs = page.locator.return_value
    resume_input = MagicMock()
    cover_input = MagicMock()
    inputs.count.return_value = 2
    inputs.nth.side_effect = [resume_input, cover_input]
    contexts = {id(resume_input): "Resume upload", id(cover_input): "Cover-letter upload"}

    result = browser_controls.upload_matching_file(
        page,
        path,
        required_terms=("cover", "letter"),
        context_resolver=lambda control: contexts[id(control)],
    )

    assert result is True
    resume_input.set_input_files.assert_not_called()
    cover_input.set_input_files.assert_called_once_with(str(path))

    inputs.nth.side_effect = [resume_input, cover_input]
    assert (
        browser_controls.upload_matching_file(
            page,
            path,
            required_terms=("portfolio",),
            context_resolver=lambda control: contexts[id(control)],
        )
        is None
    )

    cover_input.set_input_files.side_effect = OSError("detached")
    inputs.nth.side_effect = [cover_input, resume_input]
    assert (
        browser_controls.upload_matching_file(
            page,
            path,
            required_terms=("cover", "letter"),
            context_resolver=lambda control: contexts[id(control)],
        )
        is False
    )


def test_matching_upload_can_preserve_single_input_resume_fallback(tmp_path: Path) -> None:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4")
    page = MagicMock()
    inputs = page.locator.return_value
    target = MagicMock()
    inputs.count.return_value = 1
    inputs.nth.return_value = target
    inputs.first = target

    assert (
        browser_controls.upload_matching_file(
            page,
            path,
            required_terms=("resume",),
            context_resolver=lambda _control: "unlabelled file input",
            fallback_to_single=True,
        )
        is True
    )
    target.set_input_files.assert_called_once_with(str(path))

    target.reset_mock()
    target.set_input_files.side_effect = [OSError("detached"), None]
    assert (
        browser_controls.upload_matching_file(
            page,
            path,
            required_terms=("resume",),
            context_resolver=lambda _control: "Resume upload",
            fallback_to_single=True,
        )
        is True
    )
    assert target.set_input_files.call_count == 2


def test_preferred_upload_failure_remains_terminal(tmp_path: Path) -> None:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4")
    page = MagicMock()
    preferred_group = MagicMock()
    failed_control = MagicMock()
    failed_control.count.return_value = 1
    failed_control.set_input_files.side_effect = OSError("upload rejected")
    preferred_group.first = failed_control
    page.locator.return_value = preferred_group

    assert (
        browser_controls.upload_preferred_file(
            page,
            path,
            preferred_selector=".preferred",
            fallback_selector='input[type="file"]',
        )
        is False
    )
    assert page.locator.call_count == 1


def test_retry_action_preserves_linear_backoff_and_final_exception() -> None:
    calls: list[int] = []
    delays: list[float] = []
    errors: list[tuple[str, int, int, str]] = []

    def succeeds_on_third_attempt() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("temporary")
        return "done"

    result = browser_controls.retry_action(
        succeeds_on_third_attempt,
        attempts=3,
        base_delay=0.5,
        label="navigation",
        sleep=delays.append,
        on_error=lambda label, attempt, total, exc: errors.append(
            (label, attempt, total, str(exc))
        ),
    )

    assert result == "done"
    assert delays == [0.5, 1.0]
    assert errors == [
        ("navigation", 1, 3, "temporary"),
        ("navigation", 2, 3, "temporary"),
    ]

    terminal_error = RuntimeError("terminal")
    with pytest.raises(RuntimeError) as exc_info:
        browser_controls.retry_action(
            lambda: (_ for _ in ()).throw(terminal_error),
            attempts=1,
        )
    assert exc_info.value is terminal_error


def test_scrolled_fill_and_click_controls_preserve_callback_order() -> None:
    control = MagicMock()
    events: list[str] = []

    assert browser_controls.fill_scrolled_control(
        control,
        "value",
        visibility_waiter=lambda _control, _timeout: events.append("visible"),
        before_primary_fill=lambda: events.append("pause"),
    )
    assert events == ["visible", "pause"]
    control.scroll_into_view_if_needed.assert_called_once_with()
    control.click.assert_called_once_with()
    control.fill.assert_called_once_with("value")

    events.clear()
    control.reset_mock()
    assert browser_controls.click_scrolled_control(
        control,
        visibility_waiter=lambda _control, _timeout: events.append("visible"),
        before_click=lambda: events.append("pause"),
        on_success=lambda: events.append("success"),
    )
    assert events == ["visible", "pause", "success"]
    control.scroll_into_view_if_needed.assert_called_once_with()
    control.click.assert_called_once_with()


def test_provider_upload_and_ashby_control_facades_delegate_with_compatibility_policies(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    page = MagicMock()

    with patch.object(greenhouse, "upload_matching_file", return_value=True) as upload:
        assert greenhouse._upload_resume(page, resume)
    assert upload.call_args.kwargs["required_terms"] == ("resume",)
    assert upload.call_args.kwargs["fallback_to_single"] is True

    with patch.object(lever, "upload_preferred_file", return_value=True) as upload:
        assert lever._upload_resume(page, resume)
    assert "resume" in upload.call_args.kwargs["preferred_selector"]
    assert upload.call_args.kwargs["fallback_selector"] == 'input[type="file"]'

    control = MagicMock()
    with patch.object(ashby, "_fill_scrolled_control", return_value=True) as fill_control:
        assert ashby.fill(page, control, "Candidate", timeout=321)
    fill_control.assert_called_once()
    assert fill_control.call_args.kwargs["timeout_ms"] == 321

    with patch.object(ashby, "_click_scrolled_control", return_value=True) as click_control:
        assert ashby.click(control, "Continue", timeout=654)
    click_control.assert_called_once()
    assert click_control.call_args.kwargs["timeout_ms"] == 654


def test_engine_shared_fill_facade_keeps_first_visible_patch_seam() -> None:
    page = MagicMock()
    control = MagicMock()
    control.input_value.return_value = "candidate@example.com"

    with patch.object(engine_shared, "first_visible", return_value=control) as resolver:
        filled = engine_shared.fill_first(
            page,
            ("input[name=email]",),
            "candidate@example.com",
        )

    assert filled is True
    resolver.assert_called_once_with(page.locator.return_value)
    control.fill.assert_called_once_with("candidate@example.com")


def test_browser_form_fill_and_blur_keeps_local_control_patch_seam() -> None:
    page = MagicMock()
    control = MagicMock()
    control.input_value.return_value = "Jane"

    with patch.object(browser_form, "_first_visible_for", return_value=control) as resolver:
        filled = browser_form._fill_and_blur(page, ("input[name=first_name]",), "Jane")

    assert filled is True
    resolver.assert_called_once_with(page, ("input[name=first_name]",))
    control.fill.assert_called_once_with("Jane")
    control.blur.assert_called_once_with()
    page.wait_for_timeout.assert_called_once_with(250)
