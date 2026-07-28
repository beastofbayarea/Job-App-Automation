"""Focused tests for dependency-free Ashby form-section planning helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.engines.ashby_sections import (
    FormSectionOutcome,
    aggregate_section_outcomes,
    choice_is_selected,
    configured_screening_answer,
    is_location_question,
    normalize_configured_value,
    plan_option_selection,
    required_field_flag,
)


class ConfiguredAnswerTests(unittest.TestCase):
    def test_first_matching_explicit_answer_wins_after_question_normalization(self) -> None:
        answer = configured_screening_answer(
            {
                "future sponsorship": " No ",
                "sponsorship": "Yes",
            },
            "Do you require   future\n sponsorship?",
        )

        self.assertEqual(answer, "No")

    def test_empty_or_invalid_configurations_do_not_provide_an_answer(self) -> None:
        self.assertIsNone(configured_screening_answer([], "Any question"))
        self.assertIsNone(configured_screening_answer({"visa": ""}, "Need visa?"))
        self.assertIsNone(normalize_configured_value(None))
        self.assertIsNone(normalize_configured_value(""))
        self.assertEqual(normalize_configured_value(False), "False")

    def test_whitespace_only_answer_preserves_explicit_answer_precedence(self) -> None:
        self.assertEqual(
            configured_screening_answer({"visa": "   "}, "Do you need a visa?"),
            "",
        )


class SelectionPlanningTests(unittest.TestCase):
    def test_plan_preserves_legacy_alias_and_prefix_candidates(self) -> None:
        self.assertEqual(plan_option_selection("male").candidates, ("Male", "Man"))
        self.assertEqual(
            plan_option_selection("Asian").candidates,
            ("Asian", "Asian or Asian American"),
        )
        self.assertEqual(plan_option_selection("India").candidates, ("Indian", "India"))
        self.assertEqual(
            plan_option_selection("Bengaluru, India").candidates,
            ("Bengaluru, India", "Bengaluru"),
        )
        self.assertEqual(plan_option_selection("Yes").candidates, ("Yes",))


class RequiredFieldAndOutcomeTests(unittest.TestCase):
    def test_required_flag_matches_label_pseudo_and_control_markers(self) -> None:
        self.assertTrue(required_field_flag(label_class="field_required__label"))
        self.assertTrue(required_field_flag(pseudo_content='"*"'))
        self.assertTrue(required_field_flag(has_required_control=True))
        self.assertFalse(required_field_flag(label_class="optional", pseudo_content="none"))

    def test_intended_work_location_questions_are_recognized(self) -> None:
        """Ashby asks where a candidate will work, not only where they live."""
        for question in (
            "where do you plan on working from (for payroll tax purposes)?",
            "where will you be working from?",
            "what is your primary work location?",
            "which office location do you plan to work out of?",
        ):
            with self.subTest(question=question):
                self.assertTrue(is_location_question(question))

    def test_existing_current_location_questions_still_match(self) -> None:
        for question in (
            "where are you based?",
            "where do you currently reside?",
            "current location",
            "city",
        ):
            with self.subTest(question=question):
                self.assertTrue(is_location_question(question))

    def test_unrelated_questions_are_not_treated_as_location(self) -> None:
        for question in (
            "how did you hear about us?",
            "what is your expected compensation range?",
            "do you now or in the future require sponsorship?",
            "what is your country of citizenship?",
        ):
            with self.subTest(question=question):
                self.assertFalse(is_location_question(question))

    def test_choice_selection_excludes_unselected_class_names(self) -> None:
        self.assertTrue(choice_is_selected(aria_pressed="true"))
        self.assertTrue(choice_is_selected(aria_pressed=None, class_name="choice _active_"))
        self.assertTrue(choice_is_selected(aria_pressed=None, class_name="choice-selected"))
        self.assertFalse(choice_is_selected(aria_pressed=None, class_name="unselected"))

    def test_later_section_checks_replace_prior_critical_field_state(self) -> None:
        first = FormSectionOutcome("initial", {"email": False, "resume": True})
        refreshed = FormSectionOutcome("refresh", {"email": True})

        self.assertEqual(
            aggregate_section_outcomes((first, refreshed)),
            {"email": True, "resume": True},
        )


if __name__ == "__main__":
    unittest.main()
