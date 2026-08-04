from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from job_application_automation.dashboard.models import DashboardContext, DashboardPaths
from job_application_automation.dashboard.operations import (
    OperationsSources,
    build_host_status,
    build_log_overview,
    build_operations_overview,
    build_process_inventory,
    read_proc_status,
    tail_text_file,
)


def _context(tmp_path: Path, *, logs: dict[str, Path] | None = None) -> DashboardContext:
    paths = DashboardPaths(
        static_dir=tmp_path / "static",
        project_root=tmp_path,
        output_dir=tmp_path / "output",
        config_dir=tmp_path / "config",
        private_archive_dir=tmp_path / "archive",
        admin_log_files=logs or {},
    )
    return DashboardContext(
        paths=paths,
        worker_providers=("greenhouse",),
        public_raw_files=frozenset(),
        public_job_fields=frozenset(),
        public_vps_fields=frozenset(),
        repository_excluded_directories=frozenset(),
        repository_private_name_markers=frozenset(),
    )


def test_process_inventory_parses_and_sorts_proc_status(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    for pid, name, memory in (("20", "worker", 12), ("10", "agent", 4)):
        process = proc / pid
        process.mkdir(parents=True)
        (process / "status").write_text(
            f"Name:\t{name}\nState:\tS (sleeping)\nPPid:\t1\nThreads:\t2\n"
            f"VmRSS:\t{memory} kB\nUid:\t1000 1000 1000 1000\n",
            encoding="utf-8",
        )
    (proc / "not-a-pid").mkdir()

    inventory = build_process_inventory(proc)

    assert inventory["process_count"] == 2
    assert inventory["by_name"] == {"agent": 1, "worker": 1}
    assert [item["pid"] for item in inventory["processes"]] == [10, 20]
    assert inventory["processes"][1]["memory_kb"] == 12
    assert read_proc_status(proc / "missing" / "status") == {}


def test_host_status_reports_portable_defaults_when_proc_reads_fail(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with (
        patch("job_application_automation.dashboard.operations.platform.node", return_value="host"),
        patch("job_application_automation.dashboard.operations.os.cpu_count", return_value=4),
        patch(
            "job_application_automation.dashboard.operations.os.getloadavg",
            side_effect=OSError,
            create=True,
        ),
    ):
        status = build_host_status(context)

    assert status["hostname"] == "host"
    assert status["cpu_count"] == 4
    assert status["load_average"] == []
    assert status["uptime_seconds"] >= 0
    assert status["disk"]["total_bytes"] > 0


def test_log_overview_is_private_and_tail_has_explicit_unavailable_result(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    context = _context(tmp_path, logs={"worker": log})

    assert build_log_overview(context) == {}
    assert build_log_overview(context, include_admin_logs=True) == {
        "worker": {"path": str(log), "content": "one\ntwo\nthree\n"}
    }
    assert tail_text_file(log, lines=2) == "two\nthree\n"
    assert tail_text_file(tmp_path / "missing.log").startswith("Unavailable:")


def test_operations_overview_composes_injected_services() -> None:
    sources = OperationsSources(
        load_json_file=lambda name, default: {"jobs": []} if name == "job_backlog.json" else {},
        build_worker_summaries=lambda: [{"status_counts": {"failed": 2, "manual_review": 1}}],
        build_host_status=lambda: {"hostname": "host"},
        build_file_inventory=lambda *, include_private=False: {"private": include_private},
        build_process_inventory=lambda: {"process_count": 1},
        build_log_overview=lambda *, include_admin_logs=False: {"private": include_admin_logs},
    )

    overview = build_operations_overview(sources, include_private=True)

    assert overview["continuous_nonconfirmed_count"] == 3
    assert overview["files"] == {"private": True}
    assert overview["logs"] == {"private": True}
    assert overview["host"] == {"hostname": "host"}
