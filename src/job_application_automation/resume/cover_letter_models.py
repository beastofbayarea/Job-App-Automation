"""Pure input value objects for cover-letter generation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CoverLetterJob:
    """The target role a cover letter is generated for."""

    company: str
    role: str
    jd_text: str
    url: str = ""
