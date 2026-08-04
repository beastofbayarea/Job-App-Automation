"""Archive, submission, coverage, and KPI aggregation services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from collections.abc import Callable

from .artifacts import JsonFileLoader, summarize_backlog


@dataclass(frozen=True, slots=True)
class MetricSources:
    """Injected artifact readers used to build the dashboard KPI payload."""

    load_json_file: JsonFileLoader
    load_csv_jobs: Callable[[], list[dict[str, str]]]
    load_vps_config: Callable[[], dict[str, Any]]
    build_worker_summaries: Callable[[], list[dict[str, Any]]]


def archive_entries(archives: Any) -> dict[str, Any]:
    """Return archive records regardless of the supported file layout version."""

    if not isinstance(archives, dict):
        return {}
    jobs = archives.get("jobs")
    if isinstance(jobs, dict):
        return jobs
    return {key: value for key, value in archives.items() if isinstance(value, dict)}


def public_archive_records(payload: Any) -> dict[str, dict[str, Any]]:
    """Remove document paths and private generation inputs from archive records."""

    public: dict[str, dict[str, Any]] = {}
    for key, record in archive_entries(payload).items():
        if not isinstance(record, dict):
            continue
        identity = record.get("identity", {})
        if not isinstance(identity, dict):
            identity = {}
        public[str(key)] = {
            "status": record.get("status"),
            "updated_at": record.get("updated_at"),
            "identity": {
                "company": identity.get("company"),
                "job_title": identity.get("job_title"),
            },
        }
    return public


def summarize_archive_status(entries: dict[str, Any]) -> dict[str, int]:
    """Count archive records by their recorded terminal status."""

    counts: dict[str, int] = {}
    for record in entries.values():
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "unknown").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def summarize_submissions(submissions: Any) -> dict[str, Any]:
    """Derive cumulative facts from the append-only submission log."""

    if not isinstance(submissions, dict):
        return {"confirmed_by_ats": {}, "by_status": {}, "latest_applied_at": ""}

    confirmed_by_ats: dict[str, int] = {}
    by_status: dict[str, int] = {}
    latest = ""
    for record in submissions.values():
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        if "confirm" in status.lower():
            ats = str(record.get("ats") or "unknown").lower()
            confirmed_by_ats[ats] = confirmed_by_ats.get(ats, 0) + 1
        applied_at = str(record.get("applied_at") or "")
        if applied_at > latest:
            latest = applied_at
    return {
        "confirmed_by_ats": confirmed_by_ats,
        "by_status": by_status,
        "latest_applied_at": latest,
    }


def summarize_coverage(coverage: Any) -> dict[str, Any]:
    """Expose search diagnostics without embedding the large failure lists."""

    if not isinstance(coverage, dict):
        return {}

    def scalars(section: str) -> dict[str, Any]:
        data = coverage.get(section)
        if not isinstance(data, dict):
            return {}
        return {
            key: value for key, value in data.items() if isinstance(value, (int, float, str, bool))
        }

    discovery = scalars("discovery")
    feed_fetch = scalars("feed_fetch")
    raw_feed = coverage.get("feed_fetch")
    if isinstance(raw_feed, dict) and isinstance(raw_feed.get("failed_boards"), list):
        feed_fetch["failed_board_count"] = len(raw_feed["failed_boards"])

    criteria = coverage.get("criteria")
    criteria_summary: dict[str, Any] = {}
    if isinstance(criteria, dict):
        for key in ("role_terms", "location_terms", "platforms"):
            value = criteria.get(key)
            if isinstance(value, list):
                criteria_summary[key] = value
        for key in ("discovery_mode", "match_mode"):
            value = criteria.get(key)
            if isinstance(value, str):
                criteria_summary[key] = value

    return {
        "generated_at": coverage.get("generated_at", ""),
        "criteria": criteria_summary,
        "cache": scalars("cache"),
        "discovery": discovery,
        "feed_fetch": feed_fetch,
        "fallback": scalars("fallback"),
        "results": scalars("results"),
    }


def build_kpi_metrics(sources: MetricSources) -> dict[str, Any]:
    """Compose the metrics endpoint from injected, independently testable readers."""

    submissions = sources.load_json_file("submission_log.json", default={})
    failures_data = sources.load_json_file("vps_application_failures.json", default={})
    coverage = sources.load_json_file("job_search_coverage.json", default={})
    generation_jobs = sources.load_json_file("vps_generation_jobs.json", default=[])
    archives = sources.load_json_file("vps_document_archive_state.json", default={})
    cache_data = sources.load_json_file("ats_boards_cache.json", default={})
    infra_status = sources.load_json_file("vps_infra_status.json", default={})
    worker_summaries = sources.build_worker_summaries()
    backlog = sources.load_json_file("job_backlog.json", default={})
    jobs = sources.load_csv_jobs()
    vps_cfg = sources.load_vps_config()

    total_submissions = len(submissions) if isinstance(submissions, dict) else 0
    current_run_failure_count = (
        failures_data.get("failure_count", 0) if isinstance(failures_data, dict) else 0
    )
    continuous_failure_count = sum(
        int(worker["status_counts"].get("failed", 0))
        + int(worker["status_counts"].get("manual_review", 0))
        for worker in worker_summaries
    )
    failure_count = continuous_failure_count or current_run_failure_count

    ats_counts: dict[str, int] = {}
    if isinstance(submissions, dict):
        for submission in submissions.values():
            if isinstance(submission, dict):
                ats = str(submission.get("ats") or "unknown").lower()
                ats_counts[ats] = ats_counts.get(ats, 0) + 1

    attempted_by_ats = (
        failures_data.get("attempted_by_ats", {}) if isinstance(failures_data, dict) else {}
    )
    confirmed_by_ats = (
        failures_data.get("confirmed_by_ats", {}) if isinstance(failures_data, dict) else {}
    )

    archive_records = archive_entries(archives)
    archive_status_counts = summarize_archive_status(archive_records)

    returned_jobs = len(jobs)
    live_status_counts: Any = {}
    if isinstance(coverage, dict):
        results = coverage.get("results")
        if isinstance(results, dict):
            returned_jobs = results.get("returned", len(jobs))
            live_status_counts = results.get("live_status_counts", {})

    submission_summary = summarize_submissions(submissions)
    coverage_summary = summarize_coverage(coverage)
    vps_info = vps_cfg.get("vps", {}) if isinstance(vps_cfg, dict) else {}
    hostinger_info = vps_cfg.get("hostinger_account", {}) if isinstance(vps_cfg, dict) else {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_submissions": total_submissions,
        "failure_count": failure_count,
        "current_run_failure_count": current_run_failure_count,
        "continuous_nonconfirmed_count": continuous_failure_count,
        "total_jobs_found": returned_jobs,
        "backlog_job_count": summarize_backlog(backlog)["job_count"],
        "generation_queue_count": len(generation_jobs) if isinstance(generation_jobs, list) else 0,
        "archived_document_sets": len(archive_records),
        "archive_status_counts": archive_status_counts,
        "cached_boards_count": len(cache_data) if isinstance(cache_data, dict) else 0,
        "ats_submissions": ats_counts,
        "attempted_by_ats": attempted_by_ats,
        "confirmed_by_ats": confirmed_by_ats,
        "run_started_at": failures_data.get("run_started_at", "")
        if isinstance(failures_data, dict)
        else "",
        "confirmed_by_ats_all_time": submission_summary["confirmed_by_ats"],
        "submissions_by_status": submission_summary["by_status"],
        "latest_submission_at": submission_summary["latest_applied_at"],
        "live_status_counts": live_status_counts,
        "coverage": coverage_summary,
        "last_failure_update": failures_data.get("updated_at", "")
        if isinstance(failures_data, dict)
        else "",
        "vps_info": vps_info,
        "hostinger_info": hostinger_info,
        "vps_infra": infra_status if isinstance(infra_status, dict) else {},
        "workers": worker_summaries,
    }
