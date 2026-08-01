from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core.submission_log import (  # noqa: E402
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

    def test_rejects_email_without_a_domain(self) -> None:
        with self.assertRaises(ValueError):
            _record(email_used="candidate@")

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

    def test_find_by_job_url_uses_canonical_url_and_ignores_invalid_entries(self) -> None:
        log = SubmissionLog(
            {
                "invalid": {"job_url": "not-a-url"},
                "confirmed": _record().to_payload(),
            }
        )

        matches = log.find_by_job_url(
            "https://jobs.ashbyhq.com/openai/example?utm_source=campaign#apply"
        )

        self.assertEqual(set(matches), {"confirmed"})

    def test_distinct_same_day_company_role_records_do_not_overwrite(self) -> None:
        log = SubmissionLog()
        first_id = log.record(_record())
        second_id = log.record(
            _record(
                job_url="https://jobs.ashbyhq.com/openai/another-role",
                email_used="candidate@example.com",
                applied_at=datetime(2026, 7, 28, 15, 0, tzinfo=timezone.utc),
            )
        )

        self.assertEqual(first_id, "20260728-openai-product-manager")
        self.assertNotEqual(second_id, first_id)
        self.assertIsNotNone(log.get(first_id))
        self.assertIsNotNone(log.get(second_id))
        self.assertEqual(len(log.find_by_company("OpenAI")), 2)

    def test_independent_process_snapshots_merge_without_lost_updates(self) -> None:
        first = SubmissionLog()
        second = SubmissionLog()
        first.record(_record(company="First Company", role="First Role"))
        second.record(_record(company="Second Company", role="Second Role"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission_log.json"
            first.save(path)
            second.save(path)

            restored = SubmissionLog()
            self.assertEqual(restored.load(path), 2)
            self.assertEqual(len(restored.find_by_company("First Company")), 1)
            self.assertEqual(len(restored.find_by_company("Second Company")), 1)
            self.assertFalse(path.with_name("submission_log.json.lock").exists())

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
        log = SubmissionLog(
            {"bad": "not-an-object", "20260728-openai-product-manager": _record().to_payload()}
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission_log.json"
            log.save(path)

            restored = SubmissionLog()
            count = restored.load(path)

        self.assertEqual(count, 1)
        self.assertIsNone(restored.get("bad"))

    def test_strict_load_rejects_a_malformed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission_log.json"
            path.write_text('{"not-a-submission": {"status": "SUBMITTED & CONFIRMED"}}')

            with self.assertRaisesRegex(ValueError, "not-a-submission.*invalid"):
                SubmissionLog().load(path, strict=True)


if __name__ == "__main__":
    unittest.main()
