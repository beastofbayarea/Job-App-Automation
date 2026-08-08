"""Minimal public, read-only status dashboard for automation workers."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import webbrowser
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
STARTED_AT = time.monotonic()
INDEXNOW_KEY = "b10fccb4cc5444ffa939921ba44ad5e0"
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Flow Status</title><style>
:root{color-scheme:dark;--bg:#0b1020;--card:#151d33;--muted:#93a4c3;--good:#4ade80;--bad:#fb7185}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eef2ff;font:15px system-ui,sans-serif}main{width:min(960px,92vw);margin:8vh auto}header{display:flex;justify-content:space-between;align-items:center;gap:1rem}h1{margin:0;font-size:clamp(1.6rem,5vw,2.5rem)}#live{color:var(--good)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1rem;margin:2rem 0}.card{background:var(--card);border:1px solid #263451;border-radius:14px;padding:1.2rem}.value{font-size:2rem;font-weight:700;margin-top:.4rem}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse;background:var(--card);border-radius:14px;overflow:hidden}th,td{padding:.9rem;text-align:left;border-bottom:1px solid #263451}th{color:var(--muted)}footer{margin-top:1.4rem;color:var(--muted)}@media(max-width:560px){main{margin:4vh auto}th:nth-child(3),td:nth-child(3){display:none}}
</style></head><body><main><header><div><h1>Job Flow</h1><div class="muted">Automation status</div></div><div id="live">● Connecting</div></header><section class="grid"><div class="card"><div class="muted">Confirmed</div><div class="value" id="confirmed">—</div></div><div class="card"><div class="muted">Failed</div><div class="value" id="failed">—</div></div><div class="card"><div class="muted">Backlog</div><div class="value" id="backlog">—</div></div><div class="card"><div class="muted">Workers</div><div class="value" id="workerCount">—</div></div></section><h2>Workers</h2><table><thead><tr><th>Worker</th><th>Status</th><th>Updated</th></tr></thead><tbody id="workers"><tr><td colspan="3" class="muted">Loading…</td></tr></tbody></table><footer id="updated">Waiting for status…</footer></main><script>
const el=id=>document.getElementById(id),esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);const d=await r.json();el('confirmed').textContent=d.submissions.confirmed;el('failed').textContent=d.submissions.failed;el('backlog').textContent=d.backlog;el('workerCount').textContent=d.workers.length;el('workers').innerHTML=d.workers.length?d.workers.map(w=>`<tr><td>${esc(w.name)}</td><td>${esc(w.status)}</td><td>${esc(w.updated_at)}</td></tr>`).join(''):'<tr><td colspan="3" class="muted">No worker state found</td></tr>';el('updated').textContent=`Updated ${d.updated_at} · dashboard uptime ${d.uptime_seconds}s`;el('live').textContent='● Live'}catch(e){el('live').textContent='● Unavailable';el('live').style.color='var(--bad)'}}refresh();setInterval(refresh,10000);
</script></body></html>"""


def _sentry() -> Any | None:
    try:
        return importlib.import_module("sentry_sdk")
    except ImportError:
        return None


def _capture_exception(exc: Exception) -> None:
    client = _sentry()
    if client is not None:
        client.capture_exception(exc)


def _read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _records(payload: object) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, Mapping):
        values = next(
            (
                payload[key]
                for key in ("records", "submissions", "jobs", "items")
                if isinstance(payload.get(key), list)
            ),
            [],
        )
    else:
        values = []
    return [value for value in values if isinstance(value, Mapping)]


def _submission_counts() -> dict[str, int]:
    rows = _records(_read_json(OUTPUT_DIR / "submission_log.json", []))
    confirmed = failed = 0
    for row in rows:
        status = str(row.get("status") or row.get("outcome") or "").casefold()
        if (
            row.get("submitted") is True and row.get("confirmed") is True
        ) or "submitted & confirmed" in status:
            confirmed += 1
        elif any(marker in status for marker in ("fail", "error", "blocked", "manual_review")):
            failed += 1
    return {"total": len(rows), "confirmed": confirmed, "failed": failed}


def _worker_summaries() -> list[dict[str, str]]:
    workers: list[dict[str, str]] = []
    for path in sorted(OUTPUT_DIR.glob("vps_*_state.json")):
        payload = _read_json(path, {})
        if isinstance(payload, Mapping):
            workers.append(
                {
                    "name": path.stem.removeprefix("vps_").removesuffix("_state").replace("_", " "),
                    "status": str(
                        payload.get("status")
                        or payload.get("last_status")
                        or payload.get("phase")
                        or "unknown"
                    ),
                    "updated_at": str(
                        payload.get("updated_at")
                        or payload.get("last_cycle_at")
                        or payload.get("last_updated_at")
                        or "—"
                    ),
                }
            )
    return workers


def build_status() -> dict[str, object]:
    """Return aggregate health without publishing job or candidate PII."""
    return {
        "healthy": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(time.monotonic() - STARTED_AT),
        "submissions": _submission_counts(),
        "backlog": len(_records(_read_json(OUTPUT_DIR / "job_backlog.json", []))),
        "workers": _worker_summaries(),
    }


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "JobFlowDashboard/1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        for name, value in (
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            (
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
            ),
        ):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(payload, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(build_status())
            elif path == "/healthz":
                self._json({"healthy": True})
            elif path == "/robots.txt":
                self._send(
                    200,
                    b"User-agent: *\nAllow: /\nSitemap: https://skybison.cloud/sitemap.xml\n",
                    "text/plain; charset=utf-8",
                )
            elif path == "/sitemap.xml":
                self._send(
                    200,
                    b'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://skybison.cloud/</loc></url></urlset>',
                    "application/xml; charset=utf-8",
                )
            elif path == f"/{INDEXNOW_KEY}.txt":
                self._send(200, INDEXNOW_KEY.encode(), "text/plain; charset=utf-8")
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as exc:
            _capture_exception(exc)
            self._json({"error": "Status unavailable"}, 500)

    def do_POST(self) -> None:
        self._json({"error": "Read-only dashboard"}, 405)

    def log_message(self, format: str, *args: object) -> None:
        return


class ReuseAddrHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def run_dashboard_server(
    host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True
) -> None:
    client = _sentry()
    if client is not None:
        client.init(
            dsn=os.environ.get("SENTRY_DSN"), send_default_pii=False, traces_sample_rate=0.0
        )
    httpd = ReuseAddrHTTPServer((host, port), DashboardRequestHandler)
    url = f"http://{host}:{port}/"
    print(f"Job Flow dashboard running at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the minimal Job Flow status dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    run_dashboard_server(args.host, args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
