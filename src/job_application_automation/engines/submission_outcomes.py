"""Pure submission-outcome classifiers shared by browser engines."""

from __future__ import annotations

from collections.abc import Sequence

from ..core.engine_shared import text_confirms_submission


def confirms_submission(
    body_text: str,
    *,
    success_phrases: Sequence[str],
    failure_phrases: Sequence[str],
) -> bool:
    """Return whether page text is positive submission evidence."""
    return text_confirms_submission(
        body_text,
        success_phrases=success_phrases,
        failure_phrases=failure_phrases,
    )


def classify_rejection(
    body_text: str,
    *,
    spam_phrases: Sequence[str],
    rejection_phrases: Sequence[str],
) -> str | None:
    """Classify explicit spam and generic rejection states."""
    if any(phrase in body_text for phrase in spam_phrases):
        return "FLAGGED_POSSIBLE_SPAM"
    if any(phrase in body_text for phrase in rejection_phrases):
        return "SUBMISSION_REJECTED"
    return None
