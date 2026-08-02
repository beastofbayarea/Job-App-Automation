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
from .ats_urls import detect_ats_job_url
from .continuous_worker_application import (
    DEFAULT_BACKLOG,
    DEFAULT_EMAIL_POOL,
    DEFAULT_LAUNCHER,
    DEFAULT_PROFILE,
    DEFAULT_SUBMISSION_LOG,
    SHARED_INPUT,
    SelectedJobApplicationConfig,
    SelectedJobApplicationService,
)
from .continuous_worker_candidates import (
    ExactConfirmedLedgerIndex,
    load_exact_confirmed_ledger_index,
    partition_candidate_state,
)
from .continuous_worker_models import SOURCE_ONCE_EXIT_POLICY, CycleStatus
from .continuous_worker_runtime import WorkerRuntime, run_worker
from .continuous_worker_sources import (
    SourceServices,
    load_source_jobs,
    validate_worker_platform,
)
from .continuous_worker_state import (
    load_worker_state,
    read_worker_state_records,
    reconcile_interrupted_submissions,
    save_worker_state,
)
from .identity import canonical_job_url
from .observability import initialize_observability
from .orchestrator import load_jobs_from_tracker
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from ..resume.ai_client import scrape_job
from ..search.config import DEAD_ROLE_MARKERS


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


_state_records = read_worker_state_records


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
    ledger_index: ExactConfirmedLedgerIndex,
) -> None:
    claim_records: dict[str, Any] = claims["jobs"]
    for submission in ledger_index.records.values():
        claim_records[submission.identity] = {
            "owner": "ledger",
            "status": "confirmed",
            "result_status": "SUBMITTED & CONFIRMED",
            "job_url": submission.job_url,
            "company": submission.company,
            "title": submission.title,
            "updated_at": submission.applied_at or _now(),
        }


def _source_jobs(
    *,
    source: str,
    ats_platform: str,
    input_path: Path,
    tracker_path: Path | None,
) -> list[dict[str, Any]]:
    """Compatibility facade over the shared search/tracker source strategies."""

    def read_tracker(path: Path) -> Sequence[Mapping[str, Any]]:
        return [dict(record) for record in load_jobs_from_tracker(path)]

    return load_source_jobs(
        source=source,
        ats_platform=ats_platform,
        input_path=input_path,
        tracker_path=tracker_path,
        services=SourceServices(
            read_json=read_json,
            read_tracker=read_tracker,
            detect_ats=detect_ats_job_url,
        ),
    )


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

    def candidate_identity(job: Mapping[str, Any]) -> str:
        return _job_identity(str(job.get("job_url") or _candidate_url(job)), ats_platform)

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
        ledger_index = load_exact_confirmed_ledger_index(
            submission_log,
            ats_platform,
            identity_for_url=lambda job_url: _job_identity(job_url, ats_platform),
        )
        _seed_claims_from_ledger(claims, ledger_index=ledger_index)
        pools = partition_candidate_state(
            jobs,
            own_records,
            state_key=_candidate_url,
            identity=candidate_identity,
            confirmed_identities=ledger_index.identities,
            blocked_identities=peer_identities,
        )
        resumable = list(pools.resumable)
        fresh = list(pools.fresh)
        random.shuffle(resumable)
        random.shuffle(fresh)

        claim_records: dict[str, Any] = claims["jobs"]
        selected: dict[str, Any] | None = None
        for job in jobs:
            identity = candidate_identity(job)
            claim = claim_records.get(identity)
            if (
                isinstance(claim, Mapping)
                and claim.get("owner") == worker_id
                and claim.get("status") == "retry_requested"
            ):
                selected = job
                break
        for job in resumable:
            if selected is not None:
                break
            identity = candidate_identity(job)
            claim = claim_records.get(identity)
            if not isinstance(claim, Mapping) or claim.get("owner") == worker_id:
                selected = job
                break
        if selected is None:
            for job in fresh:
                identity = candidate_identity(job)
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

        identity = candidate_identity(selected)
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
    state = load_worker_state(state_path, ats_platform)
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
    save_worker_state(state_path, state)


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


