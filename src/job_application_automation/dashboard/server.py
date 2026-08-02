"""HTTP adapter and bootstrap for the public VPS Output Monitor Dashboard.

This server is unauthenticated by design: every route it serves is public and
read-only. Anything reachable here should be treated as published to the open
internet, so do not add routes that expose secrets or perform actions.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from . import artifacts, downloads, metrics, operations
from .models import DashboardContext, DashboardPaths, DashboardRequest, HttpResponse
from .routes import (
    ApiRouteMatch,
    DashboardApplication,
    DashboardRouteServices,
    RouteKey,
    StaticPassthrough,
    StaticRouteMatch,
    match_get_request,
)

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


def _dashboard_context() -> DashboardContext:
    """Build context from patchable compatibility globals for each request."""

    return DashboardContext(
        paths=DashboardPaths(
            static_dir=STATIC_DIR,
            project_root=PROJECT_ROOT,
            output_dir=OUTPUT_DIR,
            config_dir=CONFIG_DIR,
            private_archive_dir=PRIVATE_ARCHIVE_DIR,
            admin_log_files=dict(_ADMIN_LOG_FILES),
        ),
        worker_providers=tuple(WORKER_PROVIDERS),
        public_raw_files=frozenset(_PUBLIC_RAW_FILES),
        public_job_fields=frozenset(_PUBLIC_JOB_FIELDS),
        public_vps_fields=frozenset(_PUBLIC_VPS_FIELDS),
        repository_excluded_directories=frozenset(_REPOSITORY_EXCLUDED_DIRECTORIES),
        repository_private_name_markers=frozenset(_REPOSITORY_PRIVATE_NAME_MARKERS),
    )


# Compatibility wrappers keep historical import and unittest.patch seams while
# all implementation lives in focused, context-injected service modules.
def get_output_file_path(filename: str) -> Path:
    """Resolve a synced VPS report or output-root artifact path."""

    return artifacts.get_output_file_path(_dashboard_context(), filename)


def load_json_file(filename: str, default: Any = None) -> Any:
    """Load a dashboard JSON artifact with the established tolerant fallback."""

    return artifacts.load_json_file(_dashboard_context(), filename, default)


def load_csv_jobs() -> list[dict[str, str]]:
    """Load public job-search CSV records."""

    return artifacts.load_csv_jobs(_dashboard_context())


def load_vps_config() -> dict[str, Any]:
    """Load only public operational VPS metadata."""

    return artifacts.load_vps_config(_dashboard_context())


def load_vps_log(lines: int = 250) -> str:
    """Return the bounded VPS synchronization log tail."""

    return artifacts.load_vps_log(_dashboard_context(), lines)


def _file_metadata(path: Path, *, scope: str, relative_path: str) -> dict[str, Any]:
    return artifacts.file_metadata(path, scope=scope, relative_path=relative_path)


def _iter_regular_files(root: Path) -> list[Path]:
    return artifacts.iter_regular_files(root)


def _is_repository_admin_file(path: Path) -> bool:
    return artifacts.is_repository_admin_file(_dashboard_context(), path)


def _iter_repository_files() -> list[Path]:
    return artifacts.iter_repository_files(_dashboard_context())


def build_file_inventory(*, include_private: bool = False) -> dict[str, Any]:
    """Inventory public or admin-visible files without returning contents."""

    return artifacts.build_file_inventory(_dashboard_context(), include_private=include_private)


def _backlog_jobs(payload: Any) -> list[dict[str, Any]]:
    return artifacts.backlog_jobs(payload)


def public_backlog_jobs() -> list[dict[str, Any]]:
    """Load backlog records with only explicitly public fields."""

    return artifacts.public_backlog_jobs(_dashboard_context(), load_json_file)


def public_submission_records(payload: Any) -> dict[str, dict[str, Any]]:
    return artifacts.public_submission_records(payload)


def public_generation_records(payload: Any) -> list[dict[str, Any]]:
    return artifacts.public_generation_records(payload)


def public_archive_records(payload: Any) -> dict[str, dict[str, Any]]:
    return metrics.public_archive_records(payload)


def summarize_backlog(payload: Any) -> dict[str, Any]:
    return artifacts.summarize_backlog(payload)


def summarize_worker_state(provider: str, payload: Any) -> dict[str, Any]:
    return operations.summarize_worker_state(_dashboard_context(), provider, payload)


def build_worker_summaries() -> list[dict[str, Any]]:
    return operations.build_worker_summaries(_dashboard_context(), load_json_file)


def _read_proc_status(path: Path) -> dict[str, str]:
    return operations.read_proc_status(path)


def build_process_inventory() -> dict[str, Any]:
    return operations.build_process_inventory()


def build_host_status() -> dict[str, Any]:
    return operations.build_host_status(_dashboard_context())


def _tail_text_file(path: Path, lines: int = 250) -> str:
    return operations.tail_text_file(path, lines)


def build_log_overview(*, include_admin_logs: bool = False) -> dict[str, Any]:
    return operations.build_log_overview(
        _dashboard_context(), include_admin_logs=include_admin_logs
    )


def build_operations_overview(*, include_private: bool = False) -> dict[str, Any]:
    """Compose the public or admin operations snapshot."""

    sources = operations.OperationsSources(
        load_json_file=load_json_file,
        build_worker_summaries=build_worker_summaries,
        build_host_status=build_host_status,
        build_file_inventory=build_file_inventory,
        build_process_inventory=build_process_inventory,
        build_log_overview=build_log_overview,
    )
    return operations.build_operations_overview(sources, include_private=include_private)


def archive_entries(archives: Any) -> dict[str, Any]:
    return metrics.archive_entries(archives)


def summarize_archive_status(entries: dict[str, Any]) -> dict[str, int]:
    return metrics.summarize_archive_status(entries)


def summarize_submissions(submissions: Any) -> dict[str, Any]:
    return metrics.summarize_submissions(submissions)


def summarize_coverage(coverage: Any) -> dict[str, Any]:
    return metrics.summarize_coverage(coverage)


def build_kpi_metrics() -> dict[str, Any]:
    """Compose dashboard KPI metrics from patchable artifact readers."""

    sources = metrics.MetricSources(
        load_json_file=load_json_file,
        load_csv_jobs=load_csv_jobs,
        load_vps_config=load_vps_config,
        build_worker_summaries=build_worker_summaries,
    )
    return metrics.build_kpi_metrics(sources)


def _admin_download_response(request: DashboardRequest) -> HttpResponse:
    resolution = downloads.resolve_admin_download(
        _dashboard_context(),
        request,
        is_repository_file_displayable=_is_repository_admin_file,
    )
    return downloads.render_download(resolution)


def _public_download_response(request: DashboardRequest) -> HttpResponse:
    resolution = downloads.resolve_public_download(_dashboard_context(), request)
    return downloads.render_download(resolution)


def _dashboard_application() -> DashboardApplication:
    """Inject compatibility wrappers into the transport-neutral route layer."""

    return DashboardApplication(
        DashboardRouteServices(
            admin_overview=lambda: build_operations_overview(include_private=True),
            admin_file=_admin_download_response,
            operations=build_operations_overview,
            metrics=build_kpi_metrics,
            system_vps=load_vps_config,
            vps_log=lambda: {"log": load_vps_log(250)},
            section1_jobs=load_csv_jobs,
            section1_backlog=public_backlog_jobs,
            section1_coverage=lambda: load_json_file("job_search_coverage.json"),
            section1_cache=lambda: load_json_file("ats_boards_cache.json"),
            section2_generation=lambda: public_generation_records(
                load_json_file("vps_generation_jobs.json")
            ),
            section2_archives=lambda: public_archive_records(
                load_json_file("vps_document_archive_state.json", default={})
            ),
            section3_submissions=lambda: public_submission_records(
                load_json_file("submission_log.json", default={})
            ),
            section3_failures=lambda: load_json_file("vps_application_failures.json"),
            section3_state=lambda: load_json_file("vps_application_state.json"),
            public_file=lambda request: downloads.public_text_file_response(
                _dashboard_context(), request
            ),
            public_download=_public_download_response,
        )
    )


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Thin HTTP adapter for the typed dashboard application."""

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
        request = DashboardRequest.parse(self.path)
        match = match_get_request(request)
        if isinstance(match, StaticRouteMatch):
            self.path = match.target
            super().do_GET()
            return
        if isinstance(match, StaticPassthrough):
            super().do_GET()
            return
        if match.key is RouteKey.PUBLIC_DOWNLOAD:
            self._handle_file_download()
            return
        self._handle_api_get()

    def do_POST(self) -> None:
        # This public dashboard never exposes write or command-executing routes.
        self.send_error(404, "Endpoint not found")

    def _send_response(self, response: HttpResponse) -> None:
        self.send_response(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def _send_json(self, data: Any, status: int = 200) -> None:
        """Compatibility seam for callers that emit JSON directly."""

        self._send_response(HttpResponse.json(data, status=status))

    def _handle_admin_file_download(self) -> None:
        """Compatibility seam for the historical admin file handler."""

        request = DashboardRequest.parse(self.path)
        self._send_response(_admin_download_response(request))

    def _handle_file_download(self) -> None:
        """Compatibility seam for the historical public download handler."""

        request = DashboardRequest.parse(self.path)
        self._send_response(_public_download_response(request))

    def _handle_api_get(self) -> None:
        request = DashboardRequest.parse(self.path)
        match = match_get_request(request)
        if not isinstance(match, ApiRouteMatch):
            self._send_json({"error": "Unknown API route"}, status=404)
            return
        self._send_response(_dashboard_application().dispatch(request, match))


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
