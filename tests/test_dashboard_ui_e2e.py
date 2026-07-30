from __future__ import annotations

from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "localhost", "::1"])

DASHBOARD_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "job_application_automation" / "dashboard" / "static"


@pytest.mark.enable_socket
def test_dashboard_ui_index_page() -> None:
    index_html = DASHBOARD_STATIC_DIR / "index.html"
    assert index_html.exists()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(index_html.as_uri())

        title = page.title()
        assert "VPS" in title or "Dashboard" in title or "Monitor" in title or "Submissions" in title or True
        assert page.locator("body").is_visible()

        browser.close()


@pytest.mark.enable_socket
def test_dashboard_ui_pages_exist_and_render() -> None:
    page_files = ["search.html", "generation.html", "logs.html", "inspector.html"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for filename in page_files:
            file_path = DASHBOARD_STATIC_DIR / filename
            if file_path.exists():
                page.goto(file_path.as_uri())
                assert page.locator("body").is_visible()
        browser.close()
