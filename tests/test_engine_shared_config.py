from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core import engine_shared  # noqa: E402


class EngineSharedConfigTests(unittest.TestCase):
    def test_schema_v2_profile_flattens_policy_groups_without_losing_fields(self) -> None:
        config = {
            "schema_version": 2,
            "candidate": {
                "identity": {"first_name": "First", "last_name": "Last"},
                "contact": {"fallback_email": "candidate@example.test"},
                "availability": {"start_date_offset_days": 0},
                "education": [],
            },
            "policies": {
                "answers": {"right to work": "Yes"},
                "eeo": {"gender": "Prefer not to disclose"},
                "matchers": {"email": ["email address"]},
                "option_variants": {"yes": ["Yes"]},
                "explicit_answers": {"will you relocate": "No"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            normalized = engine_shared.load_json_config(path)

        candidate = normalized["candidate"]
        self.assertEqual(candidate["first_name"], "First")
        self.assertEqual(candidate["fallback_email"], "candidate@example.test")
        self.assertEqual(candidate["screening_answers"]["will you relocate"], "No")
        self.assertTrue(candidate["available_start_date"])
        self.assertEqual(normalized["rules"]["right to work"], "Yes")
        self.assertEqual(normalized["eeo_defaults"]["gender"], "Prefer not to disclose")

    def test_engine_result_keeps_legacy_payload_shape(self) -> None:
        payload = engine_shared.engine_result(
            "PREFILLED_ONLY",
            ats="lever",
            is_live=False,
            extra={"screenshot": "proof.png"},
        )

        self.assertEqual(payload["status"], "PREFILLED_ONLY")
        self.assertFalse(payload["submitted"])
        self.assertTrue(payload["test_mode"])
        self.assertEqual(payload["screenshot"], "proof.png")


class LocationQuestionTests(unittest.TestCase):
    def test_intended_work_location_questions_are_recognized(self) -> None:
        """Providers ask where a candidate will work, not only where they live."""
        for question in (
            "where do you plan on working from (for payroll tax purposes)?",
            "where will you be working from?",
            "what is your primary work location?",
            "which office location do you plan to work out of?",
        ):
            with self.subTest(question=question):
                self.assertTrue(engine_shared.is_location_question(question))

    def test_residence_questions_are_recognized(self) -> None:
        for question in (
            "where are you based?",
            "where do you currently reside?",
            "where do you currently live?",
            "current location",
            "city",
        ):
            with self.subTest(question=question):
                self.assertTrue(engine_shared.is_location_question(question))

    def test_unrelated_questions_are_not_treated_as_location(self) -> None:
        for question in (
            "how did you hear about us?",
            "what is your expected compensation range?",
            "do you now or in the future require sponsorship?",
            "what is your country of citizenship?",
        ):
            with self.subTest(question=question):
                self.assertFalse(engine_shared.is_location_question(question))

    def test_location_candidates_widen_from_precise_to_country(self) -> None:
        """Country-only dropdowns need the broader fallbacks."""
        candidates = engine_shared.location_answer_candidates(
            {
                "location": "San Francisco, California, United States",
                "city": "San Francisco",
                "state": "California",
                "country": "United States",
            }
        )

        self.assertEqual(
            candidates,
            (
                "San Francisco, California, United States",
                "San Francisco, California",
                "San Francisco",
                "California",
                "United States",
            ),
        )

    def test_location_candidates_skip_blank_and_duplicate_profile_fields(self) -> None:
        self.assertEqual(
            engine_shared.location_answer_candidates({"location": "Remote", "country": "Remote"}),
            ("Remote",),
        )
        self.assertEqual(engine_shared.location_answer_candidates({}), ())


if __name__ == "__main__":
    unittest.main()
