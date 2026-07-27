"""Lever application engine built on the shared ATS engine foundation."""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ats_application_engine_common import (
    SENSITIVE_FIELD_PATTERN,
    answer_variants,
    build_engine_parser,
    capture_screenshot,
    configured_answer,
    confirmation_visible,
    emit_engine_result,
    require_orchestrated_invocation,
    fill_first,
    fill_required_consent,
    first_visible,
    generate_essay_answer as _generate_essay,
    is_essay_question,
    load_json_config,
    open_chrome_session,
    navigate_reusing_tab,
    requested_live_mode,
    validate_ats_url,
    validate_nonempty_file,
    validate_required_fields,
    valid_email,
)
from project_paths import CONFIG_DIR, DATA_DIR, OUTPUT_DIR, resolve_project_dir


ATS_NAME = "lever"
DEFAULT_CONFIG = CONFIG_DIR / "candidate_profile_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config(path: Optional[Path]) -> dict[str, Any]:
    return load_json_config(path or DEFAULT_CONFIG)


def _context(control: Locator) -> str:
    try:
        text = control.evaluate(
            """el => {
                const field = el.closest('.application-field');
                const siblingLabel = field?.previousElementSibling;
                if (siblingLabel?.classList?.contains('application-label')) {
                    return `${siblingLabel.innerText || ''} ${field.innerText || ''}`;
                }
                const root = el.closest(
                    '.application-question, .application-additional, li, label'
                ) || el.parentElement;
                return root?.innerText || '';
            }"""
        )
        return " ".join(str(text).split()).replace("✱", "").strip()
    except Exception:
        return ""


def _upload_resume(page: Page, resume: Path) -> bool:
    target = page.locator('input[type="file"][name="resume"], input#resume-upload-input').first
    if not target.count():
        target = page.locator('input[type="file"]').first
    if not target.count():
        return False
    try:
        target.set_input_files(str(resume))
        return True
    except Exception:
        return False


