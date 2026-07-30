"""Pure planning and status helpers for Ashby application form sections.

This module intentionally has no Playwright dependency.  The browser-facing
engine retains its selectors and interaction order while delegating small,
deterministic decisions here for focused testing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable, Mapping


_SELECTED_CHOICE_CLASS = re.compile(r"(?:^|[_\-\s])(active|selected)(?:[_\-\s]|$)")


def normalize_question_text(value: Any) -> str:
    """Normalize a question for the engine's fragment-based answer lookup."""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def normalize_configured_value(value: Any) -> str | None:
    """Convert an explicitly configured answer to the engine's text form.

    ``None`` and an actual empty string mean an answer was not configured.
    Whitespace-only values intentionally remain an empty normalized string to
    preserve the legacy answer precedence: their matching fragment still wins
    over lower-priority rules.
    """
    if value in (None, ""):
        return None
    return str(value).strip()


def configured_screening_answer(
    answers: object,
    question_text: str,
) -> str | None:
    """Return the first explicit answer whose configured fragment matches.

    This deliberately preserves the legacy dictionary-order precedence and
    only accepts a plain dictionary, matching the existing profile contract.
    """
    if not isinstance(answers, dict):
        return None

    normalized_question = normalize_question_text(question_text)
    for fragment, answer in answers.items():
        if str(fragment).strip().lower() in normalized_question:
            normalized_answer = normalize_configured_value(answer)
            if normalized_answer is not None:
                return normalized_answer
    return None


@dataclass(frozen=True, slots=True)
class OptionSelectionPlan:
    """Ordered candidate labels for an Ashby choice control."""

    value: str
    candidates: tuple[str, ...]


def plan_option_selection(value: str) -> OptionSelectionPlan:
    """Build the compatible candidate order used for Ashby radio choices."""
    normalized = value.lower()
    if normalized in ("male", "man"):
        candidates = ("Male", "Man")
    elif normalized == "asian":
        candidates = ("Asian", "Asian or Asian American")
    elif normalized in ("indian", "india"):
        candidates = ("Indian", "India")
    elif "," in value:
        candidates = (value, value.split(",")[0].strip())
    elif normalized == "yes":
        candidates = ("Yes",)
    else:
        candidates = (value,)
    return OptionSelectionPlan(value=value, candidates=candidates)


def required_field_flag(
    *,
    label_class: str = "",
    pseudo_content: object = "",
    has_required_control: bool = False,
) -> bool:
    """Return whether an Ashby field exposes any legacy required marker."""
    return "required" in label_class.lower() or "*" in str(pseudo_content) or has_required_control


def choice_is_selected(*, aria_pressed: object, class_name: str = "") -> bool:
    """Recognize Ashby's selected Yes/No button states without DOM access."""
    return aria_pressed == "true" or bool(_SELECTED_CHOICE_CLASS.search(class_name.lower()))


@dataclass(frozen=True, slots=True)
class FormSectionOutcome:
    """Critical-field flags yielded by one browser-facing form section."""

    section: str
    critical_fields: Mapping[str, bool] = field(default_factory=dict)


def aggregate_section_outcomes(
    outcomes: Iterable[FormSectionOutcome],
) -> dict[str, bool]:
    """Merge section flags in execution order, letting later checks win."""
    merged: dict[str, bool] = {}
    for outcome in outcomes:
        merged.update(outcome.critical_fields)
    return merged
