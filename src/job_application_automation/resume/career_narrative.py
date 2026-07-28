"""Optional, candidate-approved narrative used only by cover-letter generation.

Missing fields are omitted rather than inferred: this module never guesses a
reason for leaving, a priority, or a tone from job or resume content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CareerNarrative:
    """Candidate-approved wording for cover-letter tone and framing."""

    reason_for_change: str = ""
    next_role_priorities: tuple[str, ...] = ()
    tone: str = ""
    default_salutation: str = "Hiring Team"
    do_not_claim: tuple[str, ...] = ()


def _trimmed_string(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _trimmed_strings(raw: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    value = raw.get(key)
    if not isinstance(value, list):
        return None
    items = tuple(str(item).strip() for item in value if str(item).strip())
    return items or None


def load_career_narrative(profile: Mapping[str, Any]) -> CareerNarrative:
    """Read the optional ``career_narrative`` block, omitting absent fields."""
    raw = profile.get("career_narrative")
    if not isinstance(raw, Mapping):
        raw = {}

    kwargs: dict[str, Any] = {}
    reason = _trimmed_string(raw, "reason_for_change")
    if reason is not None:
        kwargs["reason_for_change"] = reason
    priorities = _trimmed_strings(raw, "next_role_priorities")
    if priorities is not None:
        kwargs["next_role_priorities"] = priorities
    tone = _trimmed_string(raw, "tone")
    if tone is not None:
        kwargs["tone"] = tone
    salutation = _trimmed_string(raw, "default_salutation")
    if salutation is not None:
        kwargs["default_salutation"] = salutation
    excluded = _trimmed_strings(raw, "do_not_claim")
    if excluded is not None:
        kwargs["do_not_claim"] = excluded

    return CareerNarrative(**kwargs)
