"""Resumable local, fill-only processing for one JSON ATS queue.

The coordinator owns one exact background Chrome target per job.  Every helper
inherits the target ID, so document generation and form filling share the same
tab without activating it.  Completed tabs remain open for manual review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import urllib.request
from contextlib import contextmanager
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit

from ..engines.browser_runtime import (
    close_background_tab,
    create_background_tab,
    reload_background_tab,
)
from ..mail.pool import load_email_pool
from .application_pipeline import ProcessTimeoutError
from .foundation import (
    CLI_ENTRYPOINT,
    CONFIG_DIR,
    OUTPUT_DIR,
    canonical_job_url,
    detect_ats_job_url,
    read_json,
    write_json,
)
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path

logger = logging.getLogger("LocalPrefillQueue")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

SUPPORTED_ATS = frozenset({"ashby", "greenhouse", "lever", "smartrecruiters", "workable"})
TERMINAL_STATE_VERSION = 2
CONFIRMED_STATUS = "SUBMITTED & CONFIRMED"
PREFILLED_STATUS = "PREFILLED_ONLY"
RECOVERY_LABELS = ("initial", "reload", "replacement")
TARGET_MARKER_PREFIX = "about:blank#job-automation-"
DEFAULT_RESUME_FILE = resolve_runtime_path(RUNTIME_CONFIG.application.base_resume_file)
_SMARTRECRUITERS_POSTING_PATH = re.compile(
    r"^/[^/]+/(?P<publication_id>\d+)(?:-[^/]*)?/?$",
    re.IGNORECASE,
)
_SMARTRECRUITERS_ONECLICK_PATH = re.compile(
    r"^/oneclick-ui/company/[^/]+/publication/(?P<publication_id>\d+)/?$",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _single_worker_lock(path: Path) -> Iterator[None]:
    """Hold an OS-backed, non-blocking lock for one ATS worker's full run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: BinaryIO = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError(
                f"another local prefill worker already owns this ATS queue: {path}"
            ) from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(  # type: ignore[attr-defined]
                        handle.fileno(),
                        fcntl.LOCK_UN,  # type: ignore[attr-defined]
                    )
            except OSError:
                logger.warning("Could not release local prefill worker lock: %s", path)
        handle.close()


