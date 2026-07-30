"""Guarded Workable browser-form application engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from ..core.paths import OUTPUT_DIR
from ._browser_form import (
    BrowserFormSpec,
    engine_parser,
    main_for_browser_form_engine,
    run_browser_form_engine,
)

ATS_NAME = "workable"
SPEC = BrowserFormSpec(
    ats=ATS_NAME,
    display_name="Workable",
    profile_name="workable-cdp-profile",
    apply_selectors=(
        'a[data-ui="apply-button"]',
        'button[data-ui="apply-button"]',
        'a:has-text("Apply for this job")',
        'button:has-text("Apply for this job")',
    ),
    first_name_selectors=('input[name="firstname"]', 'input[name="first_name"]'),
    last_name_selectors=('input[name="lastname"]', 'input[name="last_name"]'),
    full_name_selectors=('input[name="full_name"]', 'input[name="name"]'),
    email_selectors=('input[name="email"]', 'input[type="email"]'),
    phone_selectors=('input[name="phone"]', 'input[type="tel"]'),
    headline_selectors=('input[name="headline"]', 'input[name="summary"]'),
    linkedin_selectors=('input[name*="linkedin" i]',),
    github_selectors=('input[name*="github" i]',),
    portfolio_selectors=('input[name*="portfolio" i]', 'input[name*="website" i]'),
    resume_selectors=(
        'input[type="file"][data-ui="resume"]',
        'input[type="file"][name*="resume" i]',
        'input[type="file"][id*="resume" i]',
        'input[type="file"]',
    ),
    cover_letter_file_selectors=(
        'input[type="file"][data-ui*="cover" i]',
        'input[type="file"][name*="cover" i]',
        'input[type="file"][id*="cover" i]',
    ),
    cover_letter_text_selectors=('textarea[name="cover_letter"]',),
    submit_selectors=(
        'button[type="submit"]:has-text("Submit application")',
        'input[type="submit"][value*="Submit" i]',
    ),
    address_selectors=('input[name="address"]', "#address"),
    city_selectors=('input[name="city"]', "#city"),
    postcode_selectors=('input[name="postcode"]', "#postcode"),
    country_selectors=('input[name="country"]', "#country"),
    render_wait_ms=1500,
)


def _extract_workable_ids(url: str) -> tuple[str | None, str | None]:
    """Extract ``(company_shortcode, job_shortcode)`` from a Workable job URL."""
    parts = [part for part in urlparse(url).path.strip("/").split("/") if part]
    if len(parts) >= 3 and parts[1].casefold() in {"j", "jobs"}:
        return parts[0], parts[2]
    if len(parts) >= 2 and parts[0].casefold() in {"j", "jobs"}:
        return None, parts[1]
    return None, None


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
