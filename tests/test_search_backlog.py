from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_application_automation.search import backlog
from job_application_automation.search.models import Job


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _job(job_id: str, **overrides: object) -> Job:
    values: dict[str, object] = {
        "platform": "greenhouse",
        "company": "Example",
        "title": f"Product Manager {job_id}",
        "posted_at": "2026-07-30T00:00:00Z",
        "days_old": 1,
        "location": "Remote",
        "workplace_type": "Remote",
        "employment_type": "Full-time",
        "department": "Product",
        "team": "",
        "salary": "",
        "job_url": f"https://boards.greenhouse.io/example/jobs/{job_id}",
        "apply_url": f"https://boards.greenhouse.io/example/jobs/{job_id}/apply",
        "board_token": "example",
        "date_source": "first_published",
        "match_reason": "role=Product Manager | AI=AI",
        "description": "Private job description that must never enter the backlog.",
        "platform_job_id": job_id,
        "board_region": "eu",
        "provider_id_trusted": True,
        "source_identity": f"https://boards.greenhouse.io/example#{job_id}",
        "url_is_record_specific": True,
        "live_status": "live",
        "unique_id": f"greenhouse:eu:example:{job_id}",
    }
    values.update(overrides)
    return Job(**values)  # type: ignore[arg-type]


def _reconcile(
    path: Path,
    jobs: list[Job],
    *,
    ledger: Path,
    now: datetime = NOW,
) -> backlog.BacklogUpdate:
    existing = backlog.load_backlog(path)
    candidates = backlog.prepare_candidates(existing, jobs, now=now)
    return backlog.reconcile_backlog(
        path,
        candidates,
        admitted_jobs=jobs,
        submission_logs=[ledger],
        now=now,
    )


def test_backlog_accumulates_jobs_and_round_trips_liveness_identity_without_private_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job_backlog.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}", encoding="utf-8")

    first = _job("1")
    _reconcile(path, [first], ledger=ledger)
    _reconcile(path, [_job("2")], ledger=ledger, now=NOW + timedelta(hours=1))

    entries = backlog.load_backlog(path)
    assert {entry.job.platform_job_id for entry in entries} == {"1", "2"}
    restored = next(entry.job for entry in entries if entry.job.platform_job_id == "1")
    assert restored.board_region == "eu"
    assert restored.provider_id_trusted is True
    assert restored.source_identity.endswith("#1")
    assert restored.unique_id == "greenhouse:eu:example:1"

    raw = path.read_text(encoding="utf-8")
    assert "Private job description" not in raw
    assert '"description"' not in raw
    assert "email" not in raw


def test_reconcile_removes_only_exact_confirmations_and_conclusive_closures(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job_backlog.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}", encoding="utf-8")
    _reconcile(path, [_job("1"), _job("2"), _job("3")], ledger=ledger)

    ledger.write_text(
        json.dumps(
            {
                "confirmed": {
                    "job_url": (
                        "https://boards.greenhouse.io/example/jobs/1/application"
                        "?utm_source=automation"
                    ),
                    "status": "SUBMITTED & CONFIRMED",
                },
                "ambiguous": {
                    "job_url": "https://boards.greenhouse.io/example/jobs/2",
                    "status": "SUBMISSION_UNCONFIRMED",
                },
            }
        ),
        encoding="utf-8",
    )
    entries = backlog.load_backlog(path)
    for entry in entries:
        if entry.job.platform_job_id == "3":
            entry.job.live_status = "closed"
        elif entry.job.platform_job_id == "2":
            entry.job.live_status = "unknown"

    result = backlog.reconcile_backlog(
        path,
        entries,
        admitted_jobs=[],
        submission_logs=[ledger],
        now=NOW + timedelta(days=1),
    )

    assert result.removed_confirmed == 1
    assert result.removed_closed == 1
    remaining = backlog.load_backlog(path)
    assert [entry.job.platform_job_id for entry in remaining] == ["2"]
    assert remaining[0].job.live_status == "unknown"


def test_confirmed_job_cannot_return_but_closed_job_can_be_rediscovered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job_backlog.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text(
        json.dumps(
            {
                "one": {
                    "job_url": "https://boards.greenhouse.io/example/jobs/1",
                    "status": "SUBMITTED & CONFIRMED",
                }
            }
        ),
        encoding="utf-8",
    )

    result = _reconcile(
        path,
        [_job("1"), _job("2", live_status="closed")],
        ledger=ledger,
    )
    assert result.retained == 0

    result = _reconcile(
        path,
        [_job("1"), _job("2", live_status="live")],
        ledger=ledger,
        now=NOW + timedelta(days=1),
    )
    assert result.retained == 1
    assert backlog.load_backlog(path)[0].job.platform_job_id == "2"


