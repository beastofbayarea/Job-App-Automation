"""Continuously prepare and submit one guarded ATS application per cycle."""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping, Sequence

from .artifacts import atomic_write_text
from .continuous_worker_application import (
    DEFAULT_BACKLOG,
    DEFAULT_EMAIL_POOL,
    DEFAULT_INPUT as _DEFAULT_INPUT,
    DEFAULT_LAUNCHER,
    DEFAULT_PROFILE,
    DEFAULT_SUBMISSION_LOG,
    SHARED_INPUT,
    SelectedJobApplicationConfig,
    SelectedJobApplicationDependencies,
    SelectedJobApplicationService,
    apply_job,
    default_application_dependencies,
    job_digest,
    masked_email,
    outcome_diagnostics,
    prepare_documents,
    read_application_result,
    requires_manual_review,
    run_command,
    strictly_confirmed,
    valid_pdf,
)
from .continuous_worker_candidates import (
    RESUMABLE_STATUSES as _RESUMABLE_STATUSES,
    choose_resumable_or_fresh,
    load_exact_confirmed_ledger_index,
    partition_candidate_state,
)
from .continuous_worker_models import (
    DIRECT_ONCE_EXIT_POLICY,
    CommandOutcome,
    CycleStatus,
)
from .continuous_worker_runtime import WorkerRuntime, cycle_event_level, run_worker
from .continuous_worker_sources import (
    ATS_PLATFORM_PATTERN as _WORKER_ATS_PLATFORM_PATTERN,
    eligible_provider_jobs,
    validate_worker_platform,
)
from .continuous_worker_state import (
    load_worker_state,
    reconcile_interrupted_submissions,
    save_worker_state,
    utc_now_iso,
)
from .observability import NOOP_TELEMETRY, OperationalTelemetry, initialize_observability
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from .screenshots import APPLICATION_SCREENSHOT_DIR_ENV as _APPLICATION_SCREENSHOT_DIR_ENV


UTC = timezone.utc
# Retained as compatibility exports for callers of the former monolithic module.
DEFAULT_INPUT = _DEFAULT_INPUT
RESUMABLE_STATUSES = _RESUMABLE_STATUSES
APPLICATION_SCREENSHOT_DIR_ENV = _APPLICATION_SCREENSHOT_DIR_ENV
TERMINAL_STATUSES = frozenset({"confirmed", "failed", "manual_review"})
ATS_PLATFORM_PATTERN = _WORKER_ATS_PLATFORM_PATTERN
_WORKER_DEFAULTS = RUNTIME_CONFIG.continuous_worker.defaults
DEFAULT_CAPTCHA_COOLDOWN_SECONDS = _WORKER_DEFAULTS.captcha_cooldown_seconds
DEFAULT_CAPTCHA_THRESHOLD = _WORKER_DEFAULTS.captcha_threshold
DEFAULT_SPAM_REJECTION_COOLDOWN_SECONDS = _WORKER_DEFAULTS.spam_rejection_cooldown_seconds
DEFAULT_SPAM_REJECTION_THRESHOLD = _WORKER_DEFAULTS.spam_rejection_threshold
DEFAULT_APPLICATION_WINDOW_SECONDS = _WORKER_DEFAULTS.application_window_seconds


_now = utc_now_iso


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


_load_state = load_worker_state
_save_state = save_worker_state


def _eligible_jobs(payload: Any, ats_platform: str) -> list[dict[str, Any]]:
    """Compatibility facade for the shared provider-source normalizer."""
    return eligible_provider_jobs(payload, ats_platform)


def _confirmed_urls(path: Path, ats_platform: str) -> set[str]:
    """Compatibility facade for the shared exact-confirmed ledger index."""
    return set(load_exact_confirmed_ledger_index(path, ats_platform).identities)


def _select_job(
    jobs: list[dict[str, Any]],
    state: Mapping[str, Any],
    confirmed_urls: set[str],
    ats_platform: str,
    *,
    choice: Callable[[Sequence[dict[str, Any]]], dict[str, Any]] = random.choice,
) -> dict[str, Any] | None:
    records = state.get("jobs", {})
    if not isinstance(records, Mapping):
        raise ValueError(f"continuous {ats_platform} state jobs must be an object")
    pools = partition_candidate_state(
        jobs,
        records,
        state_key=lambda job: str(job["_canonical_url"]),
        identity=lambda job: str(job["_canonical_url"]),
        confirmed_identities=confirmed_urls,
    )
    return choose_resumable_or_fresh(pools, choice=choice)


