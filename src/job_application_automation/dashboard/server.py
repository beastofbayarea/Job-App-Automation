"""HTTP REST & Static server for VPS Output Monitor Dashboard."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Sequence

STATIC_DIR = Path(__file__).parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_DIR = PROJECT_ROOT / "config"


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
    except Exception:
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
    except Exception:
        pass
    return jobs


def load_vps_config() -> dict[str, Any]:
    config_path = CONFIG_DIR / "vps_config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Redact password
            if "vps" in data and "ssh_password" in data["vps"]:
                data["vps"]["ssh_password"] = "******"
            return data
    except Exception:
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

    archived_sets = len(archives) if isinstance(archives, dict) else 0
    gen_queue_size = len(generation_jobs) if isinstance(generation_jobs, list) else 0

    returned_jobs = 0
    live_status_counts = {}
    if isinstance(coverage, dict) and "results" in coverage:
        returned_jobs = coverage["results"].get("returned", len(jobs))
        live_status_counts = coverage["results"].get("live_status_counts", {})
    else:
        returned_jobs = len(jobs)

    cached_boards_count = len(cache_data) if isinstance(cache_data, dict) else 0

    return {
        "total_submissions": total_submissions,
        "failure_count": failure_count,
        "total_jobs_found": returned_jobs,
        "generation_queue_count": gen_queue_size,
        "archived_document_sets": archived_sets,
        "cached_boards_count": cached_boards_count,
        "ats_submissions": ats_counts,
        "attempted_by_ats": attempted_by_ats,
        "confirmed_by_ats": confirmed_by_ats,
        "live_status_counts": live_status_counts,
        "last_failure_update": failures_data.get("updated_at", "") if isinstance(failures_data, dict) else "",
        "vps_info": vps_cfg.get("vps", {}),
        "hostinger_info": vps_cfg.get("hostinger_account", {}),
    }


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/api/download/"):
            self._handle_file_download()
            return
        if self.path.startswith("/api/"):
            self._handle_api_get()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/vps/sync":
            self._handle_vps_sync()
            return
        if self.path == "/api/vps/status":
            self._handle_vps_status()
            return
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

        candidates = [
            OUTPUT_DIR / filename,
            OUTPUT_DIR / "vps_reports" / filename,
        ]

        if not any(c.exists() for c in candidates):
            for path in OUTPUT_DIR.rglob(filename):
                candidates.append(path)
                break

        target_file = None
        for c in candidates:
            if c.exists() and c.is_file():
                target_file = c
                break

        if not target_file:
            self._send_json({"error": f"Resume PDF not found: {filename}"}, status=404)
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

    def _handle_vps_sync(self) -> None:
        try:
            script = PROJECT_ROOT / "scripts" / "pull_vps_application_reports.ps1"
            if not script.exists():
                self._send_json({"status": "error", "message": "Pull script not found"}, status=400)
                return

            cmd = ["pwsh", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Overwrite"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if res.returncode == 0:
                self._send_json({"status": "success", "output": res.stdout})
            else:
                self._send_json({"status": "error", "output": res.stderr}, status=500)
        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, status=500)

    def _handle_vps_status(self) -> None:
        try:
            script = PROJECT_ROOT / "scripts" / "check_vps_automation_status.ps1"
            if not script.exists():
                self._send_json({"status": "error", "message": "Status script not found"}, status=400)
                return

            cmd = ["pwsh", "-ExecutionPolicy", "Bypass", "-File", str(script), "-LogLines", "50"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            self._send_json({"status": "success", "output": res.stdout, "exit_code": res.returncode})
        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, status=500)


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
