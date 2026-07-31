"""Unit and mock integration tests for ashby.py ATS engine."""

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from job_application_automation.engines.ashby import (
    REQUIRED_FIELD_VALIDATION,
    _attach_file,
    _capture_submission_outcome,
    _engine_result,
    _extract_cover_letter_text,
    _safe_filename_part,
    _start_date_from_offset,
    _submission_failure_status,
    _submission_page_outcome,
    _submit_application,
    _wait_for_submission_outcome,
    build_argument_parser,
    expand,
    extract_lowest_salary,
    is_ashby_url,
    load_config,
    retry,
    signal_handler,
)


def test_is_ashby_url() -> None:
    assert is_ashby_url("https://jobs.ashbyhq.com/company/job-id") is True
    assert is_ashby_url("https://ashbyhq.com/company/job-id") is True
    assert is_ashby_url("https://example.com/job") is False
    assert is_ashby_url("") is False


def test_extract_lowest_salary() -> None:
    assert extract_lowest_salary("", "") == "Competitive / Market rate"
    assert extract_lowest_salary("n/a", "") == "Competitive / Market rate"
    assert extract_lowest_salary("$120,000 - $150,000", "") == "$120,000"
    assert extract_lowest_salary("120k - 150k", "") == "$120,000"
    assert extract_lowest_salary("80k - 100k GBP", "London, UK") == "£80,000"
    assert extract_lowest_salary("60k - 80k EUR", "Germany") == "€60,000"
    assert extract_lowest_salary("2500000 INR", "India") == "₹2,500,000"


def test_safe_filename_part() -> None:
    assert _safe_filename_part("Acme Corp / Tech") == "Acme_Corp_Tech"
    assert _safe_filename_part("", fallback="Fallback") == "Fallback"


def test_attach_file_waits_for_ashby_widget_confirmation(tmp_path: Path) -> None:
    cover_letter = tmp_path / "cover_letter.pdf"
    cover_letter.write_bytes(b"%PDF-test")
    page = MagicMock()
    field = MagicMock()
    file_input = MagicMock()
    upload_button = MagicMock()
    field.count.return_value = 1
    field.inner_text.side_effect = ["", "cover_letter.pdf"]
    field.get_by_text.return_value.first = upload_button
    upload_button.count.return_value = 0

    with patch("job_application_automation.engines.ashby.time.sleep"):
        attached = _attach_file(
            page,
            field,
            file_input,
            cover_letter,
            "Cover letter",
        )

    assert attached is True
    file_input.set_input_files.assert_called_once_with(str(cover_letter))


def test_extract_cover_letter_text_preserves_page_boundaries() -> None:
    reader = MagicMock()
    first_page = MagicMock()
    second_page = MagicMock()
    first_page.extract_text.return_value = "Opening paragraph"
    second_page.extract_text.return_value = "Closing paragraph"
    reader.pages = [first_page, second_page]

    with patch(
        "job_application_automation.engines.ashby.PdfReader",
        return_value=reader,
    ):
        text = _extract_cover_letter_text(Path("cover_letter.pdf"))

    assert text == "Opening paragraph\n\nClosing paragraph"


def test_default_relative_start_date() -> None:
    from datetime import date

    base = date(2026, 8, 1)
    res = _start_date_from_offset(offset_days=10, base_date=base)
    assert res == "2026-08-11"


def test_expand_path(tmp_path: Path) -> None:
    test_file = tmp_path / "test.txt"
    test_file.touch()
    expanded = expand(test_file)
    assert isinstance(expanded, Path)
    assert expanded.exists()


def test_retry_success_and_failure() -> None:
    calls = []

    def mock_fn():
        calls.append(1)
        if len(calls) < 2:
            raise ValueError("Temporary failure")
        return "success"

    res = retry(mock_fn, attempts=3, base_delay=0.01, label="test_action")
    assert res == "success"
    assert len(calls) == 2

    def failing_fn():
        raise RuntimeError("Persistent failure")

    with pytest.raises(RuntimeError, match="Persistent failure"):
        retry(failing_fn, attempts=2, base_delay=0.01, label="fail_action")

    with pytest.raises(ValueError, match="attempts must be at least 1"):
        retry(failing_fn, attempts=0)


def test_load_config_valid_and_missing(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        '{"schema_version": 2, "candidate": {"first_name": "Alice"}}', encoding="utf-8"
    )

    with patch("job_application_automation.engines.ashby.load_json_config") as mock_ljc:
        mock_ljc.return_value = {
            "candidate": {"first_name": "Alice"},
            "defaults": {},
            "paths": {},
            "company_overrides": {},
        }
        cfg = load_config(cfg_file)
        assert cfg["candidate"]["first_name"] == "Alice"

    missing_file = tmp_path / "non_existent.json"
    with pytest.raises(FileNotFoundError):
        load_config(missing_file)


def test_signal_handler() -> None:
    import job_application_automation.engines.ashby as ashby_mod

    ashby_mod._shutdown = False
    signal_handler(15, None)
    assert ashby_mod._shutdown is True
    ashby_mod._shutdown = False