_reconcile_interrupted_submissions = reconcile_interrupted_submissions


def _run_command(
    command: list[str],
    timeout_seconds: int,
    *,
    environment: Mapping[str, str] | None = None,
) -> CommandOutcome:
    """Compatibility facade for the shared bounded command runner."""
    return run_command(command, timeout_seconds, environment=environment)


def _masked_email(email: str) -> str:
    return masked_email(email)


def _sleep_between_cycles(delay: int, ats_platform: str) -> bool:
    try:
        time.sleep(delay)
    except KeyboardInterrupt:
        print(
            f"{ats_platform.upper()}_WORKER_STOPPED signal=keyboard_interrupt",
            flush=True,
        )
        return False
    return True


_cycle_event_level = cycle_event_level


def _job_digest(canonical_url: str) -> str:
    return job_digest(canonical_url)


def _valid_pdf(path: Path) -> bool:
    return valid_pdf(path)


def _read_result(path: Path) -> dict[str, Any]:
    return read_application_result(path)


def _strictly_confirmed(result: Mapping[str, Any]) -> bool:
    return strictly_confirmed(result)


def _requires_manual_review(
    result: Mapping[str, Any],
    outcome: CommandOutcome,
) -> bool:
    return requires_manual_review(result, outcome)


def _diagnostics(outcome: CommandOutcome) -> dict[str, Any]:
    return outcome_diagnostics(outcome)


def _prepare_documents(
    *,
    job: Mapping[str, Any],
    ats_platform: str,
    email: str,
    launcher: Path,
    profile: Path,
    output_dir: Path,
    timeout_seconds: int,
    generate_cover_letter: bool = True,
) -> CommandOutcome:
    """Compatibility facade that retains the patchable command-runner seam."""
    return prepare_documents(
        job=job,
        ats_platform=ats_platform,
        email=email,
        launcher=launcher,
        profile=profile,
        output_dir=output_dir,
        timeout_seconds=timeout_seconds,
        generate_cover_letter=generate_cover_letter,
        runner=_run_command,
    )


def _apply(
    *,
    job: Mapping[str, Any],
    email: str,
    launcher: Path,
    profile: Path,
    resume_path: Path,
    cover_letter_path: Path | None,
    result_path: Path,
    submission_log: Path,
    screenshot_dir: Path,
    engine_timeout_seconds: int,
    process_timeout_seconds: int,
) -> CommandOutcome:
    """Compatibility facade that retains the patchable command-runner seam."""
    return apply_job(
        job=job,
        email=email,
        launcher=launcher,
        profile=profile,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        result_path=result_path,
        submission_log=submission_log,
        screenshot_dir=screenshot_dir,
        engine_timeout_seconds=engine_timeout_seconds,
        process_timeout_seconds=process_timeout_seconds,
        runner=_run_command,
    )


def _application_service(
    *,
    ats_platform: str,
    profile: Path,
    email_pool: Path,
    launcher: Path,
    state_path: Path,
    results_dir: Path,
    documents_dir: Path,
    submission_log: Path,
    document_timeout_seconds: int,
    engine_timeout_seconds: int,
    application_timeout_seconds: int,
    backlog_path: Path | None,
    telemetry: OperationalTelemetry | None,
    generate_cover_letter: bool = True,
) -> SelectedJobApplicationService:
    defaults = default_application_dependencies()
    dependencies = SelectedJobApplicationDependencies(
        prepare_documents=_prepare_documents,
        apply_job=_apply,
        load_email_pool=defaults.load_email_pool,
        choose_email=defaults.choose_email,
        now=defaults.now,
        create_screenshot_directory=defaults.create_screenshot_directory,
        cleanup_screenshot_directory=defaults.cleanup_screenshot_directory,
        prune_backlog=defaults.prune_backlog,
    )
    return SelectedJobApplicationService(
        config=SelectedJobApplicationConfig(
            ats_platform=ats_platform,
            profile=profile,
            email_pool=email_pool,
            launcher=launcher,
            state_path=state_path,
            results_dir=results_dir,
            documents_dir=documents_dir,
            submission_log=submission_log,
            document_timeout_seconds=document_timeout_seconds,
            engine_timeout_seconds=engine_timeout_seconds,
            application_timeout_seconds=application_timeout_seconds,
            generate_cover_letter=generate_cover_letter,
            backlog_path=backlog_path,
        ),
        telemetry=telemetry or NOOP_TELEMETRY,
        dependencies=dependencies,
    )


