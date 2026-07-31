from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.resume import generate as resume_generate  # noqa: E402
from job_application_automation.resume.source import (  # noqa: E402
    load_resume_source,
    parse_tagged_candidate,
)


def _source_text() -> str:
    experience = "\n".join(
        "\n".join(
            (
                f"[COMPANY] Company {index}",
                f"[BASE_TITLE] Role {index}",
                "[DATES] 2020 - 2021",
                "[LOCATION] Remote",
                f"[CLAIM C{index}] Delivered measurable outcome {index}.",
            )
        )
        for index in range(1, 6)
    )
    education = "\n".join(
        "\n".join(
            (
                f"[SCHOOL] School {index}",
                "[DEGREE] Degree",
                "[DATES] 2010 - 2014",
            )
        )
        for index in range(1, 4)
    )
    return "\n".join(
        (
            "[NAME] Candidate Name",
            "[LOCATION] City, Country",
            "[EMAIL] candidate@example.test",
            "[PHONE] 5555555555",
            "[LINKEDIN] https://example.test/in/candidate",
            experience,
            "EDUCATION",
            education,
        )
    )


class ResumeSourceTests(unittest.TestCase):
    def test_resume_generator_import_does_not_read_candidate_source(self) -> None:
        self.assertEqual(resume_generate.BASE_RESUME_TEXT, "")
        self.assertEqual(resume_generate.ORIGINAL_EXPERIENCE, [])

    def test_loader_validates_and_returns_tagged_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base_resume.txt"
            path.write_text(_source_text(), encoding="utf-8")

            source = load_resume_source(path)

        self.assertEqual(source.candidate["name"], "Candidate Name")
        self.assertEqual(source.companies, tuple(f"Company {index}" for index in range(1, 6)))
        self.assertEqual(len(source.education), 3)

    def test_candidate_parser_reports_missing_identity_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "candidate section is missing"):
            parse_tagged_candidate("[NAME] Candidate\n[COMPANY] Example")


if __name__ == "__main__":
    unittest.main()
