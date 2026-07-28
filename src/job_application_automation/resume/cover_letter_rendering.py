"""Injectable boundary for the PDF rendering step of cover-letter generation."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CoverLetterRenderRequest:
    """Inputs required to render one cover-letter artifact."""

    letter: Mapping[str, Any]
    candidate: Mapping[str, Any]
    output_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.letter, Mapping):
            raise ValueError("letter must be a mapping")
        if not isinstance(self.candidate, Mapping):
            raise ValueError("candidate must be a mapping")
        object.__setattr__(self, "output_path", Path(self.output_path))


@runtime_checkable
class CoverLetterRenderer(Protocol):
    """Renders an isolated request without depending on orchestration state."""

    def render(self, request: CoverLetterRenderRequest) -> bool:
        """Render one cover letter and return whether the artifact was written."""


class CallableCoverLetterRenderer:
    """Adapter that wraps a plain rendering callback as a ``CoverLetterRenderer``."""

    def __init__(
        self,
        callback: Callable[[Mapping[str, Any], Mapping[str, Any], Path], bool],
    ) -> None:
        self._callback = callback

    def render(self, request: CoverLetterRenderRequest) -> bool:
        return bool(self._callback(request.letter, request.candidate, request.output_path))


def render_cover_letter(
    renderer: CoverLetterRenderer,
    letter: Mapping[str, Any],
    candidate: Mapping[str, Any],
    output_path: Path,
) -> bool:
    """Create a validated render request and delegate it to a renderer port."""
    return bool(
        renderer.render(
            CoverLetterRenderRequest(letter=letter, candidate=candidate, output_path=output_path)
        )
    )


def render_cover_letter_pdf(
    letter: Mapping[str, Any],
    candidate: Mapping[str, Any],
    output_path: Path,
) -> bool:
    """Render a simple, one-page business letter with ReportLab."""
    from reportlab.lib.pagesizes import letter as LETTER_SIZE
    from reportlab.lib.units import inch
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from xml.sax.saxutils import escape as xml_escape

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER_SIZE,
        leftMargin=1.0 * inch,
        rightMargin=1.0 * inch,
        topMargin=1.0 * inch,
        bottomMargin=1.0 * inch,
    )

    body = ParagraphStyle("Body", fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=10)
    header = ParagraphStyle("Header", fontName="Helvetica-Bold", fontSize=11, spaceAfter=14)

    elements: list[Any] = []
    contact_line = " | ".join(
        str(candidate[key])
        for key in ("name", "email", "phone", "location")
        if candidate.get(key)
    )
    if contact_line:
        elements.append(Paragraph(xml_escape(contact_line), header))

    salutation = str(letter.get("salutation", "")).strip()
    if salutation:
        elements.append(Paragraph(xml_escape(salutation), body))

    for paragraph in letter.get("paragraphs", []) or []:
        elements.append(Paragraph(xml_escape(str(paragraph)), body))

    closing = str(letter.get("closing", "")).strip()
    if closing:
        elements.append(Spacer(1, 6))
        elements.append(Paragraph(xml_escape(closing), body))

    signature = str(letter.get("signature", "")).strip()
    if signature:
        elements.append(Paragraph(xml_escape(signature), body))

    try:
        doc.build(elements)
        return True
    except Exception as exc:
        print(f"  [COVER LETTER PDF] Render error: {exc}", flush=True)
        traceback.print_exc()
        return False


DEFAULT_COVER_LETTER_RENDERER = CallableCoverLetterRenderer(render_cover_letter_pdf)
