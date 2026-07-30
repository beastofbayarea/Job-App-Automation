"""Guarded shared browser-form runtime for the phase-one ATS engines."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from playwright.sync_api import Page, sync_playwright
from pypdf import PdfReader

from ..core.engine_shared import (
    build_engine_parser,
    capture_screenshot,
    confirmation_visible,
    emit_engine_result,
    fill_first,
    fill_required_consent,
    first_visible,
    load_json_config,
    navigate_reusing_tab,
    normalize_profile_config,
    open_chrome_session,
    orchestrated_config_path,
    page_has_captcha,
    require_orchestrated_invocation,
    requested_live_mode,
    resolve_candidate_email,
    validate_ats_job_url,
    validate_nonempty_file,
    validate_required_fields,
)
from ..core.paths import OUTPUT_DIR

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BrowserFormSpec:
    """Provider selectors and display metadata for one browser-form engine."""

    ats: str
    display_name: str
    profile_name: str
    apply_selectors: tuple[str, ...]
    first_name_selectors: tuple[str, ...]
    last_name_selectors: tuple[str, ...]
    full_name_selectors: tuple[str, ...]
    email_selectors: tuple[str, ...]
    phone_selectors: tuple[str, ...]
    headline_selectors: tuple[str, ...]
    linkedin_selectors: tuple[str, ...]
    github_selectors: tuple[str, ...]
    portfolio_selectors: tuple[str, ...]
    resume_selectors: tuple[str, ...]
    cover_letter_file_selectors: tuple[str, ...]
    cover_letter_text_selectors: tuple[str, ...]
    submit_selectors: tuple[str, ...]
    render_wait_ms: int = 0


@dataclass(frozen=True, slots=True)
class CandidateFields:
    first_name: str
    last_name: str
    email: str
    phone: str
    headline: str
    linkedin: str
    github: str
    portfolio: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(
    candidate: Mapping[str, Any],
    group: Mapping[str, Any],
    *keys: str,
) -> str:
    for source in (candidate, group):
        for key in keys:
            value = str(source.get(key, "") or "").strip()
            if value:
                return value
    return ""


def candidate_fields(
    config: Mapping[str, Any],
    email_override: Optional[str],
) -> CandidateFields:
    """Read both normalized flat keys and the source configuration groups."""
    candidate = _mapping(config.get("candidate"))
    identity = _mapping(candidate.get("identity"))
    contact = _mapping(candidate.get("contact"))
    first_name = _first_value(candidate, identity, "first_name")
    last_name = _first_value(candidate, identity, "last_name")
    email_profile = dict(contact)
    email_profile.update(candidate)
    email = resolve_candidate_email(email_profile, email_override or "")
    if not first_name or not last_name:
        raise ValueError("Missing required candidate profile identity fields.")
    return CandidateFields(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=_first_value(candidate, contact, "phone"),
        headline=_first_value(
            candidate,
            identity,
            "current_job_title",
            "current_title",
            "headline",
        ),
        linkedin=_first_value(candidate, contact, "linkedin", "linkedin_url"),
        github=_first_value(candidate, contact, "github", "github_url"),
        portfolio=_first_value(candidate, contact, "portfolio", "website"),
    )


def _first_visible_for(page: Page, selectors: Sequence[str]) -> Any | None:
    for selector in selectors:
        target = first_visible(page.locator(selector))
        if target is not None:
            return target
    return None


def _upload_first(page: Page, selectors: Sequence[str], path: Path) -> bool:
    """Upload to the first matching attached input, including hidden controls."""
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            control = locator.nth(index)
            try:
                control.set_input_files(str(path))
                return True
            except Exception:
                continue
    return False


def _document_text(path: Path) -> str:
    if path.suffix.casefold() == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    return path.read_text(encoding="utf-8").strip()


def _fill_cover_letter(page: Page, spec: BrowserFormSpec, path: Path) -> bool:
    if _upload_first(page, spec.cover_letter_file_selectors, path):
        return True
    text = _document_text(path)
    return bool(text and fill_first(page, spec.cover_letter_text_selectors, text))


def _required_issues(page: Page) -> list[str]:
    """Inspect native and visibly asterisk-marked required controls."""
    try:
        return list(
            page.locator("input, select, textarea").evaluate_all(
                """controls => {
                    const issues = [];
                    const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                    for (const control of controls) {
                        const type = (control.type || "").toLowerCase();
                        if (control.disabled || type === "hidden") continue;
                        const labels = Array.from(control.labels || [])
                            .map(label => clean(label.innerText))
                            .filter(Boolean);
                        const container = control.closest(
                            ".form-group, .field, [data-ui], [class*='field'], [class*='question']"
                        );
                        const context = clean(
                            labels.join(" ") || (container && container.innerText) ||
                            (control.parentElement && control.parentElement.innerText)
                        ).slice(0, 180);
                        const required = control.required ||
                            control.getAttribute("aria-required") === "true" ||
                            /(^|\\s)\\*(\\s|$)/.test(context);
                        if (!required && !control.matches(":invalid")) continue;
                        let missing = false;
                        if (type === "radio") {
                            const escaped = window.CSS && CSS.escape
                                ? CSS.escape(control.name)
                                : control.name.replace(/["\\\\]/g, "\\\\$&");
                            missing = !control.name ||
                                !document.querySelector(`input[type="radio"][name="${escaped}"]:checked`);
                        } else if (type === "checkbox") {
                            missing = !control.checked;
                        } else if (type === "file") {
                            missing = !control.files || control.files.length === 0;
                        } else {
                            const value = clean(control.value);
                            missing = !value || /^no answer$/i.test(value);
                        }
                        if (missing || !control.checkValidity()) {
                            issues.push(context || control.name || control.id || "Required field missing");
                        }
                    }
                    return Array.from(new Set(issues));
                }"""
            )
        )
    except Exception as exc:
        logger.warning("Required-field inspection failed: %s", exc)
        return ["Required-field inspection failed"]


def _fill_standard_fields(
    page: Page,
    spec: BrowserFormSpec,
    candidate: CandidateFields,
    resume: Path,
    cover_letter: Path | None,
) -> tuple[dict[str, bool], list[str]]:
    first_name = fill_first(page, spec.first_name_selectors, candidate.first_name)
    last_name = fill_first(page, spec.last_name_selectors, candidate.last_name)
    full_name = False
    if not (first_name and last_name):
        full_name = fill_first(page, spec.full_name_selectors, candidate.full_name)
    email = fill_first(page, spec.email_selectors, candidate.email)

    filled = {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "email": email,
        "phone": fill_first(page, spec.phone_selectors, candidate.phone),
        "headline": fill_first(page, spec.headline_selectors, candidate.headline),
        "linkedin": fill_first(page, spec.linkedin_selectors, candidate.linkedin),
        "github": fill_first(page, spec.github_selectors, candidate.github),
        "portfolio": fill_first(page, spec.portfolio_selectors, candidate.portfolio),
        "resume": _upload_first(page, spec.resume_selectors, resume),
        "cover_letter": (
            _fill_cover_letter(page, spec, cover_letter) if cover_letter is not None else False
        ),
    }
    missing_critical = []
    if not ((first_name and last_name) or full_name):
        missing_critical.append("candidate name")
    if not email:
        missing_critical.append("candidate email")
    if not filled["resume"]:
        missing_critical.append("resume")
    if cover_letter is not None and not filled["cover_letter"]:
        missing_critical.append("cover letter")
    return filled, missing_critical


def _result(
    *,
    spec: BrowserFormSpec,
    status: str,
    live_submit: bool,
    submitted: bool,
    confirmed: bool,
    filled_fields: Mapping[str, bool],
    consent_fields: Sequence[str],
    missing_critical: Sequence[str],
    missing_required: Sequence[str],
    captcha_present: bool,
    screenshot: str,
    preexisting_confirmation: bool = False,
) -> dict[str, Any]:
    return {
        "success": status in {"PREFILLED_ONLY", "SUBMITTED & CONFIRMED"},
        "status": status,
        "ats": spec.ats,
        "submitted": submitted,
        "confirmed": confirmed,
        "test_mode": not live_submit,
        "filled_fields": dict(filled_fields),
        "custom_questions": {},
        "consent_fields": list(consent_fields),
        "missing_critical": list(missing_critical),
        "missing_required": list(missing_required),
        "captcha_present": captcha_present,
        "preexisting_confirmation": preexisting_confirmation,
        "screenshot": screenshot,
    }


def run_browser_form_engine(
    spec: BrowserFormSpec,
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
    """Fill one configured provider form and submit only after all safety gates pass."""
    del headless, role
    if not validate_ats_job_url(url, spec.ats):
        raise ValueError(f"URL is not a job-specific {spec.display_name} URL: {url}")
    resume = validate_nonempty_file(resume, "resume")
    if cover_letter is not None:
        cover_letter = validate_nonempty_file(cover_letter, "cover letter")
    candidate = candidate_fields(config, email_override)

    with sync_playwright() as playwright:
        session = open_chrome_session(
            playwright,
            profile_name=spec.profile_name,
            target_url=url,
        )
        page = session.page
        try:
            navigate_reusing_tab(page, url, timeout=timeout)
            if spec.render_wait_ms:
                page.wait_for_timeout(spec.render_wait_ms)

            apply_button = _first_visible_for(page, spec.apply_selectors)
            if apply_button is not None:
                apply_button.click()
                page.wait_for_timeout(max(1000, spec.render_wait_ms))

            filled, missing_critical = _fill_standard_fields(
                page,
                spec,
                candidate,
                resume,
                cover_letter,
            )
            consent = fill_required_consent(page)
            captcha = page_has_captcha(page)
            missing_required = validate_required_fields(page, _required_issues)
            screenshot = capture_screenshot(
                page,
                screenshot_dir,
                company or spec.display_name,
                "prefill",
            )

            if missing_critical or missing_required:
                return _result(
                    spec=spec,
                    status="REQUIRED_FIELDS_NOT_FILLED",
                    live_submit=live_submit,
                    submitted=False,
                    confirmed=False,
                    filled_fields=filled,
                    consent_fields=consent,
                    missing_critical=missing_critical,
                    missing_required=missing_required,
                    captcha_present=captcha,
                    screenshot=screenshot,
                )
            if not live_submit:
                return _result(
                    spec=spec,
                    status="PREFILLED_ONLY",
                    live_submit=False,
                    submitted=False,
                    confirmed=False,
                    filled_fields=filled,
                    consent_fields=consent,
                    missing_critical=[],
                    missing_required=[],
                    captcha_present=captcha,
                    screenshot=screenshot,
                )
            if captcha:
                return _result(
                    spec=spec,
                    status="CAPTCHA_REQUIRED",
                    live_submit=True,
                    submitted=False,
                    confirmed=False,
                    filled_fields=filled,
                    consent_fields=consent,
                    missing_critical=[],
                    missing_required=[],
                    captcha_present=True,
                    screenshot=screenshot,
                )

            submit_button = _first_visible_for(page, spec.submit_selectors)
            if submit_button is None:
                return _result(
                    spec=spec,
                    status="SUBMIT_BUTTON_NOT_FOUND",
                    live_submit=True,
                    submitted=False,
                    confirmed=False,
                    filled_fields=filled,
                    consent_fields=consent,
                    missing_critical=[],
                    missing_required=[],
                    captcha_present=False,
                    screenshot=screenshot,
                )

            # Never treat success wording that was already present on the form
            # as proof of a new submission, and do not risk a duplicate click.
            if confirmation_visible(page):
                return _result(
                    spec=spec,
                    status="CONFIRMATION_PRESENT_BEFORE_SUBMIT",
                    live_submit=True,
                    submitted=False,
                    confirmed=False,
                    filled_fields=filled,
                    consent_fields=consent,
                    missing_critical=[],
                    missing_required=[],
                    captcha_present=False,
                    screenshot=screenshot,
                    preexisting_confirmation=True,
                )

            submit_button.click()
            submitted = True
            confirmed = False
            post_submit_captcha = False
            for _ in range(15):
                page.wait_for_timeout(1000)
                if page_has_captcha(page):
                    post_submit_captcha = True
                    break
                if confirmation_visible(page):
                    confirmed = True
                    break
            status = (
                "SUBMITTED & CONFIRMED"
                if confirmed
                else "CAPTCHA_REQUIRED"
                if post_submit_captcha
                else "SUBMISSION_UNCONFIRMED"
            )
            screenshot = capture_screenshot(
                page,
                screenshot_dir,
                company or spec.display_name,
                "submitted",
            )
            return _result(
                spec=spec,
                status=status,
                live_submit=True,
                submitted=submitted,
                confirmed=confirmed,
                filled_fields=filled,
                consent_fields=consent,
                missing_critical=[],
                missing_required=validate_required_fields(page, _required_issues),
                captcha_present=post_submit_captcha,
                screenshot=screenshot,
            )
        finally:
            if session.close_browser_on_exit:
                session.browser.close()


def engine_parser(spec: BrowserFormSpec) -> argparse.ArgumentParser:
    return build_engine_parser(f"{spec.display_name} application engine")


def load_orchestrated_config() -> dict[str, Any]:
    return normalize_profile_config(load_json_config(orchestrated_config_path()))


def main_for_browser_form_engine(
    spec: BrowserFormSpec,
    argv: Optional[Sequence[str]] = None,
) -> int:
    args = engine_parser(spec).parse_args(argv)
    live_submit = requested_live_mode(args)
    try:
        require_orchestrated_invocation(args.url)
        result = run_browser_form_engine(
            spec,
            url=args.url,
            resume=Path(args.resume).expanduser().resolve(),
            cover_letter=(
                Path(args.cover_letter).expanduser().resolve() if args.cover_letter else None
            ),
            email_override=args.email,
            config=load_orchestrated_config(),
            company=args.company,
            role=args.role,
            live_submit=live_submit,
            headless=not args.headed,
        )
    except Exception as exc:
        logger.exception("%s engine failed", spec.display_name)
        result = {
            "success": False,
            "status": "ENGINE_EXECUTION_ERROR",
            "ats": spec.ats,
            "submitted": False,
            "confirmed": False,
            "test_mode": not live_submit,
            "error": f"{type(exc).__name__}: {exc}",
        }
    emit_engine_result(result)
    return 0 if result.get("success") else 1
