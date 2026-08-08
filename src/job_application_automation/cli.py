"""Unified command dispatcher for the public job-automation workflows.

The implementation package owns every workflow.  This module keeps command
selection lightweight and lazy so ``--help`` does not instantiate browser,
Gmail, or LLM dependencies.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Sequence
from typing import TextIO


CommandMain = Callable[[Sequence[str] | None], int]

COMMAND_MODULES = {
    "apply": "job_application_automation.core.orchestrator",
    "queue": "job_application_automation.core.queue_runner",
    "prefill-queue": "job_application_automation.core.local_prefill",
    "resume": "job_application_automation.resume.generate",
    "cover-letter": "job_application_automation.resume.cover_letter",
    "documents": "job_application_automation.core.document_cli",
    "search": "job_application_automation.search.job_boards",
    "gmail": "job_application_automation.mail.gmail_client",
    "email-pool": "job_application_automation.mail.pool_select",
    "dashboard": "job_application_automation.dashboard.server",
    "google-indexing": "job_application_automation.core.google_indexing",
    "continuous-ashby": "job_application_automation.core.continuous_ashby",
    "continuous-greenhouse": "job_application_automation.core.continuous_greenhouse",
    "continuous-lever": "job_application_automation.core.continuous_lever",
}
COMMAND_ALIASES = {
    "orchestrate": "apply",
    "archive": "documents",
    "email": "gmail",
    "ui": "dashboard",
}
ENGINE_MODULES = {
    "ashby": "job_application_automation.engines.ashby",
    "greenhouse": "job_application_automation.engines.greenhouse",
    "lever": "job_application_automation.engines.lever",
    "workable": "job_application_automation.engines.workable",
    "smartrecruiters": "job_application_automation.engines.smartrecruiters",
}


def _print_usage(stream: TextIO) -> None:
    print(
        "Usage: job_automation.py <command> [arguments]\n\n"
        "Public commands:\n"
        "  apply       Run the ATS-aware application workflow\n"
        "  queue       Run a sequential application queue\n"
        "  prefill-queue  Fill one JSON ATS queue locally without submitting\n"
        "  resume      Generate a personalised resume\n"
        "  cover-letter  Generate a one-page personalised cover letter\n"
        "  documents   Generate, store, or retrieve a private CV/cover-letter pair\n"
        "  search      Search supported ATS job boards\n"
        "  gmail       Read, export, draft, or send Gmail messages\n"
        "  email-pool  Select configured candidate email addresses\n"
        "  dashboard   Launch interactive web dashboard for output statistics\n"
        "  google-indexing  Submit a sitemap or eligible URL notifications to Google\n"
        "  continuous-ashby  Run the persistent one-job Ashby worker\n"
        "  continuous-greenhouse  Run the persistent one-job Greenhouse worker\n"
        "  continuous-lever  Run the persistent one-job Lever worker\n\n"
        "Internal command:\n"
        "  engine <provider>  Run an ATS engine for the orchestrator\n\n"
        "Use `job_automation.py <command> --help` for command-specific help.",
        file=stream,
    )


def _print_engine_usage(stream: TextIO) -> None:
    print(
        "Usage: job_automation.py engine <provider> [arguments]\n\n"
        "Supported providers: ashby, greenhouse, lever, workable, smartrecruiters.\n"
        "The application workflow invokes engines internally. For direct diagnostic help, "
        "use `job_automation.py engine <provider> --help`.",
        file=stream,
    )


def _load_main(module_name: str) -> CommandMain:
    module = importlib.import_module(module_name)
    handler = getattr(module, "main", None)
    if not callable(handler):
        raise RuntimeError(f"Command module has no callable main(): {module_name}")
    return handler


def dispatch(
    argv: Sequence[str],
    *,
    resolve_main: Callable[[str], CommandMain] = _load_main,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Route one command and forward the remaining arguments unchanged."""
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    arguments = list(argv)
    if not arguments:
        _print_usage(errors)
        return 2

    command = arguments.pop(0)
    if command in {"-h", "--help"}:
        _print_usage(output)
        return 0

    command = COMMAND_ALIASES.get(command, command)
    if command == "engine":
        if not arguments:
            choices = ", ".join(sorted(ENGINE_MODULES))
            print(f"engine requires one of: {choices}", file=errors)
            return 2
        if arguments[0] in {"-h", "--help"}:
            _print_engine_usage(output)
            return 0
        engine = arguments.pop(0).lower()
        module_name = ENGINE_MODULES.get(engine)
        if module_name is None:
            choices = ", ".join(sorted(ENGINE_MODULES))
            print(f"unknown engine {engine!r}; expected one of: {choices}", file=errors)
            return 2
        return int(resolve_main(module_name)(arguments))

    module_name = COMMAND_MODULES.get(command)
    if module_name is None:
        choices = ", ".join(sorted(COMMAND_MODULES))
        print(f"unknown command {command!r}; expected one of: {choices}", file=errors)
        return 2
    return int(resolve_main(module_name)(arguments))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the unified command-line interface."""
    return dispatch(sys.argv[1:] if argv is None else argv)
