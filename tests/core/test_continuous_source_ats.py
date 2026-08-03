from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from job_application_automation.core import continuous_source_ats
from job_application_automation.core.artifacts import read_json
from job_application_automation.core.observability import NOOP_TELEMETRY
from job_application_automation.core.runtime_config import RUNTIME_CONFIG


def _write_tracker(path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Updated", "Company", "Title", "Location", "URL"])
    sheet.append(
        [
            "2026-07-31",
            "Example",
            "Product Manager",
            "Remote",
            "https://boards.greenhouse.io/example/jobs/12345?gh_jid=12345",
        ]
    )
    workbook.save(path)
    workbook.close()


def _job(job_id: str = "12345") -> dict[str, str]:
    return {
        "job_url": f"https://boards.greenhouse.io/example/jobs/{job_id}?gh_jid={job_id}",
        "company": "Example",
        "title": "Product Manager",
        "description": "A" * 300,
        "platform": "greenhouse",
        "live_status": "live",
    }


def test_greenhouse_identity_matches_branded_and_native_urls() -> None:
    native = "https://boards.greenhouse.io/stripe/jobs/8064526?gh_jid=8064526"
    branded = "https://stripe.com/jobs/search?gh_jid=8064526"

    assert continuous_source_ats._job_identity(native, "greenhouse") == "greenhouse:8064526"
    assert continuous_source_ats._job_identity(branded, "greenhouse") == "greenhouse:8064526"


def test_supervised_pause_only_stops_for_question_failures(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": {
                    "https://example.test/jobs/1": {
                        "updated_at": "2026-08-03T01:00:00+00:00",
                        "result_status": "JOB_CONTEXT_UNAVAILABLE",
                    },
                    "https://example.test/jobs/2": {
                        "updated_at": "2026-08-03T02:00:00+00:00",
                        "result": {"status": "REQUIRED_FIELDS_NOT_FILLED"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    assert continuous_source_ats._requires_clarification(state, "failed") is False
    assert continuous_source_ats._requires_clarification(state, "confirmed") is False
    assert continuous_source_ats._requires_clarification(state, "manual_review") is False
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["jobs"].pop("https://example.test/jobs/2")
    state.write_text(json.dumps(payload), encoding="utf-8")
    assert continuous_source_ats._requires_clarification(state, "failed") is False


def test_tracker_source_loads_only_recognized_provider_jobs(tmp_path: Path) -> None:
    tracker = tmp_path / "greenhouse.xlsx"
    _write_tracker(tracker)

    jobs = continuous_source_ats._source_jobs(
        source="tracker",
        ats_platform="greenhouse",
        input_path=tmp_path / "unused.json",
        tracker_path=tracker,
    )

    assert jobs == [
        {
            "job_url": "https://boards.greenhouse.io/example/jobs/12345?gh_jid=12345",
            "company": "Example",
            "title": "Product Manager",
            "platform": "greenhouse",
            "tracker_row": 2,
        }
    ]


def test_tracker_source_rejects_declared_and_detected_provider_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        continuous_source_ats,
        "load_jobs_from_tracker",
        lambda _path: [
            {
                "url": "https://jobs.lever.co/example/123",
                "company": "Example",
                "role": "Product Manager",
                "ats": "greenhouse",
                "row_number": 2,
            }
        ],
    )

    jobs = continuous_source_ats._source_jobs(
        source="tracker",
        ats_platform="greenhouse",
        input_path=tmp_path / "unused.json",
        tracker_path=tmp_path / "tracker.xlsx",
    )

    assert jobs == []


def test_source_worker_rejects_uninstalled_provider_before_file_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="engine is not installed"):
        continuous_source_ats.main(
            [
                "--ats-platform",
                "futureats",
                "--source",
                "search",
                "--worker-id",
                "search",
                "--state",
                str(tmp_path / "state.json"),
                "--selected-input",
                str(tmp_path / "selected.json"),
                "--results-dir",
                str(tmp_path / "results"),
                "--documents-dir",
                str(tmp_path / "documents"),
                "--validate-only",
            ]
        )


def test_claims_prevent_peer_worker_from_selecting_same_greenhouse_job(
    tmp_path: Path,
) -> None:
    claims = tmp_path / "claims.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}\n", encoding="utf-8")
    search_state = tmp_path / "search_state.json"
    excel_state = tmp_path / "excel_state.json"
    search_state.write_text('{"version":1,"jobs":{}}\n', encoding="utf-8")
    excel_state.write_text('{"version":1,"jobs":{}}\n', encoding="utf-8")

    selected = continuous_source_ats._claim_next_job(
        [_job()],
        ats_platform="greenhouse",
        worker_id="search",
        state_path=search_state,
        peer_states=[excel_state],
        claims_path=claims,
        submission_log=ledger,
    )
    blocked = continuous_source_ats._claim_next_job(
        [
            {
                **_job(),
                "job_url": "https://example.com/careers?gh_jid=12345",
            }
        ],
        ats_platform="greenhouse",
        worker_id="excel",
        state_path=excel_state,
        peer_states=[search_state],
        claims_path=claims,
        submission_log=ledger,
    )

    assert selected is not None
    assert blocked is None
    assert read_json(claims)["jobs"]["greenhouse:12345"]["owner"] == "search"


def test_peer_terminal_state_blocks_tracker_variant(tmp_path: Path) -> None:
    claims = tmp_path / "claims.json"
    ledger = tmp_path / "submission_log.json"
    ledger.write_text("{}\n", encoding="utf-8")
    search_state = tmp_path / "search_state.json"
    excel_state = tmp_path / "excel_state.json"
    search_state.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": {
                    "https://boards.greenhouse.io/stripe/jobs/8064526?gh_jid=8064526": {
                        "status": "failed",
                        "job_url": (
                            "https://boards.greenhouse.io/stripe/jobs/8064526?gh_jid=8064526"
                        ),
                        "result_status": "REQUIRED_FIELDS_NOT_FILLED",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    excel_state.write_text('{"version":1,"jobs":{}}\n', encoding="utf-8")

    selected = continuous_source_ats._claim_next_job(
        [
            {
                **_job("8064526"),
                "job_url": "https://stripe.com/jobs/search?gh_jid=8064526",
            }
        ],
        ats_platform="greenhouse",
        worker_id="excel",
        state_path=excel_state,
        peer_states=[search_state],
        claims_path=claims,
        submission_log=ledger,
    )

    assert selected is None


def test_hydrate_tracker_job_rejects_closed_role(monkeypatch) -> None:
    monkeypatch.setattr(
        continuous_source_ats,
        "scrape_job",
        lambda _url: {"jd_text": "This job is no longer available. " + ("x" * 300)},
    )

    try:
        continuous_source_ats._hydrate_tracker_job(_job(), "greenhouse")
    except RuntimeError as exc:
        assert "no longer available" in str(exc)
    else:
        raise AssertionError("closed tracker job should be rejected")


def test_sleep_until_next_cycle_handles_keyboard_interrupt(
    monkeypatch,
    capsys,
) -> None:
    def interrupt_sleep(_delay: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(continuous_source_ats.time, "sleep", interrupt_sleep)

    completed = continuous_source_ats._sleep_until_next_cycle(
        120,
        ats_platform="greenhouse",
        worker_id="search",
    )
    assert completed is False
    assert (
        capsys.readouterr().out == "GREENHOUSE_SOURCE_WORKER_STOPPED "
        "worker=search signal=keyboard_interrupt\n"
    )


def test_critical_failure_is_automatically_scheduled_for_retry(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    claims_path = tmp_path / "claims.json"
    job = _job()
    state_path.write_text(
        json.dumps({
            "version": 1,
            "jobs": {
                job["job_url"]: {
                    "status": "failed",
                    "result_status": "REQUIRED_FIELDS_NOT_FILLED",
                    "updated_at": "2026-08-03T00:00:00+00:00",
                }
            },
        }),
        encoding="utf-8",
    )
    claims_path.write_text(
        json.dumps({
            "version": 1,
            "jobs": {
                "greenhouse:12345": {
                    "owner": "failed-core-product-management",
                    "status": "claimed",
                    "retry_count": 1,
                }
            },
        }),
        encoding="utf-8",
    )

    continuous_source_ats._sync_claim_from_state(
        job=job,
        ats_platform="greenhouse",
        worker_id="failed-core-product-management",
        state_path=state_path,
        claims_path=claims_path,
        fallback_status="failed",
    )

    claim = read_json(claims_path)["jobs"]["greenhouse:12345"]
    assert claim["status"] == "retry_requested"
    assert claim["retry_count"] == 2
    assert claim["critical_error"] is True
    assert claim["next_retry_at"]


def test_retry_due_honors_future_backoff() -> None:
    now = continuous_source_ats.datetime(2026, 8, 3, tzinfo=continuous_source_ats.UTC)

    assert continuous_source_ats._retry_due(
        {"next_retry_at": "2026-08-02T23:59:59+00:00"}, now=now
    )
    assert not continuous_source_ats._retry_due(
        {"next_retry_at": "2026-08-03T00:00:01+00:00"}, now=now
    )

def test_source_main_runs_runtime_claim_application_and_claim_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "search_jobs.json"
    input_path.write_text(json.dumps([_job()]), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    email_pool = tmp_path / "emails.json"
    email_pool.write_text('["candidate@example.test"]\n', encoding="utf-8")
    launcher = tmp_path / "job_automation.py"
    launcher.write_text("", encoding="utf-8")
    submission_log = tmp_path / "submission_log.json"
    submission_log.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    claims_path = tmp_path / "claims.json"
    selected_input = tmp_path / "selected.json"
    applied: list[dict[str, object]] = []
    application_configs = []
    source_reads: list[Path] = []
    source_read_json = continuous_source_ats.read_json

    def record_source_read(path: Path) -> object:
        source_reads.append(path)
        return source_read_json(path)

    def process_selected(service, job):
        application_configs.append(service.config)
        applied.append(dict(job))
        state = continuous_source_ats.load_worker_state(
            service.config.state_path,
            service.config.ats_platform,
        )
        canonical_url = continuous_source_ats.canonical_job_url(job["job_url"])
        state["jobs"][canonical_url] = {
            "status": "confirmed",
            "stage": "application",
            "job_url": str(job["job_url"]),
            "company": str(job["company"]),
            "title": str(job["title"]),
            "result_status": "SUBMITTED & CONFIRMED",
            "updated_at": "2026-08-02T00:00:00+00:00",
        }
        continuous_source_ats.save_worker_state(service.config.state_path, state)
        return "confirmed"

    monkeypatch.setattr(
        continuous_source_ats.SelectedJobApplicationService,
        "process",
        process_selected,
    )
    monkeypatch.setattr(
        continuous_source_ats,
        "initialize_observability",
        lambda **_kwargs: NOOP_TELEMETRY,
    )
    monkeypatch.setattr(
        continuous_source_ats,
        "read_json",
        record_source_read,
    )

    exit_code = continuous_source_ats.main(
        [
            "--ats-platform",
            "greenhouse",
            "--source",
            "search",
            "--worker-id",
            "search",
            "--input",
            str(input_path),
            "--state",
            str(state_path),
            "--claims",
            str(claims_path),
            "--selected-input",
            str(selected_input),
            "--results-dir",
            str(tmp_path / "results"),
            "--documents-dir",
            str(tmp_path / "documents"),
            "--profile",
            str(profile),
            "--email-pool",
            str(email_pool),
            "--launcher",
            str(launcher),
            "--submission-log",
            str(submission_log),
            "--backlog",
            str(tmp_path / "backlog.json"),
            "--once",
        ]
    )

    assert exit_code == 0
    assert len(applied) == 1
    assert len(application_configs) == 1
    source_defaults = RUNTIME_CONFIG.continuous_worker.source
    assert (
        application_configs[0].document_timeout_seconds == source_defaults.document_timeout_seconds
    )
    assert application_configs[0].engine_timeout_seconds == source_defaults.engine_timeout_seconds
    assert (
        application_configs[0].application_timeout_seconds
        == source_defaults.application_timeout_seconds
    )
    assert selected_input not in source_reads
    assert read_json(selected_input)[0] == applied[0]
    assert read_json(state_path)["jobs"][_job()["job_url"]]["status"] == "confirmed"
    claim = read_json(claims_path)["jobs"]["greenhouse:12345"]
    assert claim["owner"] == "search"
    assert claim["status"] == "confirmed"
    assert claim["result_status"] == "SUBMITTED & CONFIRMED"


def test_source_worker_does_not_import_the_direct_worker_implementation() -> None:
    source = Path(continuous_source_ats.__file__).read_text(encoding="utf-8")

    assert "from .continuous_ats" not in source
    assert "import continuous_ats" not in source
