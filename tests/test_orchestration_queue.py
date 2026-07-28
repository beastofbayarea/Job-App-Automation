"""Deterministic contracts for the orchestration and live queue boundaries."""

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

from job_application_automation.core import orchestrator  # noqa: E402
from job_application_automation.core import queue_runner  # noqa: E402
from job_application_automation.core.contracts import EngineResult, EngineStatus  # noqa: E402


class EngineCommandContractTests(unittest.TestCase):
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
        prefilled = {**confirmed, "status": EngineStatus.PREFILLED_ONLY.value,
                     "submitted": False, "confirmed": False}

        self.assertTrue(orchestrator._is_confirmed_submission(confirmed))
        self.assertFalse(orchestrator._is_confirmed_submission(prefilled))
        self.assertFalse(orchestrator._is_confirmed_submission({"status": "SUBMITTED & CONFIRMED"}))

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
        self.assertFalse(queue_runner._confirmed_submission({"success": "true"}))

    def test_queue_url_helpers_require_a_company_path_segment(self) -> None:
        url = "https://jobs.lever.co/acme-inc/123?source=queue"

        self.assertEqual(queue_runner._slug(url), "acme_inc_123")
        self.assertEqual(queue_runner._company_from_url(url), "acme-inc")
        with self.assertRaisesRegex(ValueError, "path segment"):
            queue_runner._slug("https://jobs.lever.co/")
        with self.assertRaisesRegex(ValueError, "path segment"):
            queue_runner._company_from_url("https://jobs.lever.co/")


if __name__ == "__main__":
    unittest.main()
