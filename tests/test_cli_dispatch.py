"""Tests for the single source-tree command dispatcher."""

from __future__ import annotations

import sys
import unittest
import importlib.util
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation import cli  # noqa: E402
from job_application_automation.core import paths  # noqa: E402


class UnifiedCliDispatchTests(unittest.TestCase):
    def test_internal_launcher_resolves_from_the_importable_module(self) -> None:
        spec = importlib.util.find_spec("job_automation")

        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.origin)
        self.assertEqual(Path(spec.origin), paths.CLI_ENTRYPOINT)

    def test_public_command_forwards_arguments_and_propagates_exit_code(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def resolve_main(module_name: str):
            def handler(arguments: list[str] | None) -> int:
                calls.append((module_name, list(arguments or [])))
                return 7

            return handler

        exit_code = cli.dispatch(
            ["search", "--role-type", "Product Manager"],
            resolve_main=resolve_main,
        )

        self.assertEqual(7, exit_code)
        self.assertEqual(
            [("job_application_automation.search.job_boards", ["--role-type", "Product Manager"])],
            calls,
        )

    def test_alias_and_internal_engine_dispatch_to_the_expected_handlers(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def resolve_main(module_name: str):
            def handler(arguments: list[str] | None) -> int:
                calls.append((module_name, list(arguments or [])))
                return 0

            return handler

        self.assertEqual(0, cli.dispatch(["orchestrate", "--dry-run"], resolve_main=resolve_main))
        self.assertEqual(
            0,
            cli.dispatch(
                ["engine", "greenhouse", "--url", "https://boards.greenhouse.io/acme/jobs/1"],
                resolve_main=resolve_main,
            ),
        )
        self.assertEqual(
            [
                ("job_application_automation.core.orchestrator", ["--dry-run"]),
                (
                    "job_application_automation.engines.greenhouse",
                    ["--url", "https://boards.greenhouse.io/acme/jobs/1"],
                ),
            ],
            calls,
        )

    def test_help_and_invalid_commands_have_safe_exit_codes(self) -> None:
        output = StringIO()
        errors = StringIO()

        self.assertEqual(0, cli.dispatch(["--help"], stdout=output, stderr=errors))
        self.assertIn("Public commands:", output.getvalue())
        self.assertEqual("", errors.getvalue())

        self.assertEqual(2, cli.dispatch(["unknown"], stdout=output, stderr=errors))
        self.assertIn("unknown command", errors.getvalue())

        output = StringIO()
        self.assertEqual(0, cli.dispatch(["engine", "--help"], stdout=output, stderr=errors))
        self.assertIn("engine <ashby|greenhouse|lever>", output.getvalue())

        errors = StringIO()
        self.assertEqual(2, cli.dispatch(["engine", "workday"], stderr=errors))
        self.assertIn("unknown engine", errors.getvalue())

    def test_cover_letter_command_dispatches_to_its_module(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def resolve_main(module_name: str):
            def handler(arguments: list[str] | None) -> int:
                calls.append((module_name, list(arguments or [])))
                return 0

            return handler

        exit_code = cli.dispatch(
            ["cover-letter", "--company", "Example Co", "--role", "PM"],
            resolve_main=resolve_main,
        )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [
                (
                    "job_application_automation.resume.cover_letter",
                    ["--company", "Example Co", "--role", "PM"],
                )
            ],
            calls,
        )

    def test_documents_command_and_archive_alias_dispatch_lazily(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def resolve_main(module_name: str):
            def handler(arguments: list[str] | None) -> int:
                calls.append((module_name, list(arguments or [])))
                return 0

            return handler

        self.assertEqual(
            0,
            cli.dispatch(
                ["documents", "retrieve", "--url", "https://example.test/job"],
                resolve_main=resolve_main,
            ),
        )
        self.assertEqual(
            0,
            cli.dispatch(["archive", "store", "--help"], resolve_main=resolve_main),
        )
        self.assertEqual(
            [
                (
                    "job_application_automation.core.document_cli",
                    ["retrieve", "--url", "https://example.test/job"],
                ),
                (
                    "job_application_automation.core.document_cli",
                    ["store", "--help"],
                ),
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
