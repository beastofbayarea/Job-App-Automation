"""Deterministic contracts for the orchestration and live queue boundaries."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import orchestrator as orchestrator_facade  # noqa: E402
import queue_runner as queue_runner_facade  # noqa: E402
from job_application_automation import orchestrator as orchestrator_impl  # noqa: E402
from job_application_automation import queue_runner as queue_runner_impl  # noqa: E402
from job_application_automation.contracts import EngineResult, EngineStatus  # noqa: E402


class CompatibilityFacadeTests(unittest.TestCase):
    def test_src_facades_expose_the_internal_workflow_modules(self) -> None:
        self.assertIs(orchestrator_facade, orchestrator_impl)
        self.assertIs(queue_runner_facade, queue_runner_impl)
        self.assertIs(
            orchestrator_facade.build_engine_command, orchestrator_impl.build_engine_command
        )
        self.assertIs(
            queue_runner_facade._confirmed_submission, queue_runner_impl._confirmed_submission
        )


class EngineCommandContractTests(unittest.TestCase):
    def test_build_engine_command_uses_typed_request_fields_and_fill_only_precedence(self) -> None:
        engine = Path("engine_ashby.py")
        command = orchestrator_facade.build_engine_command(
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

    def test_legacy_mode_flags_resolve_to_one_safe_typed_mode(self) -> None:
        cases = (
            ({"live_submit": True, "fill_only": True, "dry_run": True}, "--fill-only"),
            ({"live_submit": True, "fill_only": False, "dry_run": True}, "--live-submit"),
            ({"live_submit": False, "fill_only": False, "dry_run": True}, "--dry-run"),
            ({"live_submit": False, "fill_only": False, "dry_run": False}, "--dry-run"),
        )

        for flags, expected in cases:
            with self.subTest(flags=flags):
                self.assertEqual(orchestrator_impl._engine_mode_flag(**flags), expected)


class EngineResultParsingTests(unittest.TestCase):
    def test_malformed_provider_result_maps_to_a_safe_error_payload(self) -> None:
        process = orchestrator_impl.ProcessResult(
            returncode=1,
            stdout='ENGINE_RESULT_JSON:{"success":"yes","status":"FAILED"}',
            stderr="provider failed",
        )

        parsed = orchestrator_impl.parse_engine_result(process, live_submit=True)

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
        process = orchestrator_impl.ProcessResult(
            returncode=0,
            stdout=f"diagnostic line\n{prefill.to_wire_line()}",
            stderr="",
        )

        dry_run = orchestrator_impl.parse_engine_result(process, live_submit=False)
        live_submit = orchestrator_impl.parse_engine_result(process, live_submit=True)

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
        process = orchestrator_impl.ProcessResult(
            returncode=0,
            stdout=confirmed.to_wire_line(),
            stderr="",
        )

        parsed = orchestrator_facade.parse_engine_result(process, live_submit=True)

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

            orchestrator_facade._write_results(target, initial)
            orchestrator_facade._write_results(target, final)

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), final)
            self.assertIn("München Labs", target.read_text(encoding="utf-8"))
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])


class QueueSafetyTests(unittest.TestCase):
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

        self.assertTrue(queue_runner_facade._confirmed_submission(confirmed))
        self.assertFalse(queue_runner_facade._confirmed_submission(prefilled))
        self.assertFalse(queue_runner_facade._confirmed_submission({"success": "true"}))

    def test_queue_url_helpers_require_a_company_path_segment(self) -> None:
        url = "https://jobs.lever.co/acme-inc/123?source=queue"

        self.assertEqual(queue_runner_impl._slug(url), "acme_inc_123")
        self.assertEqual(queue_runner_impl._company_from_url(url), "acme-inc")
        with self.assertRaisesRegex(ValueError, "path segment"):
            queue_runner_impl._slug("https://jobs.lever.co/")
        with self.assertRaisesRegex(ValueError, "path segment"):
            queue_runner_impl._company_from_url("https://jobs.lever.co/")


if __name__ == "__main__":
    unittest.main()
