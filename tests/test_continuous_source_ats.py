from __future__ import annotations

import json
from pathlib import Path

import openpyxl

from job_application_automation.core import continuous_source_ats
from job_application_automation.core.artifacts import read_json


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
        capsys.readouterr().out
        == "GREENHOUSE_SOURCE_WORKER_STOPPED "
        "worker=search signal=keyboard_interrupt\n"
    )
