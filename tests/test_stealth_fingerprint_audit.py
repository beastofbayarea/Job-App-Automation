from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright
from job_application_automation.core.adapters import BrowserSettings


@pytest.mark.enable_socket
def test_playwright_browser_stealth_attributes() -> None:
    settings = BrowserSettings(headed=False, timeout_ms=10000)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not settings.headed)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Evaluate navigator properties
        user_agent = page.evaluate("navigator.userAgent")
        languages = page.evaluate("navigator.languages")
        hardware_concurrency = page.evaluate("navigator.hardwareConcurrency")

        assert "Mozilla" in user_agent
        assert isinstance(languages, list)
        assert len(languages) > 0
        assert isinstance(hardware_concurrency, int)

        browser.close()