def _queue_digest(jobs: Sequence[Mapping[str, str]]) -> str:
    payload = json.dumps(
        [
            {
                "company": job["company"],
                "title": job["title"],
                "url": canonical_job_url(job["url"]),
            }
            for job in jobs
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _job_key(url: str) -> str:
    return hashlib.sha256(canonical_job_url(url).encode("utf-8")).hexdigest()[:16]


def _load_queue(path: Path, expected_ats: str | None) -> tuple[str, list[dict[str, str]]]:
    payload = read_json(path)
    if not isinstance(payload, list) or not payload:
        raise ValueError("queue JSON must be a non-empty array")

    jobs: list[dict[str, str]] = []
    seen: set[str] = set()
    detected_platform = ""
    for index, raw_job in enumerate(payload, start=1):
        if not isinstance(raw_job, Mapping):
            raise ValueError(f"queue record {index} must be an object")
        company = str(raw_job.get("company", "")).strip()
        title = str(raw_job.get("title", raw_job.get("role", ""))).strip()
        url = str(raw_job.get("url", "")).strip()
        ats = detect_ats_job_url(url)
        if not company or not title or ats not in SUPPORTED_ATS:
            raise ValueError(f"queue record {index} has invalid company, title, or ATS URL")
        if expected_ats and ats != expected_ats:
            raise ValueError(f"queue record {index} is {ats}, but --ats requires {expected_ats}")
        if detected_platform and ats != detected_platform:
            raise ValueError("one local prefill worker may process only one ATS platform")
        detected_platform = ats
        canonical = canonical_job_url(url)
        if canonical in seen:
            raise ValueError(f"queue contains a duplicate canonical URL at record {index}")
        seen.add(canonical)
        jobs.append({"company": company, "title": title, "url": url, "ats": ats})
    return detected_platform, jobs


def _ledger_urls(path: Path, *, quarantine: bool) -> set[str]:
    if not path.is_file():
        return set()
    payload = read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError(f"ledger must be a JSON object: {path}")
    urls: set[str] = set()
    for record_id, raw_record in payload.items():
        if not isinstance(record_id, str) or not isinstance(raw_record, Mapping):
            raise ValueError(f"ledger contains an invalid record: {path}")
        status = str(raw_record.get("status", "")).strip()
        raw_url = str(raw_record.get("job_url", raw_record.get("url", ""))).strip()
        if raw_url and (quarantine or status == CONFIRMED_STATUS):
            urls.add(canonical_job_url(raw_url))
    return urls


def _submitted_urls(submission_log: Path) -> set[str]:
    suffix = submission_log.suffix or ".json"
    quarantine = submission_log.with_name(f"{submission_log.stem}_quarantine{suffix}")
    return _ledger_urls(submission_log, quarantine=False) | _ledger_urls(
        quarantine, quarantine=True
    )


def _cdp_payload(endpoint: str, route: str) -> object:
    with urllib.request.urlopen(  # noqa: S310 - endpoint is validated as loopback below.
        f"{endpoint.rstrip('/')}/{route.lstrip('/')}", timeout=4
    ) as response:
        return json.load(response)


def _require_shared_cdp(endpoint: str) -> None:
    if endpoint.rstrip("/") not in {"http://127.0.0.1:9222", "http://localhost:9222"}:
        raise ValueError("local prefill requires the configured loopback Chrome CDP endpoint")
    payload = _cdp_payload(endpoint, "json/version")
    if not isinstance(payload, Mapping) or not payload.get("webSocketDebuggerUrl"):
        raise RuntimeError(f"Chrome CDP is not ready on {endpoint}")


def _live_targets(endpoint: str) -> dict[str, dict[str, Any]]:
    payload = _cdp_payload(endpoint, "json/list")
    if not isinstance(payload, list):
        raise RuntimeError("Chrome CDP target list is invalid")
    return {
        str(item["id"]): dict(item)
        for item in payload
        if isinstance(item, Mapping) and item.get("id") and item.get("type") == "page"
    }


def _new_target(endpoint: str) -> tuple[str, str]:
    marker, target_id = create_background_tab(endpoint)
    target = _live_targets(endpoint).get(target_id)
    if target is None or str(target.get("url", "")) != marker:
        close_background_tab(endpoint, target_id)
        raise RuntimeError("Chrome did not retain the newly created background target")
    return marker, target_id


def _smartrecruiters_publication_id(url: str) -> str:
    """Extract one exact SmartRecruiters publication ID from a supported URL shape."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not (
        host == "smartrecruiters.com" or host.endswith(".smartrecruiters.com")
    ):
        return ""
    path = unquote(parsed.path or "/")
    for pattern in (_SMARTRECRUITERS_POSTING_PATH, _SMARTRECRUITERS_ONECLICK_PATH):
        match = pattern.fullmatch(path)
        if match is not None:
            return match.group("publication_id")
    return ""


def _job_urls_are_equivalent(current_url: str, job_url: str) -> bool:
    """Match canonical URLs or the two exact SmartRecruiters publication shapes."""
    if canonical_job_url(current_url) == canonical_job_url(job_url):
        return True
    current_publication = _smartrecruiters_publication_id(current_url)
    expected_publication = _smartrecruiters_publication_id(job_url)
    return bool(current_publication and current_publication == expected_publication)


def _saved_target_is_owned(
    endpoint: str,
    *,
    target_id: str,
    marker: str,
    job_url: str,
) -> bool:
    """Accept only the saved marker target or that target on this exact job URL."""
    if not target_id or not marker.startswith(TARGET_MARKER_PREFIX):
        return False
    target = _live_targets(endpoint).get(target_id)
    if target is None:
        return False
    current_url = str(target.get("url", ""))
    if current_url == marker:
        return True
    try:
        return _job_urls_are_equivalent(current_url, job_url)
    except ValueError:
        return False


def _load_state(
    path: Path,
    *,
    queue_path: Path,
    ats: str,
    digest: str,
    queue_keys: set[str],
) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": TERMINAL_STATE_VERSION,
            "queue": str(queue_path),
            "queue_digest": digest,
            "ats": ats,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "records": {},
        }
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), dict):
        raise ValueError(f"local prefill state is invalid: {path}")
    if payload.get("schema_version") != TERMINAL_STATE_VERSION:
        raise ValueError("existing local prefill state has an unsupported schema version")
    if payload.get("queue") != str(queue_path):
        raise ValueError("existing local prefill state belongs to a different queue path")
    if payload.get("queue_digest") != digest or payload.get("ats") != ats:
        raise ValueError("existing local prefill state belongs to a different queue revision")
    record_keys = set(payload["records"])
    if not all(isinstance(key, str) for key in record_keys) or not record_keys.issubset(queue_keys):
        raise ValueError("existing local prefill state contains records outside this queue")
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    write_json(path, state, indent=2, ensure_ascii=False, sort_keys=True)


def _read_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = read_json(path)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return None
    return dict(payload[0])


def _is_hang(status: str, detail: str) -> bool:
    normalized = f"{status} {detail}".casefold()
    return status == "TIMED_OUT" or any(
        marker in normalized
        for marker in (
            "timeout",
            "timed out",
            "page did not provide usable context",
            "target closed",
            "navigation failed",
        )
    )


def _redact_email(value: object, email: str) -> str:
    text = str(value or "")
    if not email:
        return text
    return re.sub(re.escape(email), "[REDACTED_EMAIL]", text, flags=re.IGNORECASE)


def _write_helper_log(
    path: Path,
    *,
    email: str,
    stdout: object = "",
    stderr: object = "",
) -> None:
    path.write_text(
        f"STDOUT\n{_redact_email(stdout, email)}\n\nSTDERR\n{_redact_email(stderr, email)}",
        encoding="utf-8",
    )


def _run_command(command: Sequence[str], timeout_seconds: int, *, env: Mapping[str, str]) -> Any:
    """Load the heavyweight orchestrator process boundary only for a real attempt."""
    from .orchestrator import run_command

    return run_command(command, timeout_seconds, env=env)


def _attempt_command(
    *,
    job: Mapping[str, str],
    email: str,
    result_path: Path,
    log_path: Path,
    submission_log: Path,
    config_path: Path,
    email_pool: Path,
    resume_path: Path,
    target_id: str,
    target_marker: str,
    render_timeout_ms: int,
    engine_timeout_seconds: int,
    resume_timeout_seconds: int,
    job_timeout_seconds: int,
) -> tuple[dict[str, Any], bool]:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.unlink(missing_ok=True)
    command = [
        sys.executable,
        str(CLI_ENTRYPOINT),
        "apply",
        "--url",
        job["url"],
        "--company",
        job["company"],
        "--role",
        job["title"],
        "--email",
        email,
        "--email-pool",
        str(email_pool),
        "--resume",
        str(resume_path),
        "--config",
        str(config_path),
        "--results-file",
        str(result_path),
        "--submission-log-file",
        str(submission_log),
        "--timeout",
        str(engine_timeout_seconds),
        "--resume-timeout",
        str(resume_timeout_seconds),
        "--fill-only",
        "--headed",
        "--no-shuffle",
    ]
    environment = {
        "JOB_APP_BACKGROUND_TABS": "1",
        "JOB_APP_CDP_ATTACH_TIMEOUT_MS": "90000",
        "JOB_APP_COORDINATED_RETRY": "1",
        "JOB_APP_FORBID_SUBMIT": "1",
        "JOB_APP_KEEP_TABS_OPEN": "1",
        "JOB_APP_RELOAD_TAB": "0",
        "JOB_APP_RENDER_TIMEOUT_MS": str(render_timeout_ms),
        "JOB_APP_REQUIRE_SHARED_CDP": "1",
        "JOB_APP_TARGET_ID": target_id,
        "JOB_APP_TARGET_MARKER": target_marker,
        "JOB_APP_TARGET_URL": job["url"],
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = _run_command(command, job_timeout_seconds, env=environment)
        _write_helper_log(
            log_path,
            email=email,
            stdout=process.stdout,
            stderr=process.stderr,
        )
        result = _read_result(result_path)
        if result is None:
            return (
                {
                    "success": False,
                    "status": "HELPER_RESULT_MISSING",
                    "detail": f"helper exited {process.returncode} without one result",
                    "submitted": False,
                    "confirmed": False,
                },
                False,
            )
        return result, _is_hang(str(result.get("status", "")), str(result.get("detail", "")))
    except ProcessTimeoutError as exc:
        _write_helper_log(
            log_path,
            email=email,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )
        return (
            {
                "success": False,
                "status": "TIMED_OUT",
                "detail": f"local helper exceeded {exc.timeout} seconds",
                "submitted": False,
                "confirmed": False,
            },
            True,
        )
    except OSError as exc:
        _write_helper_log(log_path, email=email, stderr=f"HELPER ERROR\n{exc}")
        return (
            {
                "success": False,
                "status": "HELPER_EXECUTION_ERROR",
                "detail": str(exc),
                "submitted": False,
                "confirmed": False,
            },
            False,
        )


def _public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "success": bool(result.get("success")),
        "status": str(result.get("status", "")),
        "submitted": bool(result.get("submitted")),
        "confirmed": bool(result.get("confirmed")),
        "test_mode": bool(result.get("test_mode", True)),
        "detail": str(result.get("detail", ""))[:2_000],
        "resume": str(result.get("resume", "")),
        "cover_letter": str(result.get("cover_letter", "")),
    }


def _cleanup_saved_stale_target(
    *,
    endpoint: str,
    job_url: str,
    canonical: str,
    records: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
) -> None:
    record = records.get(canonical)
    if not isinstance(record, Mapping):
        return
    stale_target_id = str(record.get("stale_target_id", ""))
    stale_marker = str(record.get("stale_target_marker", ""))
    if not stale_target_id:
        return
    owned = _saved_target_is_owned(
        endpoint,
        target_id=stale_target_id,
        marker=stale_marker,
        job_url=job_url,
    )
    if owned:
        close_background_tab(endpoint, stale_target_id)
    still_live = stale_target_id in _live_targets(endpoint)
    if owned and still_live:
        logger.warning("Could not close stale target owned by job %s", _job_key(job_url))
        return
    updated = dict(record)
    updated.pop("stale_target_id", None)
    updated.pop("stale_target_marker", None)
    updated["updated_at"] = _utc_now()
    records[canonical] = updated
    _save_state(state_path, state)


def _persist_replacement_target(
    *,
    endpoint: str,
    job_url: str,
    canonical: str,
    records: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    stale_target_id: str,
    stale_marker: str,
    result: Mapping[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    replacement_marker, replacement_target_id = _new_target(endpoint)
    current = records.get(canonical)
    updated = {
        **(dict(current) if isinstance(current, Mapping) else {}),
        **(dict(result) if result is not None else {}),
        "target_id": replacement_target_id,
        "target_marker": replacement_marker,
        "stale_target_id": stale_target_id,
        "stale_target_marker": stale_marker,
        "recovery_stage": 2,
        "recovery_label": RECOVERY_LABELS[2],
        "terminal": False,
        "updated_at": _utc_now(),
    }
    records[canonical] = updated
    try:
        # The replacement must be durable before the old target is touched.
        _save_state(state_path, state)
    except Exception:
        close_background_tab(endpoint, replacement_target_id)
        raise
    _cleanup_saved_stale_target(
        endpoint=endpoint,
        job_url=job_url,
        canonical=canonical,
        records=records,
        state=state,
        state_path=state_path,
    )
    latest = records.get(canonical)
    return (
        replacement_marker,
        replacement_target_id,
        dict(latest) if isinstance(latest, Mapping) else updated,
    )


def _run_queue(
    args: argparse.Namespace,
    *,
    queue_path: Path,
    ats: str,
    jobs: list[dict[str, str]],
    digest: str,
    output_root: Path,
    state_path: Path,
    results_dir: Path,
    submission_log: Path,
    config_path: Path,
    email_pool_path: Path,
    resume_path: Path,
) -> int:
    with _single_worker_lock(output_root / "worker.lock"):
        endpoint = RUNTIME_CONFIG.browser.cdp_endpoint
        _require_shared_cdp(endpoint)
        emails = load_email_pool(email_pool_path)
        if not emails:
            raise ValueError("candidate email pool is empty")

        queue_keys = {canonical_job_url(job["url"]) for job in jobs}
        state = _load_state(
            state_path,
            queue_path=queue_path,
            ats=ats,
            digest=digest,
            queue_keys=queue_keys,
        )
        records = state["records"]
        if not isinstance(records, dict):
            raise ValueError("local prefill state records must be an object")

        processed_now = 0
        failures = 0
        for index, job in enumerate(jobs, start=1):
            canonical = canonical_job_url(job["url"])
            record = records.get(canonical)
            if isinstance(record, Mapping) and record.get("terminal") is True:
                failures += int(record.get("status") not in {PREFILLED_STATUS, "SKIPPED_SUBMITTED"})
                continue
            if args.limit is not None and processed_now >= args.limit:
                break

            # Ledgers can change during a long five-platform run. Refresh at the
            # final preflight boundary before allocating an email or Chrome tab.
            if canonical in _submitted_urls(submission_log):
                records[canonical] = {
                    "index": index,
                    "company": job["company"],
                    "title": job["title"],
                    "url": job["url"],
                    "status": "SKIPPED_SUBMITTED",
                    "terminal": True,
                    "updated_at": _utc_now(),
                }
                _save_state(state_path, state)
                processed_now += 1
                logger.info("[%d/%d] %s: skipped confirmed submission", index, len(jobs), ats)
                continue

            mutable_record = dict(record) if isinstance(record, Mapping) else {}
            email = str(mutable_record.get("email", "")) or secrets.choice(emails)
            _cleanup_saved_stale_target(
                endpoint=endpoint,
                job_url=job["url"],
                canonical=canonical,
                records=records,
                state=state,
                state_path=state_path,
            )
            mutable_record = (
                dict(records.get(canonical, {}))
                if isinstance(records.get(canonical), Mapping)
                else {}
            )
            target_id = str(mutable_record.get("target_id", ""))
            marker = str(mutable_record.get("target_marker", ""))
            recovery_stage = int(mutable_record.get("recovery_stage", 0))
            if not _saved_target_is_owned(
                endpoint,
                target_id=target_id,
                marker=marker,
                job_url=job["url"],
            ):
                marker, target_id = _new_target(endpoint)

            result_path = results_dir / f"{index:04d}-{_job_key(job['url'])}.json"
            while recovery_stage < len(RECOVERY_LABELS):
                if recovery_stage == 1 and not mutable_record.get("reload_attempted"):
                    # Record the transition before the external CDP operation.
                    # On an interrupted resume, an attempted reload is never
                    # repeated against a shared Chrome target.
                    mutable_record = {
                        **mutable_record,
                        "recovery_stage": recovery_stage,
                        "recovery_label": RECOVERY_LABELS[recovery_stage],
                        "reload_attempted": True,
                        "terminal": False,
                        "updated_at": _utc_now(),
                    }
                    records[canonical] = mutable_record
                    _save_state(state_path, state)
                    try:
                        if not _saved_target_is_owned(
                            endpoint,
                            target_id=target_id,
                            marker=marker,
                            job_url=job["url"],
                        ):
                            raise RuntimeError("saved reload target is not owned by this job")
                        reload_background_tab(endpoint, target_id)
                    except Exception as exc:
                        mutable_record = {
                            **mutable_record,
                            "reload_error": type(exc).__name__,
                            "updated_at": _utc_now(),
                        }
                        records[canonical] = mutable_record
                        _save_state(state_path, state)
                        marker, target_id, mutable_record = _persist_replacement_target(
                            endpoint=endpoint,
                            job_url=job["url"],
                            canonical=canonical,
                            records=records,
                            state=state,
                            state_path=state_path,
                            stale_target_id=target_id,
                            stale_marker=marker,
                        )
                        recovery_stage = 2
                        continue
                    mutable_record = {
                        **mutable_record,
                        "reload_completed": True,
                        "updated_at": _utc_now(),
                    }
                    records[canonical] = mutable_record
                    _save_state(state_path, state)
                checkpoint = {
                    **mutable_record,
                    "index": index,
                    "company": job["company"],
                    "title": job["title"],
                    "url": job["url"],
                    "email": email,
                    "target_id": target_id,
                    "target_marker": marker,
                    "recovery_stage": recovery_stage,
                    "recovery_label": RECOVERY_LABELS[recovery_stage],
                    "terminal": False,
                    "updated_at": _utc_now(),
                }
                records[canonical] = checkpoint
                _save_state(state_path, state)
                mutable_record = checkpoint
                logger.info(
                    "[%d/%d] %s stage=%s company=%s title=%s",
                    index,
                    len(jobs),
                    ats,
                    RECOVERY_LABELS[recovery_stage],
                    job["company"],
                    job["title"],
                )
                log_path = (
                    output_root
                    / "logs"
                    / (f"{index:04d}-{_job_key(job['url'])}-{RECOVERY_LABELS[recovery_stage]}.log")
                )
                result, hung = _attempt_command(
                    job=job,
                    email=email,
                    result_path=result_path,
                    log_path=log_path,
                    submission_log=submission_log,
                    config_path=config_path,
                    email_pool=email_pool_path,
                    resume_path=resume_path,
                    target_id=target_id,
                    target_marker=marker,
                    render_timeout_ms=args.render_timeout_ms,
                    engine_timeout_seconds=args.engine_timeout,
                    resume_timeout_seconds=args.resume_timeout,
                    job_timeout_seconds=args.job_timeout,
                )
                public = _public_result(result)
                if (
                    public["submitted"]
                    or public["confirmed"]
                    or public["status"] == CONFIRMED_STATUS
                ):
                    records[canonical] = {
                        **records[canonical],
                        **public,
                        "status": "FORBIDDEN_SUBMISSION_SIGNAL",
                        "terminal": True,
                        "updated_at": _utc_now(),
                    }
                    _save_state(state_path, state)
                    raise RuntimeError("fill-only helper reported a forbidden submission signal")
                if public["status"] == PREFILLED_STATUS and not (
                    public["success"] and public["test_mode"]
                ):
                    public["success"] = False
                    public["status"] = "INVALID_PREFILL_RESULT"
                    public["detail"] = "helper returned an inconsistent fill-only result contract"
                if not hung:
                    records[canonical] = {
                        **records[canonical],
                        **public,
                        "terminal": True,
                        "result_file": str(result_path),
                        "updated_at": _utc_now(),
                    }
                    failures += int(public["status"] != PREFILLED_STATUS)
                    _save_state(state_path, state)
                    break
                if recovery_stage == 0:
                    recovery_stage = 1
                    mutable_record = {
                        **records[canonical],
                        **public,
                        "recovery_stage": recovery_stage,
                        "recovery_label": RECOVERY_LABELS[recovery_stage],
                        "terminal": False,
                        "updated_at": _utc_now(),
                    }
                    records[canonical] = mutable_record
                    _save_state(state_path, state)
                    continue
                if recovery_stage == 1:
                    stale_target_id = target_id
                    stale_marker = marker
                    marker, target_id, mutable_record = _persist_replacement_target(
                        endpoint=endpoint,
                        job_url=job["url"],
                        canonical=canonical,
                        records=records,
                        state=state,
                        state_path=state_path,
                        stale_target_id=stale_target_id,
                        stale_marker=stale_marker,
                        result=public,
                    )
                    recovery_stage = 2
                    continue
                records[canonical] = {
                    **records[canonical],
                    **public,
                    "terminal": True,
                    "result_file": str(result_path),
                    "updated_at": _utc_now(),
                }
                failures += 1
                _save_state(state_path, state)
                break
            processed_now += 1

        terminal_count = sum(
            1
            for key in queue_keys
            if isinstance(records.get(key), Mapping) and records[key].get("terminal")
        )
        logger.info(
            "Queue checkpoint: ats=%s terminal=%d total=%d processed_now=%d failures=%d state=%s",
            ats,
            terminal_count,
            len(jobs),
            processed_now,
            failures,
            state_path,
        )
        return 0 if terminal_count == len(jobs) and failures == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Process one ATS JSON queue locally in background fill-only Chrome tabs"
    )
    parser.add_argument("--queue", required=True, help="JSON array of one ATS platform's jobs")
    parser.add_argument("--ats", choices=sorted(SUPPORTED_ATS), default=None)
    parser.add_argument("--state-file", default="")
    parser.add_argument("--results-dir", default="")
    parser.add_argument(
        "--submission-log-file",
        default=str(resolve_runtime_path(RUNTIME_CONFIG.application.submission_log_file)),
    )
    parser.add_argument("--config", default=str(CONFIG_DIR / "candidate_profile_config.json"))
    parser.add_argument(
        "--email-pool",
        default=str(resolve_runtime_path(RUNTIME_CONFIG.application.candidate_email_pool_file)),
    )
    parser.add_argument("--resume", default=str(DEFAULT_RESUME_FILE))
    parser.add_argument("--render-timeout-ms", type=int, default=12_000)
    parser.add_argument("--engine-timeout", type=int, default=180)
    parser.add_argument("--resume-timeout", type=int, default=300)
    parser.add_argument("--job-timeout", type=int, default=900)
    parser.add_argument("--limit", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    queue_path = Path(args.queue).expanduser().resolve()
    ats, jobs = _load_queue(queue_path, args.ats)
    digest = _queue_digest(jobs)
    output_root = OUTPUT_DIR / "local-prefill" / ats
    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else output_root / "state.json"
    )
    results_dir = (
        Path(args.results_dir).expanduser().resolve()
        if args.results_dir
        else output_root / "results"
    )
    submission_log = Path(args.submission_log_file).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    email_pool_path = Path(args.email_pool).expanduser().resolve()
    resume_path = Path(args.resume).expanduser().resolve()
    for required in (queue_path, config_path, email_pool_path, resume_path):
        if not required.is_file():
            raise FileNotFoundError(required)
    for name, value in (
        ("render timeout", args.render_timeout_ms),
        ("engine timeout", args.engine_timeout),
        ("resume timeout", args.resume_timeout),
        ("job timeout", args.job_timeout),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")
    if args.limit is not None and args.limit < 0:
        raise ValueError("limit cannot be negative")
    return _run_queue(
        args,
        queue_path=queue_path,
        ats=ats,
        jobs=jobs,
        digest=digest,
        output_root=output_root,
        state_path=state_path,
        results_dir=results_dir,
        submission_log=submission_log,
        config_path=config_path,
        email_pool_path=email_pool_path,
        resume_path=resume_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
