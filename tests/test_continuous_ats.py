from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from job_application_automation.core import continuous_ats as worker


def _job(
    url: str = "https://jobs.ashbyhq.com/example/123",
    *,
    platform: str = "ashby",
) -> dict[str, object]:
    return {
        "platform": platform,
        "company": "Example",
        "title": "Product Manager",
        "description": "Build useful products.",
        "job_url": url,
        "live_status": "live",
        "location": "Remote",
    }


def _pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-" + b"x" * 1500)


def test_process_one_uses_one_random_email_and_both_personalized_documents() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        input_path = root / "jobs.json"
        profile = root / "profile.json"
        pool = root / "pool.json"
        launcher = root / "launcher.py"
        state = root / "state.json"
        submission_log = root / "submissions.json"
        results = root / "results"
        documents = root / "documents"
        input_path.write_text(json.dumps([_job()]), encoding="utf-8")
        profile.write_text("{}", encoding="utf-8")
        pool.write_text(
            json.dumps(["first@example.test", "chosen@example.test"]),
            encoding="utf-8",
        )
        launcher.write_text("", encoding="utf-8")

        def prepare(**kwargs: object) -> worker.CommandOutcome:
            output_dir = Path(str(kwargs["output_dir"]))
            _pdf(output_dir / "resume.pdf")
            _pdf(output_dir / "cover_letter.pdf")
            return worker.CommandOutcome(0, "documents ready", "")

        def apply(**kwargs: object) -> worker.CommandOutcome:
            result_path = Path(str(kwargs["result_path"]))
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(
                    [
                        {
                            "success": True,
                            "status": "SUBMITTED & CONFIRMED",
                            "ats": "ashby",
                            "submitted": True,
                            "confirmed": True,
                            "test_mode": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            submission_log.write_text(
                json.dumps(
                    {
                        "confirmed": {
                            "job_url": _job()["job_url"],
                            "status": "SUBMITTED & CONFIRMED",
                            "ats": "ashby",
                        }
                    }
                ),
                encoding="utf-8",
            )
            return worker.CommandOutcome(0, "submitted", "")

        with (
            patch.object(worker.random, "choice", return_value="chosen@example.test"),
            patch.object(worker, "_prepare_documents", side_effect=prepare) as prepare_call,
            patch.object(worker, "_apply", side_effect=apply) as apply_call,
        ):
            status = worker.process_one(
                ats_platform="ashby",
                input_path=input_path,
                profile=profile,
                email_pool=pool,
                launcher=launcher,
                state_path=state,
                results_dir=results,
                documents_dir=documents,
                submission_log=submission_log,
                document_timeout_seconds=60,
                engine_timeout_seconds=30,
                application_timeout_seconds=60,
            )

        assert status == "confirmed"
        assert prepare_call.call_count == 1
        assert apply_call.call_count == 1
        assert prepare_call.call_args.kwargs["email"] == "chosen@example.test"
        assert apply_call.call_args.kwargs["email"] == "chosen@example.test"
        assert apply_call.call_args.kwargs["resume_path"].name == "resume.pdf"
        assert apply_call.call_args.kwargs["cover_letter_path"].name == "cover_letter.pdf"
        saved = json.loads(state.read_text(encoding="utf-8"))
        record = next(iter(saved["jobs"].values()))
        assert record["status"] == "confirmed"
        assert record["ledger_confirmed"] is True
        assert record["email"] == "chosen@example.test"

        with (
            patch.object(worker, "_prepare_documents") as no_prepare,
            patch.object(worker, "_apply") as no_apply,
        ):
            second_status = worker.process_one(
                ats_platform="ashby",
                input_path=input_path,
                profile=profile,
                email_pool=pool,
                launcher=launcher,
                state_path=state,
                results_dir=results,
                documents_dir=documents,
                submission_log=submission_log,
                document_timeout_seconds=60,
                engine_timeout_seconds=30,
                application_timeout_seconds=60,
            )
        assert second_status == "no_work"
        no_prepare.assert_not_called()
        no_apply.assert_not_called()


def test_unconfirmed_submitted_result_is_quarantined_and_never_retried() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        input_path = root / "jobs.json"
        input_path.write_text(json.dumps([_job()]), encoding="utf-8")
        pool = root / "pool.json"
        pool.write_text(json.dumps(["candidate@example.test"]), encoding="utf-8")
        profile = root / "profile.json"
        profile.write_text("{}", encoding="utf-8")
        launcher = root / "launcher.py"
        launcher.write_text("", encoding="utf-8")

        def prepare(**kwargs: object) -> worker.CommandOutcome:
            output_dir = Path(str(kwargs["output_dir"]))
            _pdf(output_dir / "resume.pdf")
            _pdf(output_dir / "cover_letter.pdf")
            return worker.CommandOutcome(0, "", "")

        def apply(**kwargs: object) -> worker.CommandOutcome:
            result_path = Path(str(kwargs["result_path"]))
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(
                    [
                        {
                            "success": False,
                            "status": "SUBMISSION_UNCONFIRMED",
                            "ats": "ashby",
                            "submitted": True,
                            "confirmed": False,
                            "test_mode": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            return worker.CommandOutcome(1, "", "confirmation missing")

        kwargs = {
            "ats_platform": "ashby",
            "input_path": input_path,
            "profile": profile,
            "email_pool": pool,
            "launcher": launcher,
            "state_path": root / "state.json",
            "results_dir": root / "results",
            "documents_dir": root / "documents",
            "submission_log": root / "submissions.json",
            "document_timeout_seconds": 60,
            "engine_timeout_seconds": 30,
            "application_timeout_seconds": 60,
        }
        with (
            patch.object(worker, "_prepare_documents", side_effect=prepare),
            patch.object(worker, "_apply", side_effect=apply) as apply_call,
        ):
            assert worker.process_one(**kwargs) == "manual_review"
            assert worker.process_one(**kwargs) == "no_work"
        assert apply_call.call_count == 1


def test_interrupted_application_is_quarantined_while_document_stage_can_resume() -> None:
    state = {
        "version": 1,
        "jobs": {
            "https://jobs.ashbyhq.com/example/1": {
                "status": "application_started"
            },
            "https://jobs.ashbyhq.com/example/2": {
                "status": "documents_ready"
            },
        },
    }

    assert worker._reconcile_interrupted_submissions(state) == 1
    assert state["jobs"]["https://jobs.ashbyhq.com/example/1"]["status"] == (
        "manual_review"
    )
    assert state["jobs"]["https://jobs.ashbyhq.com/example/2"]["status"] == (
        "documents_ready"
    )


def test_eligibility_is_platform_specific_live_complete_and_deduplicated() -> None:
    live = _job()
    jobs = worker._eligible_jobs(
        [
            live,
            dict(live),
            _job(
                "https://boards.greenhouse.io/example/jobs/1",
                platform="greenhouse",
            ),
            _job("https://jobs.ashbyhq.com/example/2") | {"live_status": "closed"},
            _job("https://jobs.ashbyhq.com/example/3") | {"description": ""},
        ],
        "ashby",
    )

    assert len(jobs) == 1
    assert jobs[0]["_canonical_url"] == "https://jobs.ashbyhq.com/example/123"


def test_parallel_workers_seed_distinct_provider_inputs() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shared = root / "vps_generation_jobs.json"
        shared.write_text(
            json.dumps(
                [
                    _job(),
                    _job(
                        "https://boards.greenhouse.io/example/jobs/1",
                        platform="greenhouse",
                    ),
                ]
            ),
            encoding="utf-8",
        )
        ashby_input = root / "continuous_ashby_jobs.json"
        greenhouse_input = root / "continuous_greenhouse_jobs.json"

        with patch.object(worker, "SHARED_INPUT", shared):
            assert worker._seed_platform_input(ashby_input, "ashby") == 1
            assert worker._seed_platform_input(greenhouse_input, "greenhouse") == 1

        ashby_jobs = json.loads(ashby_input.read_text(encoding="utf-8"))
        greenhouse_jobs = json.loads(greenhouse_input.read_text(encoding="utf-8"))
        assert ashby_jobs[0]["platform"] == "ashby"
        assert greenhouse_jobs[0]["platform"] == "greenhouse"
        assert "_canonical_url" not in ashby_jobs[0]


def test_sleep_between_cycles_handles_service_stop_without_traceback() -> None:
    with patch.object(worker.time, "sleep", side_effect=KeyboardInterrupt):
        assert worker._sleep_between_cycles(120, "ashby") is False
