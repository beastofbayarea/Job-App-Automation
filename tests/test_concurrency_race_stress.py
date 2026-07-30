from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from job_application_automation.core.artifacts import read_json, write_json
from job_application_automation.core.submission_log import SubmissionLog, SubmissionRecord


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
    log = SubmissionLog(log_file)

    def append_worker(index: int) -> None:
        record = SubmissionRecord(
            url=f"https://example.com/job/{index}",
            ats="greenhouse",
            company="Acme",
            role="AI Engineer",
            applied_at=f"2026-07-30T10:00:{index:02d}Z",
            status="PREFILLED_ONLY",
            confirmation_method="form",
            screenshot="",
            error_message="",
            test_mode=True,
        )
        log.record(record)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append_worker, i) for i in range(5)]
        for f in futures:
            f.result()

    records = log.load()
    assert len(records) == 5
