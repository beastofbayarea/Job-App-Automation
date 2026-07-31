from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from job_application_automation.core import continuous_ats as worker


UTC = timezone.utc


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


def test_apply_passes_isolated_screenshot_directory_to_orchestrator(tmp_path: Path) -> None:
    screenshot_dir = tmp_path / "screenshots"
    expected = worker.CommandOutcome(0, "done", "")

    with patch.object(worker, "_run_command", return_value=expected) as run_command:
        actual = worker._apply(
            job=_job(),
            email="candidate@example.test",
            launcher=tmp_path / "launcher.py",
            profile=tmp_path / "profile.json",
            resume_path=tmp_path / "resume.pdf",
            cover_letter_path=tmp_path / "cover_letter.pdf",
            result_path=tmp_path / "result.json",
            submission_log=tmp_path / "submission_log.json",
            screenshot_dir=screenshot_dir,
            engine_timeout_seconds=30,
            process_timeout_seconds=60,
        )

    assert actual == expected
    assert run_command.call_args.kwargs["environment"] == {
        worker.APPLICATION_SCREENSHOT_DIR_ENV: str(screenshot_dir)
    }


def test_process_one_uses_one_random_email_and_both_personalized_documents() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        input_path = root / "jobs.json"
        profile = root / "profile.json"
        pool = root / "pool.json"
        launcher = root / "launcher.py"
        state = root / "state.json"
        submission_log = root / "submissions.json"
        backlog = root / "job_backlog.json"
        results = root / "results"
        documents = root / "documents"
        input_path.write_text(json.dumps([_job()]), encoding="utf-8")
        profile.write_text("{}", encoding="utf-8")
        pool.write_text(
            json.dumps(["first@example.test", "chosen@example.test"]),
            encoding="utf-8",
        )
        launcher.write_text("", encoding="utf-8")
        backlog.write_text(
            json.dumps(
                {
                    "version": 1,
                    "updated_at": "2026-07-31T00:00:00+00:00",
                    "jobs": [
                        {
                            "platform": "ashby",
                            "company": "Example",
                            "title": "Product Manager",
                            "job_url": _job()["job_url"],
                            "apply_url": _job()["job_url"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        def prepare(**kwargs: object) -> worker.CommandOutcome:
            output_dir = Path(str(kwargs["output_dir"]))
            _pdf(output_dir / "resume.pdf")
            _pdf(output_dir / "cover_letter.pdf")
            return worker.CommandOutcome(0, "documents ready", "")

        created_screenshot_dirs: list[Path] = []

        def apply(**kwargs: object) -> worker.CommandOutcome:
            result_path = Path(str(kwargs["result_path"]))
            screenshot_dir = Path(str(kwargs["screenshot_dir"]))
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            (screenshot_dir / "confirmed.png").write_bytes(b"proof")
            created_screenshot_dirs.append(screenshot_dir)
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
                backlog_path=backlog,
            )

        assert status == "confirmed"
        assert prepare_call.call_count == 1
        assert apply_call.call_count == 1
        assert prepare_call.call_args.kwargs["email"] == "chosen@example.test"
        assert apply_call.call_args.kwargs["email"] == "chosen@example.test"
        assert apply_call.call_args.kwargs["resume_path"].name == "resume.pdf"
        assert apply_call.call_args.kwargs["cover_letter_path"].name == "cover_letter.pdf"
        assert len(created_screenshot_dirs) == 1
        assert not created_screenshot_dirs[0].exists()
        saved = json.loads(state.read_text(encoding="utf-8"))
        record = next(iter(saved["jobs"].values()))
        assert record["status"] == "confirmed"
        assert record["ledger_confirmed"] is True
        assert record["email"] == "chosen@example.test"
        assert json.loads(backlog.read_text(encoding="utf-8"))["jobs"] == []

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


def test_unconfirmed_submit_attempt_is_quarantined_and_never_retried() -> None:
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

        created_screenshot_dirs: list[Path] = []

        def apply(**kwargs: object) -> worker.CommandOutcome:
            result_path = Path(str(kwargs["result_path"]))
            screenshot_dir = Path(str(kwargs["screenshot_dir"]))
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            (screenshot_dir / "failed.png").write_bytes(b"proof")
            created_screenshot_dirs.append(screenshot_dir)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(
                    [
                        {
                            "success": False,
                            "status": "SUBMIT_ATTEMPT_UNCONFIRMED",
                            "ats": "ashby",
                            "submitted": False,
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
        assert len(created_screenshot_dirs) == 1
        assert not created_screenshot_dirs[0].exists()


def test_documents_ready_resume_reuses_saved_email_without_sampling_again() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        job = _job()
        canonical_url = str(job["job_url"])
        input_path = root / "jobs.json"
        input_path.write_text(json.dumps([job]), encoding="utf-8")
        profile = root / "profile.json"
        profile.write_text("{}", encoding="utf-8")
        pool = root / "pool.json"
        pool.write_text(json.dumps(["different@example.test"]), encoding="utf-8")
        launcher = root / "launcher.py"
        launcher.write_text("", encoding="utf-8")
        documents = root / "documents" / "saved"
        _pdf(documents / "resume.pdf")
        _pdf(documents / "cover_letter.pdf")
        result_path = root / "results" / "saved.json"
        state_path = root / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "jobs": {
                        canonical_url: {
                            "status": "documents_ready",
                            "stage": "application",
                            "job_url": canonical_url,
                            "company": "Example",
                            "title": "Product Manager",
                            "platform": "ashby",
                            "email": "saved@example.test",
                            "document_dir": str(documents),
                            "result_path": str(result_path),
                            "started_at": "2026-07-31T00:00:00+00:00",
                            "updated_at": "2026-07-31T00:00:00+00:00",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        def apply(**kwargs: object) -> worker.CommandOutcome:
            path = Path(str(kwargs["result_path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    [
                        {
                            "success": False,
                            "status": "ABORTED_MISSING_REQUIRED_FIELDS",
                            "ats": "ashby",
                            "submitted": False,
                            "confirmed": False,
                            "test_mode": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            return worker.CommandOutcome(1, "", "")

        with (
            patch.object(
                worker.random,
                "choice",
                side_effect=AssertionError("saved jobs must not choose another email"),
            ),
            patch.object(worker, "_prepare_documents") as prepare,
            patch.object(worker, "_apply", side_effect=apply) as apply_call,
        ):
            status = worker.process_one(
                ats_platform="ashby",
                input_path=input_path,
                profile=profile,
                email_pool=pool,
                launcher=launcher,
                state_path=state_path,
                results_dir=root / "results",
                documents_dir=root / "documents",
                submission_log=root / "submissions.json",
                document_timeout_seconds=60,
                engine_timeout_seconds=30,
                application_timeout_seconds=60,
            )

        assert status == "failed"
        prepare.assert_not_called()
        assert apply_call.call_args.kwargs["email"] == "saved@example.test"


def test_interrupted_application_is_quarantined_while_document_stage_can_resume() -> None:
    state = {
        "version": 1,
        "jobs": {
            "https://jobs.ashbyhq.com/example/1": {"status": "application_started"},
            "https://jobs.ashbyhq.com/example/2": {"status": "documents_ready"},
        },
    }

    assert worker._reconcile_interrupted_submissions(state) == 1
    assert state["jobs"]["https://jobs.ashbyhq.com/example/1"]["status"] == ("manual_review")
    assert state["jobs"]["https://jobs.ashbyhq.com/example/2"]["status"] == ("documents_ready")


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


def test_provider_refresh_uses_shared_backlog_and_isolated_search_artifacts(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "continuous_greenhouse_jobs.json"
    backlog_path = tmp_path / "job_backlog.json"
    submission_log = tmp_path / "submission_log.json"

    with patch.object(
        worker,
        "_run_command",
        return_value=worker.CommandOutcome(0, "", ""),
    ) as run_command:
        worker._refresh_jobs(
            ats_platform="greenhouse",
            launcher=tmp_path / "job_automation.py",
            input_path=input_path,
            backlog_path=backlog_path,
            submission_log=submission_log,
            timeout_seconds=60,
        )

    command = run_command.call_args.args[0]
    assert command[command.index("--backlog-output") + 1] == str(backlog_path)
    assert command[command.index("--submission-log") + 1] == str(submission_log)
    assert command[command.index("--output") + 1] == str(input_path.with_suffix(".csv"))
    assert command[command.index("--cache") + 1].endswith("_cache.json")
    assert command[command.index("--coverage-report") + 1].endswith("_coverage.json")


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


def test_shared_input_can_refresh_an_exhausted_provider_list() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shared = root / "vps_generation_jobs.json"
        provider_input = root / "continuous_greenhouse_jobs.json"
        provider_input.write_text("[]", encoding="utf-8")
        shared.write_text(
            json.dumps(
                [
                    _job(
                        "https://boards.greenhouse.io/example/jobs/2",
                        platform="greenhouse",
                    )
                ]
            ),
            encoding="utf-8",
        )

        with patch.object(worker, "SHARED_INPUT", shared):
            assert worker._seed_platform_input(provider_input, "greenhouse") == 0
            assert (
                worker._seed_platform_input(
                    provider_input,
                    "greenhouse",
                    overwrite=True,
                )
                == 1
            )

        refreshed = json.loads(provider_input.read_text(encoding="utf-8"))
        assert refreshed[0]["job_url"].endswith("/jobs/2")


def test_captcha_circuit_opens_until_cooldown_and_resets_after_confirmation() -> None:
    now = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    captcha_result = {"captcha_present": True}
    state = {
        "jobs": {
            "first": {
                "status": "manual_review",
                "result": captcha_result,
                "updated_at": (now - timedelta(hours=2)).isoformat(),
            },
            "second": {
                "status": "manual_review",
                "result": captcha_result,
                "updated_at": (now - timedelta(hours=1)).isoformat(),
            },
        }
    }

    remaining, observed = worker._captcha_cooldown_remaining(
        state,
        now=now,
        cooldown_seconds=6 * 60 * 60,
    )
    assert observed == 2
    assert remaining == 5 * 60 * 60

    state["jobs"]["confirmed"] = {
        "status": "confirmed",
        "updated_at": (now - timedelta(minutes=30)).isoformat(),
    }
    assert worker._captcha_cooldown_remaining(state, now=now) == (0, 0)


def test_possible_spam_circuit_opens_immediately_and_resets_after_confirmation() -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    state = {
        "jobs": {
            "rejected": {
                "status": "failed",
                "result_status": "FLAGGED_POSSIBLE_SPAM",
                "updated_at": (now - timedelta(hours=2)).isoformat(),
            },
            "generic": {
                "status": "failed",
                "result_status": "SUBMISSION_REJECTED",
                "updated_at": (now - timedelta(hours=1)).isoformat(),
            },
        }
    }

    remaining, observed = worker._spam_rejection_cooldown_remaining(
        state,
        now=now,
        cooldown_seconds=24 * 60 * 60,
    )
    assert observed == 1
    assert remaining == 22 * 60 * 60

    state["jobs"]["confirmed"] = {
        "status": "confirmed",
        "updated_at": (now - timedelta(minutes=30)).isoformat(),
    }
    assert worker._spam_rejection_cooldown_remaining(state, now=now) == (0, 0)


def test_possible_spam_circuit_prevents_application_work(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "jobs.json"
    profile = tmp_path / "profile.json"
    pool = tmp_path / "pool.json"
    launcher = tmp_path / "launcher.py"
    state = tmp_path / "state.json"
    input_path.write_text("[]", encoding="utf-8")
    profile.write_text("{}", encoding="utf-8")
    pool.write_text(json.dumps(["candidate@example.test"]), encoding="utf-8")
    launcher.write_text("", encoding="utf-8")
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": {
                    "rejected": {
                        "status": "failed",
                        "result_status": "FLAGGED_POSSIBLE_SPAM",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with patch.object(worker, "process_one") as process:
        exit_code = worker.main(
            [
                "--input",
                str(input_path),
                "--profile",
                str(profile),
                "--email-pool",
                str(pool),
                "--launcher",
                str(launcher),
                "--state",
                str(state),
                "--once",
            ],
            ats_platform="ashby",
        )

    assert exit_code == 1
    process.assert_not_called()
    assert "ASHBY_POSSIBLE_SPAM_CIRCUIT_OPEN" in capsys.readouterr().out


def test_once_does_not_replace_a_custom_input_after_no_work(tmp_path: Path) -> None:
    input_path = tmp_path / "jobs.json"
    profile = tmp_path / "profile.json"
    pool = tmp_path / "pool.json"
    launcher = tmp_path / "launcher.py"
    input_path.write_text("[]", encoding="utf-8")
    profile.write_text("{}", encoding="utf-8")
    pool.write_text(json.dumps(["candidate@example.test"]), encoding="utf-8")
    launcher.write_text("", encoding="utf-8")

    with (
        patch.object(worker, "_seed_platform_input", return_value=0) as seed_input,
        patch.object(worker, "process_one", return_value="no_work"),
        patch.object(worker, "_refresh_jobs") as refresh_jobs,
    ):
        exit_code = worker.main(
            [
                "--input",
                str(input_path),
                "--profile",
                str(profile),
                "--email-pool",
                str(pool),
                "--launcher",
                str(launcher),
                "--state",
                str(tmp_path / "state.json"),
                "--once",
            ],
            ats_platform="ashby",
        )

    assert exit_code == 1
    seed_input.assert_called_once_with(input_path, "ashby")
    refresh_jobs.assert_not_called()


def test_rolling_application_limit_caps_provider_volume() -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    state = {
        "jobs": {
            str(index): {
                "application_started_at": (now - timedelta(hours=index)).isoformat(),
            }
            for index in range(1, 4)
        }
    }

    remaining, observed = worker._application_rate_limit_remaining(
        state,
        now=now,
        window_seconds=24 * 60 * 60,
        limit=3,
    )
    assert observed == 3
    assert remaining == 21 * 60 * 60
    assert worker._application_rate_limit_remaining(
        state,
        now=now,
        window_seconds=24 * 60 * 60,
        limit=4,
    ) == (0, 3)


def test_sleep_between_cycles_handles_service_stop_without_traceback() -> None:
    with patch.object(worker.time, "sleep", side_effect=KeyboardInterrupt):
        assert worker._sleep_between_cycles(120, "ashby") is False


def test_installed_lever_engine_is_accepted_without_static_provider_registry() -> None:
    assert worker._validate_platform(" Lever ") == "lever"
    parser = worker.build_parser("lever")
    args = parser.parse_args(["--once"])

    assert args.once is True
    assert args.input.name == "continuous_lever_jobs.json"
    assert args.state.name == "continuous_lever_state.json"


def test_ashby_uses_conservative_provider_pacing_and_rejection_cooldown() -> None:
    args = worker.build_parser("ashby").parse_args(["--once"])

    assert args.sleep_min_seconds == 900
    assert args.sleep_max_seconds == 1800
    assert args.application_limit == 12
    assert args.application_window_seconds == 86_400
    assert args.spam_rejection_cooldown_seconds == 86_400
    assert args.spam_rejection_threshold == 1


def test_invalid_or_missing_engine_platform_is_rejected() -> None:
    with patch.object(worker.importlib.util, "find_spec", return_value=None):
        with pytest.raises(ValueError, match="engine is not installed"):
            worker._validate_platform("futureats")
    with pytest.raises(ValueError, match="lowercase letters and digits"):
        worker._validate_platform("lever;stop")
