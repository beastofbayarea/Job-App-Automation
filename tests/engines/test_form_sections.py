"""Tests for typed, ordered form-section execution."""

from __future__ import annotations

import inspect
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.engines import _browser_form as browser_form
from job_application_automation.engines import ashby, greenhouse, lever
from job_application_automation.engines.form_sections import (
    CallableSectionHandler,
    FormSectionHandler,
    FormSectionOutcome,
    run_section_handlers,
)
from job_application_automation.engines.workable import SPEC


class FormSectionPipelineTests(unittest.TestCase):
    def test_handlers_run_once_in_declared_order_and_report_stable_aggregates(self) -> None:
        calls: list[str] = []

        def outcome(
            section: str,
            fields: dict[str, bool],
            *,
            missing: tuple[str, ...] = (),
            completed: tuple[str, ...] = (),
        ) -> FormSectionOutcome:
            calls.append(section)
            return FormSectionOutcome(section, fields, missing, completed)

        handlers = (
            CallableSectionHandler(
                "standard",
                lambda: outcome(
                    "standard",
                    {"email": False, "resume": True},
                    missing=("email", "email"),
                ),
            ),
            CallableSectionHandler(
                "repair",
                lambda: outcome(
                    "repair",
                    {"email": True},
                    completed=("privacy", "privacy"),
                ),
            ),
        )

        report = run_section_handlers(handlers)

        self.assertEqual(calls, ["standard", "repair"])
        self.assertEqual(
            [item.section for item in report.outcomes],
            ["standard", "repair"],
        )
        self.assertEqual(report.fields, {"email": True, "resume": True})
        self.assertEqual(report.missing, ["email"])
        self.assertEqual(report.completed, ["privacy"])
        self.assertEqual(report.outcome("repair").fields, {"email": True})
        self.assertIsInstance(handlers[0], FormSectionHandler)

    def test_duplicate_sections_are_rejected(self) -> None:
        handler = CallableSectionHandler(
            "standard",
            lambda: FormSectionOutcome("standard"),
        )

        with self.assertRaisesRegex(ValueError, "duplicate form section handler"):
            run_section_handlers((handler, handler))

    def test_handler_rejects_an_outcome_for_a_different_section(self) -> None:
        handler = CallableSectionHandler(
            "standard",
            lambda: FormSectionOutcome("custom"),
        )

        with self.assertRaisesRegex(ValueError, "returned outcome"):
            handler.handle()


class GenericBrowserSectionParityTests(unittest.TestCase):
    def test_initial_sections_preserve_standard_custom_consent_order(self) -> None:
        page = MagicMock()
        candidate = browser_form.CandidateFields(
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            phone="123",
            headline="Engineer",
            linkedin="https://linkedin.example/ada",
            github="https://github.example/ada",
            portfolio="https://ada.example",
            street_address="1 Example Road",
            city="London",
            postcode="SW1A",
            country="United Kingdom",
        )
        calls: list[str] = []

        def fill_standard(*_args: object, **_kwargs: object) -> tuple[dict[str, bool], list[str]]:
            calls.append("standard_fields")
            return {"email": True, "resume": True}, ["phone"]

        def fill_custom(*_args: object, **_kwargs: object) -> dict[str, bool]:
            calls.append("custom_questions")
            return {"sponsorship": True}

        def fill_consent(*_args: object, **_kwargs: object) -> list[str]:
            calls.append("required_consent")
            return ["privacy"]

        with (
            patch.object(browser_form, "_fill_standard_fields", side_effect=fill_standard),
            patch.object(browser_form, "_fill_custom_questions", side_effect=fill_custom),
            patch.object(browser_form, "fill_required_consent", side_effect=fill_consent),
        ):
            report = browser_form._run_initial_form_sections(
                page,
                SPEC,
                candidate,
                Path("resume.pdf"),
                Path("cover-letter.pdf"),
                {},
                company="Analytical Engines",
                role="Programmer",
                candidate_evidence="evidence",
                job_context="job context",
            )

        self.assertEqual(
            calls,
            ["standard_fields", "custom_questions", "required_consent"],
        )
        self.assertEqual(report.outcome("standard_fields").missing, ("phone",))
        self.assertEqual(
            report.outcome("custom_questions").fields,
            {"sponsorship": True},
        )
        self.assertEqual(
            report.outcome("required_consent").completed,
            ("privacy",),
        )


