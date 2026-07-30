from __future__ import annotations

from pathlib import Path
import fitz  # PyMuPDF
from job_application_automation.resume.cover_letter_rendering import render_cover_letter_pdf


def test_pdf_visual_layout_and_margins(tmp_path: Path) -> None:
    out_pdf = tmp_path / "test_visual_layout.pdf"
    letter_data = {
        "salutation": "Dear Hiring Manager,",
        "paragraphs": [
            "I am writing to express my strong interest in the AI Engineer position.",
            "With extensive experience in Python, PyTorch, and Playwright automation, I have built reliable systems.",
            "Thank you for considering my application. I look forward to hearing from you.",
        ],
        "closing": "Sincerely,",
        "signature": "Jane Doe",
    }
    candidate_data = {
        "name": "Jane Doe",
        "email": "jane.doe@example.com",
        "phone": "+15550199",
        "location": "San Francisco, CA",
    }

    rendered = render_cover_letter_pdf(letter_data, candidate_data, out_pdf)
    assert rendered is True
    assert out_pdf.exists()

    doc = fitz.open(str(out_pdf))
    assert len(doc) == 1, "Cover letter must fit on a single page"

    page = doc[0]
    rect = page.rect
    assert int(rect.width) == 612  # Standard Letter width (8.5 inches * 72)
    assert int(rect.height) == 792  # Standard Letter height (11 inches * 72)

    blocks = page.get_text("dict").get("blocks", [])
    text_y_positions: list[float] = []

    for block in blocks:
        if block.get("type") == 0:  # Text block
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bbox = span.get("bbox", ())
                    if len(bbox) >= 4:
                        text_y_positions.append(float(bbox[3]))

    assert len(text_y_positions) > 0, "PDF page must contain text elements"
    max_y = max(text_y_positions)
    bottom_margin = rect.height - max_y
    assert bottom_margin > 50, f"Bottom margin must be at least 50pt, got {bottom_margin:.1f}pt"

    doc.close()
