from __future__ import annotations

from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "localhost", "::1"])

DASHBOARD_STATIC_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "job_application_automation"
    / "dashboard"
    / "static"
)


@pytest.mark.enable_socket
def test_dashboard_ui_index_page() -> None:
    index_html = DASHBOARD_STATIC_DIR / "index.html"
    assert index_html.exists()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(index_html.as_uri())

        title = page.title()
        assert "Sky Bison" in title
        assert page.locator("body").is_visible()

        browser.close()


@pytest.mark.enable_socket
def test_dashboard_ui_pages_exist_and_render() -> None:
    page_files = {
        "search.html": "Sky Bison",
        "generation.html": "Sky Bison",
        "inspector.html": "Sky Bison",
        "cent-capital.html": "Cent Capital",
        "system-status.html": "Sky Bison",
        "admin.html": "Sky Bison",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for filename, expected_title in page_files.items():
            file_path = DASHBOARD_STATIC_DIR / filename
            assert file_path.exists()
            page.goto(file_path.as_uri())
            assert page.locator("body").is_visible()
            assert expected_title in page.title()
        browser.close()
