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
import os
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("DashboardServer")

STATIC_DIR = Path(__file__).parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_DIR = PROJECT_ROOT / "config"
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
        with open(path, "r", encoding="utf-8") as f:
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
        with open(path, "r", encoding="utf-8", errors="replace") as f:
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
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        vps = data.get("vps", {})
        if not isinstance(vps, dict):
            return {}
        return {
            "vps": {
                key: vps[key]
                for key in sorted(_PUBLIC_VPS_FIELDS)
                if key in vps
            }
        }
    except Exception as exc:
        logger.warning("Failed to load VPS config from %s: %s", config_path, exc)
        return {}


def load_vps_log(lines: int = 250) -> str:
    log_path = get_output_file_path("vps_sync.log")
    if not log_path.exists():
        return "vps_sync.log not found."
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except Exception as e:
        return f"Error reading log file: {e}"


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
    jobs = load_csv_jobs()
    vps_cfg = load_vps_config()

    total_submissions = len(submissions) if isinstance(submissions, dict) else 0
    failure_count = failures_data.get("failure_count", 0) if isinstance(failures_data, dict) else 0

    ats_counts: dict[str, int] = {}
    if isinstance(submissions, dict):
        for sub in submissions.values():
            if isinstance(sub, dict):
                ats = sub.get("ats", "unknown").lower()
                ats_counts[ats] = ats_counts.get(ats, 0) + 1

    attempted_by_ats = failures_data.get("attempted_by_ats", {}) if isinstance(failures_data, dict) else {}
    confirmed_by_ats = failures_data.get("confirmed_by_ats", {}) if isinstance(failures_data, dict) else {}

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
        "total_jobs_found": returned_jobs,
        "generation_queue_count": gen_queue_size,
        "archived_document_sets": archived_sets,
        "archive_status_counts": archive_status_counts,
        "cached_boards_count": cached_boards_count,
        "ats_submissions": ats_counts,
        # Per-run counters, sourced from vps_application_failures.json.
        "attempted_by_ats": attempted_by_ats,
        "confirmed_by_ats": confirmed_by_ats,
        "run_started_at": failures_data.get("run_started_at", "") if isinstance(failures_data, dict) else "",
        # Cumulative counters, sourced from the append-only submission log.
        "confirmed_by_ats_all_time": submission_summary["confirmed_by_ats"],
        "submissions_by_status": submission_summary["by_status"],
        "latest_submission_at": submission_summary["latest_applied_at"],
        "live_status_counts": live_status_counts,
        "coverage": coverage_summary,
        "last_failure_update": failures_data.get("updated_at", "") if isinstance(failures_data, dict) else "",
        "vps_info": vps_cfg.get("vps", {}),
        "hostinger_info": vps_cfg.get("hostinger_account", {}),
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
        path = self.path.split("?")[0]
        
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

    def _handle_file_download(self) -> None:
        filename = os.path.basename(self.path.split("?")[0])
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            self._send_json({"error": "Invalid filename"}, status=400)
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
            if not str(resolved).startswith(str(output_root)):
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

            content_type = "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream"
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
        if path == "/api/metrics":
            self._send_json(build_kpi_metrics())
        elif path == "/api/system/vps":
            self._send_json(load_vps_config())
        elif path == "/api/vps/log":
            self._send_json({"log": load_vps_log(250)})
        elif path == "/api/section1/jobs":
            self._send_json(load_csv_jobs())
        elif path == "/api/section1/coverage":
            self._send_json(load_json_file("job_search_coverage.json"))
        elif path == "/api/section1/cache":
            self._send_json(load_json_file("ats_boards_cache.json"))
        elif path == "/api/section2/generation":
            self._send_json(load_json_file("vps_generation_jobs.json"))
        elif path == "/api/section2/archives":
            self._send_json(load_json_file("vps_document_archive_state.json"))
        elif path == "/api/section3/submissions":
            self._send_json(load_json_file("submission_log.json"))
        elif path == "/api/section3/failures":
            self._send_json(load_json_file("vps_application_failures.json"))
        elif path == "/api/section3/state":
            self._send_json(load_json_file("vps_application_state.json"))
        elif path.startswith("/api/files/"):
            filename = os.path.basename(path)
            file_path = get_output_file_path(filename)
            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    self._send_json({"filename": filename, "path": str(file_path), "content": content})
                except Exception as e:
                    self._send_json({"error": str(e)}, status=500)
            else:
                self._send_json({"error": "File not found"}, status=404)
        else:
            self._send_json({"error": "Unknown API route"}, status=404)


class ReuseAddrHTTPServer(HTTPServer):
    allow_reuse_address = True


def run_dashboard_server(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True) -> None:
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
    parser.add_argument("--host", default="127.0.0.1", help="Host address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser on startup")

    args = parser.parse_args(argv)
    run_dashboard_server(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