def process_one(
    *,
    ats_platform: str,
    input_path: Path,
    profile: Path,
    email_pool: Path,
    launcher: Path,
    state_path: Path,
    results_dir: Path,
    documents_dir: Path,
    submission_log: Path,
    document_timeout_seconds: int,
    engine_timeout_seconds: int,
    application_timeout_seconds: int,
    backlog_path: Path | None = None,
    telemetry: OperationalTelemetry | None = None,
) -> CycleStatus:
    """Compatibility facade: load/select one input job, then invoke the shared service."""
    state = _load_state(state_path, ats_platform)
    jobs = _eligible_jobs(_load_json(input_path), ats_platform)
    job = _select_job(
        jobs,
        state,
        _confirmed_urls(submission_log, ats_platform),
        ats_platform,
    )
    if job is None:
        return "no_work"
    service = _application_service(
        ats_platform=ats_platform,
        profile=profile,
        email_pool=email_pool,
        launcher=launcher,
        state_path=state_path,
        results_dir=results_dir,
        documents_dir=documents_dir,
        submission_log=submission_log,
        document_timeout_seconds=document_timeout_seconds,
        engine_timeout_seconds=engine_timeout_seconds,
        application_timeout_seconds=application_timeout_seconds,
        backlog_path=backlog_path,
        telemetry=telemetry,
    )
    return service.process(job)


def _refresh_jobs(
    *,
    ats_platform: str,
    launcher: Path,
    input_path: Path,
    backlog_path: Path,
    submission_log: Path,
    timeout_seconds: int,
) -> CommandOutcome:
    csv_output = input_path.with_suffix(".csv")
    coverage_output = input_path.with_name(f"{input_path.stem}_coverage.json")
    cache_output = input_path.with_name(f"{input_path.stem}_cache.json")
    command = [
        sys.executable,
        str(launcher),
        "search",
        "--role-type",
        "Product Manager",
        "--ats-platform",
        ats_platform,
        "--verify-live",
        "--output",
        str(csv_output),
        "--coverage-report",
        str(coverage_output),
        "--cache",
        str(cache_output),
        "--backlog-output",
        str(backlog_path),
        "--submission-log",
        str(submission_log),
        "--private-generation-output",
        str(input_path),
    ]
    return _run_command(command, timeout_seconds)


def _platform_output_path(ats_platform: str, suffix: str = "") -> Path:
    name = f"continuous_{ats_platform}{suffix}"
    return resolve_runtime_path(f"output/{name}")


def _validate_platform(ats_platform: str) -> str:
    """Accept installed ATS engines without maintaining a second provider registry."""
    return validate_worker_platform(
        ats_platform,
        find_module=lambda module_name: importlib.util.find_spec(module_name),
    )


