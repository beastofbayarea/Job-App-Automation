"""HTTP REST & Static server for the public VPS Output Monitor Dashboard.

This server is unauthenticated by design: every route it serves is public and
read-only. Anything reachable here should be treated as published to the open
internet, so do not add routes that expose secrets or perform actions.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from collections.abc import Sequence
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("DashboardServer")

STATIC_DIR = Path(__file__).parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_DIR = PROJECT_ROOT / "config"
PRIVATE_ARCHIVE_DIR = Path(
    os.environ.get(
        "JOB_APP_PRIVATE_ARCHIVE_DIR",
        "/var/lib/job-application-automation/private-archive",
    )
)
WORKER_PROVIDERS = ("ashby", "greenhouse", "lever")
_ADMIN_LOG_FILES = {
    "vps-sync": OUTPUT_DIR / "vps_sync.log",
    "nginx-access": Path("/var/log/nginx/access.log"),
    "nginx-error": Path("/var/log/nginx/error.log"),
    "system": Path("/var/log/syslog"),
}
_PUBLIC_JOB_FIELDS = {
    "platform",
    "company",
    "title",
    "posted_at",
    "days_old",
    "location",
    "workplace_type",
    "employment_type",
    "department",
    "team",
    "salary",
    "job_url",
    "apply_url",
    "board_token",
    "date_source",
    "match_reason",
    "platform_job_id",
    "live_status",
    "live_checked_at",
    "live_check_source",
    "live_check_http_status",
    "live_check_final_url",
    "live_check_reason",
    "board_region",
    "provider_id_trusted",
    "source_identity",
    "url_is_record_specific",
    "unique_id",
    "first_seen_at",
    "last_seen_at",
}
_PUBLIC_RAW_FILES = {
    "ai_jobs.csv",
    "ats_boards_cache.json",
    "job_backlog.json",
    "job_search_coverage.json",
    "vps_infra_status.json",
    "vps_run_status.json",
}
_REPOSITORY_EXCLUDED_DIRECTORIES = {
    ".agents",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".sync-worktree",
    ".venv",
    "__pycache__",
    "node_modules",
    "output",
}
_REPOSITORY_PRIVATE_NAME_MARKERS = {
    "candidate_email_pool",
    "candidate_profile",
    "credential",
    "dashboard.env",
    "password",
    "private",
    "secret",
    "service_account",
    "token",
    "vps_config.json",
}
_PUBLIC_VPS_FIELDS = {
    "hostname",
    "os",
    "plan",
    "cpu_cores",
    "memory_gb",
    "disk_gb",
    "bandwidth_tb",
    "datacenter",
    "backup_schedule",
    "plan_expiration_date",
    "auto_renewal",
}


def get_output_file_path(filename: str) -> Path:
    """Resolve file path prioritizing output/vps_reports/ if present, falling back to output/."""
    vps_report_path = OUTPUT_DIR / "vps_reports" / filename
    if vps_report_path.exists():
        return vps_report_path
    return OUTPUT_DIR / filename


def load_json_file(filename: str, default: Any = None) -> Any:
    path = get_output_file_path(filename)
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to load JSON file %s: %s", path, exc)
        return default if default is not None else {}


def load_csv_jobs() -> list[dict[str, str]]:
    path = get_output_file_path("ai_jobs.csv")
    if not path.exists():
        return []
    jobs = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                jobs.append(dict(row))
    except Exception as exc:
        logger.warning("Failed to load CSV jobs from %s: %s", path, exc)
    return jobs


def load_vps_config() -> dict[str, Any]:
    """Load only non-sensitive operational VPS metadata for the dashboard."""
    config_path = CONFIG_DIR / "vps_config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        vps = data.get("vps", {})
        if not isinstance(vps, dict):
            return {}
        return {"vps": {key: vps[key] for key in sorted(_PUBLIC_VPS_FIELDS) if key in vps}}
    except Exception as exc:
        logger.warning("Failed to load VPS config from %s: %s", config_path, exc)
        return {}


def load_vps_log(lines: int = 250) -> str:
    log_path = get_output_file_path("vps_sync.log")
    if not log_path.exists():
        return "vps_sync.log not found."
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except Exception as e:
        return f"Error reading log file: {e}"


def _file_metadata(path: Path, *, scope: str, relative_path: str) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    return {
        "scope": scope,
        "path": relative_path.replace("\\", "/"),
        "name": path.name,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def _iter_regular_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files: list[Path] = []
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    files.append(path)
            except OSError:
                continue
    except OSError:
        return []
    return files


def _is_repository_admin_file(path: Path) -> bool:
    try:
        relative = path.relative_to(PROJECT_ROOT)
    except ValueError:
        return False
    lowered_parts = [part.lower() for part in relative.parts]
    if any(part in _REPOSITORY_EXCLUDED_DIRECTORIES for part in lowered_parts[:-1]):
        return False
    lowered_name = relative.name.lower()
    if lowered_name.endswith((".env", ".key", ".p12", ".pem")):
        return False
    if any(marker in lowered_name for marker in _REPOSITORY_PRIVATE_NAME_MARKERS):
        return False
    if lowered_parts[0] == "config":
        safe_config_names = {
            "config.js",
            "seo_config.example.json",
            "seo_config.json",
        }
        safe_runtime_names = {
            "application.json",
            "ashby.json",
            "browser.json",
            "cover_letter.json",
            "gmail.json",
            "resume.json",
            "schema_version.json",
            "search.json",
            "vertex.json",
        }
        is_runtime_section = (
            len(lowered_parts) == 3
            and lowered_parts[1] == "runtime"
            and lowered_name in safe_runtime_names
        )
        return (
            is_runtime_section
            or lowered_name.endswith(".example.json")
            or lowered_name in safe_config_names
        )
    return True


def _iter_repository_files() -> list[Path]:
    files: list[Path] = []
    if not PROJECT_ROOT.is_dir():
        return files
    try:
        for current_root, directories, names in os.walk(PROJECT_ROOT):
            directories[:] = [
                directory
                for directory in directories
                if directory.lower() not in _REPOSITORY_EXCLUDED_DIRECTORIES
            ]
            root = Path(current_root)
            for name in names:
                path = root / name
                try:
                    if path.is_file() and not path.is_symlink() and _is_repository_admin_file(path):
                        files.append(path)
                except OSError:
                    continue
    except OSError:
        return []
    return files


def build_file_inventory(*, include_private: bool = False) -> dict[str, Any]:
    """Inventory VPS artifacts without returning their contents."""

    scopes: list[tuple[str, Path]] = [("output", OUTPUT_DIR)]
    if include_private:
        scopes.extend(
            (
                ("private_archive", PRIVATE_ARCHIVE_DIR),
                ("repository", PROJECT_ROOT),
            )
        )

    entries: list[dict[str, Any]] = []
    by_extension: dict[str, int] = {}
    by_scope: dict[str, dict[str, int]] = {}
    total_bytes = 0
    for scope, root in scopes:
        scope_count = 0
        scope_bytes = 0
        scope_files = (
            _iter_repository_files() if scope == "repository" else _iter_regular_files(root)
        )
        for path in scope_files:
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                continue
            metadata = _file_metadata(path, scope=scope, relative_path=relative)
            if not metadata:
                continue
            entries.append(metadata)
            size = int(metadata["size_bytes"])
            total_bytes += size
            scope_bytes += size
            scope_count += 1
            extension = path.suffix.lower() or "[none]"
            by_extension[extension] = by_extension.get(extension, 0) + 1
        by_scope[scope] = {"file_count": scope_count, "size_bytes": scope_bytes}

    entries.sort(key=lambda item: str(item.get("modified_at", "")), reverse=True)
    recent_entries = (
        entries[:25]
        if include_private
        else [
            entry
            for entry in entries
            if entry["scope"] == "output" and entry["path"] in _PUBLIC_RAW_FILES
        ][:25]
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(entries),
        "size_bytes": total_bytes,
        "by_scope": by_scope,
        "by_extension": dict(sorted(by_extension.items())),
        "files": entries if include_private else [],
        "recent_files": recent_entries,
    }


def _backlog_jobs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        jobs = payload.get("jobs")
    else:
        jobs = payload
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def public_backlog_jobs() -> list[dict[str, Any]]:
    payload = load_json_file("job_backlog.json", default={})
    return [
        {key: value for key, value in job.items() if key in _PUBLIC_JOB_FIELDS}
        for job in _backlog_jobs(payload)
    ]


def public_submission_records(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    fields = {"applied_at", "ats", "company", "role", "status"}
    return {
        str(key): {field: value for field, value in record.items() if field in fields}
        for key, record in payload.items()
        if isinstance(record, dict)
    }


def public_generation_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    fields = {
        "company",
        "target_company",
        "role",
        "job_title",
        "status",
        "ats",
        "platform",
    }
    return [
        {field: value for field, value in record.items() if field in fields}
        for record in payload
        if isinstance(record, dict)
    ]


def public_archive_records(payload: Any) -> dict[str, dict[str, Any]]:
    return {
        str(key): {
            "status": record.get("status"),
            "updated_at": record.get("updated_at"),
            "identity": {
                "company": record.get("identity", {}).get("company"),
                "job_title": record.get("identity", {}).get("job_title"),
            },
        }
        for key, record in archive_entries(payload).items()
        if isinstance(record, dict)
    }


def summarize_backlog(payload: Any) -> dict[str, Any]:
    jobs = _backlog_jobs(payload)
    by_platform: dict[str, int] = {}
    by_live_status: dict[str, int] = {}
    latest_seen_at = ""
    for job in jobs:
        platform = str(job.get("platform") or "unknown").lower()
        live_status = str(job.get("live_status") or "unknown").lower()
        by_platform[platform] = by_platform.get(platform, 0) + 1
        by_live_status[live_status] = by_live_status.get(live_status, 0) + 1
        latest_seen_at = max(
            latest_seen_at,
            str(job.get("last_seen_at") or job.get("live_checked_at") or ""),
        )
    return {
        "version": payload.get("version") if isinstance(payload, dict) else None,
        "updated_at": payload.get("updated_at", "") if isinstance(payload, dict) else "",
        "job_count": len(jobs),
        "by_platform": dict(sorted(by_platform.items())),
        "by_live_status": dict(sorted(by_live_status.items())),
        "latest_seen_at": latest_seen_at,
    }


def summarize_worker_state(provider: str, payload: Any) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_records = payload.get("jobs", {})
        if isinstance(raw_records, dict):
            records = [value for value in raw_records.values() if isinstance(value, dict)]

    status_counts: dict[str, int] = {}
    result_counts: dict[str, int] = {}
    latest: dict[str, Any] = {}
    for record in records:
        status = str(record.get("status") or "unknown").lower()
        result = str(record.get("result_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        result_counts[result] = result_counts.get(result, 0) + 1
        if str(record.get("updated_at") or "") > str(latest.get("updated_at") or ""):
            latest = record

    results_dir = OUTPUT_DIR / f"continuous_{provider}_results"
    documents_dir = OUTPUT_DIR / f"continuous_{provider}_documents"
    result_files = _iter_regular_files(results_dir)
    document_files = _iter_regular_files(documents_dir)
    latest_summary = {
        key: latest.get(key)
        for key in (
            "updated_at",
            "company",
            "title",
            "status",
            "stage",
            "result_status",
            "exit_code",
            "timed_out",
            "ledger_confirmed",
            "captcha_present",
        )
        if key in latest
    }
    return {
        "provider": provider,
        "record_count": len(records),
        "status_counts": dict(sorted(status_counts.items())),
        "result_counts": dict(sorted(result_counts.items())),
        "result_file_count": len(result_files),
        "document_file_count": len(document_files),
        "latest": latest_summary,
    }


def build_worker_summaries() -> list[dict[str, Any]]:
    return [
        summarize_worker_state(
            provider,
            load_json_file(f"continuous_{provider}_state.json", default={}),
        )
        for provider in WORKER_PROVIDERS
    ]


def _read_proc_status(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key] = value.strip()
    except OSError:
        return {}
    return values


def build_process_inventory() -> dict[str, Any]:
    proc_root = Path("/proc")
    processes: list[dict[str, Any]] = []
    if not proc_root.is_dir():
        return {"process_count": 0, "by_name": {}, "processes": []}

    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        status = _read_proc_status(process_dir / "status")
        if not status:
            continue
        name = status.get("Name", "unknown")
        memory_match = re.match(r"(\d+)", status.get("VmRSS", "0"))
        processes.append(
            {
                "pid": int(process_dir.name),
                "name": name,
                "state": status.get("State", ""),
                "parent_pid": int(status.get("PPid", "0") or 0),
                "threads": int(status.get("Threads", "0") or 0),
                "memory_kb": int(memory_match.group(1)) if memory_match else 0,
                "uid": (status.get("Uid", "").split() or [""])[0],
            }
        )

    by_name: dict[str, int] = {}
    for process in processes:
        name = str(process["name"])
        by_name[name] = by_name.get(name, 0) + 1
    processes.sort(key=lambda item: (str(item["name"]).lower(), int(item["pid"])))
    return {
        "process_count": len(processes),
        "by_name": dict(sorted(by_name.items(), key=lambda item: (-item[1], item[0]))),
        "processes": processes,
    }


def build_host_status() -> dict[str, Any]:
    """Read bounded host resource facts without spawning external commands."""

    host: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 0,
        "hostname": "",
        "load_average": [],
        "uptime_seconds": 0.0,
        "memory": {},
        "disk": {},
    }
    try:
        host["hostname"] = os.uname().nodename
    except (AttributeError, OSError):
        pass
    try:
        host["load_average"] = [round(value, 3) for value in os.getloadavg()]
    except (AttributeError, OSError):
        pass
    try:
        host["uptime_seconds"] = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        pass

    memory: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            match = re.match(r"\s*(\d+)", value)
            if separator and match:
                memory[key] = int(match.group(1)) * 1024
    except OSError:
        pass
    host["memory"] = {
        "total_bytes": memory.get("MemTotal", 0),
        "available_bytes": memory.get("MemAvailable", 0),
        "swap_total_bytes": memory.get("SwapTotal", 0),
        "swap_free_bytes": memory.get("SwapFree", 0),
    }
    try:
        disk = shutil.disk_usage(PROJECT_ROOT)
        host["disk"] = {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        }
    except OSError:
        pass
    return host


def _tail_text_file(path: Path, lines: int = 250) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return "".join(stream.readlines()[-lines:])
    except OSError as exc:
        return f"Unavailable: {exc}"


def build_log_overview(*, include_admin_logs: bool = False) -> dict[str, Any]:
    log_paths = (
        _ADMIN_LOG_FILES if include_admin_logs else {"vps-sync": _ADMIN_LOG_FILES["vps-sync"]}
    )
    return {
        name: {
            "path": str(path),
            "content": _tail_text_file(path),
        }
        for name, path in log_paths.items()
    }


def build_operations_overview(*, include_private: bool = False) -> dict[str, Any]:
    backlog = load_json_file("job_backlog.json", default={})
    run_status = load_json_file("vps_run_status.json", default={})
    infra_status = load_json_file("vps_infra_status.json", default={})
    workers = build_worker_summaries()
    nonconfirmed = sum(
        int(worker["status_counts"].get("failed", 0))
        + int(worker["status_counts"].get("manual_review", 0))
        for worker in workers
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_status": run_status if isinstance(run_status, dict) else {},
        "backlog": summarize_backlog(backlog),
        "workers": workers,
        "continuous_nonconfirmed_count": nonconfirmed,
        "infrastructure": infra_status if isinstance(infra_status, dict) else {},
        "host": build_host_status(),
        "files": build_file_inventory(include_private=include_private),
        "processes": build_process_inventory(),
        "logs": build_log_overview(include_admin_logs=include_private),
    }


def archive_entries(archives: Any) -> dict[str, Any]:
    """Return the per-job archive records regardless of file layout version.

    ``vps_document_archive_state.json`` nests its records under a ``jobs`` key
    alongside a scalar ``version``. Counting the top-level keys therefore
    reports 2 instead of the real record count, so unwrap ``jobs`` when present
    and otherwise treat the mapping itself as the record set.
    """
    if not isinstance(archives, dict):
        return {}
    jobs = archives.get("jobs")
    if isinstance(jobs, dict):
        return jobs
    return {key: value for key, value in archives.items() if isinstance(value, dict)}


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
    """Derive cumulative submission facts from the append-only submission log.

    ``vps_application_failures.json`` only describes the most recent run, so the
    per-ATS totals shown for "all time" have to come from the log itself.
    """
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
    """Expose the search-run diagnostics the dashboard renders, minus bulk lists.

    ``query_log`` and ``failed_boards`` are hundreds of entries long and are
    already downloadable from the coverage endpoint, so only their sizes travel
    with the metrics payload.
    """
    if not isinstance(coverage, dict):
        return {}

    def _scalars(section: str) -> dict[str, Any]:
        data = coverage.get(section)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, (int, float, str, bool))}

    discovery = _scalars("discovery")
    feed_fetch = _scalars("feed_fetch")
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
        "cache": _scalars("cache"),
        "discovery": discovery,
        "feed_fetch": feed_fetch,
        "fallback": _scalars("fallback"),
        "results": _scalars("results"),
    }


def build_kpi_metrics() -> dict[str, Any]:
    submissions = load_json_file("submission_log.json", default={})
    failures_data = load_json_file("vps_application_failures.json", default={})
    coverage = load_json_file("job_search_coverage.json", default={})
    generation_jobs = load_json_file("vps_generation_jobs.json", default=[])
    archives = load_json_file("vps_document_archive_state.json", default={})
    cache_data = load_json_file("ats_boards_cache.json", default={})
    infra_status = load_json_file("vps_infra_status.json", default={})
    worker_summaries = build_worker_summaries()
    backlog = load_json_file("job_backlog.json", default={})
    run_status = load_json_file("vps_run_status.json", default={})
    jobs = load_csv_jobs()
    vps_cfg = load_vps_config()

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
        for sub in submissions.values():
            if isinstance(sub, dict):
                ats = sub.get("ats", "unknown").lower()
                ats_counts[ats] = ats_counts.get(ats, 0) + 1

    attempted_by_ats = (
        failures_data.get("attempted_by_ats", {}) if isinstance(failures_data, dict) else {}
    )
    confirmed_by_ats = (
        failures_data.get("confirmed_by_ats", {}) if isinstance(failures_data, dict) else {}
    )

    archive_records = archive_entries(archives)
    archived_sets = len(archive_records)
    archive_status_counts = summarize_archive_status(archive_records)
    gen_queue_size = len(generation_jobs) if isinstance(generation_jobs, list) else 0

    returned_jobs = 0
    live_status_counts = {}
    if isinstance(coverage, dict) and "results" in coverage:
        returned_jobs = coverage["results"].get("returned", len(jobs))
        live_status_counts = coverage["results"].get("live_status_counts", {})
    else:
        returned_jobs = len(jobs)

    cached_boards_count = len(cache_data) if isinstance(cache_data, dict) else 0

    submission_summary = summarize_submissions(submissions)
    coverage_summary = summarize_coverage(coverage)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_submissions": total_submissions,
        "failure_count": failure_count,
        "current_run_failure_count": current_run_failure_count,
        "continuous_nonconfirmed_count": continuous_failure_count,
        "total_jobs_found": returned_jobs,
        "backlog_job_count": summarize_backlog(backlog)["job_count"],
        "generation_queue_count": gen_queue_size,
        "archived_document_sets": archived_sets,
        "archive_status_counts": archive_status_counts,
        "cached_boards_count": cached_boards_count,
        "ats_submissions": ats_counts,
        # Per-run counters, sourced from vps_application_failures.json.
        "attempted_by_ats": attempted_by_ats,
        "confirmed_by_ats": confirmed_by_ats,
        "run_started_at": failures_data.get("run_started_at", "")
        if isinstance(failures_data, dict)
        else "",
        # Cumulative counters, sourced from the append-only submission log.
        "confirmed_by_ats_all_time": submission_summary["confirmed_by_ats"],
        "submissions_by_status": submission_summary["by_status"],
        "latest_submission_at": submission_summary["latest_applied_at"],
        "live_status_counts": live_status_counts,
        "coverage": coverage_summary,
        "last_failure_update": failures_data.get("updated_at", "")
        if isinstance(failures_data, dict)
        else "",
        "vps_info": vps_cfg.get("vps", {}),
        "hostinger_info": vps_cfg.get("hostinger_account", {}),
        "vps_infra": infra_status if isinstance(infra_status, dict) else {},
        "run_status": run_status if isinstance(run_status, dict) else {},
        "workers": worker_summaries,
    }


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path

        if path in {"/admin", "/admin/"}:
            self.path = "/admin.html"
            path = "/admin.html"

        # Clean page route mappings
        if path in {"/search", "/search/"}:
            self.path = "/search.html"
        elif path in {"/generation", "/generation/"}:
            self.path = "/generation.html"
        elif path in {"/logs", "/logs/"}:
            self.path = "/logs.html"
        elif path in {"/inspector", "/inspector/"}:
            self.path = "/inspector.html"
        elif path in {"/cent-capital", "/cent-capital/", "/cent-capital.html"}:
            self.path = "/cent-capital.html"
        elif path in {"/system-status", "/system-status/"}:
            self.path = "/system-status.html"
        elif path in {"/submissions", "/submissions/"}:
            self.path = "/index.html"
        elif path in {"/sitemap", "/sitemap/", "/sitemap.xml"}:
            self.path = "/sitemap.xml"
        elif path in {"/robots", "/robots.txt"}:
            self.path = "/robots.txt"
        elif path in {"/manifest", "/manifest.json", "/site.webmanifest"}:
            self.path = "/site.webmanifest"

        if self.path.startswith("/api/download/"):
            self._handle_file_download()
            return
        if self.path.startswith("/api/"):
            self._handle_api_get()
            return
        super().do_GET()

    def do_POST(self) -> None:
        # The dashboard is a public, read-only site. The former /api/vps/sync and
        # /api/vps/status endpoints shelled out to PowerShell scripts that SSH into
        # the VPS; with no authentication in front of them they would let any
        # anonymous visitor trigger privileged remote actions, so no write or
        # command-executing route is exposed. Run those scripts from a shell instead.
        self.send_error(404, "Endpoint not found")

    def _send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_admin_file_download(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        scope = (query.get("scope") or [""])[0]
        relative = (query.get("path") or [""])[0]
        roots = {
            "output": OUTPUT_DIR,
            "private_archive": PRIVATE_ARCHIVE_DIR,
            "repository": PROJECT_ROOT,
        }
        root = roots.get(scope)
        if root is None or not relative:
            self._send_json({"error": "scope and path are required"}, status=400)
            return
        try:
            resolved_root = root.resolve()
            target = (resolved_root / relative).resolve()
            target.relative_to(resolved_root)
        except (OSError, ValueError):
            self._send_json({"error": "Invalid file path"}, status=400)
            return
        if not target.is_file() or target.is_symlink():
            self._send_json({"error": "File not found"}, status=404)
            return
        if scope == "repository" and not _is_repository_admin_file(target):
            self._send_json({"error": "File is not displayable"}, status=403)
            return
        try:
            data = target.read_bytes()
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        safe_name = target.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_file_download(self) -> None:
        filename = os.path.basename(self.path.split("?")[0])
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            self._send_json({"error": "Invalid filename"}, status=400)
            return
        if filename not in _PUBLIC_RAW_FILES:
            self._send_json(
                {"error": "This raw artifact is available through the Admin Vault"},
                status=403,
            )
            return

        output_root = OUTPUT_DIR.resolve()
        candidates = [
            OUTPUT_DIR / filename,
            OUTPUT_DIR / "vps_reports" / filename,
        ]

        if not any(c.exists() for c in candidates):
            for path in output_root.rglob(filename):
                candidates.append(path)
                break

        target_file = None
        for c in candidates:
            try:
                resolved = c.resolve()
            except OSError:
                continue
            # Guard against directory traversal: the resolved path must stay
            # within OUTPUT_DIR even if symlinks or unexpected paths were added.
            try:
                resolved.relative_to(output_root)
            except ValueError:
                continue
            if resolved.exists() and resolved.is_file():
                target_file = resolved
                break

        if not target_file:
            self._send_json({"error": f"File not found: {filename}"}, status=404)
            return

        try:
            with open(target_file, "rb") as f:
                data = f.read()

            content_type = (
                "application/pdf"
                if filename.lower().endswith(".pdf")
                else "application/octet-stream"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_api_get(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/admin/overview":
            self._send_json(build_operations_overview(include_private=True))
        elif path == "/api/admin/file":
            self._handle_admin_file_download()
        elif path == "/api/operations":
            self._send_json(build_operations_overview())
        elif path == "/api/metrics":
            self._send_json(build_kpi_metrics())
        elif path == "/api/system/vps":
            self._send_json(load_vps_config())
        elif path == "/api/vps/log":
            self._send_json({"log": load_vps_log(250)})
        elif path == "/api/section1/jobs":
            self._send_json(load_csv_jobs())
        elif path == "/api/section1/backlog":
            self._send_json(public_backlog_jobs())
        elif path == "/api/section1/coverage":
            self._send_json(load_json_file("job_search_coverage.json"))
        elif path == "/api/section1/cache":
            self._send_json(load_json_file("ats_boards_cache.json"))
        elif path == "/api/section2/generation":
            self._send_json(public_generation_records(load_json_file("vps_generation_jobs.json")))
        elif path == "/api/section2/archives":
            self._send_json(
                public_archive_records(
                    load_json_file("vps_document_archive_state.json", default={})
                )
            )
        elif path == "/api/section3/submissions":
            self._send_json(
                public_submission_records(load_json_file("submission_log.json", default={}))
            )
        elif path == "/api/section3/failures":
            self._send_json(load_json_file("vps_application_failures.json"))
        elif path == "/api/section3/state":
            self._send_json(load_json_file("vps_application_state.json"))
        elif path.startswith("/api/files/"):
            filename = os.path.basename(path)
            if filename not in _PUBLIC_RAW_FILES:
                self._send_json(
                    {"error": "This raw artifact is available through the Admin Vault"},
                    status=403,
                )
                return
            file_path = get_output_file_path(filename)
            if file_path.exists():
                try:
                    with open(file_path, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    self._send_json(
                        {"filename": filename, "path": str(file_path), "content": content}
                    )
                except Exception as e:
                    self._send_json({"error": str(e)}, status=500)
            else:
                self._send_json({"error": "File not found"}, status=404)
        else:
            self._send_json({"error": "Unknown API route"}, status=404)


class ReuseAddrHTTPServer(HTTPServer):
    allow_reuse_address = True


def run_dashboard_server(
    host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True
) -> None:
    server_address = (host, port)
    httpd = ReuseAddrHTTPServer(server_address, DashboardRequestHandler)
    url = f"http://{host}:{port}/"
    print("\n=======================================================")
    print(f"VPS Output Monitor Dashboard running at: {url}")
    print("Press Ctrl+C to stop the server.")
    print("=======================================================\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
        httpd.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch VPS Output Monitor Dashboard")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host address to bind (default: 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not auto-open browser on startup"
    )

    args = parser.parse_args(argv)
    run_dashboard_server(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
