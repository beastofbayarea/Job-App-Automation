"""Contracts for provider-neutral browser controls and legacy facades."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from job_application_automation.core import engine_shared
from job_application_automation.engines import _browser_form as browser_form
from job_application_automation.engines import browser_controls


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
