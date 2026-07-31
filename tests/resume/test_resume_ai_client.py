from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core.adapters import LLMSettings  # noqa: E402
from job_application_automation.resume import ai_client as ai  # noqa: E402


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, LLMSettings, bool]] = []

    def generate(
        self,
        prompt: str,
        *,
        system: str,
        settings: LLMSettings,
        json_mode: bool = False,
    ) -> str:
        self.calls.append((prompt, system, settings, json_mode))
        return '{"answer": "ok"}'


class ResumeAIClientTests(unittest.TestCase):
    def test_ask_gemini_uses_an_injected_gateway_without_environment_mutation(self) -> None:
        gateway = FakeGateway()
        original_credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

        response = ai.ask_gemini(
            "Prompt",
            system="System",
            json_mode=True,
            gateway=gateway,
            settings=LLMSettings(model="test-model", temperature=0.7),
        )

        self.assertEqual(response, '{"answer": "ok"}')
        self.assertEqual(gateway.calls[0][0:2], ("Prompt", "System"))
        self.assertTrue(gateway.calls[0][3])
        self.assertEqual(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), original_credentials)

    def test_vertex_settings_normalize_values_and_validate_empty_fields(self) -> None:
        settings = ai.VertexSettings(
            project_id=" project ",
            location=" global ",
            model=" model ",
            service_account_file=Path("credentials.json"),
        )

        self.assertEqual(settings.project_id, "project")
        self.assertEqual(settings.location, "global")
        self.assertEqual(settings.model, "model")
        with self.assertRaisesRegex(ValueError, "project_id"):
            ai.VertexSettings(project_id="")

    def test_legacy_credentials_environment_path_is_resolved_without_mutation(self) -> None:
        settings = ai.VertexSettings(service_account_file=Path("configured.json"))
        original_credentials = os.environ.get(ai.GOOGLE_APPLICATION_CREDENTIALS)

        resolved = ai.credential_file_for(
            settings,
            {ai.GOOGLE_APPLICATION_CREDENTIALS: "  legacy-credentials.json  "},
        )

        self.assertEqual(resolved, Path("legacy-credentials.json"))
        self.assertEqual(os.environ.get(ai.GOOGLE_APPLICATION_CREDENTIALS), original_credentials)

    def test_vertex_project_can_be_derived_from_the_configured_service_account(self) -> None:
        settings = ai.VertexSettings(
            project_id=ai.PROJECT_ID_FROM_SERVICE_ACCOUNT,
            service_account_file=Path("configured.json"),
        )
        with tempfile.TemporaryDirectory() as directory:
            credentials_path = Path(directory) / "service_account.json"
            credentials_path.write_text(
                json.dumps({"project_id": "configured-project"}),
                encoding="utf-8",
            )

            project_id = ai.project_id_for(settings, credentials_path)

        self.assertEqual(project_id, "configured-project")

    def test_strip_markdown_formatting_strips_backticks(self) -> None:
        raw_markdown = "```json\n* Tailored engineer **summary**\n```"
        cleaned = ai.strip_markdown_formatting(raw_markdown)
        self.assertIn("Tailored engineer summary", cleaned)

    def test_job_scraping_uses_an_isolated_headless_browser(self) -> None:
        playwright = MagicMock()
        browser = playwright.chromium.launch.return_value
        browser.contexts = []
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.evaluate.return_value = "Product Manager job description " * 20
        page.locator.return_value.all.return_value = []
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        manager.__exit__.return_value = False

        with patch.object(ai, "sync_playwright", return_value=manager):
            result = ai.scrape_job("https://apply.workable.com/example/j/ABC123/")

        self.assertEqual(result["ats"], "workable")
        playwright.chromium.launch.assert_called_once_with(headless=True)
        playwright.chromium.connect_over_cdp.assert_not_called()
        page.close.assert_not_called()
        browser.close.assert_called_once()

    def test_smartrecruiters_job_scraping_closes_its_owned_background_session(self) -> None:
        playwright = MagicMock()
        page = MagicMock()
        page.evaluate.return_value = "Product Manager job description " * 20
        page.locator.return_value.all.return_value = []
        session = MagicMock(page=page)
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        manager.__exit__.return_value = False

        with (
            patch.object(ai, "sync_playwright", return_value=manager),
            patch.object(ai, "open_chrome_session", return_value=session) as open_session,
            patch.object(ai, "close_browser_session") as close_session,
        ):
            result = ai.scrape_job(
                "https://jobs.smartrecruiters.com/Example/744000123456789-product-manager"
            )

        self.assertEqual(result["ats"], "smartrecruiters")
        self.assertTrue(open_session.call_args.kwargs["headless"])
        self.assertTrue(open_session.call_args.kwargs["background"])
        close_session.assert_called_once_with(session)

    def test_job_scraping_rejects_a_non_job_url_before_opening_a_browser(self) -> None:
        with patch.object(ai, "sync_playwright") as sync_playwright:
            with self.assertRaisesRegex(ValueError, "job-specific HTTPS"):
                ai.scrape_job("https://example.com/careers")
        sync_playwright.assert_not_called()


if __name__ == "__main__":
    unittest.main()
