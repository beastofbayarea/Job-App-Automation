"""SmartRecruiters ATS application engine built on the shared engine foundation."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from playwright.sync_api import sync_playwright

from ..core.engine_shared import (
    build_engine_parser,
    capture_screenshot,
    confirmation_visible,
    emit_engine_result,
    fill_first,
    fill_required_consent,
    first_visible,
    load_json_config,
    open_chrome_session,
    orchestrated_config_path,
    page_has_captcha,
    require_orchestrated_invocation,
    requested_live_mode,
    resolve_candidate_email,
    validate_ats_url,
    validate_nonempty_file,
    validate_required_fields,
)
from ..core.paths import OUTPUT_DIR

ATS_NAME = "smartrecruiters"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    return load_json_config(orchestrated_config_path())


def _required_issues(page: Any) -> list[str]:
    try:
        elements = page.locator('input:invalid, select:invalid, textarea:invalid').all()
        return [el.inner_text().strip() or "Required field missing" for el in elements]
    except Exception:
        return []


def run(
    *,
    url: str,
    resume: Path,
    email_override: Optional[str] = None,
    config: Mapping[str, Any],
    company: str = "",
    role: str = "",
    live_submit: bool = False,
    headless: bool = True,
    screenshot_dir: Path = OUTPUT_DIR,
    timeout: int = 30000,
) -> dict[str, Any]:
    validate_ats_url(url, ATS_NAME)
    validate_nonempty_file(resume, "resume")

    candidate = config.get("candidate", {})
    identity = candidate.get("identity", {})
    contact = candidate.get("contact", {})

    first_name = str(identity.get("first_name", "")).strip()
    last_name = str(identity.get("last_name", "")).strip()
    full_name = f"{first_name} {last_name}".strip()
    email = resolve_candidate_email(config, email_override)
    phone = str(contact.get("phone", "")).strip()
    linkedin = str(contact.get("linkedin", "") or contact.get("linkedin_url", "")).strip()
    github = str(contact.get("github", "") or contact.get("github_url", "")).strip()
    portfolio = str(contact.get("portfolio", "") or contact.get("website", "")).strip()
    headline = str(identity.get("current_title", "") or identity.get("headline", "")).strip()

    if not first_name or not last_name or not email:
        raise ValueError("Missing required candidate profile identity or email fields.")

    fields = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "linkedin": linkedin,
        "github": github,
        "portfolio": portfolio,
        "headline": headline,
        "resume": str(resume),
    }

    with sync_playwright() as playwright:
        del headless
        session = open_chrome_session(
            playwright,
            profile_name="smartrecruiters-cdp-profile",
            target_url=url,
        )
        page = session.page

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            apply_btn = first_visible(
                page.locator('a[id*="st-apply"], button:has-text("Apply"), a:has-text("I\'m interested"), button:has-text("I\'m interested")')
            )
            if apply_btn:
                try:
                    apply_btn.click()
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

            filled_fn = fill_first(page, ['input[name="first-name"]', 'input[id*="first-name"]', 'input[name="firstName"]', 'input[id*="firstName"]', 'input[data-qa="first-name"]'], first_name)
            filled_ln = fill_first(page, ['input[name="last-name"]', 'input[id*="last-name"]', 'input[name="lastName"]', 'input[id*="lastName"]', 'input[data-qa="last-name"]'], last_name)
            if not filled_fn or not filled_ln:
                fill_first(page, ['input[name="name"]', 'input[name="full-name"]', 'input[id*="full-name"]'], full_name)

            fill_first(page, ['input[name="email"]', 'input[type="email"]', 'input[id*="email"]', 'input[data-qa="email"]'], email)
            if phone:
                fill_first(page, ['input[name="phone-number"]', 'input[name="phone"]', 'input[type="tel"]', 'input[id*="phone"]', 'input[data-qa="phone"]'], phone)
            if headline:
                fill_first(page, ['input[name*="headline"]', 'input[name*="title"]', 'input[id*="headline"]', 'textarea[name*="message"]'], headline)
            if linkedin:
                fill_first(page, ['input[name*="linkedin"]', 'input[id*="linkedin"]', 'input[data-qa="linkedin"]'], linkedin)
            if github:
                fill_first(page, ['input[name*="github"]', 'input[id*="github"]'], github)
            if portfolio:
                fill_first(page, ['input[name*="website"]', 'input[name*="portfolio"]'], portfolio)

            file_input = page.locator('input[type="file"]').first
            if file_input.count() > 0:
                file_input.set_input_files(str(resume))

            consent = fill_required_consent(page)
            captcha = page_has_captcha(page)
            missing_req = validate_required_fields(page, _required_issues)
            screenshot = capture_screenshot(page, screenshot_dir, company or "SmartRecruiters", "prefill")

            status = "PREFILLED_ONLY"
            confirmed = False
            submitted = False

            if live_submit:
                if captcha:
                    status = "CAPTCHA_REQUIRED"
                else:
                    submit_btn = first_visible(
                        page.locator('button[type="submit"], button:has-text("Submit"), button:has-text("Send")')
                    )
                    if submit_btn:
                        submit_btn.click()
                        page.wait_for_timeout(3000)
                        confirmed = confirmation_visible(page)
                        status = "SUBMITTED & CONFIRMED" if confirmed else "SUBMISSION_UNCONFIRMED"
                        submitted = True
                        screenshot = capture_screenshot(page, screenshot_dir, company or "SmartRecruiters", "submitted")

            return {
                "success": True if (not live_submit or confirmed) else False,
                "status": status,
                "ats": ATS_NAME,
                "submitted": submitted,
                "confirmed": confirmed,
                "test_mode": not live_submit,
                "filled_fields": fields,
                "custom_questions": {},
                "consent_fields": consent,
                "missing_critical": [],
                "missing_required": missing_req,
                "captcha_present": captcha,
                "screenshot": screenshot,
            }
        finally:
            if session.close_browser_on_exit:
                session.browser.close()


def _parser() -> argparse.ArgumentParser:
    return build_engine_parser("SmartRecruiters application engine")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    live_submit = requested_live_mode(args)
    headless = not getattr(args, "headed", False)
    try:
        require_orchestrated_invocation(args.url)
        result = run(
            url=args.url,
            resume=Path(args.resume),
            email_override=args.email,
            config=_load_config(),
            company=args.company,
            role=args.role,
            live_submit=live_submit,
            headless=headless,
        )
    except Exception as exc:
        logger.exception("SmartRecruiters engine failed")
        result = {
            "success": False,
            "status": "ENGINE_EXECUTION_ERROR",
            "ats": ATS_NAME,
            "submitted": False,
            "confirmed": False,
            "test_mode": not live_submit,
            "error": f"{type(exc).__name__}: {exc}",
        }
    emit_engine_result(result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
