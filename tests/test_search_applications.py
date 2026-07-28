from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core import search_applications  # noqa: E402


def _job(url: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "platform": "greenhouse",
        "company": "Example",
        "title": "Product Manager",
        "description": "Build useful products.",
        "job_url": url,
        "live_status": "live",
    }
    payload.update(overrides)
    return payload


def _confirmed_result() -> dict[str, object]:
    return {
        "success": True,
        "status": "SUBMITTED & CONFIRMED",
        "ats": "greenhouse",
        "submitted": True,
        "confirmed": True,
        "test_mode": False,
    }


class SearchApplicationTests(unittest.TestCase):
    def test_runner_initializes_empty_submission_and_failure_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "jobs.json").write_text("[]", encoding="utf-8")
            (root / "profile.json").write_text("{}", encoding="utf-8")

            exit_code = search_applications.main(
                [
                    "--input",
                    str(root / "jobs.json"),
                    "--profile",
                    str(root / "profile.json"),
                    "--submission-log",
                    str(root / "submission_log.json"),
                    "--state",
                    str(root / "state.json"),
                    "--results-dir",
                    str(root / "results"),
                    "--failure-report",
                    str(root / "failures.json"),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads((root / "submission_log.json").read_text(encoding="utf-8")),
                {},
            )
            self.assertEqual(
                json.loads((root / "failures.json").read_text(encoding="utf-8"))["failure_count"],
                0,
            )

    def test_eligibility_requires_live_supported_complete_unique_jobs(self) -> None:
        live = _job("https://boards.greenhouse.io/example/jobs/1")
        jobs = search_applications._eligible_jobs(
            [
                live,
                dict(live),
                _job("https://jobs.example.test/closed", live_status="closed"),
                _job("https://jobs.example.test/web", platform="web"),
                _job("https://jobs.example.test/no-description", description=""),
                _job("not-a-url"),
                None,
            ]
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_url"], live["job_url"])

    def test_confirmed_submission_log_urls_are_loaded_strictly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submissions.json"
            path.write_text(
                json.dumps(
                    {
                        "confirmed": {
                            "job_url": "https://boards.greenhouse.io/example/jobs/1?source=x",
                            "status": "SUBMITTED & CONFIRMED",
                        },
                        "unsafe": {
                            "job_url": "https://jobs.lever.co/example/2",
                            "status": "SUBMISSION_UNCONFIRMED",
                        },
                    }
                ),
                encoding="utf-8",
            )

            urls = search_applications._confirmed_urls(path)

        self.assertEqual(len(urls), 1)
        self.assertIn("https://boards.greenhouse.io/example/jobs/1", urls)

    def test_runner_records_failure_continues_and_never_retries_prior_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "jobs.json"
            profile = root / "profile.json"
            submission_log = root / "submissions.json"
            state = root / "state.json"
            results = root / "results"
            input_path.write_text(
                json.dumps(
                    [
                        _job("https://boards.greenhouse.io/example/jobs/1"),
                        _job("https://jobs.lever.co/example/2", platform="lever"),
                    ]
                ),
                encoding="utf-8",
            )
            profile.write_text("{}", encoding="utf-8")
            submission_log.write_text("{}", encoding="utf-8")

            calls = 0

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                nonlocal calls
                calls += 1
                result_path = Path(command[command.index("--results-file") + 1])
                if calls == 1:
                    result_path.write_text(
                        json.dumps(
                            [
                                {
                                    "success": False,
                                    "status": "SUBMISSION_UNCONFIRMED",
                                    "ats": "greenhouse",
                                    "submitted": True,
                                    "confirmed": False,
                                    "test_mode": False,
                                    "error": "confirmation page did not appear",
                                    "missing_fields": ["work authorization"],
                                }
                            ]
                        ),
                        encoding="utf-8",
                    )
                    return SimpleNamespace(
                        returncode=1,
                        stdout="engine output",
                        stderr="confirmation timeout",
                    )
                result_path.write_text(json.dumps([_confirmed_result()]), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            arguments = [
                "--input",
                str(input_path),
                "--profile",
                str(profile),
                "--launcher",
                "launcher.py",
                "--results-dir",
                str(results),
                "--submission-log",
                str(submission_log),
                "--state",
                str(state),
                "--failure-report",
                str(root / "failures.json"),
            ]
            with patch.object(search_applications.subprocess, "run", side_effect=fake_run) as run:
                self.assertEqual(search_applications.main(arguments), 1)
                self.assertEqual(run.call_count, 2)
                report = json.loads((root / "failures.json").read_text(encoding="utf-8"))
                self.assertEqual(report["failures"][0]["missing_fields"], ["work authorization"])
                self.assertEqual(report["failures"][0]["error"], "confirmation page did not appear")
                self.assertEqual(search_applications.main(arguments), 0)
                self.assertEqual(run.call_count, 2)

            saved = json.loads(state.read_text(encoding="utf-8"))
            failed = next(
                record for record in saved["jobs"].values() if record["status"] == "failed"
            )
            self.assertEqual(failed["result_status"], "SUBMISSION_UNCONFIRMED")
            self.assertEqual(failed["stderr_tail"], "confirmation timeout")
            latest = json.loads((root / "failures.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["failure_count"], 0)

    def test_runner_honors_independent_attempt_limit_for_each_ats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = []
            for platform, base in (
                ("greenhouse", "https://boards.greenhouse.io/example/jobs"),
                ("lever", "https://jobs.lever.co/example"),
                ("ashby", "https://jobs.ashbyhq.com/example"),
            ):
                jobs.extend(_job(f"{base}/{index}", platform=platform) for index in range(1, 4))
            (root / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
            (root / "profile.json").write_text("{}", encoding="utf-8")
            (root / "submissions.json").write_text("{}", encoding="utf-8")

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                result_path = Path(command[command.index("--results-file") + 1])
                result_path.write_text(json.dumps([_confirmed_result()]), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(search_applications.subprocess, "run", side_effect=fake_run) as run:
                exit_code = search_applications.main(
                    [
                        "--input",
                        str(root / "jobs.json"),
                        "--profile",
                        str(root / "profile.json"),
                        "--results-dir",
                        str(root / "results"),
                        "--submission-log",
                        str(root / "submissions.json"),
                        "--state",
                        str(root / "state.json"),
                        "--max-attempts-per-ats",
                        "2",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(run.call_count, 6)
            for call in run.call_args_list:
                command = call.args[0]
                self.assertIn("--live-submit", command)
                self.assertIn("--no-shuffle", command)
                self.assertEqual(command[command.index("--config") + 1], str(root / "profile.json"))

    def test_existing_confirmed_log_and_completed_state_are_both_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = "https://boards.greenhouse.io/example/jobs/1"
            second = "https://jobs.lever.co/example/2"
            (root / "jobs.json").write_text(
                json.dumps([_job(first), _job(second, platform="lever")]),
                encoding="utf-8",
            )
            (root / "profile.json").write_text("{}", encoding="utf-8")
            (root / "submissions.json").write_text(
                json.dumps(
                    {
                        "one": {
                            "job_url": first,
                            "status": "SUBMITTED & CONFIRMED",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "jobs": {
                            second: {
                                "status": "confirmed",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(search_applications.subprocess, "run") as run:
                exit_code = search_applications.main(
                    [
                        "--input",
                        str(root / "jobs.json"),
                        "--profile",
                        str(root / "profile.json"),
                        "--submission-log",
                        str(root / "submissions.json"),
                        "--state",
                        str(root / "state.json"),
                        "--results-dir",
                        str(root / "results"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
