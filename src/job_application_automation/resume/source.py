"""Lazy loading and validation for tagged resume source material.

The resume generator is imported by the orchestrator and tests.  Reading
candidate data during import made both workflows depend on a local, ignored
file.  This module keeps parsing pure and makes the filesystem boundary
explicit at generation time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResumeSource:
    """Validated immutable source inputs used to constrain generated resumes."""

    text: str
    experience: tuple[dict[str, Any], ...]
    education: tuple[dict[str, str], ...]
    candidate: dict[str, str]

    @property
    def companies(self) -> tuple[str, ...]:
        return tuple(str(entry["company"]) for entry in self.experience)


def parse_tagged_source(text: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse and validate the tagged career history format."""
    experience: list[dict[str, Any]] = []
    education: list[dict[str, str]] = []
    current_experience: dict[str, Any] | None = None
    current_education: dict[str, str] | None = None
    in_education = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "EDUCATION":
            in_education = True
            current_experience = None
            continue

        match = re.match(r"^\[([A-Z_]+)(?:\s+([A-Z0-9-]+))?\]\s*(.*)$", line)
        if not match:
            continue
        tag, identifier, value = match.groups()
        value = value.strip()

        if not in_education:
            if tag == "COMPANY":
                current_experience = {
                    "company": value,
                    "location": "",
                    "dates": "",
                    "title": "",
                    "tags": [],
                    "claims": [],
                    "bullets": [],
                }
                experience.append(current_experience)
            elif current_experience is not None:
                if tag in {"BASE_TITLE", "OFFICIAL_TITLE"}:
                    current_experience["title"] = value
                elif tag == "DATES":
                    current_experience["dates"] = value
                elif tag == "LOCATION":
                    current_experience["location"] = value
                elif tag == "TAGS":
                    current_experience["tags"] = [
                        item.strip() for item in value.split(";") if item.strip()
                    ]
                elif tag == "CLAIM":
                    claim = {"id": identifier or "", "text": value}
                    current_experience["claims"].append(claim)
                    current_experience["bullets"].append(value)
        else:
            if tag == "SCHOOL":
                current_education = {
                    "school": value,
                    "degree": "",
                    "dates": "",
                    "details": "",
                }
                education.append(current_education)
            elif current_education is not None:
                if tag == "DEGREE":
                    current_education["degree"] = value
                elif tag == "DATES":
                    current_education["dates"] = value
                elif tag == "DETAILS":
                    current_education["details"] = value

    if len(experience) != 5:
        raise ValueError(
            f"base-resume.txt must contain exactly 5 tagged companies; found {len(experience)}"
        )
    if len(education) < 3:
        raise ValueError(
            f"base-resume.txt must contain at least 3 education records; found {len(education)}"
        )
    required_experience = ("company", "title", "dates", "location")
    for entry in experience:
        missing = [key for key in required_experience if not entry.get(key)]
        if missing or not entry["claims"]:
            raise ValueError(f"Incomplete tagged experience for {entry.get('company')}: {missing}")
    return experience, education


def parse_tagged_candidate(text: str) -> dict[str, str]:
    """Read the identity header that precedes tagged experience records."""
    candidate: dict[str, str] = {}
    supported = {
        "NAME": "name",
        "PREFERRED_NAME": "preferred_name",
        "LOCATION": "location",
        "EMAIL": "email",
        "PHONE": "phone",
        "LINKEDIN": "linkedin",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[COMPANY]"):
            break
        match = re.match(r"^\[([A-Z_]+)\]\s*(.*)$", line)
        if match and match.group(1) in supported:
            candidate[supported[match.group(1)]] = match.group(2).strip()
    required = ("name", "location", "email", "phone", "linkedin")
    missing = [key for key in required if not candidate.get(key)]
    if missing:
        raise ValueError("base-resume.txt candidate section is missing: " + ", ".join(missing))
    return candidate


def load_resume_source(path: Path) -> ResumeSource:
    """Load source material only when a resume generation workflow begins."""
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Tagged resume source file not found: {source_path}")
    text = source_path.read_text(encoding="utf-8").strip()
    experience, education = parse_tagged_source(text)
    return ResumeSource(
        text=text,
        experience=tuple(experience),
        education=tuple(education),
        candidate=parse_tagged_candidate(text),
    )