@pytest.mark.parametrize(
    ("body_text", "expected"),
    [
        ("Your application was flagged as possible spam.", "FLAGGED_POSSIBLE_SPAM"),
        ("We couldn't submit your application.", "SUBMISSION_REJECTED"),
        ("Apply for this job", None),
    ],
)
def test_submission_failure_status_distinguishes_spam_from_generic_rejection(
    body_text: str,
    expected: str | None,
) -> None:
    assert _submission_failure_status(body_text.lower()) == expected


@pytest.mark.parametrize(
    ("body_text", "expected"),
    [
        ("flagged as possible spam", "FLAGGED_POSSIBLE_SPAM"),
        ("couldn't submit", "SUBMISSION_REJECTED"),
        ("application form remains visible", "SUBMIT_ATTEMPT_UNCONFIRMED"),
    ],
)
def test_submit_application_never_reclicks_after_terminal_or_ambiguous_outcome(
    tmp_path: Path,
    body_text: str,
    expected: str,
) -> None:
    page = MagicMock()
    page.is_closed.return_value = False
    submit = MagicMock()
    submit.count.return_value = 1
    submit.is_visible.return_value = True

    with (
        patch("job_application_automation.engines.ashby.expect") as expect_call,
        patch(
            "job_application_automation.engines.ashby._locate_submit_btn",
            return_value=submit,
        ),
        patch(
            "job_application_automation.engines.ashby._page_body_lower",
            return_value="application form remains visible",
        ),
        patch(
            "job_application_automation.engines.ashby._wait_for_submission_outcome",
            return_value=(expected, body_text),
        ),
        patch("job_application_automation.engines.ashby._capture_submission_outcome") as capture,
        patch("job_application_automation.engines.ashby.smooth_mouse_move"),
        patch("job_application_automation.engines.ashby.human_delay"),
        patch("job_application_automation.engines.ashby.time.sleep"),
    ):
        status = _submit_application(page, tmp_path, "Example")

    expect_call.return_value.to_be_visible.assert_called_once()
    assert submit.click.call_count == 1
    assert status == expected
    capture.assert_called_once_with(page, tmp_path, "Example", expected)


def test_submit_application_reclicks_only_after_required_field_repair(
    tmp_path: Path,
) -> None:
    page = MagicMock()
    page.is_closed.return_value = False
    submit = MagicMock()
    submit.count.return_value = 1
    submit.is_visible.return_value = True
    repair = MagicMock()

    with (
        patch("job_application_automation.engines.ashby.expect"),
        patch(
            "job_application_automation.engines.ashby._locate_submit_btn",
            return_value=submit,
        ),
        patch(
            "job_application_automation.engines.ashby._page_body_lower",
            return_value="application form remains visible",
        ),
        patch(
            "job_application_automation.engines.ashby._wait_for_submission_outcome",
            side_effect=[
                (REQUIRED_FIELD_VALIDATION, "missing entry for required field"),
                ("SUBMITTED & CONFIRMED", "application submitted"),
            ],
        ),
        patch("job_application_automation.engines.ashby._capture_submission_outcome") as capture,
        patch("job_application_automation.engines.ashby.smooth_mouse_move"),
        patch("job_application_automation.engines.ashby.human_delay"),
        patch("job_application_automation.engines.ashby.time.sleep"),
    ):
        status = _submit_application(
            page,
            tmp_path,
            "Example",
            repair_dynamic_fields=repair,
        )

    assert submit.click.call_count == 2
    repair.assert_called_once_with()
    assert status == "SUBMITTED & CONFIRMED"
    capture.assert_called_once_with(
        page,
        tmp_path,
        "Example",
        "SUBMITTED & CONFIRMED",
    )


def test_mixed_rejection_and_required_field_signals_never_trigger_repair(
    tmp_path: Path,
) -> None:
    page = MagicMock()
    page.is_closed.return_value = False
    submit = MagicMock()
    submit.count.return_value = 1
    submit.is_visible.return_value = True
    repair = MagicMock()

    with (
        patch("job_application_automation.engines.ashby.expect"),
        patch(
            "job_application_automation.engines.ashby._locate_submit_btn",
            return_value=submit,
        ),
        patch(
            "job_application_automation.engines.ashby._page_body_lower",
            side_effect=[
                "application form remains visible",
                "missing entry for required field; flagged as possible spam",
            ],
        ),
        patch("job_application_automation.engines.ashby._capture_submission_outcome"),
        patch("job_application_automation.engines.ashby.smooth_mouse_move"),
        patch("job_application_automation.engines.ashby.human_delay"),
    ):
        status = _submit_application(
            page,
            tmp_path,
            "Example",
            repair_dynamic_fields=repair,
        )

    assert status == "FLAGGED_POSSIBLE_SPAM"
    assert submit.click.call_count == 1
    repair.assert_not_called()


