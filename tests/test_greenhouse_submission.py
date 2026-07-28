from __future__ import annotations

from job_application_automation.engines.greenhouse import (
    SUBMIT_BUTTON_TEXT_PATTERN,
    _security_challenge_visible,
)


def test_submit_button_pattern_excludes_page_level_apply_cta() -> None:
    assert SUBMIT_BUTTON_TEXT_PATTERN.search("Submit application")
    assert SUBMIT_BUTTON_TEXT_PATTERN.search("Envoyer ma candidature")
    assert SUBMIT_BUTTON_TEXT_PATTERN.search("Postuler")
    assert not SUBMIT_BUTTON_TEXT_PATTERN.search("Apply")


def test_security_challenge_selector_requires_all_eight_code_inputs() -> None:
    class Locator:
        def __init__(self, count: int) -> None:
            self._count = count

        def count(self) -> int:
            return self._count

    class Page:
        def __init__(self, count: int) -> None:
            self._count = count

        def locator(self, selector: str) -> Locator:
            assert selector == 'input[id^="security-input"]'
            return Locator(self._count)

    assert not _security_challenge_visible(Page(7))
    assert _security_challenge_visible(Page(8))
