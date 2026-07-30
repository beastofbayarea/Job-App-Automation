"""PDF quality scoring for the generated-resume retry loop.

The scoring policy is data-driven and accepts an optional PyMuPDF-compatible
module, allowing deterministic tests without opening a real PDF or importing
the external dependency at module import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class ResumeScorePolicy:
    """Baseline values used to judge a one-page generated resume."""

    original_character_count: int
    page_height: float
    source_companies: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.original_character_count <= 0:
            raise ValueError("original_character_count must be greater than zero")
        if self.page_height <= 0:
            raise ValueError("page_height must be greater than zero")


def score_pdf(
    pdf_path: str | Path,
    policy: ResumeScorePolicy,
    *,
    fitz_module: Any | None = None,
) -> tuple[int, list[str]]:
    """Score a PDF with the legacy layout/content penalties.

    ``fitz_module`` supports a small fake with an ``open`` method for tests.
    Production callers omit it and import PyMuPDF only when scoring is needed.
    """
    if fitz_module is None:
        try:
            import fitz as fitz_module  # PyMuPDF
        except ImportError:
            return 0, ["PDF scorer dependency is unavailable"]
    try:
        document = fitz_module.open(str(pdf_path))
    except Exception:
        return 0, ["PDF corrupt or unreadable"]

    try:
        score = 100
        feedback: list[str] = []
        if len(document) == 0:
            return 0, ["Empty PDF"]
        if len(document) > 1:
            score -= 40
            feedback.append("OVERFLOW: Resume spans multiple pages. Reduce bullet length slightly.")

        page = document[0]
        blocks = page.get_text("dict").get("blocks", [])
        substantial_ys: list[float] = []
        all_text = ""
        fonts_used: set[str] = set()
        bullet_count = 0
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = "".join(str(span.get("text", "")) for span in spans).strip()
                all_text += line_text + "\n"
                if len(line_text) > 10:
                    for span in spans:
                        if str(span.get("text", "")).strip():
                            bbox = span.get("bbox", ())
                            if len(bbox) > 1:
                                substantial_ys.append(float(bbox[1]))
                            fonts_used.add(str(span.get("font", "")))
                if "\u2022" in line_text or "\u25aa" in line_text:
                    bullet_count += 1

        if substantial_ys:
            bottom_margin = policy.page_height - max(substantial_ys)
            if bottom_margin > 120:
                score -= 25
                feedback.append(
                    f"HUGE EMPTY SPACE: {bottom_margin:.0f}pt bottom margin. "
                    "LLM should write fuller bullets."
                )
            elif bottom_margin > 85:
                score -= 10
                feedback.append(f"Too much empty space: {bottom_margin:.0f}pt bottom margin.")
        else:
            score -= 50
            feedback.append("No substantial content found on page.")

        ratio = len(all_text.strip()) / policy.original_character_count
        if ratio < 0.70:
            score -= 20
            feedback.append(f"CRITICALLY SHORT: {ratio:.0%} of original char count.")
        elif ratio < 0.82:
            score -= 8
            feedback.append(f"Slightly short: {ratio:.0%} of original. Prefer fuller bullets.")

        all_text_lower = all_text.lower()
        missing = [
            company for company in policy.source_companies if company.lower() not in all_text_lower
        ]
        if missing:
            score -= 20
            feedback.append(
                f"Missing companies: {', '.join(missing)}. ALL 5 companies must be present."
            )

        if bullet_count < 12:
            score -= 15
            feedback.append(
                f"Only {bullet_count} bullet points found. Write 3-4 bullets per company."
            )

        has_bold = any("bold" in font.lower() for font in fonts_used)
        has_italic = any("ital" in font.lower() for font in fonts_used)
        if not has_bold or not has_italic:
            score -= 5
        return max(score, 0), feedback
    finally:
        document.close()


def policy_from_source(
    *,
    original_character_count: int,
    page_height: float,
    source_companies: Sequence[str],
) -> ResumeScorePolicy:
    """Build an immutable scoring policy from lazy-loaded source facts."""
    return ResumeScorePolicy(
        original_character_count=original_character_count,
        page_height=page_height,
        source_companies=tuple(str(company) for company in source_companies),
    )
