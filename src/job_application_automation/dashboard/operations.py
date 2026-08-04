"""Process, worker, host, log, and operations-overview services."""

from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast
from collections.abc import Callable

from .artifacts import JsonFileLoader, iter_regular_files, summarize_backlog
from .models import DashboardContext


class FileInventoryBuilder(Protocol):
    """Injected file-inventory service contract."""

    def __call__(self, *, include_private: bool = False) -> dict[str, Any]:
        """Build public or full inventory metadata."""


class LogOverviewBuilder(Protocol):
    """Injected log-overview service contract."""

    def __call__(self, *, include_admin_logs: bool = False) -> dict[str, Any]:
        """Build the public or full log view."""


@dataclass(frozen=True, slots=True)
class OperationsSources:
    """Injected dependencies used to compose the operations endpoint."""

    load_json_file: JsonFileLoader
    build_worker_summaries: Callable[[], list[dict[str, Any]]]
    build_host_status: Callable[[], dict[str, Any]]
    build_file_inventory: FileInventoryBuilder
    build_process_inventory: Callable[[], dict[str, Any]]
    build_log_overview: LogOverviewBuilder


def summarize_worker_state(
    context: DashboardContext,
    provider: str,
    payload: Any,
) -> dict[str, Any]:
    """Aggregate one continuous worker's durable state and artifact counts."""

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

    output_dir = context.paths.output_dir
    result_files = iter_regular_files(output_dir / f"continuous_{provider}_results")
    document_files = iter_regular_files(output_dir / f"continuous_{provider}_documents")
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


def build_worker_summaries(
    context: DashboardContext,
    load_json_file: JsonFileLoader,
) -> list[dict[str, Any]]:
    """Build summaries for every configured continuous ATS worker."""

    return [
        summarize_worker_state(
            context,
            provider,
            load_json_file(f"continuous_{provider}_state.json", default={}),
        )
        for provider in context.worker_providers
    ]


def read_proc_status(path: Path) -> dict[str, str]:
    """Parse the colon-delimited fields in one Linux process status file."""

    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key] = value.strip()
    except OSError:
        return {}
    return values


def build_process_inventory(proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Read a bounded, command-free process snapshot from procfs."""

    processes: list[dict[str, Any]] = []
    if not proc_root.is_dir():
        return {"process_count": 0, "by_name": {}, "processes": []}

    try:
        process_directories = list(proc_root.iterdir())
    except OSError:
        process_directories = []
    for process_dir in process_directories:
        if not process_dir.name.isdigit():
            continue
        status = read_proc_status(process_dir / "status")
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


def build_host_status(context: DashboardContext) -> dict[str, Any]:
    """Read bounded host resource facts without spawning external commands."""

    host: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 0,
        "hostname": "",
        "load_average": [],
        "uptime_seconds": 0.0,
        "memory": {},
        "disk": {},
    }
    host["hostname"] = platform.node()
    get_load_average = cast(
        "Callable[[], tuple[float, float, float]] | None",
        getattr(os, "getloadavg", None),
    )
    if get_load_average is not None:
        try:
            host["load_average"] = [round(value, 3) for value in get_load_average()]
        except OSError:
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
        disk = shutil.disk_usage(context.paths.project_root)
        host["disk"] = {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        }
    except OSError:
        pass
    return host


def tail_text_file(path: Path, lines: int = 250) -> str:
    """Read the last lines of a text file with an explicit unavailable result."""

    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return "".join(stream.readlines()[-lines:])
    except OSError as exc:
        return f"Unavailable: {exc}"


def build_log_overview(
    context: DashboardContext,
    *,
    include_admin_logs: bool = False,
) -> dict[str, Any]:
    """Return public or full log tails without command execution."""

    log_paths = context.paths.admin_log_files if include_admin_logs else {}
    return {
        name: {"path": str(path), "content": tail_text_file(path)}
        for name, path in log_paths.items()
    }


def build_operations_overview(
    sources: OperationsSources,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    """Compose the public operations snapshot from injected read services."""

    backlog = sources.load_json_file("job_backlog.json", default={})
    infra_status = sources.load_json_file("vps_infra_status.json", default={})
    workers = sources.build_worker_summaries()
    nonconfirmed = sum(
        int(worker["status_counts"].get("failed", 0))
        + int(worker["status_counts"].get("manual_review", 0))
        for worker in workers
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backlog": summarize_backlog(backlog),
        "workers": workers,
        "continuous_nonconfirmed_count": nonconfirmed,
        "infrastructure": infra_status if isinstance(infra_status, dict) else {},
        "host": sources.build_host_status(),
        "files": sources.build_file_inventory(include_private=include_private),
        "processes": sources.build_process_inventory(),
        "logs": sources.build_log_overview(include_admin_logs=include_private),
    }
