"""Unit and mock integration tests for ashby.py ATS engine."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from job_application_automation.engines.ashby import (
    _attach_file,
    _extract_cover_letter_text,
    _safe_filename_part,
    _start_date_from_offset,
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
    cfg_file.write_text('{"schema_version": 2, "candidate": {"first_name": "Alice"}}', encoding="utf-8")

    with patch("job_application_automation.engines.ashby.load_json_config") as mock_ljc:
        mock_ljc.return_value = {"candidate": {"first_name": "Alice"}, "defaults": {}, "paths": {}, "company_overrides": {}}
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
