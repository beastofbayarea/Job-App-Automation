"""Unit tests for cover_letter_rendering.py boundary components."""

from pathlib import Path
import pytest

from job_application_automation.resume.cover_letter_rendering import (
    CallableCoverLetterRenderer,
    CoverLetterRenderRequest,
    render_cover_letter,
)


def test_cover_letter_render_request_validation(tmp_path: Path) -> None:
    output_file = tmp_path / "cover_letter.pdf"
    req = CoverLetterRenderRequest(
        letter={"salutation": "Dear Hiring Manager"},
        candidate={"name": "Alice"},
        output_path=output_file,
    )
    assert req.output_path == output_file

    with pytest.raises(ValueError, match="letter must be a mapping"):
        CoverLetterRenderRequest(letter="invalid", candidate={}, output_path=output_file)

    with pytest.raises(ValueError, match="candidate must be a mapping"):
        CoverLetterRenderRequest(letter={}, candidate="invalid", output_path=output_file)


def test_callable_cover_letter_renderer_and_render_helper(tmp_path: Path) -> None:
    output_file = tmp_path / "letter.pdf"

    def mock_callback(letter: dict, candidate: dict, path: Path) -> bool:
        path.write_text("Mock PDF content", encoding="utf-8")
        return True

    renderer = CallableCoverLetterRenderer(mock_callback)
    success = render_cover_letter(
        renderer=renderer,
        letter={"salutation": "Dear Recruiter"},
        candidate={"name": "Bob"},
        output_path=output_file,
    )
    assert success is True
    assert output_file.read_text(encoding="utf-8") == "Mock PDF content"
