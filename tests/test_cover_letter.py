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


if __name__ == "__main__":
    unittest.main()
