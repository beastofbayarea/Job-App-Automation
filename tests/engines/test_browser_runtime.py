"""Direct contracts for the extracted shared Playwright runtime."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from job_application_automation.core.adapters import BrowserSession as BrowserSessionProtocol
from job_application_automation.engines import browser_runtime


def test_runtime_session_implements_the_injectable_browser_protocol() -> None:
    browser = MagicMock()
    page = MagicMock()
    session = browser_runtime.PlaywrightBrowserSession(
        browser=browser,
        page=page,
        close_browser_on_exit=True,
    )

    assert isinstance(session, BrowserSessionProtocol)
    session.close()

    browser.close.assert_called_once_with()
    page.close.assert_not_called()


def test_navigation_contract_preserves_matching_page_without_reload_or_goto() -> None:
    page = MagicMock()
    page.url = "https://jobs.lever.co/example/role/"
    captcha_checker = MagicMock(return_value=False)

    browser_runtime.navigate_reusing_tab(
        page,
        "https://jobs.lever.co/example/role",
        timeout=5_000,
        captcha_checker=captcha_checker,
    )

    captcha_checker.assert_called_once_with(page)
    page.goto.assert_not_called()
    page.reload.assert_not_called()


def test_navigation_contract_fails_closed_for_captcha_in_matching_page() -> None:
    page = MagicMock()
    page.url = "https://jobs.ashbyhq.com/example/role"

    with pytest.raises(RuntimeError, match="CAPTCHA_REQUIRED"):
        browser_runtime.navigate_reusing_tab(
            page,
            page.url,
            timeout=5_000,
            captcha_checker=lambda _page: True,
        )

    page.goto.assert_not_called()


def test_confirmation_contract_rejects_mixed_success_and_failure_evidence() -> None:
    assert browser_runtime.text_confirms_submission("Thanks a lot for applying")
    assert not browser_runtime.text_confirms_submission(
        "Thanks a lot for applying, but your submission failed"
    )
