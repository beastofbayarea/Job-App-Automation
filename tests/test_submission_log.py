from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.submission_log import (  # noqa: E402
    SubmissionLog,
    SubmissionRecord,
    make_submission_id,
)


def _record(**overrides: object) -> SubmissionRecord:
    defaults: dict[str, object] = {
        "company": "OpenAI",
        "role": "Product Manager",
        "job_url": "https://jobs.ashbyhq.com/openai/example",
        "ats": "ashby",
        "status": "SUBMITTED & CONFIRMED",
        "email_used": "shivamsi@umich.edu",
        "resume_filename": "shivam_singh_openai_pm.pdf",
        "cover_letter_filename": "shivam_singh_openai_pm_cover_letter.pdf",
        "remote_path": "/opt/job-application-automation/submissions/20260728-openai-product-manager/",
        "applied_at": datetime(2026, 7, 28, 14, 32, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return SubmissionRecord(**defaults)  # type: ignore[arg-type]


class SubmissionIdTests(unittest.TestCase):
    def test_id_slugifies_company_and_role_with_date_prefix(self) -> None:
        stamp = datetime(2026, 7, 28, tzinfo=timezone.utc)
        self.assertEqual(
            make_submission_id("Cent Capital", "Product Manager", applied_at=stamp),
            "20260728-cent-capital-product-manager",
        )


class SubmissionRecordTests(unittest.TestCase):
    def test_rejects_non_https_job_url(self) -> None:
        with self.assertRaises(ValueError):
            _record(job_url="http://example.com/job")

    def test_rejects_email_without_local_part(self) -> None:
        with self.assertRaises(ValueError):
            _record(email_used="@example.com")

    def test_payload_round_trips_through_from_payload(self) -> None:
        record = _record()
        restored = SubmissionRecord.from_payload(record.to_payload())
        self.assertEqual(restored, record)


class SubmissionLogTests(unittest.TestCase):
    def test_record_keys_entry_by_computed_submission_id(self) -> None:
        log = SubmissionLog()
        submission_id = log.record(_record())
        self.assertEqual(submission_id, "20260728-openai-product-manager")
        self.assertEqual(log.get(submission_id)["company"], "OpenAI")

    def test_find_by_company_is_case_insensitive(self) -> None:
        log = SubmissionLog()
        log.record(_record())
        matches = log.find_by_company("openai")
        self.assertEqual(len(matches), 1)

    def test_save_and_load_round_trip_preserves_entries(self) -> None:
        log = SubmissionLog()
        log.record(_record())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission_log.json"
            log.save(path)

            restored = SubmissionLog()
            self.assertEqual(restored.load(path), 1)

        self.assertEqual(
            restored.get("20260728-openai-product-manager")["resume_filename"],
            "shivam_singh_openai_pm.pdf",
        )

    def test_load_ignores_non_object_entries(self) -> None:
        log = SubmissionLog({"bad": "not-an-object", "20260728-openai-product-manager": _record().to_payload()})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission_log.json"
            log.save(path)

            restored = SubmissionLog()
            count = restored.load(path)

        self.assertEqual(count, 1)
        self.assertIsNone(restored.get("bad"))


if __name__ == "__main__":
    unittest.main()
