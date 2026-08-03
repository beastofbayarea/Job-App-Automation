"""Remove conclusively closed Ashby jobs from a local failed-JSON queue."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


API_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


@dataclass(frozen=True)
class Check:
    url: str
    status: str
    reason: str
    board: str = ""
    job_id: str = ""
    http_status: int | None = None


def ashby_identity(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.hostname not in {"jobs.ashbyhq.com", "ashbyhq.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def fetch_active_ids(board: str, timeout: float) -> tuple[set[str] | None, str, int | None]:
    try:
        response = requests.get(
            API_URL.format(token=quote(board, safe="")),
            headers=HEADERS,
            timeout=timeout,
        )
    except requests.RequestException:
        return None, "request_failed", None
    if response.status_code in {404, 410}:
        return set(), f"board_http_{response.status_code}", response.status_code
    if response.status_code >= 400:
        return None, f"indeterminate_http_{response.status_code}", response.status_code
    try:
        payload = response.json()
    except ValueError:
        return None, "unexpected_api_response", response.status_code
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    active_ids = {
        str(item.get("id", "")).strip()
        for item in jobs
        if isinstance(item, dict) and item.get("id") and item.get("isListed") is not False
    }
    return active_ids, "active_board", response.status_code


def check_urls(urls: list[str], timeout: float, workers: int) -> dict[str, Check]:
    by_board: dict[str, list[tuple[str, str]]] = defaultdict(list)
    checks: dict[str, Check] = {}
    for url in urls:
        identity = ashby_identity(url)
        if identity is None:
            checks[url] = Check(url, "unknown", "invalid_ashby_url")
            continue
        board, job_id = identity
        by_board[board].append((url, job_id))

    results: dict[str, tuple[set[str] | None, str, int | None]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_active_ids, board, timeout): board for board in by_board
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    for board, jobs in by_board.items():
        active_ids, reason, http_status = results[board]
        for url, job_id in jobs:
            if active_ids is None:
                checks[url] = Check(url, "unknown", reason, board, job_id, http_status)
            elif job_id in active_ids:
                checks[url] = Check(
                    url, "live", "job_present_in_current_board_response", board, job_id, http_status
                )
            else:
                checks[url] = Check(
                    url, "closed", "job_missing_from_current_board_response", board, job_id,
                    http_status,
                )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/application-queues/ashby/product-management.json"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Ashby failed JSON must contain a list")
    urls = list(
        dict.fromkeys(
            str(record.get("job_url", "")).strip()
            for record in payload
            if isinstance(record, dict) and str(record.get("job_url", "")).strip()
        )
    )
    checks = check_urls(urls, args.timeout_seconds, args.workers)
    retained = [
        record for record in payload if checks[str(record.get("job_url", "")).strip()].status != "closed"
    ]
    removed = [
        {"record": record, "check": asdict(checks[str(record.get("job_url", "")).strip()])}
        for record in payload
        if checks[str(record.get("job_url", "")).strip()].status == "closed"
    ]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = args.output_dir / f"ashby_failed_product_management_backup_{timestamp}.json"
    if args.apply:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.input, backup_path)
        temporary = args.input.with_suffix(args.input.suffix + ".tmp")
        temporary.write_text(
            json.dumps(retained, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(args.input)

    reason_counts = Counter((check.status, check.reason) for check in checks.values())
    report = {
        "applied": args.apply,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "unique_urls_checked": len(urls),
        "retained": len(retained),
        "removed": len(removed),
        "status_counts": dict(Counter(check.status for check in checks.values())),
        "reason_counts": {
            f"{status}:{reason}": count
            for (status, reason), count in sorted(reason_counts.items())
        },
        "removed_records": removed,
        "backup_path": str(backup_path) if args.apply else "",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "ashby_failed_url_prune_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("applied", "unique_urls_checked", "retained", "removed", "status_counts", "backup_path")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
