"""Remove conclusively closed Lever jobs from a local review queue."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}


@dataclass(frozen=True)
class Check:
    url: str
    status: str
    reason: str
    http_status: int | None = None


def identity(url: str) -> tuple[str, str, str] | None:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname not in HOSTS or len(parts) < 2:
        return None
    region = "eu" if parsed.hostname == "jobs.eu.lever.co" else "global"
    return region, parts[0], parts[1]


def check_url(url: str, timeout: float) -> Check:
    parsed = identity(url)
    if parsed is None:
        return Check(url, "unknown", "invalid_lever_url")
    region, board, job_id = parsed
    api_host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    target = f"https://{api_host}/v0/postings/{quote(board, safe='')}/{quote(job_id, safe='')}?mode=json"
    try:
        response = requests.get(target, timeout=timeout, headers={"Accept": "application/json"})
    except requests.RequestException:
        return Check(url, "unknown", "request_failed")
    if response.status_code in {404, 410}:
        return Check(url, "closed", f"http_{response.status_code}", response.status_code)
    if response.status_code >= 400:
        return Check(url, "unknown", f"indeterminate_http_{response.status_code}", response.status_code)
    try:
        payload = response.json()
    except ValueError:
        return Check(url, "unknown", "unexpected_api_response", response.status_code)
    if isinstance(payload, dict) and str(payload.get("id", "")) == job_id:
        return Check(url, "live", "posting_present", response.status_code)
    return Check(url, "unknown", "unexpected_posting_identity", response.status_code)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/lever_failed_product_management.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    urls = list(dict.fromkeys(str(item.get("job_url", "")).strip() for item in payload))
    checks: dict[str, Check] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_url, url, args.timeout_seconds): url for url in urls}
        for future in as_completed(futures):
            checks[futures[future]] = future.result()
    retained = [item for item in payload if checks[str(item.get("job_url", "")).strip()].status != "closed"]
    removed = [
        {"record": item, "check": asdict(checks[str(item.get("job_url", "")).strip()])}
        for item in payload if checks[str(item.get("job_url", "")).strip()].status == "closed"
    ]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.output_dir / f"lever_failed_product_management_backup_{stamp}.json"
    if args.apply:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.input, backup)
        temporary = args.input.with_suffix(args.input.suffix + ".tmp")
        temporary.write_text(json.dumps(retained, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(args.input)
    report = {
        "applied": args.apply,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "unique_urls_checked": len(urls),
        "retained": len(retained),
        "removed": len(removed),
        "status_counts": dict(Counter(check.status for check in checks.values())),
        "removed_records": removed,
        "backup_path": str(backup) if args.apply else "",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "lever_failed_url_prune_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: report[key] for key in ("applied", "unique_urls_checked", "retained", "removed", "status_counts", "backup_path")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
