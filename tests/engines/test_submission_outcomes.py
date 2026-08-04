from job_application_automation.engines.submission_outcomes import (
    classify_rejection,
    confirms_submission,
)


def test_confirms_submission_requires_positive_evidence_without_failure_text() -> None:
    options = {
        "success_phrases": ("application received",),
        "failure_phrases": ("could not submit",),
    }

    assert confirms_submission("application received", **options)
    assert not confirms_submission("could not submit application received", **options)
    assert not confirms_submission("application form", **options)


def test_classify_rejection_prioritizes_spam_and_handles_no_match() -> None:
    options = {
        "spam_phrases": ("possible spam",),
        "rejection_phrases": ("unable to accept", "possible spam"),
    }

    assert classify_rejection("possible spam", **options) == "FLAGGED_POSSIBLE_SPAM"
    assert classify_rejection("unable to accept", **options) == "SUBMISSION_REJECTED"
    assert classify_rejection("application form", **options) is None
