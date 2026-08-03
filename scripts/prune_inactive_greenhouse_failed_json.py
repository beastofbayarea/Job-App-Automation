"""Remove only conclusively inactive/invalid URLs from failed Greenhouse JSON files."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


GREENHOUSE_HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
}
JOB_ID = re.compile(r"/jobs/(?:[^/]+/)?(?P<id>\d+)(?:/|$)", re.IGNORECASE)
DEAD_MARKERS = (
    "job is no longer available",
    "this job is no longer available",
    "job posting is no longer available",
    "position has been filled",
    "position is no longer available",
    "job has expired",
    "this role has been filled",
    "job not found",
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}


@dataclass(frozen=True)
class Check:
    url: str
    removable: bool
    reason: str
    http_status: int | None = None
    final_url: str = ""


def _native_identity(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.hostname not in GREENHOUSE_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    match = JOB_ID.search(parsed.path)
    if not parts or match is None:
        return None
    return parts[0], match.group("id")


def _request(url: str, timeout: float) -> requests.Response | None:
    for attempt in range(2):
        try:
            return requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            if attempt == 0:
                time.sleep(0.25)
    return None


def check_url(url: str, timeout: float) -> Check:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return Check(url, True, "malformed_url")
    identity = _native_identity(url)
    target = url
    expected_id = ""
    if identity is not None:
        token, expected_id = identity
        target = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{quote(token, safe='')}/jobs/{quote(expected_id, safe='')}"
        )
    response = _request(target, timeout)
    if response is None:
        return Check(url, False, "request_failed")
    if response.status_code in {404, 410}:
        return Check(url, True, f"http_{response.status_code}", response.status_code, response.url)
    if response.status_code >= 400:
        return Check(
            url, False, f"indeterminate_http_{response.status_code}", response.status_code, response.url
        )
    if identity is not None:
        try:
            payload = response.json()
        except ValueError:
            return Check(url, False, "unexpected_api_response", response.status_code, response.url)
        if isinstance(payload, dict) and str(payload.get("id", "")) == expected_id:
            return Check(url, False, "active_api_job", response.status_code, response.url)
        return Check(url, False, "unexpected_api_identity", response.status_code, response.url)
    lowered = response.text.casefold()
    if any(marker in lowered for marker in DEAD_MARKERS):
        return Check(url, True, "explicit_closed_message", response.status_code, response.url)
    return Check(url, False, "not_conclusively_inactive", response.status_code, response.url)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir", type=Path, default=Path("data/application-queues/greenhouse")
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    paths = sorted(args.data_dir.glob("*.json"))
    payloads = {path: json.loads(path.read_text(encoding="utf-8")) for path in paths}
    urls = sorted(
        {
            str(record.get("job_url", "")).strip()
            for payload in payloads.values()
            for record in payload
            if isinstance(record, dict)
        }
    )
    checks: dict[str, Check] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_url, url, args.timeout_seconds): url for url in urls}
        for future in as_completed(futures):
            checks[futures[future]] = future.result()

    removed_by_file: dict[str, int] = {}
    removed_records: list[dict[str, object]] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = args.output_dir / f"greenhouse_failed_json_backup_{timestamp}"
    if args.apply:
        backup_dir.mkdir(parents=True, exist_ok=False)
    for path, payload in payloads.items():
        retained = []
        removed = []
        for record in payload:
            url = str(record.get("job_url", "")).strip() if isinstance(record, dict) else ""
            check = checks[url]
            if check.removable:
                removed.append({"record": record, "check": asdict(check)})
            else:
                retained.append(record)
        removed_by_file[path.name] = len(removed)
        removed_records.extend({"file": path.name, **item} for item in removed)
        if args.apply:
            shutil.copy2(path, backup_dir / path.name)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(retained, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            temporary.replace(path)

    report = {
        "applied": args.apply,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "unique_urls_checked": len(urls),
        "removed_by_file": removed_by_file,
        "removed_records": removed_records,
        "backup_dir": str(backup_dir) if args.apply else "",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "greenhouse_failed_url_prune_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("applied", "unique_urls_checked", "removed_by_file", "backup_dir")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
