"""Guarded JazzHR browser-form application engine."""

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

ATS_NAME = "jazzhr"
SPEC = BrowserFormSpec(
    ats=ATS_NAME,
    display_name="JazzHR",
    profile_name="jazzhr-cdp-profile",
    apply_selectors=(
        "button#resumator-mobile-apply-button",
        'a:has-text("Apply Now")',
    ),
    first_name_selectors=(
        "input#resumator-firstname-value",
        'input[name="resumator-firstname-value"]',
    ),
    last_name_selectors=(
        "input#resumator-lastname-value",
        'input[name="resumator-lastname-value"]',
    ),
    full_name_selectors=('input[name="full_name"]', 'input[name="name"]'),
    email_selectors=(
        "input#resumator-email-value",
        'input[name="resumator-email-value"]',
        'input[type="email"]',
    ),
    phone_selectors=(
        "input#resumator-phone-value",
        'input[name="resumator-phone-value"]',
        'input[type="tel"]',
    ),
    headline_selectors=('input[name*="headline" i]', 'input[name*="title" i]'),
    linkedin_selectors=(
        "input#resumator-linkedin-value",
        'input[name*="linkedin" i]',
    ),
    github_selectors=('input[name*="github" i]',),
    portfolio_selectors=('input[name*="website" i]', 'input[name*="portfolio" i]'),
    resume_selectors=(
        "input#resumator-resume-value",
        'input[type="file"][name="resumator-resume-value"]',
    ),
    cover_letter_file_selectors=(
        'input[type="file"][name*="cover" i]',
        'input[type="file"][id*="cover" i]',
    ),
    cover_letter_text_selectors=(
        "textarea#resumator-coverletter-value",
        'textarea[name="resumator-coverletter-value"]',
    ),
    submit_selectors=(
        "a#resumator-submit-resume",
        'button:has-text("Submit Application")',
    ),
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