class ProviderSectionRunnerParityTests(unittest.TestCase):
    @staticmethod
    def _record(
        calls: list[str],
        name: str,
        result: object,
    ) -> Callable[..., object]:
        def action(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            return result

        return action

    @staticmethod
    def _browser_session(page: MagicMock) -> tuple[MagicMock, MagicMock]:
        playwright_context = MagicMock()
        playwright_context.__enter__.return_value = MagicMock()
        session = MagicMock()
        session.page = page
        session.close_browser_on_exit = False
        return playwright_context, session

    def test_greenhouse_sections_use_shared_runner_and_preserve_provider_results(self) -> None:
        calls: list[str] = []
        page = MagicMock()

        with (
            patch.object(
                greenhouse,
                "_fill_standard_fields",
                side_effect=self._record(
                    calls,
                    "standard_fields",
                    {"first_name": True, "resume": True, "cover_letter": None},
                ),
            ),
            patch.object(
                greenhouse,
                "_fill_custom_questions",
                side_effect=self._record(calls, "custom_questions", {"question": True}),
            ),
            patch.object(
                greenhouse,
                "_fill_export_control_questions",
                side_effect=self._record(calls, "export_control", {"export": True}),
            ),
            patch.object(
                greenhouse,
                "_fill_source_checkbox",
                side_effect=self._record(calls, "source_attribution", {"source": True}),
            ),
            patch.object(
                greenhouse,
                "_fill_eeo_fields",
                side_effect=self._record(calls, "eeo_fields", {"eeo": True}),
            ),
            patch.object(
                greenhouse,
                "_fill_consent",
                side_effect=self._record(calls, "required_consent", ["privacy"]),
            ),
            patch.object(
                greenhouse,
                "_fill_explicit_required_consents",
                side_effect=self._record(calls, "explicit_consent", ["terms"]),
            ),
            patch.object(
                greenhouse,
                "_security_challenge_visible",
                side_effect=self._record(calls, "security_visible", True),
            ),
            patch.object(
                greenhouse,
                "_fill_pre_submit_security_challenge",
                side_effect=self._record(calls, "security_fill", False),
            ),
            patch.object(
                greenhouse,
                "run_section_handlers",
                wraps=run_section_handlers,
            ) as runner,
        ):
            sections = greenhouse._run_form_sections(
                page,
                {},
                "candidate@example.com",
                Path("resume.pdf"),
                Path("cover-letter.pdf"),
                {},
                "Example",
                "Engineer",
                "candidate evidence",
                live_submit=True,
            )

        runner.assert_called_once()
        self.assertEqual(
            calls,
            [
                "standard_fields",
                "custom_questions",
                "export_control",
                "source_attribution",
                "eeo_fields",
                "required_consent",
                "explicit_consent",
                "security_visible",
                "security_fill",
            ],
        )
        self.assertEqual(
            [outcome.section for outcome in sections.report.outcomes],
            [
                "standard_fields",
                "custom_questions",
                "export_control",
                "source_attribution",
                "eeo_fields",
                "required_consent",
                "security_challenge",
            ],
        )
        self.assertEqual(
            sections.fields,
            {"first_name": True, "resume": True, "cover_letter": None},
        )
        self.assertEqual(
            sections.custom_questions, {"question": True, "export": True, "source": True}
        )
        self.assertEqual(sections.eeo_fields, {"eeo": True})
        self.assertEqual(sections.consent_fields, ("privacy", "terms"))
        self.assertEqual(
            sections.report.outcome("security_challenge").fields,
            {"visible": True, "filled": False},
        )
        self.assertTrue(sections.challenge_visible)
        self.assertFalse(sections.challenge_filled)

    def test_greenhouse_prefill_status_and_wire_payload_survive_section_refactor(
        self,
    ) -> None:
        page = MagicMock()
        page.url = "about:blank"
        playwright_context, session = self._browser_session(page)
        resume = Path(self._testMethodName + "-resume.pdf")
        cover_letter = Path(self._testMethodName + "-cover.pdf")
        resume.write_bytes(b"resume")
        cover_letter.write_bytes(b"cover")
        self.addCleanup(resume.unlink, missing_ok=True)
        self.addCleanup(cover_letter.unlink, missing_ok=True)

        with (
            patch.object(greenhouse, "sync_playwright", return_value=playwright_context),
            patch.object(greenhouse, "open_chrome_session", return_value=session),
            patch.object(greenhouse, "navigate_reusing_tab"),
            patch.object(greenhouse, "_open_application_form"),
            patch.object(greenhouse, "_confirmation_visible", return_value=False),
            patch.object(greenhouse, "_skip_application_topic", return_value=None),
            patch.object(
                greenhouse,
                "_load_personalized_resume_evidence",
                return_value="candidate evidence",
            ),
            patch.object(
                greenhouse,
                "_fill_standard_fields",
                return_value={
                    "first_name": True,
                    "last_name": True,
                    "email": True,
                    "resume": True,
                    "cover_letter": None,
                },
            ),
            patch.object(
                greenhouse,
                "_fill_custom_questions",
                return_value={"question": True},
            ),
            patch.object(
                greenhouse,
                "_fill_export_control_questions",
                return_value={"export": True},
            ),
            patch.object(
                greenhouse,
                "_fill_source_checkbox",
                return_value={"source": True},
            ),
            patch.object(greenhouse, "_fill_eeo_fields", return_value={"eeo": True}),
            patch.object(greenhouse, "_fill_consent", return_value=["privacy", "privacy"]),
            patch.object(
                greenhouse,
                "_fill_explicit_required_consents",
                return_value=["terms"],
            ),
            patch.object(greenhouse, "_security_challenge_visible", return_value=False),
            patch.object(
                greenhouse,
                "_fill_pre_submit_security_challenge",
                return_value=False,
            ),
            patch.object(greenhouse, "validate_required_fields", return_value=[]),
            patch.object(greenhouse, "_screenshot", return_value="prefilled.png"),
            patch.object(
                greenhouse,
                "run_section_handlers",
                wraps=run_section_handlers,
            ) as runner,
        ):
            result = greenhouse.run(
                url="https://boards.greenhouse.io/example/jobs/123",
                resume=resume,
                cover_letter=cover_letter,
                email_override="candidate@example.com",
                config={"candidate": {}},
                company="Example",
                role="Engineer",
                headed=False,
                live_submit=False,
            )

        runner.assert_called_once()
        self.assertEqual(result["status"], "PREFILLED_ONLY")
        self.assertTrue(result["success"])
        self.assertFalse(result["submitted"])
        self.assertEqual(result["filled_fields"]["cover_letter"], None)
        self.assertEqual(
            result["custom_questions"],
            {"question": True, "export": True, "source": True},
        )
        self.assertEqual(result["eeo_fields"], {"eeo": True})
        self.assertEqual(result["consent_fields"], ["privacy", "privacy", "terms"])

    def test_lever_sections_use_shared_runner_and_preserve_provider_results(self) -> None:
        calls: list[str] = []
        page = MagicMock()

        with (
            patch.object(
                lever,
                "_fill_standard_fields",
                side_effect=self._record(
                    calls,
                    "standard_fields",
                    {"name": True, "resume": True, "cover_letter": None},
                ),
            ),
            patch.object(
                lever,
                "_fill_custom_questions",
                side_effect=self._record(calls, "custom_questions", {"question": True}),
            ),
            patch.object(
                lever,
                "fill_required_consent",
                side_effect=self._record(calls, "required_consent", ["privacy"]),
            ),
            patch.object(
                lever,
                "run_section_handlers",
                wraps=run_section_handlers,
            ) as runner,
        ):
            sections = lever._run_form_sections(
                page,
                {},
                "candidate@example.com",
                Path("resume.pdf"),
                Path("cover-letter.pdf"),
                {},
                "Example",
                "Engineer",
                "candidate evidence",
            )

        runner.assert_called_once()
        self.assertEqual(
            calls,
            ["standard_fields", "custom_questions", "required_consent"],
        )
        self.assertEqual(
            [outcome.section for outcome in sections.report.outcomes],
            ["standard_fields", "custom_questions", "required_consent"],
        )
        self.assertEqual(
            sections.fields,
            {"name": True, "resume": True, "cover_letter": None},
        )
        self.assertEqual(sections.custom_questions, {"question": True})
        self.assertEqual(sections.consent_fields, ("privacy",))

    def test_lever_prefill_status_and_wire_payload_survive_section_refactor(self) -> None:
        page = MagicMock()
        playwright_context, session = self._browser_session(page)
        resume = Path(self._testMethodName + "-resume.pdf")
        cover_letter = Path(self._testMethodName + "-cover.pdf")
        resume.write_bytes(b"resume")
        cover_letter.write_bytes(b"cover")
        self.addCleanup(resume.unlink, missing_ok=True)
        self.addCleanup(cover_letter.unlink, missing_ok=True)

        with (
            patch.object(lever, "sync_playwright", return_value=playwright_context),
            patch.object(lever, "open_chrome_session", return_value=session),
            patch.object(lever, "navigate_reusing_tab"),
            patch.object(
                lever,
                "load_personalized_resume_evidence",
                return_value="candidate evidence",
            ),
            patch.object(
                lever,
                "_fill_standard_fields",
                return_value={
                    "name": True,
                    "email": True,
                    "resume": True,
                    "cover_letter": None,
                },
            ),
            patch.object(
                lever,
                "_fill_custom_questions",
                return_value={"question": True},
            ),
            patch.object(lever, "fill_required_consent", return_value=["privacy"]),
            patch.object(lever, "validate_required_fields", return_value=[]),
            patch.object(lever, "_captcha_present", return_value=False),
            patch.object(lever, "capture_screenshot", return_value="prefilled.png"),
            patch.object(
                lever,
                "run_section_handlers",
                wraps=run_section_handlers,
            ) as runner,
        ):
            result = lever.run(
                url="https://jobs.lever.co/example/123",
                resume=resume,
                cover_letter=cover_letter,
                email_override="candidate@example.com",
                config={"candidate": {}},
                company="Example",
                role="Engineer",
                live_submit=False,
            )

        runner.assert_called_once()
        self.assertEqual(result["status"], "PREFILLED_ONLY")
        self.assertTrue(result["success"])
        self.assertFalse(result["submitted"])
        self.assertEqual(result["filled_fields"]["cover_letter"], None)
        self.assertEqual(result["custom_questions"], {"question": True})
        self.assertEqual(result["consent_fields"], ["privacy"])

    def test_ashby_sections_use_shared_runner_and_preserve_critical_fields(self) -> None:
        calls: list[str] = []
        page = MagicMock()

        with (
            patch.object(
                ashby,
                "fill_personal_and_files",
                side_effect=self._record(
                    calls,
                    "personal_and_files",
                    {"name": True, "resume": True},
                ),
            ),
            patch.object(
                ashby,
                "fill_secondary",
                side_effect=self._record(calls, "secondary", None),
            ),
            patch.object(
                ashby,
                "fill_education_history",
                side_effect=self._record(calls, "education_history", None),
            ),
            patch.object(
                ashby,
                "fill_consent_checkboxes",
                side_effect=self._record(calls, "consent", None),
            ),
            patch.object(
                ashby,
                "fill_eeo",
                side_effect=self._record(calls, "eeo", None),
            ),
            patch.object(
                ashby,
                "run_section_handlers",
                wraps=run_section_handlers,
            ) as runner,
        ):
            fields = ashby._fill_current_form(
                page,
                {},
                {},
                "essay",
                "Example",
                "Engineer",
                "candidate@example.com",
                Path("resume.pdf"),
                Path("cover-letter.pdf"),
            )

        runner.assert_called_once()
        self.assertEqual(
            calls,
            ["personal_and_files", "secondary", "education_history", "consent", "eeo"],
        )
        self.assertEqual(fields, {"name": True, "resume": True})

    def test_ashby_validation_repair_replays_sections_and_choice_refresh_in_order(self) -> None:
        calls: list[str] = []
        page = MagicMock()

        with (
            patch.object(
                ashby,
                "fill_personal_and_files",
                side_effect=self._record(calls, "personal_and_files", {"resume": True}),
            ),
            patch.object(
                ashby,
                "fill_secondary",
                side_effect=self._record(calls, "secondary", None),
            ),
            patch.object(
                ashby,
                "fill_education_history",
                side_effect=self._record(calls, "education_history", None),
            ),
            patch.object(
                ashby,
                "fill_consent_checkboxes",
                side_effect=self._record(calls, "consent", None),
            ),
            patch.object(
                ashby,
                "fill_eeo",
                side_effect=self._record(calls, "eeo", None),
            ),
            patch.object(
                ashby,
                "_refresh_selected_choice_groups",
                side_effect=self._record(calls, "selected_choice_refresh", None),
            ),
            patch.object(
                ashby,
                "run_section_handlers",
                wraps=run_section_handlers,
            ) as runner,
        ):
            result = ashby._repair_dynamic_form(
                page,
                {},
                {},
                "essay",
                "Example",
                "Engineer",
                "candidate@example.com",
                Path("resume.pdf"),
                Path("cover-letter.pdf"),
            )

        runner.assert_called_once()
        self.assertIsNone(result)
        self.assertEqual(
            calls,
            [
                "personal_and_files",
                "secondary",
                "education_history",
                "consent",
                "eeo",
                "selected_choice_refresh",
            ],
        )

    def test_production_runners_reach_the_shared_section_boundary(self) -> None:
        production_calls: dict[str, tuple[Any, str]] = {
            "greenhouse": (greenhouse.run, "_run_form_sections("),
            "lever": (lever.run, "_run_form_sections("),
            "ashby_steps": (ashby._fill_current_form, "_run_form_sections("),
            "ashby_plan": (ashby._run_form_sections, "run_section_handlers("),
            "ashby_refresh": (ashby.run_job, "run_section_handlers("),
            "ashby_repair": (ashby.run_job, "_repair_dynamic_form("),
        }

        for provider, (production_runner, expected_call) in production_calls.items():
            with self.subTest(provider=provider):
                self.assertIn(expected_call, inspect.getsource(production_runner))


if __name__ == "__main__":
    unittest.main()
