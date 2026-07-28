"""Injectable PyMuPDF-based validator for the strict one-page cover letter rule.

Mirrors ``resume/scoring.py``'s pattern of accepting an optional ``fitz``-like
module so tests never open a real PDF or import PyMuPDF at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CoverLetterValidationPolicy:
    """Pass/fail thresholds for a rendered cover-letter PDF."""

    minimum_words: int
    maximum_words: int
    required_signature: str

    def __post_init__(self) -> None:
        if self.minimum_words <= 0:
            raise ValueError("minimum_words must be greater than zero")
        if self.maximum_words <= self.minimum_words:
            raise ValueError("maximum_words must be greater than minimum_words")
        if not self.required_signature.strip():
            raise ValueError("required_signature cannot be empty")


def validate_cover_letter_pdf(
    pdf_path: str | Path,
    policy: CoverLetterValidationPolicy,
    *,
    fitz_module: Any | None = None,
) -> tuple[bool, list[str]]:
    """Return ``(is_valid, issues)`` for one rendered cover-letter attempt.

    A two-page (or unreadable) PDF is always invalid; callers must not promote
    it to the final output path.
    """
    if fitz_module is None:
        try:
            import fitz as fitz_module  # PyMuPDF
        except ImportError:
            return False, ["PDF validator dependency is unavailable"]
    try:
        document = fitz_module.open(str(pdf_path))
    except Exception:
        return False, ["PDF corrupt or unreadable"]

    try:
        if len(document) == 0:
            return False, ["Empty PDF"]
        if len(document) > 1:
            return False, [f"Cover letter spans {len(document)} pages; must be exactly one page."]

        text = str(document[0].get_text("text") or "")
        stripped = text.strip()
        if not stripped:
            return False, ["No text extracted from the cover-letter page"]

        issues: list[str] = []
        word_count = len(stripped.split())
        if word_count < policy.minimum_words:
            issues.append(f"Too short: {word_count} words (minimum {policy.minimum_words}).")
        if word_count > policy.maximum_words:
            issues.append(f"Too long: {word_count} words (maximum {policy.maximum_words}).")
        if policy.required_signature.lower() not in stripped.lower():
            issues.append(f"Missing required signature: {policy.required_signature!r}")

        return (len(issues) == 0), issues
    finally:
        document.close()