def _seed_platform_input(
    input_path: Path,
    ats_platform: str,
    *,
    overwrite: bool = False,
) -> int:
    """Copy one provider's latest jobs from the shared search output."""
    if (input_path.exists() and not overwrite) or not SHARED_INPUT.is_file():
        return 0
    jobs = _eligible_jobs(_load_json(SHARED_INPUT), ats_platform)
    payload = [
        {key: value for key, value in job.items() if not key.startswith("_")} for job in jobs
    ]
    atomic_write_text(
        input_path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(payload)


def _outcome_cooldown_remaining(
    state: Mapping[str, Any],
    *,
    matches: Callable[[Mapping[str, Any]], bool],
    now: datetime | None = None,
    cooldown_seconds: int,
    threshold: int,
) -> tuple[int, int]:
    """Return a provider-wide cooldown for matching outcomes after the latest success."""
    if cooldown_seconds <= 0 or threshold <= 0:
        raise ValueError("outcome cooldown and threshold must be greater than zero")
    timestamped: list[tuple[datetime, Mapping[str, Any]]] = []
    records = state.get("jobs", {})
    if not isinstance(records, Mapping):
        return 0, 0
    for record in records.values():
        if not isinstance(record, Mapping):
            continue
        raw_timestamp = str(record.get("updated_at", "")).strip()
        if not raw_timestamp:
            continue
        try:
            timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        timestamped.append((timestamp.astimezone(UTC), record))
    latest_confirmation = max(
        (timestamp for timestamp, record in timestamped if record.get("status") == "confirmed"),
        default=None,
    )
    outcome_timestamps = [
        timestamp
        for timestamp, record in timestamped
        if (latest_confirmation is None or timestamp > latest_confirmation) and matches(record)
    ]
    outcome_count = len(outcome_timestamps)
    if outcome_count < threshold:
        return 0, outcome_count
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    elapsed = max(0, int((current_time - max(outcome_timestamps)).total_seconds()))
    return max(0, cooldown_seconds - elapsed), outcome_count


def _captcha_cooldown_remaining(
    state: Mapping[str, Any],
    *,
    now: datetime | None = None,
    cooldown_seconds: int = DEFAULT_CAPTCHA_COOLDOWN_SECONDS,
    threshold: int = DEFAULT_CAPTCHA_THRESHOLD,
) -> tuple[int, int]:
    """Return the remaining provider-wide CAPTCHA cooldown and observed count."""
    return _outcome_cooldown_remaining(
        state,
        matches=lambda record: (
            record.get("status") == "manual_review"
            and isinstance(record.get("result"), Mapping)
            and record["result"].get("captcha_present") is True
        ),
        now=now,
        cooldown_seconds=cooldown_seconds,
        threshold=threshold,
    )


def _spam_rejection_cooldown_remaining(
    state: Mapping[str, Any],
    *,
    now: datetime | None = None,
    cooldown_seconds: int = DEFAULT_SPAM_REJECTION_COOLDOWN_SECONDS,
    threshold: int = DEFAULT_SPAM_REJECTION_THRESHOLD,
) -> tuple[int, int]:
    """Pause a provider after an explicit possible-spam rejection."""
    return _outcome_cooldown_remaining(
        state,
        matches=lambda record: (
            record.get("status") == "failed"
            and record.get("result_status") == "FLAGGED_POSSIBLE_SPAM"
        ),
        now=now,
        cooldown_seconds=cooldown_seconds,
        threshold=threshold,
    )


def _application_rate_limit_remaining(
    state: Mapping[str, Any],
    *,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_APPLICATION_WINDOW_SECONDS,
    limit: int,
) -> tuple[int, int]:
    """Return the rolling provider application-limit wait and observed attempts."""
    if window_seconds <= 0 or limit < 0:
        raise ValueError("application window must be positive and limit cannot be negative")
    if limit == 0:
        return 0, 0

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current_time.timestamp() - window_seconds
    attempts: list[datetime] = []
    records = state.get("jobs", {})
    if not isinstance(records, Mapping):
        return 0, 0
    for record in records.values():
        if not isinstance(record, Mapping):
            continue
        raw_timestamp = str(record.get("application_started_at", "")).strip()
        if not raw_timestamp:
            continue
        try:
            timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        timestamp = timestamp.astimezone(UTC)
        if timestamp.timestamp() > cutoff:
            attempts.append(timestamp)

    attempt_count = len(attempts)
    if attempt_count < limit:
        return 0, attempt_count
    blocking_timestamp = sorted(attempts, reverse=True)[limit - 1]
    elapsed = max(0, int((current_time - blocking_timestamp).total_seconds()))
    return max(0, window_seconds - elapsed), attempt_count


def build_parser(ats_platform: str) -> argparse.ArgumentParser:
    ats_platform = _validate_platform(ats_platform)
    provider_config = RUNTIME_CONFIG.continuous_worker.for_provider(ats_platform)
    parser = argparse.ArgumentParser(
        description=(
            f"Continuously select one verified-live {ats_platform.title()} job, "
            "generate a personalized "
            "resume and cover letter with a random configured email, submit it through the "
            "guarded orchestrator, then wait for the provider-specific pacing interval."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=_platform_output_path(ats_platform, "_jobs.json"),
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--email-pool", type=Path, default=DEFAULT_EMAIL_POOL)
    parser.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    parser.add_argument(
        "--state",
        type=Path,
        default=_platform_output_path(ats_platform, "_state.json"),
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=_platform_output_path(ats_platform, "_results"),
    )
    parser.add_argument(
        "--documents-dir",
        type=Path,
        default=_platform_output_path(ats_platform, "_documents"),
    )
    parser.add_argument("--submission-log", type=Path, default=DEFAULT_SUBMISSION_LOG)
    parser.add_argument(
        "--backlog",
        type=Path,
        default=DEFAULT_BACKLOG,
        help="Active-job backlog pruned only after confirmed ledger evidence.",
    )
    parser.add_argument("--sleep-min-seconds", type=int, default=provider_config.sleep_min_seconds)
    parser.add_argument("--sleep-max-seconds", type=int, default=provider_config.sleep_max_seconds)
    parser.add_argument("--application-limit", type=int, default=provider_config.application_limit)
    parser.add_argument(
        "--application-window-seconds",
        type=int,
        default=provider_config.application_window_seconds,
    )
    parser.add_argument(
        "--document-timeout-seconds",
        type=int,
        default=provider_config.document_timeout_seconds,
    )
    parser.add_argument(
        "--engine-timeout-seconds",
        type=int,
        default=provider_config.engine_timeout_seconds,
    )
    parser.add_argument(
        "--application-timeout-seconds",
        type=int,
        default=provider_config.application_timeout_seconds,
    )
    parser.add_argument(
        "--refresh-timeout-seconds",
        type=int,
        default=provider_config.refresh_timeout_seconds,
    )
    parser.add_argument(
        "--captcha-cooldown-seconds",
        type=int,
        default=provider_config.captcha_cooldown_seconds,
    )
    parser.add_argument(
        "--captcha-threshold",
        type=int,
        default=provider_config.captcha_threshold,
    )
    parser.add_argument(
        "--spam-rejection-cooldown-seconds",
        type=int,
        default=provider_config.spam_rejection_cooldown_seconds,
    )
    parser.add_argument(
        "--spam-rejection-threshold",
        type=int,
        default=provider_config.spam_rejection_threshold,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one job and return; intended for diagnostics and tests",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    ats_platform: str | None = None,
) -> int:
    if ats_platform is None:
        platform_parser = argparse.ArgumentParser(add_help=False)
        platform_parser.add_argument("--ats-platform", required=True)
        platform_args, argv = platform_parser.parse_known_args(argv)
        ats_platform = platform_args.ats_platform
    ats_platform = _validate_platform(ats_platform)
    args = build_parser(ats_platform).parse_args(argv)
    for label, value in (
        ("sleep minimum", args.sleep_min_seconds),
        ("sleep maximum", args.sleep_max_seconds),
        ("document timeout", args.document_timeout_seconds),
        ("engine timeout", args.engine_timeout_seconds),
        ("application timeout", args.application_timeout_seconds),
        ("refresh timeout", args.refresh_timeout_seconds),
        ("CAPTCHA cooldown", args.captcha_cooldown_seconds),
        ("CAPTCHA threshold", args.captcha_threshold),
        ("possible-spam rejection cooldown", args.spam_rejection_cooldown_seconds),
        ("possible-spam rejection threshold", args.spam_rejection_threshold),
        ("application window", args.application_window_seconds),
    ):
        if value <= 0:
            raise SystemExit(f"{label} must be greater than zero")
    if args.application_limit < 0:
        raise SystemExit("application limit cannot be negative")
    if args.sleep_min_seconds > args.sleep_max_seconds:
        raise SystemExit("sleep minimum cannot exceed sleep maximum")
    for label, path in (
        ("profile", args.profile),
        ("email pool", args.email_pool),
        ("launcher", args.launcher),
    ):
        if not path.is_file():
            raise SystemExit(f"{label} file not found: {path}")

    telemetry = initialize_observability(worker_kind="continuous_ats", provider=ats_platform)

    seeded = _seed_platform_input(args.input, ats_platform)
    if seeded:
        print(
            f"{ats_platform.upper()}_INPUT_SEEDED count={seeded}",
            flush=True,
        )

    state = _load_state(args.state, ats_platform)
    reconciled = _reconcile_interrupted_submissions(state)
    if reconciled:
        _save_state(args.state, state)
        print(
            f"{ats_platform.upper()}_INTERRUPTED_QUARANTINED count={reconciled}",
            flush=True,
        )

    application_service = _application_service(
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
        backlog_path=args.backlog,
        telemetry=telemetry,
    )

    def process_available_input() -> CycleStatus:
        current_state = _load_state(args.state, ats_platform)
        candidates = _eligible_jobs(_load_json(args.input), ats_platform)
        selected = _select_job(
            candidates,
            current_state,
            _confirmed_urls(args.submission_log, ats_platform),
            ats_platform,
        )
        return "no_work" if selected is None else application_service.process(selected)

    def run_cycle() -> CycleStatus:
        current_state = _load_state(args.state, ats_platform)
        spam_cooldown_remaining, spam_count = _spam_rejection_cooldown_remaining(
            current_state,
            cooldown_seconds=args.spam_rejection_cooldown_seconds,
            threshold=args.spam_rejection_threshold,
        )
        captcha_cooldown_remaining, captcha_count = _captcha_cooldown_remaining(
            current_state,
            cooldown_seconds=args.captcha_cooldown_seconds,
            threshold=args.captcha_threshold,
        )
        rate_limit_remaining, application_count = _application_rate_limit_remaining(
            current_state,
            window_seconds=args.application_window_seconds,
            limit=args.application_limit,
        )
        if spam_cooldown_remaining:
            print(
                f"{ats_platform.upper()}_POSSIBLE_SPAM_CIRCUIT_OPEN "
                f"observed={spam_count} "
                f"cooldown_remaining={spam_cooldown_remaining}",
                flush=True,
            )
            cycle_status: CycleStatus = "possible_spam_cooldown"
        elif captcha_cooldown_remaining:
            print(
                f"{ats_platform.upper()}_CAPTCHA_CIRCUIT_OPEN "
                f"observed={captcha_count} "
                f"cooldown_remaining={captcha_cooldown_remaining}",
                flush=True,
            )
            cycle_status = "captcha_cooldown"
        elif rate_limit_remaining:
            print(
                f"{ats_platform.upper()}_APPLICATION_RATE_LIMIT_OPEN "
                f"observed={application_count} "
                f"limit={args.application_limit} "
                f"window_seconds={args.application_window_seconds} "
                f"cooldown_remaining={rate_limit_remaining}",
                flush=True,
            )
            cycle_status = "application_rate_limit"
        elif not args.input.is_file():
            outcome = _refresh_jobs(
                ats_platform=ats_platform,
                launcher=args.launcher,
                input_path=args.input,
                backlog_path=args.backlog,
                submission_log=args.submission_log,
                timeout_seconds=args.refresh_timeout_seconds,
            )
            if outcome.return_code != 0:
                print(
                    f"{ats_platform.upper()}_REFRESH_FAILED "
                    f"exit_code={outcome.return_code} timed_out={outcome.timed_out}",
                    flush=True,
                )
                cycle_status = "refresh_failed"
            else:
                cycle_status = "refreshed"
        else:
            cycle_status = process_available_input()
            if cycle_status == "no_work" and not args.once:
                shared_count = _seed_platform_input(
                    args.input,
                    ats_platform,
                    overwrite=True,
                )
                if shared_count:
                    print(
                        f"{ats_platform.upper()}_INPUT_REFRESHED_FROM_SHARED count={shared_count}",
                        flush=True,
                    )
                    cycle_status = process_available_input()
                if cycle_status == "no_work":
                    outcome = _refresh_jobs(
                        ats_platform=ats_platform,
                        launcher=args.launcher,
                        input_path=args.input,
                        backlog_path=args.backlog,
                        submission_log=args.submission_log,
                        timeout_seconds=args.refresh_timeout_seconds,
                    )
                    print(
                        f"{ats_platform.upper()}_REFRESH_FINISHED "
                        f"exit_code={outcome.return_code} timed_out={outcome.timed_out}",
                        flush=True,
                    )
                    cycle_status = "refreshed" if outcome.return_code == 0 else "refresh_failed"
        return cycle_status

    def report_interrupt() -> None:
        print(
            f"{ats_platform.upper()}_WORKER_STOPPED signal=keyboard_interrupt",
            flush=True,
        )

    def report_exception(exc: Exception) -> None:
        print(
            f"{ats_platform.upper()}_CYCLE_EXCEPTION "
            f"type={type(exc).__name__} detail={str(exc)[:500]!r}",
            file=sys.stderr,
            flush=True,
        )

    def announce_sleep(delay: int, cycle_status: CycleStatus) -> None:
        print(
            f"{ats_platform.upper()}_CYCLE_SLEEP seconds={delay} prior_status={cycle_status}",
            flush=True,
        )

    return run_worker(
        WorkerRuntime(
            provider=ats_platform,
            cycle_stage="worker_cycle",
            once=args.once,
            once_exit_policy=DIRECT_ONCE_EXIT_POLICY,
            telemetry=telemetry,
            run_cycle=run_cycle,
            delay_for=lambda _status: random.randint(
                args.sleep_min_seconds,
                args.sleep_max_seconds,
            ),
            announce_sleep=announce_sleep,
            sleep=lambda delay: _sleep_between_cycles(delay, ats_platform),
            report_interrupt=report_interrupt,
            report_exception=report_exception,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
