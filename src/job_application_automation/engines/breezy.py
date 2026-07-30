"""Guarded Breezy HR browser-form application engine."""

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

ATS_NAME = "breezy"
SPEC = BrowserFormSpec(
    ats=ATS_NAME,
    display_name="Breezy HR",
    profile_name="breezy-cdp-profile",
    apply_selectors=(
        'a:has-text("Apply for this Job")',
        'button:has-text("Apply")',
        'a[href$="/apply"]',
    ),
    first_name_selectors=('input[name="first_name"]', 'input[name="firstName"]'),
    last_name_selectors=('input[name="last_name"]', 'input[name="lastName"]'),
    full_name_selectors=('input[name="cName"]', 'input[name="name"]'),
    email_selectors=(
        'input[name="cEmail"]',
        'input[name="email"]',
        'input[type="email"]',
    ),
    phone_selectors=(
        'input[name="cPhoneNumber"]',
        'input[name="phone"]',
        'input[type="tel"]',
    ),
    headline_selectors=('input[name*="headline" i]', 'input[name*="summary" i]'),
    linkedin_selectors=('input[name*="linkedin" i]',),
    github_selectors=('input[name*="github" i]',),
    portfolio_selectors=('input[name*="website" i]', 'input[name*="portfolio" i]'),
    resume_selectors=(
        "input#main-attachment",
        'input[type="file"][name="cResume"]',
        'input[type="file"][name*="resume" i]',
    ),
    cover_letter_file_selectors=(
        'input[type="file"][name*="cover" i]',
        'input[type="file"][id*="cover" i]',
    ),
    cover_letter_text_selectors=('textarea[name="cCoverLetter"]',),
    submit_selectors=(
        'button:has-text("Submit Application")',
        'input[type="submit"][value*="Submit" i]',
    ),
    render_wait_ms=1000,
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
