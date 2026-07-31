"""Run coordinated continuous ATS workers from search output or an Excel tracker."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from collections.abc import Mapping, Sequence

from .artifacts import atomic_write_text, interprocess_file_lock, read_json
from .continuous_ats import (
    DEFAULT_BACKLOG,
    DEFAULT_EMAIL_POOL,
    DEFAULT_LAUNCHER,
    DEFAULT_PROFILE,
    DEFAULT_SUBMISSION_LOG,
    RESUMABLE_STATUSES,
    SHARED_INPUT,
    _eligible_jobs,
    _load_state,
    _reconcile_interrupted_submissions,
    _save_state,
    process_one,
)
from .identity import canonical_job_url
from .orchestrator import load_jobs_from_tracker
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from ..resume.ai_client import scrape_job
from ..search.job_boards import DEAD_ROLE_MARKERS


UTC = timezone.utc
WORKER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
GREENHOUSE_JOB_PATH = re.compile(r"/jobs/(?:[^/]+/)?(?P<job_id>\d+)(?:/|$)", re.IGNORECASE)
CLAIMS_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _job_identity(job_url: str, ats_platform: str) -> str:
    """Return a provider identity stable across branded and native job URLs."""
    canonical = canonical_job_url(job_url)
    if ats_platform != "greenhouse":
        return canonical
    parsed = urlsplit(canonical)
    query = {
        key.casefold(): value.strip()
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
    }
    job_id = query.get("gh_jid", "")
    if not job_id:
        match = GREENHOUSE_JOB_PATH.search(parsed.path)
        job_id = match.group("job_id") if match else ""
    return f"greenhouse:{job_id}" if job_id else canonical


def _load_claims(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": CLAIMS_VERSION, "jobs": {}}
    payload = read_json(path)
    if (
        not isinstance(payload, dict)
        or payload.get("version") != CLAIMS_VERSION
        or not isinstance(payload.get("jobs"), dict)
    ):
        raise ValueError(f"invalid continuous source claims: {path}")
    return payload


def _state_records(path: Path) -> dict[str, Mapping[str, Any]]:
    if not path.is_file():
        return {}
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid continuous ATS state: {path}")
    return {
        str(url): record for url, record in payload["jobs"].items() if isinstance(record, Mapping)
    }


def _seed_claims_from_state(
    claims: dict[str, Any],
    *,
    state_path: Path,
    owner: str,
    ats_platform: str,
) -> None:
    claim_records: dict[str, Any] = claims["jobs"]
    for canonical_url, record in _state_records(state_path).items():
        job_url = str(record.get("job_url") or canonical_url)
        try:
            identity = _job_identity(job_url, ats_platform)
        except ValueError:
            continue
        if identity in claim_records:
            continue
        claim_records[identity] = {
            "owner": owner,
            "status": str(record.get("status") or "recorded"),
            "result_status": str(record.get("result_status") or ""),
            "job_url": job_url,
            "company": str(record.get("company") or ""),
            "title": str(record.get("title") or ""),
            "updated_at": str(record.get("updated_at") or _now()),
        }


def _seed_claims_from_ledger(
    claims: dict[str, Any],
    *,
    submission_log: Path,
    ats_platform: str,
) -> None:
    if not submission_log.is_file():
        return
    payload = read_json(submission_log)
    if not isinstance(payload, dict):
        raise ValueError("submission log root must be an object")
    claim_records: dict[str, Any] = claims["jobs"]
    for record in payload.values():
        if (
            not isinstance(record, Mapping)
            or str(record.get("ats", "")).strip().lower() != ats_platform
            or str(record.get("status", "")).strip() != "SUBMITTED & CONFIRMED"
        ):
            continue
        job_url = str(record.get("job_url") or "")
        try:
            identity = _job_identity(job_url, ats_platform)
        except ValueError:
            continue
        claim_records[identity] = {
            "owner": "ledger",
            "status": "confirmed",
            "result_status": "SUBMITTED & CONFIRMED",
            "job_url": job_url,
            "company": str(record.get("company") or ""),
            "title": str(record.get("role") or ""),
            "updated_at": str(record.get("applied_at") or _now()),
        }


def _source_jobs(
    *,
    source: str,
    ats_platform: str,
    input_path: Path,
    tracker_path: Path | None,
) -> list[dict[str, Any]]:
    if source == "search":
        if not input_path.is_file():
            return []
        return _eligible_jobs(read_json(input_path), ats_platform)
    if tracker_path is None:
        raise ValueError("--tracker is required for tracker source workers")
    jobs = load_jobs_from_tracker(tracker_path)
    return [
        {
            "job_url": str(job["url"]),
            "company": str(job["company"]),
            "title": str(job["role"]),
            "platform": ats_platform,
            "tracker_row": int(job["row_number"]),
        }
        for job in jobs
        if str(job["ats"]).strip().lower() == ats_platform
    ]


def _candidate_url(job: Mapping[str, Any]) -> str:
    return str(job.get("_canonical_url") or canonical_job_url(job.get("job_url", "")))


def _claim_next_job(
    jobs: Sequence[dict[str, Any]],
    *,
    ats_platform: str,
    worker_id: str,
    state_path: Path,
    peer_states: Sequence[Path],
    claims_path: Path,
    submission_log: Path,
) -> dict[str, Any] | None:
    own_records = _state_records(state_path)
    peer_identities: set[str] = set()
    for peer_state in peer_states:
        for canonical_url, record in _state_records(peer_state).items():
            job_url = str(record.get("job_url") or canonical_url)
            try:
                peer_identities.add(_job_identity(job_url, ats_platform))
            except ValueError:
                continue

    resumable: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    for job in jobs:
        try:
            canonical_url = _candidate_url(job)
            identity = _job_identity(str(job.get("job_url") or canonical_url), ats_platform)
        except ValueError:
            continue
        if identity in peer_identities:
            continue
        record = own_records.get(canonical_url)
        if isinstance(record, Mapping) and record.get("status") in RESUMABLE_STATUSES:
            resumable.append(job)
        elif canonical_url not in own_records:
            fresh.append(job)

    random.shuffle(resumable)
    random.shuffle(fresh)
    with interprocess_file_lock(claims_path):
        claims = _load_claims(claims_path)
        _seed_claims_from_state(
            claims,
            state_path=state_path,
            owner=worker_id,
            ats_platform=ats_platform,
        )
        for peer_state in peer_states:
            _seed_claims_from_state(
                claims,
                state_path=peer_state,
                owner=f"peer:{peer_state.stem}",
                ats_platform=ats_platform,
            )
        _seed_claims_from_ledger(
            claims,
            submission_log=submission_log,
            ats_platform=ats_platform,
        )
        claim_records: dict[str, Any] = claims["jobs"]
        selected: dict[str, Any] | None = None
        for job in resumable:
            identity = _job_identity(str(job["job_url"]), ats_platform)
            claim = claim_records.get(identity)
            if not isinstance(claim, Mapping) or claim.get("owner") == worker_id:
                selected = job
                break
        if selected is None:
            for job in fresh:
                identity = _job_identity(str(job["job_url"]), ats_platform)
                claim = claim_records.get(identity)
                if not isinstance(claim, Mapping):
                    selected = job
                    break
                if (
                    claim.get("owner") == worker_id
                    and claim.get("status") == "claimed"
                    and _candidate_url(job) not in own_records
                ):
                    selected = job
                    break
        if selected is None:
            claims["updated_at"] = _now()
            atomic_write_text(
                claims_path,
                json.dumps(claims, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return None
        identity = _job_identity(str(selected["job_url"]), ats_platform)
        claim_records[identity] = {
            "owner": worker_id,
            "status": "claimed",
            "job_url": str(selected["job_url"]),
            "company": str(selected.get("company") or ""),
            "title": str(selected.get("title") or ""),
            "updated_at": _now(),
        }
        claims["updated_at"] = _now()
        atomic_write_text(
            claims_path,
            json.dumps(claims, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return selected


def _hydrate_tracker_job(job: Mapping[str, Any], ats_platform: str) -> dict[str, Any]:
    scraped = scrape_job(str(job["job_url"]))
    description = str(scraped.get("jd_text") or "").strip()
    if len(description) < 200:
        raise RuntimeError("job page did not provide a usable job description")
    lowered = description.casefold()
    if any(marker in lowered for marker in DEAD_ROLE_MARKERS):
        raise RuntimeError("job page reports that the role is no longer available")
    return {
        **dict(job),
        "description": description,
        "platform": ats_platform,
        "live_status": "live",
    }


def _record_source_failure(
    *,
    job: Mapping[str, Any],
    ats_platform: str,
    state_path: Path,
    result_status: str,
    detail: str,
) -> None:
    state = _load_state(state_path, ats_platform)
    canonical_url = canonical_job_url(job["job_url"])
    state["jobs"][canonical_url] = {
        "status": "failed",
        "stage": "source",
        "job_url": str(job["job_url"]),
        "company": str(job.get("company") or ""),
        "title": str(job.get("title") or ""),
        "platform": ats_platform,
        "result_status": result_status,
        "stderr_tail": detail[-2000:],
        "started_at": _now(),
        "updated_at": _now(),
    }
    _save_state(state_path, state)


def _sync_claim_from_state(
    *,
    job: Mapping[str, Any],
    ats_platform: str,
    worker_id: str,
    state_path: Path,
    claims_path: Path,
    fallback_status: str,
) -> None:
    canonical_url = canonical_job_url(job["job_url"])
    record = _state_records(state_path).get(canonical_url, {})
    with interprocess_file_lock(claims_path):
        claims = _load_claims(claims_path)
        claims["jobs"][_job_identity(str(job["job_url"]), ats_platform)] = {
            "owner": worker_id,
            "status": str(record.get("status") or fallback_status),
            "result_status": str(record.get("result_status") or ""),
            "job_url": str(job["job_url"]),
            "company": str(job.get("company") or ""),
            "title": str(job.get("title") or ""),
            "updated_at": str(record.get("updated_at") or _now()),
        }
        claims["updated_at"] = _now()
        atomic_write_text(
            claims_path,
            json.dumps(claims, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _sleep_until_next_cycle(
    delay: int,
    *,
    ats_platform: str,
    worker_id: str,
) -> bool:
    try:
        time.sleep(delay)
    except KeyboardInterrupt:
        print(
            f"{ats_platform.upper()}_SOURCE_WORKER_STOPPED "
            f"worker={worker_id} signal=keyboard_interrupt",
            flush=True,
        )
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Continuously process disjoint ATS jobs from search output or an Excel tracker."
    )
    parser.add_argument("--ats-platform", required=True)
    parser.add_argument("--source", choices=("search", "tracker"), required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--input", type=Path, default=SHARED_INPUT)
    parser.add_argument("--tracker", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--peer-state", type=Path, action="append", default=[])
    parser.add_argument(
        "--claims",
        type=Path,
        default=resolve_runtime_path("output/continuous_greenhouse_claims.json"),
    )
    parser.add_argument("--selected-input", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--documents-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--email-pool", type=Path, default=DEFAULT_EMAIL_POOL)
    parser.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    parser.add_argument("--submission-log", type=Path, default=DEFAULT_SUBMISSION_LOG)
    parser.add_argument("--backlog", type=Path, default=DEFAULT_BACKLOG)
    parser.add_argument("--sleep-min-seconds", type=int, default=5)
    parser.add_argument("--sleep-max-seconds", type=int, default=15)
    parser.add_argument("--document-timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--engine-timeout-seconds",
        type=int,
        default=int(RUNTIME_CONFIG.application["queue_timeout_seconds"]),
    )
    parser.add_argument("--application-timeout-seconds", type=int, default=420)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate source files and print eligible counts without generating or submitting.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ats_platform = str(args.ats_platform).strip().lower()
    worker_id = str(args.worker_id).strip().lower()
    if not WORKER_ID_PATTERN.fullmatch(worker_id):
        raise SystemExit(
            "worker ID must contain only lowercase letters, digits, underscores, or dashes"
        )
    if args.source == "tracker" and args.tracker is None:
        raise SystemExit("--tracker is required for tracker source workers")
    if args.sleep_min_seconds <= 0 or args.sleep_max_seconds <= 0:
        raise SystemExit("sleep intervals must be greater than zero")
    if args.sleep_min_seconds > args.sleep_max_seconds:
        raise SystemExit("sleep minimum cannot exceed sleep maximum")
    for path in (args.profile, args.email_pool, args.launcher):
        if not path.is_file():
            raise SystemExit(f"required worker file not found: {path}")

    jobs = _source_jobs(
        source=args.source,
        ats_platform=ats_platform,
        input_path=args.input,
        tracker_path=args.tracker,
    )
    if args.validate_only:
        print(
            f"{ats_platform.upper()}_SOURCE_VALID "
            f"worker={worker_id} source={args.source} eligible={len(jobs)}",
            flush=True,
        )
        return 0 if jobs else 1

    state = _load_state(args.state, ats_platform)
    reconciled = _reconcile_interrupted_submissions(state)
    if reconciled:
        _save_state(args.state, state)
        print(
            f"{ats_platform.upper()}_SOURCE_INTERRUPTED_QUARANTINED "
            f"worker={worker_id} count={reconciled}",
            flush=True,
        )

    while True:
        try:
            jobs = _source_jobs(
                source=args.source,
                ats_platform=ats_platform,
                input_path=args.input,
                tracker_path=args.tracker,
            )
            selected = _claim_next_job(
                jobs,
                ats_platform=ats_platform,
                worker_id=worker_id,
                state_path=args.state,
                peer_states=args.peer_state,
                claims_path=args.claims,
                submission_log=args.submission_log,
            )
            if selected is None:
                cycle_status = "no_work"
                print(
                    f"{ats_platform.upper()}_SOURCE_IDLE "
                    f"worker={worker_id} source={args.source} candidates={len(jobs)}",
                    flush=True,
                )
            else:
                job = selected
                if args.source == "tracker":
                    try:
                        job = _hydrate_tracker_job(selected, ats_platform)
                    except Exception as exc:
                        _record_source_failure(
                            job=selected,
                            ats_platform=ats_platform,
                            state_path=args.state,
                            result_status="JOB_CONTEXT_UNAVAILABLE",
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                        cycle_status = "failed"
                        print(
                            f"{ats_platform.upper()}_SOURCE_FAILED "
                            f"worker={worker_id} stage=context "
                            f"detail={str(exc)[:300]!r}",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        args.selected_input.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_text(
                            args.selected_input,
                            json.dumps([job], indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                        cycle_status = process_one(
                            ats_platform=ats_platform,
                            input_path=args.selected_input,
                            profile=args.profile,
                            email_pool=args.email_pool,
                            launcher=args.launcher,
                            state_path=args.state,
                            results_dir=args.results_dir,
                            documents_dir=args.documents_dir,
                            submission_log=args.submission_log,
                            document_timeout_seconds=args.document_timeout_seconds,
                            engine_timeout_seconds=args.engine_timeout_seconds,
                            application_timeout_seconds=args.application_timeout_seconds,
                            backlog_path=args.backlog,
                        )
                else:
                    args.selected_input.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(
                        args.selected_input,
                        json.dumps([job], indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    cycle_status = process_one(
                        ats_platform=ats_platform,
                        input_path=args.selected_input,
                        profile=args.profile,
                        email_pool=args.email_pool,
                        launcher=args.launcher,
                        state_path=args.state,
                        results_dir=args.results_dir,
                        documents_dir=args.documents_dir,
                        submission_log=args.submission_log,
                        document_timeout_seconds=args.document_timeout_seconds,
                        engine_timeout_seconds=args.engine_timeout_seconds,
                        application_timeout_seconds=args.application_timeout_seconds,
                        backlog_path=args.backlog,
                    )
                _sync_claim_from_state(
                    job=selected,
                    ats_platform=ats_platform,
                    worker_id=worker_id,
                    state_path=args.state,
                    claims_path=args.claims,
                    fallback_status=cycle_status,
                )
        except KeyboardInterrupt:
            print(
                f"{ats_platform.upper()}_SOURCE_WORKER_STOPPED "
                f"worker={worker_id} signal=keyboard_interrupt",
                flush=True,
            )
            return 130
        except Exception as exc:
            cycle_status = "exception"
            print(
                f"{ats_platform.upper()}_SOURCE_EXCEPTION "
                f"worker={worker_id} type={type(exc).__name__} detail={str(exc)[:500]!r}",
                file=sys.stderr,
                flush=True,
            )
        if args.once:
            return 0 if cycle_status in {"confirmed", "no_work"} else 1
        if cycle_status == "no_work":
            delay = random.randint(args.sleep_min_seconds, args.sleep_max_seconds)
        else:
            delay = min(args.sleep_min_seconds, args.sleep_max_seconds)
        print(
            f"{ats_platform.upper()}_SOURCE_SLEEP "
            f"worker={worker_id} seconds={delay} prior_status={cycle_status}",
            flush=True,
        )
        if not _sleep_until_next_cycle(
            delay,
            ats_platform=ats_platform,
            worker_id=worker_id,
        ):
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
