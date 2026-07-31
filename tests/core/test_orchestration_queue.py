"""Deterministic contracts for the orchestration and live queue boundaries."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core import (
    orchestrator,  # noqa: E402
    queue_runner,  # noqa: E402
)
from job_application_automation.core.contracts import EngineResult, EngineStatus  # noqa: E402
from job_application_automation.core.submission_log import (  # noqa: E402
    SubmissionLog,
    SubmissionRecord,
)


class EngineCommandContractTests(unittest.TestCase):
    def test_orchestrator_resolves_every_registered_engine_by_default(self) -> None:
        args = orchestrator._build_parser().parse_args([])

        engine_paths = orchestrator._resolve_engine_paths(args)

        self.assertEqual(set(engine_paths), set(orchestrator.DEFAULT_ENGINE_FILES))
        self.assertTrue(
            all(path == orchestrator.CLI_ENTRYPOINT.resolve() for path in engine_paths.values())
        )

    def test_orchestrator_accepts_a_phase_one_engine_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            custom_engine = Path(temporary_directory) / "custom_workable.py"
            custom_engine.write_text("print('custom')\n", encoding="utf-8")
            args = orchestrator._build_parser().parse_args(
                ["--workable-engine", str(custom_engine)]
            )

            engine_paths = orchestrator._resolve_engine_paths(args)

        self.assertEqual(engine_paths["workable"], custom_engine.resolve())

    def test_build_engine_command_uses_typed_request_fields_and_fill_only_precedence(self) -> None:
        engine = Path("engine_ashby.py")
        command = orchestrator.build_engine_command(
            engine,
            "https://jobs.ashbyhq.com/acme/123",
            Path("candidate.pdf"),
            "Acme",
            "Product Manager",
            "candidate@example.test",
            live_submit=True,
            headed=True,
            fill_only=True,
            dry_run=True,
        )

        self.assertEqual(
            command,
            [
                sys.executable,
                str(engine),
                "--url",
                "https://jobs.ashbyhq.com/acme/123",
                "--resume",
                "candidate.pdf",
                "--company",
                "Acme",
                "--role",
                "Product Manager",
                "--email",
                "candidate@example.test",
                "--fill-only",
                "--headed",
            ],
        )

    def test_default_engine_command_uses_the_unified_launcher_and_provider(self) -> None:
        command = orchestrator.build_engine_command(
            orchestrator.CLI_ENTRYPOINT,
            "https://jobs.ashbyhq.com/acme/123",
            Path("candidate.pdf"),
            "Acme",
            "Product Manager",
            "candidate@example.test",
            live_submit=False,
            headed=False,
            fill_only=False,
            dry_run=True,
        )

        self.assertEqual(
            command,
            [
                sys.executable,
                str(orchestrator.CLI_ENTRYPOINT),
                "engine",
                "ashby",
                "--url",
                "https://jobs.ashbyhq.com/acme/123",
                "--resume",
                "candidate.pdf",
                "--company",
                "Acme",
                "--role",
                "Product Manager",
                "--email",
                "candidate@example.test",
                "--dry-run",
            ],
        )

    def test_engine_command_passes_the_personalized_cover_letter(self) -> None:
        command = orchestrator.build_engine_command(
            orchestrator.CLI_ENTRYPOINT,
            "https://boards.greenhouse.io/acme/jobs/123",
            Path("candidate.pdf"),
            "Acme",
            "Product Manager",
            "candidate@example.test",
            live_submit=True,
            cover_letter_path=Path("cover_letter.pdf"),
        )

        self.assertEqual(
            command[command.index("--cover-letter") + 1],
            "cover_letter.pdf",
        )
        self.assertIn("--live-submit", command)


class ChildProcessRoutingTests(unittest.TestCase):
    def test_resume_generation_uses_the_unified_resume_command(self) -> None:
        class ResumeRunner:
            command: list[str] = []

            def run(self, command: list[str], _settings: object) -> orchestrator.CommandResult:
                self.command = command
                output = Path(command[command.index("--output") + 1])
                output.write_bytes(b"x" * 5001)
                return orchestrator.CommandResult(returncode=0, stdout="", stderr="")

        runner = ResumeRunner()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(orchestrator, "OUTPUT_DIR", Path(directory)):
                generated = orchestrator.generate_personalized_resume(
                    "Acme",
                    "Product Manager",
                    "https://jobs.ashbyhq.com/acme/123",
                    timeout_seconds=30,
                    email="candidate@example.test",
                    process_runner=runner,
                )

        self.assertIsNotNone(generated)
        self.assertEqual(
            [
                sys.executable,
                str(orchestrator.CLI_ENTRYPOINT),
                "resume",
                "--company",
                "Acme",
                "--role",
                "Product Manager",
                "--url",
                "https://jobs.ashbyhq.com/acme/123",
            ],
            runner.command[:9],
        )
        self.assertIn("--email", runner.command)

    def test_cover_letter_generation_uses_the_unified_command_and_promotes_audit(self) -> None:
        class CoverLetterRunner:
            command: list[str] = []

            def run(self, command: list[str], _settings: object) -> orchestrator.CommandResult:
                self.command = command
                output = Path(command[command.index("--output") + 1])
                output.write_bytes(b"x" * orchestrator.MIN_COVER_LETTER_BYTES)
                output.with_name(f"{output.stem}.audit.json").write_text(
                    json.dumps(
                        {
                            "validated": True,
                            "prompt_template_version": orchestrator.PROMPT_TEMPLATE_VERSION,
                        }
                    ),
                    encoding="utf-8",
                )
                return orchestrator.CommandResult(returncode=0, stdout="", stderr="")

        runner = CoverLetterRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text("{}", encoding="utf-8")
            with patch.object(orchestrator, "OUTPUT_DIR", root):
                generated = orchestrator.generate_personalized_cover_letter(
                    "Acme",
                    "Product Manager",
                    "https://apply.workable.com/acme/j/ABC123/",
                    "candidate@example.test",
                    profile,
                    timeout_seconds=30,
                    process_runner=runner,
                )

            self.assertIsNotNone(generated)
            assert generated is not None
            self.assertTrue(generated.is_file())
            self.assertTrue(generated.with_name(f"{generated.stem}.audit.json").is_file())
            self.assertEqual(
                [
                    sys.executable,
                    str(orchestrator.CLI_ENTRYPOINT),
                    "cover-letter",
                    "--company",
                    "Acme",
                    "--role",
                    "Product Manager",
                    "--url",
                    "https://apply.workable.com/acme/j/ABC123/",
                ],
                runner.command[:9],
            )
            self.assertEqual(
                runner.command[runner.command.index("--email") + 1],
                "candidate@example.test",
            )

    def test_queue_uses_the_unified_apply_command(self) -> None:
        confirmed = {
            "success": True,
            "status": EngineStatus.SUBMITTED_CONFIRMED.value,
            "ats": "ashby",
            "submitted": True,
            "confirmed": True,
            "test_mode": False,
        }
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue_path = root / "queue.txt"
            queue_path.write_text("https://jobs.ashbyhq.com/acme/123\n", encoding="utf-8")
            with (
                patch.object(queue_runner, "OUTPUT_DIR", root),
                patch.object(
                    queue_runner, "DEFAULT_QUEUE_PROGRESS_FILE", root / "queue_progress.json"
                ),
                patch.object(queue_runner.subprocess, "run", return_value=completed) as run,
                patch.object(queue_runner, "read_json", return_value=[confirmed]),
            ):
                exit_code = queue_runner.main(["--queue", str(queue_path)])

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [sys.executable, str(queue_runner.CLI_ENTRYPOINT), "apply"],
            run.call_args.args[0][:3],
        )

    def test_legacy_mode_flags_resolve_to_one_safe_typed_mode(self) -> None:
        cases = (
            ({"live_submit": True, "fill_only": True, "dry_run": True}, "--fill-only"),
            ({"live_submit": True, "fill_only": False, "dry_run": True}, "--live-submit"),
            ({"live_submit": False, "fill_only": False, "dry_run": True}, "--dry-run"),
            ({"live_submit": False, "fill_only": False, "dry_run": False}, "--dry-run"),
        )

        for flags, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(orchestrator._engine_mode_flag(**flags), expected)


class EngineResultParsingTests(unittest.TestCase):
    def test_malformed_provider_result_maps_to_a_safe_error_payload(self) -> None:
        process = orchestrator.ProcessResult(
            returncode=1,
            stdout='ENGINE_RESULT_JSON:{"success":"yes","status":"FAILED"}',
            stderr="provider failed",
        )

        parsed = orchestrator.parse_engine_result(process, live_submit=True)

        self.assertFalse(parsed["success"])
        self.assertEqual(parsed["status"], "INVALID_ENGINE_RESULT")
        self.assertIn("boolean", parsed["detail"])

    def test_structured_prefill_is_not_a_successful_live_submission(self) -> None:
        prefill = EngineResult(
            success=True,
            status=EngineStatus.PREFILLED_ONLY.value,
            ats="greenhouse",
            test_mode=False,
            extra={"screenshot": "prefilled.png"},
        )
        process = orchestrator.ProcessResult(
            returncode=0,
            stdout=f"diagnostic line\n{prefill.to_wire_line()}",
            stderr="",
        )

        dry_run = orchestrator.parse_engine_result(process, live_submit=False)
        live_submit = orchestrator.parse_engine_result(process, live_submit=True)

        self.assertTrue(dry_run["success"])
        self.assertEqual(dry_run["screenshot"], "prefilled.png")
        self.assertFalse(live_submit["success"])
        self.assertEqual(live_submit["status"], EngineStatus.PREFILLED_ONLY.value)
        self.assertFalse(live_submit["submitted"])
        self.assertFalse(live_submit["confirmed"])

    def test_structured_confirmed_submission_stays_successful_in_live_mode(self) -> None:
        confirmed = EngineResult(
            success=True,
            status=EngineStatus.SUBMITTED_CONFIRMED.value,
            ats="lever",
            submitted=True,
            confirmed=True,
            test_mode=False,
            extra={"confirmation_url": "https://jobs.lever.co/acme/123"},
        )
        process = orchestrator.ProcessResult(
            returncode=0,
            stdout=confirmed.to_wire_line(),
            stderr="",
        )

        parsed = orchestrator.parse_engine_result(process, live_submit=True)

        self.assertTrue(parsed["success"])
        self.assertTrue(parsed["submitted"])
        self.assertTrue(parsed["confirmed"])
        self.assertEqual(parsed["confirmation_url"], "https://jobs.lever.co/acme/123")


class OrchestrationPersistenceTests(unittest.TestCase):
    def test_result_snapshots_replace_atomically_without_temp_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "orchestration.json"
            initial = [{"status": "PREFILLED_ONLY", "success": True}]
            final = [
                {
                    "status": EngineStatus.SUBMITTED_CONFIRMED.value,
                    "success": True,
                    "company": "München Labs",
                }
            ]

            orchestrator._write_results(target, initial)
            orchestrator._write_results(target, final)

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), final)
            self.assertIn("München Labs", target.read_text(encoding="utf-8"))
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])


class QueueSafetyTests(unittest.TestCase):
    def test_orchestrator_submission_log_predicate_requires_exact_safe_result(self) -> None:
        confirmed = {
            "success": True,
            "status": EngineStatus.SUBMITTED_CONFIRMED.value,
            "ats": "ashby",
            "submitted": True,
            "confirmed": True,
            "test_mode": False,
        }
        prefilled = {
            **confirmed,
            "status": EngineStatus.PREFILLED_ONLY.value,
            "submitted": False,
            "confirmed": False,
        }

        self.assertTrue(orchestrator._is_confirmed_submission(confirmed))
        self.assertFalse(orchestrator._is_confirmed_submission(prefilled))
        self.assertFalse(orchestrator._is_confirmed_submission({**confirmed, "test_mode": True}))
        self.assertFalse(orchestrator._is_confirmed_submission({"status": "SUBMITTED & CONFIRMED"}))

    def test_live_orchestrator_skips_a_url_already_confirmed_in_the_ledger(self) -> None:
        job_url = "https://apply.workable.com/acme/j/ABC123/"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resume = root / "base.pdf"
            resume.write_bytes(b"x" * 5001)
            profile = root / "profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "candidate": {
                            "identity": {"first_name": "Jane", "last_name": "Doe"},
                            "contact": {"fallback_email": "fallback@example.test"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            pool = root / "emails.json"
            pool.write_text('["candidate@example.test"]', encoding="utf-8")
            ledger = SubmissionLog()
            ledger.record(
                SubmissionRecord(
                    company="Acme",
                    role="Product Manager",
                    job_url=job_url,
                    ats="workable",
                    status="SUBMITTED & CONFIRMED",
                    email_used="candidate@example.test",
                    resume_filename="personalized.pdf",
                )
            )
            ledger_path = root / "submission_log.json"
            ledger.save(ledger_path)
            engine = root / "engine.py"
            engine.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
            runner = MagicMock()

            results = orchestrator.run_orchestrator(
                {"workable": engine},
                None,
                resume,
                profile,
                root / "results.json",
                email_pool_path=pool,
                submission_log_path=ledger_path,
                live_submit=True,
                shuffle=False,
                process_runner=runner,
                direct_url=job_url,
                direct_company="Acme",
                direct_role="Product Manager",
            )

        self.assertEqual(results[0]["status"], "ALREADY_SUBMITTED")
        self.assertTrue(results[0]["confirmed"])
        self.assertFalse(results[0]["submitted"])
        runner.run.assert_not_called()

    def test_queue_rejects_invalid_indexes_and_timeouts_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "queue.txt"
            queue_path.write_text("https://jobs.ashbyhq.com/acme/123\n", encoding="utf-8")

            with self.assertRaises(SystemExit) as negative_index:
                queue_runner.main(["--queue", str(queue_path), "--start-index", "-1"])
            with self.assertRaises(SystemExit) as zero_timeout:
                queue_runner.main(["--queue", str(queue_path), "--timeout", "0"])

        self.assertEqual(2, negative_index.exception.code)
        self.assertEqual(2, zero_timeout.exception.code)

    def test_confirmed_submission_predicate_requires_exact_safe_result(self) -> None:
        confirmed = {
            "success": True,
            "status": EngineStatus.SUBMITTED_CONFIRMED.value,
            "ats": "ashby",
            "submitted": True,
            "confirmed": True,
            "test_mode": False,
        }
        prefilled = {
            "success": True,
            "status": EngineStatus.PREFILLED_ONLY.value,
            "ats": "ashby",
            "submitted": False,
            "confirmed": False,
            "test_mode": False,
        }

        self.assertTrue(queue_runner._confirmed_submission(confirmed))
        self.assertFalse(queue_runner._confirmed_submission(prefilled))
        self.assertFalse(queue_runner._confirmed_submission({**confirmed, "test_mode": True}))
        self.assertFalse(queue_runner._confirmed_submission({"success": "true"}))

    def test_queue_url_helpers_require_a_company_path_segment(self) -> None:
        url = "https://jobs.lever.co/acme-inc/123?source=queue"

        self.assertEqual(queue_runner._slug(url), "acme_inc_123")
        self.assertEqual(queue_runner._company_from_url(url), "acme-inc")
        with self.assertRaisesRegex(ValueError, "path segment"):
            queue_runner._slug("https://jobs.lever.co/")
        with self.assertRaisesRegex(ValueError, "path segment"):
            queue_runner._company_from_url("https://jobs.lever.co/")

    def test_queue_reports_an_unusable_url_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "queue.txt"
            # A path-less URL cannot yield either a slug or a company name.
            queue_path.write_text("https://jobs.lever.co/\n", encoding="utf-8")

            with patch("sys.stdout", new=io.StringIO()) as stdout:
                exit_code = queue_runner.main(["--queue", str(queue_path)])

        self.assertEqual(exit_code, 2)
        self.assertIn("QUEUE_STOP", stdout.getvalue())
        self.assertIn("path segment", stdout.getvalue())

    def test_queue_tolerates_a_non_object_orchestration_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "queue.txt"
            queue_path.write_text("https://jobs.lever.co/acme/123\n", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(queue_runner, "OUTPUT_DIR", Path(directory)),
                patch.object(
                    queue_runner, "DEFAULT_QUEUE_PROGRESS_FILE", Path(directory) / "p.json"
                ),
                patch.object(queue_runner.subprocess, "run", return_value=completed),
                # A malformed results file yields a bare string rather than an object.
                patch.object(queue_runner, "read_json", return_value=["not-an-object"]),
                patch("sys.stdout", new=io.StringIO()) as stdout,
            ):
                exit_code = queue_runner.main(["--queue", str(queue_path)])

        self.assertEqual(exit_code, 1)
        self.assertIn("status=NO_RESULT", stdout.getvalue())

    def test_queue_slug_cannot_escape_the_output_directory(self) -> None:
        slug = queue_runner._slug("https://jobs.lever.co/acme/..%2Foutside")

        self.assertEqual("acme_outside", slug)
        self.assertNotIn("/", slug)
        self.assertNotIn("\\", slug)
        self.assertNotIn("..", slug)


class AdditionalOrchestratorCoverageTests(unittest.TestCase):
    def test_random_job_emails_assigns_a_unique_pool_address_per_job(self) -> None:
        jobs = [{"row_number": number} for number in range(1, 4)]
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory) / "emails.json"
            pool.write_text(
                '["one@example.test", "two@example.test", "three@example.test"]',
                encoding="utf-8",
            )
            with patch.object(
                orchestrator.random,
                "sample",
                return_value=[
                    "three@example.test",
                    "one@example.test",
                    "two@example.test",
                ],
            ) as sample:
                selected = orchestrator._random_job_emails(
                    jobs,  # type: ignore[arg-type]
                    email_override="",
                    email_pool_path=pool,
                    prepared_resume_path=None,
                    fallback_email="fallback@example.test",
                )

        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(selected[0], "three@example.test")
        sample.assert_called_once()

    def test_personalized_document_paths_include_email_identity(self) -> None:
        first = orchestrator._personalized_resume_path(
            "Acme",
            "Product Manager",
            "https://apply.workable.com/acme/j/ABC123/",
            "first@example.test",
        )
        second = orchestrator._personalized_resume_path(
            "Acme",
            "Product Manager",
            "https://apply.workable.com/acme/j/ABC123/",
            "second@example.test",
        )

        self.assertNotEqual(first, second)

    def test_detect_ats_valid_and_invalid(self) -> None:
        self.assertEqual(orchestrator.detect_ats("https://jobs.ashbyhq.com/company/123"), "ashby")
        self.assertEqual(
            orchestrator.detect_ats("https://boards.greenhouse.io/company/jobs/123"), "greenhouse"
        )
        self.assertEqual(orchestrator.detect_ats("https://jobs.lever.co/company/123"), "lever")
        self.assertIsNone(orchestrator.detect_ats("https://example.com/careers"))
        self.assertIsNone(orchestrator.detect_ats(""))
        self.assertIsNone(orchestrator.detect_ats(123))  # type: ignore[arg-type]

    def test_process_timeout_error(self) -> None:
        err = orchestrator.ProcessTimeoutError(30, stdout="out", stderr="err")
        self.assertIn("30 seconds", str(err))
        self.assertEqual(err.timeout, 30)
        self.assertEqual(err.stdout, "out")
        self.assertEqual(err.stderr, "err")

    def test_find_header_success_and_error(self) -> None:
        headers = ["company_name", "job_title", "url_link"]
        self.assertEqual(
            orchestrator._find_header(headers, ("company", "company_name"), "company"), 0
        )
        self.assertEqual(orchestrator._find_header(headers, ("title", "job_title"), "role"), 1)
        with self.assertRaises(ValueError):
            orchestrator._find_header(headers, ("missing",), "missing")


if __name__ == "__main__":
    unittest.main()
