from __future__ import annotations

from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "localhost", "::1"])


MOCK_GREENHOUSE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Greenhouse Mock Job Application</title></head>
<body>
  <form id="application_form">
    <label for="first_name">First Name *</label>
    <input type="text" id="first_name" name="first_name" required value="" />

    <label for="last_name">Last Name *</label>
    <input type="text" id="last_name" name="last_name" required value="" />

    <label for="email">Email *</label>
    <input type="email" id="email" name="email" required value="" />

    <label for="phone">Phone</label>
    <input type="tel" id="phone" name="phone" value="" />

    <label for="resume">Resume / CV *</label>
    <input type="file" id="resume" name="resume" required />

    <button type="submit" id="submit_app">Submit Application</button>
  </form>
</body>
</html>
"""

MOCK_ASHBY_HTML = """
<!DOCTYPE html>
<html>
<head><title>Ashby Mock Application</title></head>
<body>
  <div class="_container_123">
    <h1>Senior AI Engineer</h1>
    <input type="text" placeholder="First Name" id="ashby-first-name" />
    <input type="text" placeholder="Last Name" id="ashby-last-name" />
    <input type="email" placeholder="Email Address" id="ashby-email" />
    <div class="_file_upload_widget">
      <span>Upload Resume</span>
      <input type="file" id="ashby-resume" />
    </div>
    <button id="ashby-submit">Submit</button>
  </div>
</body>
</html>
"""


@pytest.mark.enable_socket
def test_playwright_mock_greenhouse_form(tmp_path: Path) -> None:
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"%PDF-mock-resume-content")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(MOCK_GREENHOUSE_HTML)

        # Fill fields
        page.fill("#first_name", "Jane")
        page.fill("#last_name", "Doe")
        page.fill("#email", "jane.doe@example.com")
        page.fill("#phone", "+15550199")
        page.set_input_files("#resume", str(resume_file))

        assert page.input_value("#first_name") == "Jane"
        assert page.input_value("#last_name") == "Doe"
        assert page.input_value("#email") == "jane.doe@example.com"
        assert page.input_value("#phone") == "+15550199"

        browser.close()


@pytest.mark.enable_socket
def test_playwright_mock_ashby_form(tmp_path: Path) -> None:
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"%PDF-mock-resume-content")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(MOCK_ASHBY_HTML)

        page.fill("#ashby-first-name", "John")
        page.fill("#ashby-last-name", "Smith")
        page.fill("#ashby-email", "john.smith@example.com")
        page.set_input_files("#ashby-resume", str(resume_file))

        assert page.input_value("#ashby-first-name") == "John"
        assert page.input_value("#ashby-last-name") == "Smith"

        browser.close()
