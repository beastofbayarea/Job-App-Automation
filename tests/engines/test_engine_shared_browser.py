"""Deterministic interaction tests for shared Playwright-facing helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core.engine_shared import (  # noqa: E402
    navigate_reusing_tab,
    page_has_captcha,
    text_confirms_submission,
)


class FakeLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    def inner_text(self, *, timeout: int | None = None) -> str:
        return ""


class FakePage:
    def __init__(self, url: str, *, captcha_count: int = 0) -> None:
        self.url = url
        self.captcha_count = captcha_count
        self.calls: list[tuple[str, str | int]] = []

    def locator(self, selector: str) -> FakeLocator:
        self.calls.append(("locator", selector))
        return FakeLocator(self.captcha_count)

    def reload(self, *, wait_until: str, timeout: int) -> None:
        self.calls.extend((("reload", wait_until), ("timeout", timeout)))

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.calls.extend((("goto", url), ("wait_until", wait_until), ("timeout", timeout)))


class NavigationTests(unittest.TestCase):
    def test_greenhouse_thanks_a_lot_confirmation_is_recognized(self) -> None:
        self.assertTrue(
            text_confirms_submission(
                "Thanks a lot for applying to Fin! We will review your application carefully."
            )
        )

    def test_matching_page_is_preserved_without_reload(self) -> None:
        page = FakePage("https://boards.greenhouse.io/example/jobs/123/")

        navigate_reusing_tab(
            page,  # type: ignore[arg-type]
            "https://boards.greenhouse.io/example/jobs/123",
            timeout=1234,
        )

        self.assertNotIn(("reload", "domcontentloaded"), page.calls)
        self.assertNotIn(("goto", "https://boards.greenhouse.io/example/jobs/123"), page.calls)

    def test_different_page_is_navigated_with_fake_playwright_page(self) -> None:
        page = FakePage("about:blank")

        navigate_reusing_tab(
            page,  # type: ignore[arg-type]
            "https://jobs.lever.co/example/123",
            timeout=4321,
            wait_until="networkidle",
        )

        self.assertIn(("goto", "https://jobs.lever.co/example/123"), page.calls)
        self.assertIn(("wait_until", "networkidle"), page.calls)
        self.assertNotIn(("reload", "networkidle"), page.calls)

    def test_captcha_in_reused_tab_stops_navigation(self) -> None:
        page = FakePage("https://jobs.ashbyhq.com/example/123", captcha_count=1)

        with self.assertRaisesRegex(RuntimeError, "CAPTCHA_REQUIRED"):
            navigate_reusing_tab(
                page,  # type: ignore[arg-type]
                "https://jobs.ashbyhq.com/example/123",
                timeout=1000,
            )

        self.assertNotIn(("reload", "domcontentloaded"), page.calls)

    def test_cloudflare_turnstile_is_included_in_captcha_detection(self) -> None:
        page = FakePage("https://apply.workable.com/example/j/ABC123/apply", captcha_count=1)

        self.assertTrue(page_has_captcha(page))  # type: ignore[arg-type]
        selector = next(value for kind, value in page.calls if kind == "locator")
        self.assertIn("challenges.cloudflare.com", selector)
        self.assertIn("turnstile", selector)


if __name__ == "__main__":
    unittest.main()
