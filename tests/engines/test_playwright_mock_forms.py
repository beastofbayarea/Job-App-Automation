from __future__ import annotations

from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright

from job_application_automation.engines.greenhouse import (
    _commit_react_form_values,
    _required_empty_fields,
)

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
def test_greenhouse_required_react_select_uses_rendered_selection() -> None:
    html = """
    <form>
      <div class="field-wrapper">
        <div class="select__container">
          <label id="veteran-label" for="veteran">Are you a veteran?</label>
          <div class="select-shell">
            <div class="select__control">
              <div class="select__value-container">
                <div class="select__single-value">No</div>
                <input id="veteran" role="combobox" aria-required="true"
                       aria-labelledby="veteran-label" value="" />
              </div>
            </div>
            <input required aria-hidden="true" tabindex="-1"
                   style="opacity:0;position:absolute;width:100%" value="" />
          </div>
        </div>
      </div>
    </form>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        assert _required_empty_fields(page) == []

        page.locator(".select__single-value").evaluate("element => element.remove()")
        assert _required_empty_fields(page) == ["Are you a veteran?"]
        browser.close()


@pytest.mark.enable_socket
def test_greenhouse_required_choices_are_scoped_to_their_question() -> None:
    html = """
    <form>
      <div class="field-wrapper">
        <fieldset id="industry-question">
          <legend>Industries</legend>
          <input id="saas" type="checkbox" name="saas" required checked />
          <label for="saas">SaaS</label>
          <input id="finance" type="checkbox" name="finance" required />
          <label for="finance">Finance</label>
        </fieldset>
      </div>
      <div class="field-wrapper">
        <fieldset id="veteran-question">
          <legend>Veteran status</legend>
          <input id="veteran-yes" type="radio" name="veteran" required />
          <label for="veteran-yes">Veteran yes</label>
          <input id="veteran-no" type="radio" name="veteran" required />
          <label for="veteran-no">Veteran no</label>
        </fieldset>
      </div>
    </form>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        missing = _required_empty_fields(page)

        assert "SaaS" not in missing
        assert "Finance" not in missing
        assert missing == ["Veteran no", "Veteran yes"]
        browser.close()


@pytest.mark.enable_socket
def test_greenhouse_controlled_value_commit_emits_react_event_sequence() -> None:
    html = """
    <form>
      <label for="email">Email</label>
      <input id="email" value="candidate@example.com" />
      <button id="submit" type="submit" disabled>Submit application</button>
    </form>
    <script>
      window.commitEvents = [];
      const email = document.querySelector('#email');
      const submit = document.querySelector('#submit');
      for (const eventName of ['focus', 'input', 'change', 'focusout']) {
        email.addEventListener(eventName, () => window.commitEvents.push(eventName));
      }
      email.addEventListener('focusout', () => { submit.disabled = false; });
      email._valueTracker = {
        setValue(value) { window.trackerValue = value; }
      };
    </script>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        assert _commit_react_form_values(page) == 1
        assert page.evaluate("window.commitEvents") == [
            "focus",
            "input",
            "change",
            "focusout",
        ]
        assert page.evaluate("window.trackerValue") == ""
        assert page.locator("#submit").is_enabled()
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
