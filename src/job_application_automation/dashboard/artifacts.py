"""Artifact, configuration, sanitization, and file-inventory services."""

from __future__ import annotations

import csv
import json
import logging
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .models import DashboardContext

logger = logging.getLogger("DashboardServer")

_SAFE_CONFIG_NAMES = frozenset(
    {
        "config.js",
        "seo_config.example.json",
        "seo_config.json",
    }
)
_SAFE_RUNTIME_CONFIG_NAMES = frozenset(
    {
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
)


class JsonFileLoader(Protocol):
    """Callable contract used to inject JSON artifact loading."""

    def __call__(self, filename: str, default: Any = None) -> Any:
        """Load one named JSON artifact."""


def get_output_file_path(context: DashboardContext, filename: str) -> Path:
    """Prefer a synced VPS report, then fall back to the output root."""

    vps_report_path = context.paths.output_dir / "vps_reports" / filename
    if vps_report_path.exists():
        return vps_report_path
    return context.paths.output_dir / filename


def load_json_file(
    context: DashboardContext,
    filename: str,
    default: Any = None,
) -> Any:
    """Load a JSON artifact and preserve the established tolerant fallback."""

    path = get_output_file_path(context, filename)
    if not path.exists():
        return default if default is not None else {}
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except Exception as exc:
        logger.warning("Failed to load JSON file %s: %s", path, exc)
        return default if default is not None else {}


def load_csv_jobs(context: DashboardContext) -> list[dict[str, str]]:
    """Load the public job-search CSV as dictionaries."""

    path = get_output_file_path(context, "ai_jobs.csv")
    if not path.exists():
        return []
    jobs: list[dict[str, str]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for row in csv.DictReader(stream):
                jobs.append(dict(row))
    except Exception as exc:
        logger.warning("Failed to load CSV jobs from %s: %s", path, exc)
    return jobs


def load_vps_config(context: DashboardContext) -> dict[str, Any]:
    """Load only allowlisted operational VPS metadata."""

    config_path = context.paths.config_dir / "vps_config.json"
    if not config_path.exists():
        return {}
    try:
        with config_path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            return {}
        vps = data.get("vps", {})
        if not isinstance(vps, dict):
            return {}
        return {"vps": {key: vps[key] for key in sorted(context.public_vps_fields) if key in vps}}
    except Exception as exc:
        logger.warning("Failed to load VPS config from %s: %s", config_path, exc)
        return {}


def load_vps_log(context: DashboardContext, lines: int = 250) -> str:
    """Read a bounded tail of the public VPS synchronization log."""

    log_path = get_output_file_path(context, "vps_sync.log")
    if not log_path.exists():
        return "vps_sync.log not found."
    try:
        with log_path.open(encoding="utf-8", errors="replace") as stream:
            return "".join(stream.readlines()[-lines:])
    except Exception as exc:
        return f"Error reading log file: {exc}"


def file_metadata(path: Path, *, scope: str, relative_path: str) -> dict[str, Any]:
    """Describe a file without exposing its contents."""

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


def iter_regular_files(root: Path) -> list[Path]:
    """Return regular, non-symlink files beneath a root."""

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


def is_repository_admin_file(context: DashboardContext, path: Path) -> bool:
    """Return whether a repository file is safe for the public admin vault."""

    try:
        relative = path.relative_to(context.paths.project_root)
    except ValueError:
        return False
    lowered_parts = [part.lower() for part in relative.parts]
    if not lowered_parts:
        return False
    if any(part in context.repository_excluded_directories for part in lowered_parts[:-1]):
        return False
    lowered_name = relative.name.lower()
    if lowered_name.endswith((".env", ".key", ".p12", ".pem")):
        return False
    if any(marker in lowered_name for marker in context.repository_private_name_markers):
        return False
    if lowered_parts[0] == "config":
        is_runtime_section = (
            len(lowered_parts) == 3
            and lowered_parts[1] == "runtime"
            and lowered_name in _SAFE_RUNTIME_CONFIG_NAMES
        )
        return (
            is_runtime_section
            or lowered_name.endswith(".example.json")
            or lowered_name in _SAFE_CONFIG_NAMES
        )
    return True


def iter_repository_files(context: DashboardContext) -> list[Path]:
    """Walk displayable repository files while pruning private directories."""

    files: list[Path] = []
    project_root = context.paths.project_root
    if not project_root.is_dir():
        return files
    try:
        for current_root, directories, names in os.walk(project_root):
            directories[:] = [
                directory
                for directory in directories
                if directory.lower() not in context.repository_excluded_directories
            ]
            root = Path(current_root)
            for name in names:
                path = root / name
                try:
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and is_repository_admin_file(context, path)
                    ):
                        files.append(path)
                except OSError:
                    continue
    except OSError:
        return []
    return files


def build_file_inventory(
    context: DashboardContext,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    """Inventory dashboard-visible artifacts without returning their contents."""

    scopes: list[tuple[str, Path]] = [("output", context.paths.output_dir)]
    if include_private:
        scopes.extend(
            (
                ("private_archive", context.paths.private_archive_dir),
                ("repository", context.paths.project_root),
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
            iter_repository_files(context) if scope == "repository" else iter_regular_files(root)
        )
        for path in scope_files:
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                continue
            metadata = file_metadata(path, scope=scope, relative_path=relative)
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
            if entry["scope"] == "output" and entry["path"] in context.public_raw_files
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


def backlog_jobs(payload: Any) -> list[dict[str, Any]]:
    """Normalize either supported backlog layout into job records."""

    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def public_backlog_jobs(
    context: DashboardContext,
    load_json: JsonFileLoader,
) -> list[dict[str, Any]]:
    """Load backlog jobs and remove fields not approved for publication."""

    payload = load_json("job_backlog.json", default={})
    return [
        {key: value for key, value in job.items() if key in context.public_job_fields}
        for job in backlog_jobs(payload)
    ]


def public_submission_records(payload: Any) -> dict[str, dict[str, Any]]:
    """Remove contact details and generated-document paths from submissions."""

    if not isinstance(payload, dict):
        return {}
    fields = frozenset({"applied_at", "ats", "company", "role", "status"})
    return {
        str(key): {field: value for field, value in record.items() if field in fields}
        for key, record in payload.items()
        if isinstance(record, dict)
    }


def public_generation_records(payload: Any) -> list[dict[str, Any]]:
    """Return only non-sensitive generation queue identity and status fields."""

    if not isinstance(payload, list):
        return []
    fields = frozenset(
        {
            "company",
            "target_company",
            "role",
            "job_title",
            "status",
            "ats",
            "platform",
        }
    )
    return [
        {field: value for field, value in record.items() if field in fields}
        for record in payload
        if isinstance(record, dict)
    ]


def summarize_backlog(payload: Any) -> dict[str, Any]:
    """Aggregate provider and liveness totals from a backlog payload."""

    jobs = backlog_jobs(payload)
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
