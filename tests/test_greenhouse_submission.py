from __future__ import annotations

from job_application_automation.engines.greenhouse import SUBMIT_BUTTON_TEXT_PATTERN


def test_submit_button_pattern_excludes_page_level_apply_cta() -> None:
    assert SUBMIT_BUTTON_TEXT_PATTERN.search("Submit application")
    assert SUBMIT_BUTTON_TEXT_PATTERN.search("Envoyer ma candidature")
    assert SUBMIT_BUTTON_TEXT_PATTERN.search("Postuler")
    assert not SUBMIT_BUTTON_TEXT_PATTERN.search("Apply")
