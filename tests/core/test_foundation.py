"""Focused tests for dependency-free internal automation foundations."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core.adapters import (  # noqa: E402
    BrowserSettings,
    CommandResult,
    LLMSettings,
    ProcessRunner,
    ProcessSettings,
)
from job_application_automation.core.artifacts import read_json, write_csv, write_json  # noqa: E402
from job_application_automation.core.contracts import (  # noqa: E402
    ENGINE_RESULT_PREFIX,
    ATSEngine,
    EngineMode,
    EngineRequest,
    EngineResult,
    EngineStatus,
    result_from_legacy_payload,
)
from job_application_automation.core.exceptions import InputContractError  # noqa: E402
from job_application_automation.core.profile import AutomationProfile  # noqa: E402


class FakeEngine:
    def run(self, request: EngineRequest) -> EngineResult:
        return EngineResult(
            success=True,
            status=EngineStatus.PREFILLED_ONLY.value,
            ats=request.ats,
            test_mode=True,
        )


class FakeRunner:
    def run(self, command: list[str], settings: ProcessSettings) -> CommandResult:
        del command, settings
        return CommandResult(returncode=0, stdout="ok")


class ContractTests(unittest.TestCase):
    def test_request_serializes_with_existing_cli_names_and_round_trips(self) -> None:
        request = EngineRequest(
            ats="Greenhouse",
            url="https://boards.greenhouse.io/example/jobs/123",
            resume_path=Path("candidate.pdf"),
            cover_letter_path=Path("cover_letter.pdf"),
            company=" Example ",
            role=" Product Manager ",
            email=" applicant@example.test ",
            mode=EngineMode.FILL_ONLY,
            headed=True,
            metadata={"source": "queue"},
        )

        payload = request.to_payload()

        self.assertEqual(payload["ats"], "greenhouse")
        self.assertEqual(payload["resume"], "candidate.pdf")
        self.assertEqual(payload["cover_letter"], "cover_letter.pdf")
        self.assertEqual(payload["mode"], "fill-only")
        self.assertEqual(request.cli_arguments()[-2:], ("--fill-only", "--headed"))
        self.assertEqual(EngineRequest.from_payload(payload), request)

    def test_request_rejects_non_https_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            EngineRequest(
                ats="lever",
                url="http://jobs.lever.co/example",
                resume_path=Path("candidate.pdf"),
            )

    def test_result_round_trips_wire_format_and_preserves_provider_fields(self) -> None:
        result = EngineResult(
            success=True,
            status=EngineStatus.SUBMITTED_CONFIRMED.value,
            ats="ashby",
            submitted=True,
            confirmed=True,
            test_mode=False,
            extra={"screenshot": "proof.png", "filled_fields": {"email": True}},
        )

        parsed = EngineResult.from_wire_line(result.to_wire_line())

        self.assertTrue(result.to_wire_line().startswith(ENGINE_RESULT_PREFIX))
        self.assertEqual(parsed, result)
        self.assertTrue(parsed.is_confirmed_submission)
        self.assertEqual(parsed.extra["screenshot"], "proof.png")

    def test_test_mode_result_is_never_a_confirmed_submission(self) -> None:
        result = EngineResult(
            success=True,
            status=EngineStatus.SUBMITTED_CONFIRMED.value,
            ats="ashby",
            submitted=True,
            confirmed=True,
            test_mode=True,
        )

        self.assertFalse(result.is_confirmed_submission)

    def test_result_enforces_confirmation_safety_invariants(self) -> None:
        with self.assertRaisesRegex(ValueError, "also be submitted"):
            EngineResult(
                success=False,
                status="FAILED",
                ats="lever",
                confirmed=True,
            )
        with self.assertRaisesRegex(ValueError, "requires success"):
            EngineResult(
                success=False,
                status=EngineStatus.SUBMITTED_CONFIRMED.value,
                ats="lever",
                submitted=True,
                confirmed=True,
                test_mode=False,
            )
        with self.assertRaisesRegex(InputContractError, "pipeline-owned|reserved result fields"):
            EngineResult(
                success=False,
                status="FAILED",
                ats="lever",
                extra={"company": "wrong company"},
            )

        with self.assertRaisesRegex(InputContractError, "reserved result fields"):
            EngineResult.from_payload(
                {
                    "success": False,
                    "status": "FAILED",
                    "ats": "lever",
                    "resume": "wrong.pdf",
                }
            )

    def test_legacy_adapter_retains_unknown_status_and_defaults(self) -> None:
        result = result_from_legacy_payload(
            {"status": "FAILED: TimeoutError", "screenshot": "failure.png"},
            ats="greenhouse",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.known_status, None)
        self.assertEqual(result.ats, "greenhouse")
        self.assertEqual(result.extra["screenshot"], "failure.png")

    def test_protocols_accept_simple_injected_fakes(self) -> None:
        self.assertIsInstance(FakeEngine(), ATSEngine)
        self.assertIsInstance(FakeRunner(), ProcessRunner)


class AdapterSettingsTests(unittest.TestCase):
    def test_settings_validate_and_environment_is_copied(self) -> None:
        original = {"JOB_APP_CONFIG": "profile.json"}
        settings = ProcessSettings(timeout_seconds=42, cwd=Path("."), environment=original)
        original["JOB_APP_CONFIG"] = "changed.json"

        self.assertEqual(settings.timeout_seconds, 42)
        self.assertEqual(settings.environment["JOB_APP_CONFIG"], "profile.json")
        with self.assertRaises(TypeError):
            settings.environment["OTHER"] = "value"  # type: ignore[index]

    def test_llm_and_browser_settings_reject_invalid_values(self) -> None:
        self.assertEqual(LLMSettings(model=" gemini ", temperature=1).model, "gemini")
        self.assertEqual(BrowserSettings(timeout_ms=1).timeout_ms, 1)
        with self.assertRaisesRegex(ValueError, "between 0 and 2"):
            LLMSettings(model="test", temperature=2.1)
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            BrowserSettings(timeout_ms=0)


class ProfileTests(unittest.TestCase):
    def test_profile_preserves_unknown_fields_and_returns_legacy_runtime_mapping(self) -> None:
        profile = AutomationProfile.from_runtime_mapping(
            {
                "schema_version": 2,
                "candidate": {"first_name": "Shivam", "last_name": "Singh"},
                "rules": {"right_to_work": "Yes"},
                "provider_extension": {"custom": True},
            }
        )

        runtime = profile.to_runtime_mapping()

        self.assertEqual(runtime["candidate"]["first_name"], "Shivam")
        self.assertEqual(runtime["rules"]["right_to_work"], "Yes")
        self.assertEqual(runtime["provider_extension"], {"custom": True})
        with self.assertRaises(TypeError):
            profile.candidate["first_name"] = "Other"  # type: ignore[index]


class ArtifactTests(unittest.TestCase):
    def test_json_write_replaces_complete_content_without_temp_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "result.json"
            write_json(target, {"name": "Shivam", "attempt": 1})
            write_json(target, {"name": "Shivam", "attempt": 2})

            self.assertEqual(read_json(target), {"name": "Shivam", "attempt": 2})
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_csv_write_uses_first_seen_fields_and_escapes_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "exports" / "messages.csv"
            write_csv(
                target,
                [
                    {"message_id": "one", "subject": "Hello, world"},
                    {"message_id": "two", "label": "new"},
                ],
            )

            with target.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))

            self.assertEqual(rows[0]["subject"], "Hello, world")
            self.assertEqual(rows[1]["label"], "new")
            self.assertEqual(list(rows[1]), ["message_id", "subject", "label"])


if __name__ == "__main__":
    unittest.main()
