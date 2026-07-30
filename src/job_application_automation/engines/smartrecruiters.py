"""Guarded SmartRecruiters browser-form application engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..core.paths import OUTPUT_DIR
from ._browser_form import (
    BrowserFormSpec,
    engine_parser,
    main_for_browser_form_engine,
    run_browser_form_engine,
)

ATS_NAME = "smartrecruiters"
SPEC = BrowserFormSpec(
    ats=ATS_NAME,
    display_name="SmartRecruiters",
    profile_name="smartrecruiters-cdp-profile",
    apply_selectors=(
        "#st-apply",
        'a:has-text("I\'m interested")',
        'button:has-text("I\'m interested")',
    ),
    first_name_selectors=(
        'input[name="firstName"]',
        'input[name="first-name"]',
        'input[data-qa="first-name"]',
    ),
    last_name_selectors=(
        'input[name="lastName"]',
        'input[name="last-name"]',
        'input[data-qa="last-name"]',
    ),
    full_name_selectors=('input[name="full-name"]', 'input[name="name"]'),
    email_selectors=(
        'input[name="email"]',
        'input[type="email"]',
        'input[data-qa="email"]',
    ),
    phone_selectors=(
        'input[name="phone-number"]',
        'input[name="phone"]',
        'input[type="tel"]',
    ),
    headline_selectors=('input[name*="headline" i]', 'input[name*="title" i]'),
    linkedin_selectors=('input[name*="linkedin" i]', 'input[data-qa="linkedin"]'),
    github_selectors=('input[name*="github" i]',),
    portfolio_selectors=('input[name*="website" i]', 'input[name*="portfolio" i]'),
    resume_selectors=(
        'input[type="file"][name*="resume" i]',
        'input[type="file"][id*="resume" i]',
        'input[type="file"][data-qa*="resume" i]',
    ),
    cover_letter_file_selectors=(
        'input[type="file"][name*="cover" i]',
        'input[type="file"][id*="cover" i]',
    ),
    cover_letter_text_selectors=(
        'textarea[name*="cover" i]',
        'textarea[data-qa*="cover" i]',
    ),
    submit_selectors=(
        'button[type="submit"]:has-text("Submit")',
        'button[data-qa*="submit" i]',
    ),
    render_wait_ms=1500,
)


def run(
    *,
    url: str,
    resume: Path,
    cover_letter: Path | None = None,
    email_override: Optional[str] = None,
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    return main_for_browser_form_engine(SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
