from __future__ import annotations

from pathlib import Path
import pytest

from job_application_automation.resume.cover_letter_rendering import (
    CoverLetterRenderRequest,
    render_cover_letter_pdf,
)
from job_application_automation.resume.scoring import (
    ResumeScorePolicy,
    policy_from_source,
    score_pdf,
)


def test_resume_score_policy_validation() -> None:
    with pytest.raises(ValueError, match="original_character_count must be greater than zero"):
        ResumeScorePolicy(original_character_count=0, page_height=792.0, source_companies=())

    with pytest.raises(ValueError, match="page_height must be greater than zero"):
        ResumeScorePolicy(original_character_count=1000, page_height=-1.0, source_companies=())

    pol = policy_from_source(
        original_character_count=500,
        page_height=792.0,
        source_companies=["Company A", "Company B"],
    )
    assert pol.original_character_count == 500
    assert pol.source_companies == ("Company A", "Company B")


def test_score_pdf_with_fake_fitz() -> None:
    policy = ResumeScorePolicy(
        original_character_count=100,
        page_height=800.0,
        source_companies=("Acme",),
    )

    class FakeSpan:
        def get(self, key: str, default: object = None) -> object:
            if key == "text":
                return "Acme Senior Engineer \u2022"
            if key == "bbox":
                return (0.0, 100.0, 500.0, 115.0)
            if key == "font":
                return "Helvetica-BoldItalic"
            return default

    class FakeLine:
        def get(self, key: str, default: object = None) -> object:
            if key == "spans":
                return [FakeSpan()]
            return default

    class FakeBlock:
        def get(self, key: str, default: object = None) -> object:
            if key == "type":
                return 0
            if key == "lines":
                return [FakeLine()]
            return default

    class FakePage:
        def get_text(self, fmt: str) -> dict:
            return {"blocks": [FakeBlock()]}

    class FakeDoc:
        def __len__(self) -> int:
            return 1

        def __getitem__(self, idx: int) -> FakePage:
            return FakePage()

        def close(self) -> None:
            pass

    class FakeFitz:
        def open(self, path: str) -> FakeDoc:
            return FakeDoc()

    score, feedback = score_pdf("fake.pdf", policy, fitz_module=FakeFitz())
    assert score > 0


def test_cover_letter_render_request_validation() -> None:
    with pytest.raises(ValueError, match="letter must be a mapping"):
        CoverLetterRenderRequest(letter="not a map", candidate={}, output_path=Path("out.pdf"))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="candidate must be a mapping"):
        CoverLetterRenderRequest(letter={}, candidate="not a map", output_path=Path("out.pdf"))  # type: ignore[arg-type]


def test_cover_letter_pdf_render(tmp_path: Path) -> None:
    out_pdf = tmp_path / "cover_letter.pdf"
    letter_data = {
        "salutation": "Dear Hiring Manager,",
        "paragraphs": ["I am writing to express my interest in this position.", "My experience aligns well."],
        "closing": "Sincerely,",
        "signature": "Jane Doe",
    }
    candidate_data = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "555-0199",
        "location": "New York, NY",
    }

    res = render_cover_letter_pdf(letter_data, candidate_data, out_pdf)
    assert res is True
    assert out_pdf.exists()
    assert out_pdf.stat().st_size > 0
