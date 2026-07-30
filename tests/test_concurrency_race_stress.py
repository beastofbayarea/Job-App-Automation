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
