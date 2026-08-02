"""Tests for typed, ordered form-section execution."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.engines import _browser_form as browser_form
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


if __name__ == "__main__":
    unittest.main()