def _fill_location(page: Page, value: str) -> bool:
    location = first_visible(page.locator('input[name="location"], input#location-input'))
    if location is None:
        return False
    try:
        location.evaluate(
            """(el, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            value,
        )
        page.wait_for_timeout(500)
        suggestions = page.locator(
            '.dropdown-results > *, .location-dropdown li, [role="option"], .pac-item'
        )
        option = first_visible(suggestions)
        if option is not None:
            option.click()
    except Exception:
        pass
    if not location.input_value().strip():
        return False
    selected = page.locator('input[name="selectedLocation"]').first
    if selected.count() and not selected.input_value().strip():
        try:
            selected.evaluate("(el, value) => { el.value = value; }", value)
        except Exception:
            pass
    return bool(location.input_value().strip())


def _select_option(
    control: Locator,
    label: str,
    desired: str,
    configured_variants: Mapping[str, Sequence[str]],
) -> bool:
    options = control.locator("option")
    variants = answer_variants(label, desired, configured_variants)
    for variant in variants:
        for index in range(options.count()):
            option = options.nth(index)
            option_label = option.inner_text().strip()
            if option_label and (
                option_label.lower() == variant.lower()
                or variant.lower() in option_label.lower()
            ):
                control.select_option(label=option_label)
                return bool(control.input_value())
    return False


def _question_label(control: Locator) -> str:
    context = _context(control)
    if not context:
        return ""
    option_text = []
    if control.evaluate("el => el.tagName.toLowerCase()") == "select":
        option_text = [option.inner_text().strip() for option in control.locator("option").all()]
    label = context
    for option in option_text:
        label = label.replace(option, "")
    return re.sub(r"[\s?*✱]+$", "", " ".join(label.split())).strip()


def _fill_choice_group(page: Page, control: Locator, desired: str) -> bool:
    name = control.get_attribute("name") or ""
    group = page.locator(f'input[name="{name}"]') if name else control
    for index in range(group.count()):
        item = group.nth(index)
        option_context = item.evaluate(
            """el => {
                const explicit = el.id
                    ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
                    : null;
                const wrapping = el.closest('label');
                return (
                    explicit?.innerText ||
                    wrapping?.innerText ||
                    el.getAttribute('value') ||
                    ''
                ).trim();
            }"""
        )
        desired_normalized = " ".join(desired.lower().split())
        option_normalized = " ".join(str(option_context).lower().split())
        if (
            option_normalized == desired_normalized
            or option_normalized.startswith(f"{desired_normalized} ")
        ):
            item.check(force=True)
            return item.is_checked()
    return False


def _select_posting_location(page: Page, control: Locator) -> bool:
    """Select the most specific offered location named in the posting header."""
    posting_context = " ".join(
        part.strip()
        for part in page.locator(
            ".posting-categories, .posting-headline, .posting-header"
        ).all_inner_texts()
        if part.strip()
    ).lower()
    options = control.locator("option")
    matches: list[tuple[int, str]] = []
    for index in range(options.count()):
        option = options.nth(index)
        value = (option.get_attribute("value") or "").strip()
        label = option.inner_text().strip()
        if value and label and label.lower() in posting_context:
            matches.append((len(label), value))
    if not matches:
        return False
    _, value = max(matches)
    control.select_option(value=value)
    return bool(control.input_value().strip())


def _fill_custom_questions(
    page: Page,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
    eeo: Mapping[str, Any],
    field_matchers: Mapping[str, Sequence[str]],
    configured_variants: Mapping[str, Sequence[str]],
    company: str,
    role: str,
    candidate_evidence: str,
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    handled: set[str] = set()
    standard_names = {
        "resume", "name", "email", "phone", "location", "selectedLocation",
        "org", "urls[LinkedIn]", "accountId", "origin", "referer",
        "timezone", "source", "h-captcha-response",
    }
    controls = page.locator(
        "select, textarea, input:not([type='hidden']):not([type='file']):"
        "not([type='submit']):not([type='button'])"
    )
    job_text = page.locator("body").inner_text()[:30_000]
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            name = control.get_attribute("name") or ""
            if name in standard_names or name.startswith("urls["):
                continue
            control_type = (control.get_attribute("type") or "").lower()
            group = name or control.get_attribute("id") or f"control-{index}"
            if control_type in {"radio", "checkbox"} and group in handled:
                continue
            label = _question_label(control)
            if not label:
                continue
            normalized_label = label.lower()
            posting_location_question = bool(
                re.search(r"\bwhich location\b.*\bapplying\b", normalized_label)
            )
            desired = configured_answer(
                label, profile, rules, eeo, field_matchers
            )
            if posting_location_question:
                desired = None
            if not desired and re.search(
                r"\b(where.*(?:located|based)|current location)\b",
                normalized_label,
            ):
                desired = str(profile.get("location", ""))
            if "are you flexible" in normalized_label:
                desired = "Yes"
            if not desired and re.fullmatch(
                r"(?:legal\s+|full\s+)?name:?", normalized_label
            ):
                desired = " ".join(
                    part
                    for part in (
                        str(profile.get("first_name", "")).strip(),
                        str(profile.get("last_name", "")).strip(),
                    )
                    if part
                )
            if not desired and re.fullmatch(
                r"(?:today'?s\s+)?date:?", normalized_label
            ):
                desired = date.today().isoformat()
            if (
                not desired
                and "visa" in normalized_label
                and re.search(r"\b(?:hold|require|specify)\b", normalized_label)
            ):
                desired = "Employer-sponsored work authorization"
            tag = control.evaluate("el => el.tagName.toLowerCase()")
            success = False
            if (
                tag == "select"
                and not desired
                and posting_location_question
            ):
                success = _select_posting_location(page, control)
            if tag == "select" and desired:
                success = _select_option(
                    control, label, desired, configured_variants
                )
                if not success and desired.lower() == "yes":
                    try:
                        success = bool(control.select_option(label="Yes"))
                    except Exception:
                        success = False
            elif control_type in {"radio", "checkbox"}:
                handled.add(group)
                if desired:
                    success = any(
                        _fill_choice_group(page, control, variant)
                        for variant in answer_variants(
                            label, desired, configured_variants
                        )
                    )
            elif tag == "textarea" or control_type in {"text", "", "date"}:
                answer = desired
                if not answer and is_essay_question(label):
                    answer = _generate_essay(
                        label, job_text, company, role, candidate_evidence
                    )
                if answer:
                    control.fill(answer)
                    success = bool(control.input_value().strip())
            results[label] = success
        except Exception as exc:
            logger.debug("Lever custom question failed at %d: %s", index, exc)
    return results


def _fill_standard_fields(
    page: Page,
    profile: Mapping[str, Any],
    email: str,
    resume: Path,
) -> dict[str, bool]:
    full_name = " ".join(
        part for part in (
            str(profile.get("first_name", "")).strip(),
            str(profile.get("last_name", "")).strip(),
        ) if part
    )
    return {
        "name": fill_first(page, ('input[name="name"]',), full_name),
        "email": fill_first(page, ('input[name="email"]', 'input[type="email"]'), email),
        "phone": fill_first(page, ('input[name="phone"]', 'input[type="tel"]'), str(profile.get("phone", ""))),
        "location": _fill_location(page, str(profile.get("location", ""))),
        "current_company": fill_first(page, ('input[name="org"]',), str(profile.get("current_company", ""))),
        "linkedin": fill_first(page, ('input[name="urls[LinkedIn]"]', 'input[name*="LinkedIn" i]'), str(profile.get("linkedin", ""))),
        "portfolio": fill_first(
            page,
            ('input[name="urls[Portfolio]"]', 'input[name*="Portfolio" i]'),
            str(profile.get("portfolio", "") or profile.get("website", "")),
        ),
        "resume": _upload_resume(page, resume),
    }


def _required_issues(page: Page) -> list[str]:
    issues: list[str] = []
    controls = page.locator(
        "input[required], select[required], textarea[required], "
        ".application-field input, .application-question select"
    )
    checked_groups: set[str] = set()
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible() or (control.get_attribute("type") or "") == "hidden":
                continue
            context = _context(control)
            explicitly_required = (
                control.get_attribute("required") is not None
                or "✱" in control.evaluate(
                    """el => (el.closest(
                        '.application-question, .application-field, li'
                    ) || el.parentElement)?.innerText || ''"""
                )
            )
            if not explicitly_required:
                continue
            control_type = (control.get_attribute("type") or "").lower()
            if control_type in {"radio", "checkbox"}:
                name = control.get_attribute("name") or ""
                if name in checked_groups:
                    continue
                checked_groups.add(name)
                if name and not page.locator(f'input[name="{name}"]:checked').count():
                    issues.append(context or name)
            elif not control.input_value().strip():
                issues.append(context or control.get_attribute("name") or f"field-{index}")
        except Exception:
            continue
    return issues


def _captcha_present(page: Page) -> bool:
    challenge = page.locator(
        'iframe[src*="captcha" i]:visible, [class*="captcha" i]:visible'
    )
    return bool(challenge.count())


def run(
    *,
    url: str,
    resume: Path,
    email_override: str,
    config: Mapping[str, Any],
    company: str,
    role: str,
    live_submit: bool,
) -> dict[str, Any]:
    if not validate_ats_url(url, ATS_NAME):
        raise ValueError("URL must be an absolute Lever HTTPS URL")
    resume = validate_nonempty_file(resume, "resume")
    profile = dict(config["candidate"])
    email = (
        email_override.strip()
        or str(profile.get("email_override", "")).strip()
        or str(profile.get("fallback_email", "")).strip()
    )
    if not valid_email(email):
        raise ValueError("a valid candidate email is required")

    timeout = int(config.get("navigation_timeout_ms", 30_000))
    screenshot_dir = resolve_project_dir(
        config.get("download_root", OUTPUT_DIR),
        OUTPUT_DIR,
    )
    evidence_path = Path(str(config.get("candidate_evidence_file", ""))).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = DATA_DIR / evidence_path
    candidate_evidence = (
        evidence_path.read_text(encoding="utf-8")
        if evidence_path.is_file()
        else ""
    )
    with sync_playwright() as playwright:
        apply_url = url.rstrip("/")
        if not apply_url.endswith("/apply"):
            apply_url += "/apply"
        session = open_chrome_session(
            playwright,
            profile_name="lever-cdp-profile",
            target_url=apply_url,
        )
        page = session.page
        page.set_default_timeout(int(config.get("action_timeout_ms", 14_000)))
        try:
            navigate_reusing_tab(
                page,
                apply_url,
                wait_until="domcontentloaded",
                timeout=timeout,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=timeout)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(2_000)

            fields = _fill_standard_fields(page, profile, email, resume)
            custom = _fill_custom_questions(
                page,
                profile,
                config.get("rules", {}),
                config.get("eeo_defaults", {}),
                config.get("field_matchers", {}),
                config.get("answer_variants", {}),
                company,
                role,
                candidate_evidence,
            )
            consent = fill_required_consent(page)
            missing = validate_required_fields(page, _required_issues)
            screenshot = capture_screenshot(
                page, screenshot_dir, company or "Lever", "prefilled"
            )
            critical_missing = [
                key for key in ("name", "email", "resume") if not fields.get(key)
            ]
            if critical_missing:
                status = "REQUIRED_FIELDS_NOT_FILLED"
                success = False
            elif not live_submit:
                status = "PREFILLED_ONLY"
                success = True
            elif missing:
                status = "REQUIRED_FIELDS_NOT_FILLED"
                success = False
            elif _captcha_present(page):
                status = "CAPTCHA_REQUIRED"
                success = False
            else:
                submit = first_visible(
                    page.locator(
                        'button.template-btn-submit, button:has-text("SUBMIT APPLICATION"), '
                        'input[type="submit"]'
                    )
                )
                if submit is None:
                    raise RuntimeError("submit button was not found")
                submit.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=timeout)
                except PlaywrightTimeoutError:
                    pass
                confirmed = confirmation_visible(page)
                status = (
                    "SUBMITTED & CONFIRMED"
                    if confirmed
                    else "SUBMISSION_UNCONFIRMED"
                )
                success = confirmed
                screenshot = capture_screenshot(
                    page, screenshot_dir, company or "Lever", "submitted_verified"
                )

            confirmed = status == "SUBMITTED & CONFIRMED"
            return {
                "success": success,
                "status": status,
                "ats": ATS_NAME,
                "submitted": confirmed,
                "confirmed": confirmed,
                "test_mode": not live_submit,
                "filled_fields": fields,
                "custom_questions": custom,
                "consent_fields": consent,
                "missing_critical": critical_missing,
                "missing_required": missing,
                "captcha_present": _captcha_present(page),
                "screenshot": screenshot,
            }
        finally:
            if session.close_browser_on_exit:
                session.browser.close()


def _parser() -> argparse.ArgumentParser:
    return build_engine_parser("Lever application automation engine")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    live_submit = requested_live_mode(args)
    try:
        require_orchestrated_invocation(args.url)
        result = run(
            url=args.url,
            resume=Path(args.resume),
            email_override=args.email,
            config=_load_config(args.config),
            company=args.company,
            role=args.role,
            live_submit=live_submit,
        )
    except Exception as exc:
        logger.exception("Lever engine failed")
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
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