def _process_selected_job(
    *,
    job: Mapping[str, Any],
    selected_input: Path,
    application_service: SelectedJobApplicationService,
) -> CycleStatus:
    """Persist the selected candidate for operations, then apply that exact in-memory job."""
    selected_input.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        selected_input,
        json.dumps([job], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return application_service.process(job)


def _requires_clarification(state_path: Path, cycle_status: CycleStatus) -> bool:
    if cycle_status != "failed":
        return False
    records = list(_state_records(state_path).values())
    if not records:
        return False
    latest = max(records, key=lambda record: str(record.get("updated_at", "")))
    result = latest.get("result") if isinstance(latest.get("result"), Mapping) else {}
    result_status = str(latest.get("result_status") or result.get("status") or "")
    return result_status == "REQUIRED_FIELDS_NOT_FILLED"


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
    source_config = RUNTIME_CONFIG.continuous_worker.source
    parser = argparse.ArgumentParser(
        description="Continuously process disjoint ATS jobs from search output or an Excel tracker."
    )
    parser.add_argument("--ats-platform", required=True)
    parser.add_argument(
        "--source", choices=("search", "tracker", "failed-json"), required=True
    )
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
    parser.add_argument("--sleep-min-seconds", type=int, default=source_config.sleep_min_seconds)
    parser.add_argument("--sleep-max-seconds", type=int, default=source_config.sleep_max_seconds)
    parser.add_argument(
        "--document-timeout-seconds",
        type=int,
        default=source_config.document_timeout_seconds,
    )
    parser.add_argument(
        "--engine-timeout-seconds",
        type=int,
        default=source_config.engine_timeout_seconds,
    )
    parser.add_argument(
        "--application-timeout-seconds",
        type=int,
        default=source_config.application_timeout_seconds,
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--skip-cover-letter",
        action="store_true",
        help="Generate and submit a tailored resume without generating a cover letter.",
    )
    parser.add_argument(
        "--pause-on-unconfirmed",
        action="store_true",
        help="Exit successfully after a missing-required-field failure for clarification.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate source files and print eligible counts without generating or submitting.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ats_platform = validate_worker_platform(args.ats_platform)
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

    telemetry = initialize_observability(
        worker_kind=f"source_{args.source}",
        provider=ats_platform,
        worker_id=worker_id,
    )
    application_service = SelectedJobApplicationService(
        config=SelectedJobApplicationConfig(
            ats_platform=ats_platform,
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
            generate_cover_letter=not args.skip_cover_letter,
            backlog_path=args.backlog,
            ledger_identity_for_url=lambda job_url: _job_identity(job_url, ats_platform),
        ),
        telemetry=telemetry,
    )

    state = load_worker_state(args.state, ats_platform)
    reconciled = reconcile_interrupted_submissions(state)
    if reconciled:
        save_worker_state(args.state, state)
        print(
            f"{ats_platform.upper()}_SOURCE_INTERRUPTED_QUARANTINED "
            f"worker={worker_id} count={reconciled}",
            flush=True,
        )

    def run_cycle() -> CycleStatus:
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
            print(
                f"{ats_platform.upper()}_SOURCE_IDLE "
                f"worker={worker_id} source={args.source} candidates={len(jobs)}",
                flush=True,
            )
            return "no_work"

        job = selected
        cycle_status: CycleStatus
        if args.source in {"tracker", "failed-json"}:
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
                telemetry.emit(
                    "source_context_failed",
                    provider=ats_platform,
                    stage="source_context",
                    cycle_status="failed",
                    error_type=type(exc),
                )
            else:
                cycle_status = _process_selected_job(
                    job=job,
                    selected_input=args.selected_input,
                    application_service=application_service,
                )
        else:
            cycle_status = _process_selected_job(
                job=job,
                selected_input=args.selected_input,
                application_service=application_service,
            )
        _sync_claim_from_state(
            job=selected,
            ats_platform=ats_platform,
            worker_id=worker_id,
            state_path=args.state,
            claims_path=args.claims,
            fallback_status=cycle_status,
        )
        return cycle_status

    def delay_for(cycle_status: CycleStatus) -> int:
        if cycle_status == "no_work":
            return random.randint(args.sleep_min_seconds, args.sleep_max_seconds)
        return int(min(args.sleep_min_seconds, args.sleep_max_seconds))

    def announce_sleep(delay: int, cycle_status: CycleStatus) -> None:
        print(
            f"{ats_platform.upper()}_SOURCE_SLEEP "
            f"worker={worker_id} seconds={delay} prior_status={cycle_status}",
            flush=True,
        )

    def report_interrupt() -> None:
        print(
            f"{ats_platform.upper()}_SOURCE_WORKER_STOPPED "
            f"worker={worker_id} signal=keyboard_interrupt",
            flush=True,
        )

    def report_exception(exc: Exception) -> None:
        print(
            f"{ats_platform.upper()}_SOURCE_EXCEPTION "
            f"worker={worker_id} type={type(exc).__name__} detail={str(exc)[:500]!r}",
            file=sys.stderr,
            flush=True,
        )

    return run_worker(
        WorkerRuntime(
            provider=ats_platform,
            cycle_stage="source_cycle",
            once=args.once,
            once_exit_policy=SOURCE_ONCE_EXIT_POLICY,
            telemetry=telemetry,
            run_cycle=run_cycle,
            delay_for=delay_for,
            announce_sleep=announce_sleep,
            sleep=lambda delay: _sleep_until_next_cycle(
                delay,
                ats_platform=ats_platform,
                worker_id=worker_id,
            ),
            report_interrupt=report_interrupt,
            report_exception=report_exception,
            stop_after_cycle=(
                (lambda status: _requires_clarification(args.state, status))
                if args.pause_on_unconfirmed
                else None
            ),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
