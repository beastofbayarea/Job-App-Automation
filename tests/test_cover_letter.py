from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.resume.career_narrative import (  # noqa: E402
    CareerNarrative,
    load_career_narrative,
)
from job_application_automation.resume.cover_letter_claims import (  # noqa: E402
    known_claim_ids,
    validate_claim_ids,
)
from job_application_automation.resume.cover_letter_models import CoverLetterJob  # noqa: E402


class CareerNarrativeTests(unittest.TestCase):
    def test_missing_fields_are_omitted_not_inferred(self) -> None:
        narrative = load_career_narrative({})

        self.assertEqual(narrative.reason_for_change, "")
        self.assertEqual(narrative.next_role_priorities, ())
        self.assertEqual(narrative.tone, "")
        self.assertEqual(narrative.default_salutation, "Hiring Team")
        self.assertEqual(narrative.do_not_claim, ())

    def test_present_fields_are_parsed_and_trimmed(self) -> None:
        narrative = load_career_narrative(
            {
                "career_narrative": {
                    "reason_for_change": "  Candidate-approved wording only  ",
                    "next_role_priorities": ["AI product ownership", "  ", "customer impact"],
                    "tone": "direct",
                    "default_salutation": "Hiring Manager",
                    "do_not_claim": ["People-management experience"],
                }
            }
        )

        self.assertEqual(narrative.reason_for_change, "Candidate-approved wording only")
        self.assertEqual(
            narrative.next_role_priorities, ("AI product ownership", "customer impact")
        )
        self.assertEqual(narrative.default_salutation, "Hiring Manager")
        self.assertEqual(narrative.do_not_claim, ("People-management experience",))

    def test_malformed_narrative_block_is_ignored_like_a_missing_one(self) -> None:
        narrative = load_career_narrative({"career_narrative": "not an object"})

        self.assertEqual(narrative, CareerNarrative())


class CoverLetterJobTests(unittest.TestCase):
    def test_job_defaults_url_to_empty_string(self) -> None:
        job = CoverLetterJob(company="Example Co", role="Product Manager", jd_text="Build things.")

        self.assertEqual(job.url, "")


class CoverLetterClaimsTests(unittest.TestCase):
    def test_known_claim_ids_collects_every_tagged_claim(self) -> None:
        experience = [
            {"claims": [{"id": "AWS-1", "text": "..."}, {"id": "AWS-2", "text": "..."}]},
            {"claims": [{"id": "META-1", "text": "..."}]},
        ]

        self.assertEqual(known_claim_ids(experience), {"AWS-1", "AWS-2", "META-1"})

    def test_validate_claim_ids_returns_only_the_unknown_ones(self) -> None:
        known = {"AWS-1", "AWS-2"}

        invalid = validate_claim_ids(["AWS-1", "MADE-UP-9"], known)

        self.assertEqual(invalid, ["MADE-UP-9"])

    def test_validate_claim_ids_accepts_an_all_known_list(self) -> None:
        known = {"AWS-1"}

        self.assertEqual(validate_claim_ids(["AWS-1"], known), [])


if __name__ == "__main__":
    unittest.main()