def test_submission_outcome_polling_observes_delayed_confirmation() -> None:
    page = MagicMock()
    page.is_closed.return_value = False
    submit = MagicMock()
    submit.count.return_value = 0
    clock = [0.0]

    def advance_clock(seconds: float) -> None:
        clock[0] += seconds

    with (
        patch(
            "job_application_automation.engines.ashby._page_body_lower",
            side_effect=["application form remains visible", "application submitted"],
        ),
        patch(
            "job_application_automation.engines.ashby._locate_submit_btn",
            return_value=submit,
        ),
        patch(
            "job_application_automation.engines.ashby.time.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch(
            "job_application_automation.engines.ashby.time.sleep",
            side_effect=advance_clock,
        ) as sleep,
    ):
        outcome, _ = _wait_for_submission_outcome(
            page,
            timeout_seconds=1,
            poll_seconds=0.5,
        )

    assert outcome == "SUBMITTED & CONFIRMED"
    sleep.assert_called_once_with(0.5)


def test_submission_outcome_polling_observes_delayed_rejection() -> None:
    page = MagicMock()
    page.is_closed.return_value = False
    clock = [0.0]

    def advance_clock(seconds: float) -> None:
        clock[0] += seconds

    with (
        patch(
            "job_application_automation.engines.ashby._page_body_lower",
            side_effect=["application form remains visible", "couldn't submit"],
        ),
        patch(
            "job_application_automation.engines.ashby.time.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch(
            "job_application_automation.engines.ashby.time.sleep",
            side_effect=advance_clock,
        ) as sleep,
    ):
        outcome, _ = _wait_for_submission_outcome(
            page,
            timeout_seconds=1,
            poll_seconds=0.5,
        )

    assert outcome == "SUBMISSION_REJECTED"
    sleep.assert_called_once_with(0.5)


def test_submission_outcome_polling_observes_at_timeout_boundary() -> None:
    page = MagicMock()
    page.is_closed.return_value = False
    clock = [0.0]

    def advance_clock(seconds: float) -> None:
        clock[0] += seconds

    with (
        patch(
            "job_application_automation.engines.ashby._page_body_lower",
            side_effect=["application form remains visible", "couldn't submit"],
        ),
        patch(
            "job_application_automation.engines.ashby.time.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch(
            "job_application_automation.engines.ashby.time.sleep",
            side_effect=advance_clock,
        ) as sleep,
    ):
        outcome, _ = _wait_for_submission_outcome(
            page,
            timeout_seconds=15,
            poll_seconds=15,
        )

    assert outcome == "SUBMISSION_REJECTED"
    sleep.assert_called_once_with(15)


def test_submission_page_outcome_prioritizes_rejection_over_stale_validation() -> None:
    page = MagicMock()
    with patch(
        "job_application_automation.engines.ashby._page_body_lower",
        return_value="missing entry for required field; flagged as possible spam",
    ):
        outcome, _ = _submission_page_outcome(page)

    assert outcome == "FLAGGED_POSSIBLE_SPAM"


def test_click_exception_is_quarantined_without_reclicking(tmp_path: Path) -> None:
    page = MagicMock()
    page.is_closed.return_value = False
    submit = MagicMock()
    submit.count.return_value = 1
    submit.is_visible.return_value = True
    submit.click.side_effect = RuntimeError("uncertain dispatch")

    with (
        patch("job_application_automation.engines.ashby.expect"),
        patch(
            "job_application_automation.engines.ashby._locate_submit_btn",
            return_value=submit,
        ),
        patch(
            "job_application_automation.engines.ashby._page_body_lower",
            return_value="application form remains visible",
        ),
        patch(
            "job_application_automation.engines.ashby._wait_for_submission_outcome",
            return_value=("SUBMIT_ATTEMPT_UNCONFIRMED", ""),
        ),
        patch("job_application_automation.engines.ashby._capture_submission_outcome") as capture,
        patch("job_application_automation.engines.ashby.smooth_mouse_move"),
        patch("job_application_automation.engines.ashby.human_delay"),
    ):
        status = _submit_application(page, tmp_path, "Example")

    assert status == "SUBMIT_ATTEMPT_UNCONFIRMED"
    assert submit.click.call_count == 1
    capture.assert_called_once_with(
        page,
        tmp_path,
        "Example",
        "SUBMIT_ATTEMPT_UNCONFIRMED",
    )


def test_unconfirmed_live_submit_is_encoded_as_possibly_submitted() -> None:
    result = _engine_result("SUBMIT_ATTEMPT_UNCONFIRMED", True)

    assert result["success"] is False
    assert result["submitted"] is True
    assert result["confirmed"] is False


def test_rejection_evidence_is_not_named_as_verified_submission(tmp_path: Path) -> None:
    page = MagicMock()
    cdp = page.context.new_cdp_session.return_value
    cdp.send.return_value = {"data": base64.b64encode(b"png").decode("ascii")}

    _capture_submission_outcome(
        page,
        tmp_path,
        "Example",
        "FLAGGED_POSSIBLE_SPAM",
    )

    evidence = list(tmp_path.iterdir())
    assert len(evidence) == 1
    assert "rejected_possible_spam" in evidence[0].name
    assert "submitted_verified" not in evidence[0].name


def test_direct_api_bypass_is_not_exposed_by_ashby_cli() -> None:
    assert "--direct-api" not in build_argument_parser().format_help()
