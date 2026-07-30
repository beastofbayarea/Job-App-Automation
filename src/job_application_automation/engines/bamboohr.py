"""Guarded BambooHR browser-form application engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

from ..core.paths import OUTPUT_DIR
from ._browser_form import (
    BrowserFormSpec,
    engine_parser,
    main_for_browser_form_engine,
    run_browser_form_engine,
)

ATS_NAME = "bamboohr"
SPEC = BrowserFormSpec(
    ats=ATS_NAME,
    display_name="BambooHR",
    profile_name="bamboohr-cdp-profile",
    apply_selectors=(
        'button:has-text("Apply for This Job")',
        'a:has-text("Apply for This Job")',
        'button:has-text("Apply")',
    ),
    first_name_selectors=('input[name="firstName"]', "input#firstName"),
    last_name_selectors=('input[name="lastName"]', "input#lastName"),
    full_name_selectors=('input[name="fullName"]', "input#fullName"),
    email_selectors=('input[name="email"]', "input#email", 'input[type="email"]'),
    phone_selectors=('input[name="phone"]', "input#phone", 'input[type="tel"]'),
    headline_selectors=('input[name*="headline" i]', 'input[name*="title" i]'),
    linkedin_selectors=('input[name*="linkedin" i]',),
    github_selectors=('input[name*="github" i]',),
    portfolio_selectors=('input[name*="website" i]', 'input[name*="portfolio" i]'),
    resume_selectors=(
        'input[type="file"][name*="resume" i]',
        'input[type="file"][id*="resume" i]',
        'input[type="file"][name*="cv" i]',
    ),
    cover_letter_file_selectors=(
        'input[type="file"][name*="cover" i]',
        'input[type="file"][id*="cover" i]',
    ),
    cover_letter_text_selectors=(
        'textarea[name*="cover" i]',
        'textarea[id*="cover" i]',
    ),
    submit_selectors=(
        'button[type="submit"]:has-text("Submit Application")',
        'input[type="submit"][value*="Submit" i]',
    ),
    render_wait_ms=1500,
)


def run(
    *,
    url: str,
    resume: Path,
    cover_letter: Path | None = None,
    email_override: str | None = None,
    config: Mapping[str, Any],
    company: str = "",
    role: str = "",
    live_submit: bool = False,
    headless: bool = True,
    screenshot_dir: Path = OUTPUT_DIR,
    timeout: int = 30000,
) -> dict[str, Any]:
    return run_browser_form_engine(
        SPEC,
        url=url,
        resume=resume,
        cover_letter=cover_letter,
        email_override=email_override,
        config=config,
        company=company,
        role=role,
        live_submit=live_submit,
        headless=headless,
        screenshot_dir=screenshot_dir,
        timeout=timeout,
    )


def _parser():
    return engine_parser(SPEC)


def main(argv: Sequence[str] | None = None) -> int:
    return main_for_browser_form_engine(SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
