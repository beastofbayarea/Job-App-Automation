"""Run coordinated continuous ATS workers from search output or an Excel tracker."""

from __future__ import annotations

import argparse
import hashlib
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
MAX_CRITICAL_FIXING_ATTEMPTS = 2
SKIPPED_AFTER_FIXING_ATTEMPTS = "skipped_after_fixing_attempts"


class StaleJobRoleError(RuntimeError):
    """Raised when a queued URL no longer represents the queued role."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fixing_attempts_used(retry_count: int) -> int:
    """Translate the failure counter into retries after the original attempt."""
    return max(retry_count - 1, 0)


def _claim_fixing_attempts(claim: Mapping[str, Any]) -> int:
    explicit = claim.get("fixing_attempts")
    if explicit is not None:
        return max(int(explicit), 0)
    return _fixing_attempts_used(int(claim.get("retry_count") or 0))


def _remediation_revision(
    *,
    ats_platform: str,
    profile_path: Path,
    email_pool_path: Path,
    launcher_path: Path,
) -> str:
    """Fingerprint only runtime inputs capable of repairing an application."""
    digest = hashlib.sha256()
    repo = Path(__file__).resolve().parents[3]
    runtime_files = (
        Path(__file__),
        repo / "src/job_application_automation/core/continuous_worker_application.py",
        repo / "src/job_application_automation/core/engine_shared.py",
        repo / "src/job_application_automation/engines/_browser_form.py",
        repo / "src/job_application_automation/engines/browser_controls.py",
        repo / "src/job_application_automation/engines/browser_runtime.py",
        repo / "src/job_application_automation/engines/form_sections.py",
        repo / f"src/job_application_automation/engines/{ats_platform}.py",
        repo / "src/job_application_automation/resume/ai_client.py",
        repo / "src/job_application_automation/resume/generate.py",
        repo / "config/runtime/application.json",
        repo / "config/runtime/browser.json",
        repo / "config/runtime/continuous_worker.json",
        repo / "config/runtime/resume.json",
        resolve_runtime_path(RUNTIME_CONFIG.application.base_resume_file),
        resolve_runtime_path(RUNTIME_CONFIG.application.resume_source_file),
        profile_path,
        email_pool_path,
        launcher_path,
    )
    for path in runtime_files:
        resolved = path.expanduser().resolve()
        digest.update(str(resolved).encode("utf-8"))
        digest.update(b"\0")
        if resolved.is_file():
            digest.update(resolved.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _retry_has_new_remediation(claim: Mapping[str, Any], revision: str) -> bool:
    failed_revision = str(claim.get("failure_revision") or "")
    return not failed_revision or not revision or failed_revision != revision


def _reconcile_exhausted_retry_claims(claims: Mapping[str, Any]) -> int:
    """Skip queued claims that already consumed both corrective retries."""
    reconciled = 0
    jobs = claims.get("jobs")
    if not isinstance(jobs, Mapping):
        return reconciled
    for claim in jobs.values():
        if not isinstance(claim, dict) or claim.get("status") != "retry_requested":
            continue
        if _claim_fixing_attempts(claim) < MAX_CRITICAL_FIXING_ATTEMPTS:
            continue
        claim["status"] = SKIPPED_AFTER_FIXING_ATTEMPTS
        claim["critical_error"] = True
        claim["skip_reason"] = (
            f"failed after {MAX_CRITICAL_FIXING_ATTEMPTS} fixing attempts"
        )
        claim["remediation_required"] = False
        claim.pop("next_retry_at", None)
        reconciled += 1
    return reconciled


def _persist_exhausted_claims_to_state(
    claims: Mapping[str, Any],
    *,
    state_path: Path,
    ats_platform: str,
) -> int:
    """Mirror exhausted claim policy into worker state for reconstruction safety."""
    claim_jobs = claims.get("jobs")
    if not isinstance(claim_jobs, Mapping):
        return 0
    state = load_worker_state(state_path, ats_platform)
    records = state.get("jobs")
    if not isinstance(records, dict):
        return 0
    updated = 0
    for canonical_url, record in records.items():
        if not isinstance(record, dict):
            continue
        job_url = str(record.get("job_url") or canonical_url)
        try:
            identity = _job_identity(job_url, ats_platform)
        except ValueError:
            continue
        claim = claim_jobs.get(identity)
        if (
            not isinstance(claim, Mapping)
            or claim.get("status") != SKIPPED_AFTER_FIXING_ATTEMPTS
        ):
            continue
        desired = {
            "retry_policy_status": SKIPPED_AFTER_FIXING_ATTEMPTS,
            "retry_count": int(claim.get("retry_count") or 0),
            "fixing_attempts": _claim_fixing_attempts(claim),
            "critical_error": True,
            "failure_revision": str(claim.get("failure_revision") or ""),
            "remediation_required": False,
            "skip_reason": str(
                claim.get("skip_reason")
                or f"failed after {MAX_CRITICAL_FIXING_ATTEMPTS} fixing attempts"
            ),
        }
        if any(record.get(key) != value for key, value in desired.items()):
            record.update(desired)
            updated += 1
    if updated:
        save_worker_state(state_path, state)
    return updated


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


def _normalized_role_title(value: str) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        value.casefold().replace("&", " and "),
    ).strip()


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
        retry_count = int(record.get("retry_count") or 0)
        fixing_attempts = (
            max(int(record["fixing_attempts"]), 0)
            if record.get("fixing_attempts") is not None
            else _fixing_attempts_used(retry_count)
        )
        claim_records[identity] = {
            "owner": owner,
            "status": str(
                record.get("retry_policy_status")
                or record.get("status")
                or "recorded"
            ),
            "result_status": str(record.get("result_status") or ""),
            "job_url": job_url,
            "company": str(record.get("company") or ""),
            "title": str(record.get("title") or ""),
            "updated_at": str(record.get("updated_at") or _now()),
            "retry_count": retry_count,
            "fixing_attempts": fixing_attempts,
            "critical_error": bool(record.get("critical_error")),
            "skip_reason": str(record.get("skip_reason") or ""),
            "failure_revision": str(record.get("failure_revision") or ""),
            "remediation_required": bool(record.get("remediation_required")),
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
    remediation_revision: str = "",
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
        _reconcile_exhausted_retry_claims(claims)
        _persist_exhausted_claims_to_state(
            claims,
            state_path=state_path,
            ats_platform=ats_platform,
        )
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
                and _retry_has_new_remediation(claim, remediation_revision)
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
        prior_claim = claim_records.get(identity)
        retry_count = (
            int(prior_claim.get("retry_count") or 0)
            if isinstance(prior_claim, Mapping)
            else 0
        )
        fixing_attempts = (
            _claim_fixing_attempts(prior_claim) + 1
            if isinstance(prior_claim, Mapping)
            and prior_claim.get("status") == "retry_requested"
            else (
                _claim_fixing_attempts(prior_claim)
                if isinstance(prior_claim, Mapping)
                and prior_claim.get("status") == "claimed"
                else 0
            )
        )
        attempt_revision = (
            str(prior_claim.get("attempt_revision") or remediation_revision)
            if isinstance(prior_claim, Mapping)
            and prior_claim.get("status") == "claimed"
            else remediation_revision
        )
        claim_records[identity] = {
            "owner": worker_id,
            "status": "claimed",
            "job_url": str(selected["job_url"]),
            "company": str(selected.get("company") or ""),
            "title": str(selected.get("title") or ""),
            "updated_at": _now(),
            "retry_count": retry_count,
            "fixing_attempts": fixing_attempts,
            "attempt_revision": attempt_revision,
            "attempt_kind": "fixing" if fixing_attempts else "original",
        }
        selected_url = _candidate_url(selected)
        selected_record = own_records.get(selected_url)
        if (
            isinstance(prior_claim, Mapping)
            and prior_claim.get("status") == "retry_requested"
            and isinstance(selected_record, Mapping)
            and selected_record.get("status") not in {"preparing", "documents_ready"}
        ):
            state = load_worker_state(state_path, ats_platform)
            state.get("jobs", {}).pop(selected_url, None)
            save_worker_state(state_path, state)
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
        raise StaleJobRoleError("job page reports that the role is no longer available")
    queued_title = str(job.get("title") or job.get("role") or "").strip()
    live_title = str(scraped.get("job_title") or "").strip()
    if (
        queued_title
        and live_title
        and _normalized_role_title(queued_title) != _normalized_role_title(live_title)
    ):
        raise StaleJobRoleError(
            f"queued role {queued_title!r} no longer matches live role {live_title!r}"
        )
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
    state_status: str = "failed",
) -> None:
    state = load_worker_state(state_path, ats_platform)
    canonical_url = canonical_job_url(job["job_url"])
    state["jobs"][canonical_url] = {
        "status": state_status,
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
    remediation_revision: str = "",
) -> None:
    canonical_url = canonical_job_url(job["job_url"])
    record = _state_records(state_path).get(canonical_url, {})
    with interprocess_file_lock(claims_path):
        claims = _load_claims(claims_path)
        identity = _job_identity(str(job["job_url"]), ats_platform)
        prior_claim = claims["jobs"].get(identity)
        retry_count = int(
            prior_claim.get("retry_count")
            if isinstance(prior_claim, Mapping)
            and prior_claim.get("retry_count") is not None
            else record.get("retry_count") or 0
        )
        fixing_attempts = (
            _claim_fixing_attempts(prior_claim)
            if isinstance(prior_claim, Mapping)
            else (
                max(int(record["fixing_attempts"]), 0)
                if record.get("fixing_attempts") is not None
                else _fixing_attempts_used(retry_count)
            )
        )
        record_status = str(record.get("status") or fallback_status)
        recorded_policy_status = str(record.get("retry_policy_status") or "")
        claim_status = (
            recorded_policy_status
            if recorded_policy_status == SKIPPED_AFTER_FIXING_ATTEMPTS
            else record_status
        )
        result_status = str(record.get("result_status") or "")
        critical_failure = record_status in {"failed", "manual_review"} or (
            claim_status == SKIPPED_AFTER_FIXING_ATTEMPTS
        )
        prior_status = (
            str(prior_claim.get("status") or "")
            if isinstance(prior_claim, Mapping)
            else ""
        )
        if critical_failure:
            if prior_status not in {
                "retry_requested",
                SKIPPED_AFTER_FIXING_ATTEMPTS,
            } and recorded_policy_status not in {
                "retry_requested",
                SKIPPED_AFTER_FIXING_ATTEMPTS,
            }:
                retry_count += 1
            claim_status = (
                SKIPPED_AFTER_FIXING_ATTEMPTS
                if recorded_policy_status == SKIPPED_AFTER_FIXING_ATTEMPTS
                or fixing_attempts >= MAX_CRITICAL_FIXING_ATTEMPTS
                else "retry_requested"
            )
        claim = {
            "owner": worker_id,
            "status": claim_status,
            "result_status": result_status,
            "job_url": str(job["job_url"]),
            "company": str(job.get("company") or ""),
            "title": str(job.get("title") or ""),
            "updated_at": str(record.get("updated_at") or _now()),
            "retry_count": retry_count,
            "fixing_attempts": fixing_attempts,
        }
        state_updates: dict[str, Any] = {}
        if critical_failure and claim_status == "retry_requested":
            prior_failure_revision = (
                str(prior_claim.get("failure_revision") or "")
                if isinstance(prior_claim, Mapping)
                and prior_status == "retry_requested"
                else ""
            )
            failure_revision = (
                prior_failure_revision
                or str(record.get("failure_revision") or "")
                or (
                    str(prior_claim.get("attempt_revision") or "")
                    if isinstance(prior_claim, Mapping)
                    else ""
                )
                or remediation_revision
            )
            claim["critical_error"] = True
            claim["failure_revision"] = failure_revision
            claim["remediation_required"] = True
            state_updates = {
                "retry_policy_status": "retry_requested",
                "retry_count": retry_count,
                "fixing_attempts": fixing_attempts,
                "critical_error": True,
                "failure_revision": failure_revision,
                "remediation_required": True,
            }
        elif critical_failure:
            claim["critical_error"] = True
            claim["failure_revision"] = (
                str(record.get("failure_revision") or "")
                or (
                    str(prior_claim.get("attempt_revision") or "")
                    if isinstance(prior_claim, Mapping)
                    else ""
                )
                or remediation_revision
            )
            claim["skip_reason"] = (
                f"failed after {MAX_CRITICAL_FIXING_ATTEMPTS} fixing attempts"
            )
            state_updates = {
                "retry_policy_status": SKIPPED_AFTER_FIXING_ATTEMPTS,
                "retry_count": retry_count,
                "fixing_attempts": fixing_attempts,
                "critical_error": True,
                "failure_revision": claim["failure_revision"],
                "remediation_required": False,
                "skip_reason": claim["skip_reason"],
            }
        if state_updates:
            state = load_worker_state(state_path, ats_platform)
            state_record = state.get("jobs", {}).get(canonical_url)
            if isinstance(state_record, dict):
                state_record.update(state_updates)
                save_worker_state(state_path, state)
        claims["jobs"][identity] = claim
        claims["updated_at"] = _now()
        atomic_write_text(
            claims_path,
            json.dumps(claims, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _sync_terminal_claims(
    *,
    state: Mapping[str, Any],
    ats_platform: str,
    worker_id: str,
    state_path: Path,
    claims_path: Path,
    remediation_revision: str = "",
) -> int:
    synced = 0
    jobs = state.get("jobs", {})
    if not isinstance(jobs, Mapping):
        return 0
    for terminal in jobs.values():
        if (
            not isinstance(terminal, Mapping)
            or terminal.get("status") not in {"failed", "manual_review"}
        ):
            continue
        _sync_claim_from_state(
            job=terminal,
            ats_platform=ats_platform,
            worker_id=worker_id,
            state_path=state_path,
            claims_path=claims_path,
            fallback_status="failed",
            remediation_revision=remediation_revision,
        )
        synced += 1
    return synced


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
    del state_path, cycle_status
    return False


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
    remediation_revision = _remediation_revision(
        ats_platform=ats_platform,
        profile_path=args.profile,
        email_pool_path=args.email_pool,
        launcher_path=args.launcher,
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
    _sync_terminal_claims(
        state=state,
        ats_platform=ats_platform,
        worker_id=worker_id,
        state_path=args.state,
        claims_path=args.claims,
        remediation_revision=remediation_revision,
    )

    def run_cycle() -> CycleStatus:
        # Reconcile terminal state on every cycle so a transient claims-write
        # failure cannot strand a job as claimed until the service restarts.
        _sync_terminal_claims(
            state=load_worker_state(args.state, ats_platform),
            ats_platform=ats_platform,
            worker_id=worker_id,
            state_path=args.state,
            claims_path=args.claims,
            remediation_revision=remediation_revision,
        )
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
            remediation_revision=remediation_revision,
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
        try:
            if args.source in {"tracker", "failed-json"}:
                job = _hydrate_tracker_job(selected, ats_platform)
            cycle_status = _process_selected_job(
                job=job,
                selected_input=args.selected_input,
                application_service=application_service,
            )
        except StaleJobRoleError as exc:
            _record_source_failure(
                job=selected,
                ats_platform=ats_platform,
                state_path=args.state,
                result_status="STALE_JOB_ROLE_MISMATCH",
                detail=str(exc),
                state_status="skipped",
            )
            cycle_status = "failed"
            print(
                f"{ats_platform.upper()}_SOURCE_SKIPPED "
                f"worker={worker_id} reason=stale_role "
                f"detail={str(exc)[:300]!r}",
                flush=True,
            )
            telemetry.emit(
                "source_stale_role_skipped",
                level="warning",
                provider=ats_platform,
                stage="source_context",
                cycle_status="failed",
            )
        except Exception as exc:
            result_status = (
                "JOB_CONTEXT_UNAVAILABLE"
                if job is selected and args.source in {"tracker", "failed-json"}
                else "WORKER_CYCLE_EXCEPTION"
            )
            _record_source_failure(
                job=selected,
                ats_platform=ats_platform,
                state_path=args.state,
                result_status=result_status,
                detail=f"{type(exc).__name__}: {exc}",
            )
            cycle_status = "failed"
            print(
                f"{ats_platform.upper()}_SOURCE_FAILED "
                f"worker={worker_id} stage=cycle status={result_status} "
                f"detail={str(exc)[:300]!r}",
                file=sys.stderr,
                flush=True,
            )
            telemetry.emit(
                "source_cycle_failed",
                provider=ats_platform,
                stage="source_cycle",
                cycle_status="failed",
                error_type=type(exc),
            )
        _sync_claim_from_state(
            job=selected,
            ats_platform=ats_platform,
            worker_id=worker_id,
            state_path=args.state,
            claims_path=args.claims,
            fallback_status=cycle_status,
            remediation_revision=remediation_revision,
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
