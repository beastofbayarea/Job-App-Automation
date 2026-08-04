"""Prune conclusively closed SmartRecruiters or Workable queue entries."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


CANONICAL_LINK = re.compile(
    r'<link\b(?=[^>]*\brel=["\']canonical["\'])(?=[^>]*\bhref=["\']([^"\']+)["\'])[^>]*>',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Check:
    url: str
    status: str
    reason: str
    http_status: int | None = None


def identity(url: str, platform: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if platform == "smartrecruiters":
        if parsed.hostname not in {"jobs.smartrecruiters.com", "www.smartrecruiters.com"} or len(parts) < 2:
            return None
        return parts[0], parts[1].split("-", 1)[0]
    if parsed.hostname != "apply.workable.com":
        return None
    if len(parts) >= 3 and parts[1].lower() in {"j", "jobs"}:
        return parts[0], parts[2]
    return None


def fetch_json(url: str, timeout: float):
    try:
        response = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except requests.RequestException:
        return None, "request_failed", None
    if response.status_code in {404, 410}:
        return {}, f"http_{response.status_code}", response.status_code
    if response.status_code >= 400:
        return None, f"indeterminate_http_{response.status_code}", response.status_code
    try:
        return response.json(), "ok", response.status_code
    except ValueError:
        return None, "unexpected_api_response", response.status_code


def check_smartrecruiters(url: str, timeout: float) -> Check:
    parsed = identity(url, "smartrecruiters")
    if parsed is None:
        return Check(url, "unknown", "invalid_smartrecruiters_url")
    company, posting = parsed
    payload, reason, code = fetch_json(
        f"https://api.smartrecruiters.com/v1/companies/{quote(company, safe='')}/postings/{quote(posting, safe='')}", timeout
    )
    if payload == {} and reason.startswith("http_"):
        return Check(url, "closed", reason, code)
    if not isinstance(payload, dict):
        return Check(url, "unknown", reason, code)
    if str(payload.get("id", "")) != posting:
        return Check(url, "unknown", "unexpected_posting_identity", code)
    if payload.get("active") is False or str(payload.get("visibility", "")).upper() not in {"", "PUBLIC"}:
        return Check(url, "closed", "posting_not_active_or_public", code)
    return Check(url, "live", "active_public_posting_present", code)


def check_workable(urls: list[str], timeout: float, workers: int) -> dict[str, Check]:
    by_board: dict[str, list[tuple[str, str]]] = defaultdict(list)
    checks: dict[str, Check] = {}
    short_urls: list[str] = []
    for url in urls:
        parsed = identity(url, "workable")
        if parsed is None:
            split = urlsplit(url)
            parts = [part for part in split.path.split("/") if part]
            if (
                split.hostname == "apply.workable.com"
                and len(parts) >= 2
                and parts[0].lower() in {"j", "jobs"}
            ):
                short_urls.append(url)
            else:
                checks[url] = Check(url, "unknown", "invalid_workable_url")
        else:
            by_board[parsed[0]].append((url, parsed[1]))
    if short_urls:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(requests.get, url, timeout=timeout, allow_redirects=True): url
                for url in short_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                except requests.RequestException:
                    checks[url] = Check(url, "unknown", "short_link_request_failed")
                    continue
                if response.status_code in {404, 410}:
                    checks[url] = Check(
                        url, "closed", f"http_{response.status_code}", response.status_code
                    )
                    continue
                resolved = identity(response.url, "workable")
                if resolved is None and response.status_code < 400:
                    canonical = CANONICAL_LINK.search(response.text)
                    if canonical:
                        resolved = identity(html.unescape(canonical.group(1)), "workable")
                if response.status_code >= 400 or resolved is None:
                    checks[url] = Check(
                        url, "unknown", "short_link_not_resolved", response.status_code
                    )
                    continue
                by_board[resolved[0]].append((url, resolved[1]))
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_json, f"https://www.workable.com/api/accounts/{quote(board, safe='')}?details=true", timeout): board
            for board in by_board
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    for board, jobs in by_board.items():
        payload, reason, code = results[board]
        if payload == {} and reason.startswith("http_"):
            active_ids: set[str] | None = set()
        elif isinstance(payload, dict):
            active_ids = {
                str(item.get("shortcode", "")).strip()
                for item in payload.get("jobs", [])
                if isinstance(item, dict) and item.get("shortcode")
            }
        else:
            active_ids = None
        for url, job_id in jobs:
            if active_ids is None:
                checks[url] = Check(url, "unknown", reason, code)
            elif job_id in active_ids:
                checks[url] = Check(url, "live", "job_present_in_current_account_response", code)
            else:
                checks[url] = Check(url, "closed", "job_missing_from_current_account_response", code)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("smartrecruiters", "workable"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--remove-unsure",
        action="store_true",
        help="Remove records whose liveness cannot be authoritatively confirmed.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    urls = list(dict.fromkeys(str(item.get("job_url", "")).strip() for item in payload))
    if args.platform == "workable":
        checks = check_workable(urls, args.timeout_seconds, args.workers)
    else:
        checks = {}
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(check_smartrecruiters, url, args.timeout_seconds): url for url in urls}
            for future in as_completed(futures):
                checks[futures[future]] = future.result()
    retained_statuses = {"live"} if args.remove_unsure else {"live", "unknown"}
    retained = [item for item in payload if checks[str(item.get("job_url", "")).strip()].status in retained_statuses]
    removed = [{"record": item, "check": asdict(checks[str(item.get("job_url", "")).strip()])} for item in payload if checks[str(item.get("job_url", "")).strip()].status not in retained_statuses]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.output_dir/f"{args.platform}_failed_product_management_backup_{stamp}.json"
    if args.apply:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.input, backup)
        temporary = args.input.with_suffix(args.input.suffix + ".tmp")
        temporary.write_text(
            json.dumps(retained, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(args.input)
    report = {"applied": args.apply, "platform": args.platform, "checked_at": datetime.now(timezone.utc).isoformat(), "unique_urls_checked": len(urls), "retained": len(retained), "removed": len(removed), "status_counts": dict(Counter(check.status for check in checks.values())), "removed_records": removed, "backup_path": str(backup) if args.apply else ""}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir/f"{args.platform}_failed_url_prune_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("platform", "applied", "unique_urls_checked", "retained", "removed", "status_counts", "backup_path")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
