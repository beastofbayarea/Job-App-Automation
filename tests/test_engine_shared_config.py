from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
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


class ShellValidatorHostTests(unittest.TestCase):
    def test_custom_domain_greenhouse_url_is_accepted_like_the_orchestrator(self) -> None:
        # Companies embed the Greenhouse form on their own domain; a numeric
        # gh_jid is what detect_ats()/validate_ats_url() key on, so the shell
        # validator must not reject a URL the orchestrator routes to Greenhouse.
        url = "https://careers.acme.test/careers?gh_jid=1234567"

        self.assertTrue(engine_shared.validate_ats_url(url, "greenhouse"))
        self.assertEqual(
            engine_shared._parse_and_validate_host(url, "greenhouse"),
            "careers.acme.test",
        )

    def test_unrelated_host_is_still_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not recognized as Greenhouse"):
            engine_shared._parse_and_validate_host("https://example.test/jobs/1", "greenhouse")
        with self.assertRaisesRegex(ValueError, "absolute HTTPS URL"):
            engine_shared._parse_and_validate_host("http://boards.greenhouse.io/a/1", "greenhouse")

    def test_main_accepts_the_custom_domain_url_it_detects(self) -> None:
        url = "https://careers.acme.test/careers?gh_jid=1234567"
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 resume")
            with unittest.mock.patch.dict(
                "os.environ", {engine_shared.ORCHESTRATOR_INVOCATION_ENV: "1"}
            ):
                exit_code = engine_shared.main(["--url", url, "--resume", str(resume)])

        self.assertEqual(exit_code, 0)


class LocationQuestionTests(unittest.TestCase):
    def test_intended_work_location_questions_are_recognized(self) -> None:
        """Providers ask where a candidate will work, not only where they live."""
        for question in (
            "where do you plan on working from (for payroll tax purposes)?",
            "where will you be working from?",
            "what is your primary work location?",
            "which office location do you plan to work out of?",
            "in what cities are you available to work?",
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
            "We work under a hybrid in-office model. Are you willing to work "
            "from our office location 3 days per week?",
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


class ScreeningMatcherTests(unittest.TestCase):
    def test_example_profile_answers_observed_greenhouse_required_fields(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )
        expected = {
            "What would be your availability to join us?": "2 weeks",
            "Reasonable Adjustments": "No",
            "If you answered yes to the Reasonable Adjustments question, "
            "please provide additional details. If not, enter N/A.": "N/A",
            "Non-Disclosure Agreement": "I Agree",
            "Upon hire, can you provide verification of your identity and legal "
            "right to work in the country where this job is located?": "Yes",
        }

        for question, expected_answer in expected.items():
            with self.subTest(question=question):
                answer = engine_shared.configured_answer(
                    question,
                    config["candidate"],
                    config["rules"],
                    config["eeo_defaults"],
                    config["field_matchers"],
                )
                self.assertEqual(answer, expected_answer)

    def test_explicit_screening_answer_is_used_before_semantic_matchers(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )

        answer = engine_shared.configured_answer(
            "Please email me about future job openings",
            config["candidate"],
            config["rules"],
            config["eeo_defaults"],
            config["field_matchers"],
        )

        self.assertEqual(answer, "No")

    def test_example_profile_answers_weekly_in_office_requirement(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )

        answer = engine_shared.configured_answer(
            "Are you available to work 3 days a week in our Melbourne office?",
            config["candidate"],
            config["rules"],
            config["eeo_defaults"],
            config["field_matchers"],
        )

        self.assertEqual(answer, "Yes")

    def test_example_profile_answers_attend_office_requirement(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )

        answer = engine_shared.configured_answer(
            "Are you able to attend the office in Cardiff 2-days per week?",
            config["candidate"],
            config["rules"],
            config["eeo_defaults"],
            config["field_matchers"],
        )

        self.assertEqual(answer, "Yes")

    def test_example_profile_denies_employee_relationship_and_referral(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )

        for question in (
            "Do you know anyone or are you related to anyone who works at Example?",
            "Were you referred to this role by a current Example Employee?",
        ):
            with self.subTest(question=question):
                answer = engine_shared.configured_answer(
                    question,
                    config["candidate"],
                    config["rules"],
                    config["eeo_defaults"],
                    config["field_matchers"],
                )
                self.assertEqual(answer, "No")

    def test_example_profile_answers_plural_city_availability(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )

        answer = engine_shared.configured_answer(
            "In what cities are you available to work?",
            config["candidate"],
            config["rules"],
            config["eeo_defaults"],
            config["field_matchers"],
        )

        self.assertEqual(answer, "City, State, Country")

    def test_example_profile_uses_portfolio_for_work_samples(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )

        answer = engine_shared.configured_answer(
            "Please share some samples of your work",
            config["candidate"],
            config["rules"],
            config["eeo_defaults"],
            config["field_matchers"],
        )

        self.assertEqual(answer, "https://example.com/portfolio")

    def test_example_profile_handles_sponsor_and_pay_transparency_wording(self) -> None:
        config = engine_shared.load_json_config(
            ROOT / "config/candidate_profile_config.example.json"
        )

        cases = {
            "Would you need us to sponsor a work visa?": "No",
            "Pay range transparency": "Acknowledge",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                answer = engine_shared.configured_answer(
                    question,
                    config["candidate"],
                    config["rules"],
                    config["eeo_defaults"],
                    config["field_matchers"],
                )
                self.assertEqual(answer, expected)


if __name__ == "__main__":
    unittest.main()
