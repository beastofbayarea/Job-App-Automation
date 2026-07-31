from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from job_application_automation.core.artifacts import read_json, write_json
from job_application_automation.core.submission_log import SubmissionLog, SubmissionRecord
from job_application_automation.search.backlog import (
    BacklogEntry,
    load_backlog,
    reconcile_backlog,
)
from job_application_automation.search.models import Job


def test_concurrent_atomic_write_json_stress(tmp_path: Path) -> None:
    def write_worker(worker_id: int) -> None:
        target_file = tmp_path / f"concurrent_data_{worker_id}.json"
        data = {"worker": worker_id, "items": list(range(50))}
        write_json(target_file, data)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(write_worker, i) for i in range(10)]
        for f in futures:
            f.result()

    for i in range(10):
        target_file = tmp_path / f"concurrent_data_{i}.json"
        assert target_file.exists()
        final_data = read_json(target_file)
        assert isinstance(final_data, dict)
        assert final_data["worker"] == i
        assert len(final_data["items"]) == 50


def test_concurrent_submission_log_append_stress(tmp_path: Path) -> None:
    log_file = tmp_path / "submission_log.json"

    def append_worker(index: int) -> None:
        log = SubmissionLog()
        record = SubmissionRecord(
            job_url=f"https://example.com/job/{index}",
            ats="greenhouse",
            company="Acme",
            role=f"AI Engineer {index}",
            status="PREFILLED_ONLY",
            email_used="candidate@example.test",
            resume_filename="resume.pdf",
        )
        log.record(record)
        log.save(log_file)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append_worker, i) for i in range(5)]
        for f in futures:
            f.result()

    final_log = SubmissionLog()
    count = final_log.load(log_file)
    assert count == 5


def test_concurrent_backlog_reconciliation_retains_every_writer(tmp_path: Path) -> None:
    backlog_path = tmp_path / "job_backlog.json"
    ledger_path = tmp_path / "submission_log.json"
    write_json(ledger_path, {})
    now = datetime(2026, 7, 31, tzinfo=UTC)

    def reconcile_worker(index: int) -> None:
        job = Job(
            platform="greenhouse",
            company="Example",
            title=f"Product Manager {index}",
            posted_at="",
            days_old="",
            location="Remote",
            workplace_type="",
            employment_type="",
            department="",
            team="",
            salary="",
            job_url=f"https://boards.greenhouse.io/example/jobs/{index}",
            apply_url=f"https://boards.greenhouse.io/example/jobs/{index}",
            board_token="example",
            date_source="",
            match_reason="",
            platform_job_id=str(index),
            provider_id_trusted=True,
            live_status="unknown",
        )
        reconcile_backlog(
            backlog_path,
            [BacklogEntry(job=job, first_seen_at=now.isoformat(), last_seen_at=now.isoformat())],
            admitted_jobs=[job],
            submission_logs=[ledger_path],
            now=now,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(reconcile_worker, index) for index in range(10)]
        for future in futures:
            future.result()

    entries = load_backlog(backlog_path)
    assert {entry.job.platform_job_id for entry in entries} == {str(index) for index in range(10)}