def test_remove_confirmed_job_matches_apply_url_and_preserves_other_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job_backlog.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}", encoding="utf-8")
    _reconcile(path, [_job("1"), _job("2")], ledger=ledger)

    assert backlog.remove_confirmed_job(
        path,
        "https://boards.greenhouse.io/example/jobs/1/apply?source=email",
    )
    assert not backlog.remove_confirmed_job(path, "https://example.test/jobs/missing")
    assert [entry.job.platform_job_id for entry in backlog.load_backlog(path)] == ["2"]


def test_generic_shared_page_jobs_keep_distinct_source_identities_and_are_not_url_pruned(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job_backlog.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}", encoding="utf-8")
    shared_url = "https://careers.example.test/open-roles"
    first = _job(
        "generic-1",
        provider_id_trusted=False,
        url_is_record_specific=False,
        job_url=shared_url,
        apply_url="",
        source_identity=f"jsonld:{shared_url}:product-manager",
        unique_id="",
    )
    second = _job(
        "generic-2",
        provider_id_trusted=False,
        url_is_record_specific=False,
        job_url=shared_url,
        apply_url="",
        source_identity=f"jsonld:{shared_url}:senior-product-manager",
        unique_id="",
    )

    _reconcile(path, [first, second], ledger=ledger)

    assert len(backlog.load_backlog(path)) == 2
    assert not backlog.remove_confirmed_job(path, shared_url)
    assert len(backlog.load_backlog(path)) == 2


def test_stale_concurrent_snapshot_cannot_resurrect_a_closed_job(tmp_path: Path) -> None:
    path = tmp_path / "job_backlog.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}", encoding="utf-8")
    original = _job("1")
    _reconcile(path, [original], ledger=ledger)

    stale_snapshot = backlog.prepare_candidates(
        backlog.load_backlog(path),
        [],
        now=NOW + timedelta(minutes=1),
    )
    closed_snapshot = backlog.prepare_candidates(
        backlog.load_backlog(path),
        [],
        now=NOW + timedelta(minutes=2),
    )
    closed_snapshot[0].job.live_status = "closed"
    backlog.reconcile_backlog(
        path,
        closed_snapshot,
        admitted_jobs=[],
        submission_logs=[ledger],
        now=NOW + timedelta(minutes=2),
    )
    assert backlog.load_backlog(path) == []

    stale_snapshot[0].job.live_status = "unknown"
    backlog.reconcile_backlog(
        path,
        stale_snapshot,
        admitted_jobs=[],
        submission_logs=[ledger],
        now=NOW + timedelta(minutes=3),
    )

    assert backlog.load_backlog(path) == []


def test_older_closed_evidence_cannot_delete_a_newer_live_record(tmp_path: Path) -> None:
    path = tmp_path / "job_backlog.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}", encoding="utf-8")
    _reconcile(path, [_job("1")], ledger=ledger)

    older_closed = backlog.prepare_candidates(
        backlog.load_backlog(path),
        [],
        now=NOW + timedelta(minutes=1),
    )
    older_closed[0].job.live_status = "closed"
    older_closed[0].job.live_checked_at = (NOW + timedelta(minutes=1)).isoformat()

    newer_live = backlog.prepare_candidates(
        backlog.load_backlog(path),
        [],
        now=NOW + timedelta(minutes=2),
    )
    newer_live[0].job.live_status = "live"
    newer_live[0].job.live_checked_at = (NOW + timedelta(minutes=2)).isoformat()
    backlog.reconcile_backlog(
        path,
        newer_live,
        admitted_jobs=[],
        submission_logs=[ledger],
        now=NOW + timedelta(minutes=2),
    )

    backlog.reconcile_backlog(
        path,
        older_closed,
        admitted_jobs=[],
        submission_logs=[ledger],
        now=NOW + timedelta(minutes=3),
    )

    entries = backlog.load_backlog(path)
    assert len(entries) == 1
    assert entries[0].job.live_status == "live"
    assert entries[0].job.live_checked_at == (NOW + timedelta(minutes=2)).isoformat()


def test_missing_backlog_can_migrate_existing_csv_without_description(tmp_path: Path) -> None:
    csv_path = tmp_path / "ai_jobs.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["platform", "company", "title", "job_url", "live_status"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "platform": "workable",
                "company": "Example",
                "title": "AI Product Manager",
                "job_url": "https://apply.workable.com/example/j/ABC123/",
                "live_status": "unknown",
            }
        )

    jobs = backlog.load_legacy_jobs(csv_path)

    assert len(jobs) == 1
    assert jobs[0].platform == "workable"
    assert jobs[0].live_status == "unknown"
    assert jobs[0].description == ""


def test_invalid_backlog_version_is_rejected_without_replacement(tmp_path: Path) -> None:
    path = tmp_path / "job_backlog.json"
    original = '{"version": 99, "updated_at": "old", "jobs": []}'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported job backlog version"):
        backlog.load_backlog(path)

    assert path.read_text(encoding="utf-8") == original
