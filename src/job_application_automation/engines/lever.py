"""Lever application engine built on the shared ATS engine foundation.

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. Lever Direct REST Endpoint Ingestion (`/postings/:id/apply` API):
   - Lever exposes a documented POST endpoint for submissions (`https://api.lever.co/v0/postings/{company}/{job_id}?mode=json`).
   - Construct a JSON payload containing base64 PDF strings and candidate profile metadata directly.
   - Benefit: Near-instantaneous response times (<300ms) with deterministic error responses (e.g. invalid file size or missing fields).

2. DOM Shadow Tree Traversal and Dynamic Input Heuristics Engine:
   - Implement a dynamic DOM tree navigator using Playwright's `evaluate` script API to evaluate custom web components and custom dropdown pickers unique to modern Lever templates.
   - Benefit: Handles custom screening questions and non-standard HTML controls without locator failures.

3. Parallel Multi-Account Lever Submission Engine:
   - Rotate applicant email alias tokens and IP proxies dynamically for high-volume enterprise application tracking testing.
==============================================================================
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ..core.engine_shared import (
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
    is_location_question,
    load_json_config,
    load_candidate_evidence,
    location_answer_candidates,
    orchestrated_config_path,
    resolve_candidate_email,
    open_chrome_session,
    navigate_reusing_tab,
    requested_live_mode,
    validate_ats_url,
    validate_nonempty_file,
    validate_required_fields,
)
from ..core.paths import OUTPUT_DIR, resolve_project_dir


ATS_NAME = "lever"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    return load_json_config(orchestrated_config_path())


def _context(control: Locator) -> str:
    try:
        # Lever renders some questions with the label as a preceding sibling
        # (.application-label) rather than nested inside the field, so that
        # case has to be checked explicitly before falling back to the
        # nearest ancestor container.
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
        # "✱" is Lever's visual required-field marker, not part of the label text.
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


def _upload_cover_letter(page: Page, cover_letter: Path) -> bool | None:
    """Upload a cover letter only when Lever exposes a matching file field."""
    inputs = page.locator('input[type="file"]')
    matched = False
    for index in range(inputs.count()):
        target = inputs.nth(index)
        try:
            context = " ".join(
                (
                    _context(target),
                    target.get_attribute("name") or "",
                    target.get_attribute("id") or "",
                    target.get_attribute("aria-label") or "",
                )
            ).lower()
            normalized = context.replace("_", " ").replace("-", " ")
            if "cover" not in normalized or "letter" not in normalized:
                continue
            matched = True
            target.set_input_files(str(cover_letter))
            return True
        except Exception:
            continue
    return False if matched else None


def _fill_location(page: Page, value: str) -> bool:
    location = first_visible(page.locator('input[name="location"], input#location-input'))
    if location is None:
        return False
    try:
        # Lever's location field is a React-controlled input; setting .value
        # directly (as Locator.fill would) doesn't register with React's
        # internal tracker, so the change goes through the native property
        # setter and synthetic input/change events are dispatched by hand.
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
        # Give the debounced autocomplete dropdown time to render before probing it.
        page.wait_for_timeout(500)
        suggestions = page.locator(
            '.dropdown-results > *, .location-dropdown li, [role="option"], .pac-item'
        )
        option = first_visible(suggestions)
        if option is not None:
            option.click()
    except Exception:
        pass
    try:
        if not location.input_value().strip():
            return False
        selected = page.locator('input[name="selectedLocation"]').first
        if selected.count() and not selected.input_value().strip():
            try:
                selected.evaluate("(el, value) => { el.value = value; }", value)
            except Exception:
                pass
        return bool(location.input_value().strip())
    except Exception:
        return False


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
            if option_label and _option_matches_variant(option_label, variant):
                control.select_option(label=option_label)
                return bool(control.input_value())
    return False


def _option_matches_variant(option_label: str, variant: str) -> bool:
    option = " ".join(option_label.lower().split())
    answer = " ".join(variant.lower().split())
    if option == answer or answer in option:
        return True
    # Lever nationality questions frequently use demonyms in the candidate
    # profile but country nouns in the dropdown (Indian -> India, Canadian ->
    # Canada, Australian -> Australia). Only accept an exact one-character
    # stem match so unrelated options cannot be selected.
    country_stems = {answer[:-1]} if answer.endswith("n") else set()
    if answer.endswith("ian"):
        country_stems.add(f"{answer[:-3]}a")
    if option in country_stems:
        return True
    duration_days = {
        "2 weeks": "15 days",
        "two weeks": "15 days",
        "4 weeks": "30 days",
        "four weeks": "30 days",
    }
    return duration_days.get(answer) == option


def _question_label(control: Locator) -> str:
    try:
        label = control.evaluate(
            """el => {
                const explicit = el.id
                    ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
                    : null;
                if (explicit?.innerText) return explicit.innerText;
                const field = el.closest('.application-field');
                const sibling = field?.previousElementSibling;
                if (sibling?.classList?.contains('application-label')) {
                    return sibling.innerText || '';
                }
                const root = el.closest(
                    '.application-question, .application-additional, li, label'
                );
                if (!root) return '';
                const copy = root.cloneNode(true);
                copy.querySelectorAll(
                    'option, input, select, textarea, .dropdown-results'
                ).forEach(node => node.remove());
                return copy.innerText || '';
            }"""
        )
    except Exception:
        label = _context(control)
    return re.sub(r"[\s?*✱]+$", "", " ".join(label.split())).strip()


def _lever_semantic_answer(
    label: str,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> Optional[str]:
    """Resolve common Lever questions whose labels are too terse for shared matchers."""
    normalized = " ".join(label.lower().split())
    language_match = re.fullmatch(r"(?:preferred\s+)?language\s*(\d+)", normalized)
    languages = profile.get("languages", ())
    if language_match and isinstance(languages, Sequence) and not isinstance(languages, str):
        index = int(language_match.group(1)) - 1
        if 0 <= index < len(languages):
            return str(languages[index]).strip() or None
    if re.search(r"\b(?:nationality|country of citizenship)\b", normalized):
        return str(profile.get("nationality") or profile.get("citizenship") or "").strip() or None
    if re.search(r"\bexpected compensation range\b", normalized):
        return str(rules.get("salary_expectation") or "").strip() or None
    if "current ctc" in normalized:
        return str(rules.get("current_salary") or "").strip() or None
    if "expected ctc" in normalized:
        return str(rules.get("salary_expectation") or "").strip() or None
    if re.search(r"\bhow soon\b.*\bjoin\b", normalized):
        return str(profile.get("available_start_date") or "").strip() or None
    return None


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
        if option_normalized == desired_normalized or option_normalized.startswith(
            f"{desired_normalized} "
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
    # Fields already covered by _fill_standard_fields/fill_required_consent,
    # or hidden Lever plumbing (accountId, origin, referer, etc.) that must
    # not be touched.
    standard_names = {
        "resume",
        "name",
        "email",
        "phone",
        "location",
        "selectedLocation",
        "org",
        "urls[LinkedIn]",
        "accountId",
        "origin",
        "referer",
        "timezone",
        "source",
        "h-captcha-response",
    }
    controls = page.locator(
        "select, textarea, input:not([type='hidden']):not([type='file']):"
        "not([type='submit']):not([type='button'])"
    )
    # Truncated to keep the essay-generation prompt within a reasonable size.
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
            # Lever's "which location are you applying for" dropdown lists the
            # office options already named in the posting header, so it is
            # answered from that context (_select_posting_location) instead of
            # the candidate's configured home location.
            posting_location_question = bool(
                re.search(r"\bwhich location\b.*\bapplying\b", normalized_label)
            )
            desired = configured_answer(label, profile, rules, eeo, field_matchers)
            if not desired:
                desired = _lever_semantic_answer(label, profile, rules)
            if posting_location_question:
                desired = None
            location_question = not posting_location_question and is_location_question(
                normalized_label
            )
            if location_question and not desired:
                desired = str(profile.get("location", ""))
            if "are you flexible" in normalized_label:
                desired = "Yes"
            if not desired and re.fullmatch(r"(?:legal\s+|full\s+)?name:?", normalized_label):
                desired = " ".join(
                    part
                    for part in (
                        str(profile.get("first_name", "")).strip(),
                        str(profile.get("last_name", "")).strip(),
                    )
                    if part
                )
            if not desired and re.fullmatch(r"(?:today'?s\s+)?date:?", normalized_label):
                desired = date.today().isoformat()
            if (
                not desired
                and "visa" in normalized_label
                and re.search(r"\b(?:hold|require|specify)\b", normalized_label)
            ):
                desired = "Employer-sponsored work authorization"
            tag = control.evaluate("el => el.tagName.toLowerCase()")
            success = False
            if tag == "select" and not desired and posting_location_question:
                success = _select_posting_location(page, control)
            if tag == "select" and desired:
                success = _select_option(control, label, desired, configured_variants)
                if not success and location_question:
                    # Country-scoped dropdowns cannot match the full
                    # "City, State, Country" string; widen progressively.
                    for fallback in location_answer_candidates(profile):
                        if _select_option(control, label, fallback, configured_variants):
                            success = True
                            break
                    if not success:
                        try:
                            success = bool(control.select_option(label="Any Other"))
                        except Exception:
                            success = False
                if not success and desired.lower() == "yes":
                    # Fallback for closed <select> elements where inner_text()
                    # can read back empty/stale option text; Playwright's own
                    # label lookup resolves the option correctly regardless.
                    try:
                        success = bool(control.select_option(label="Yes"))
                    except Exception:
                        success = False
            elif control_type in {"radio", "checkbox"}:
                handled.add(group)
                if desired:
                    success = any(
                        _fill_choice_group(page, control, variant)
                        for variant in answer_variants(label, desired, configured_variants)
                    )
            elif tag == "textarea" or control_type in {"text", "", "date"}:
                answer = desired
                if not answer and is_essay_question(label):
                    answer = _generate_essay(label, job_text, company, role, candidate_evidence)
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
    cover_letter: Path | None = None,
) -> dict[str, bool | None]:
    full_name = " ".join(
        part
        for part in (
            str(profile.get("first_name", "")).strip(),
            str(profile.get("last_name", "")).strip(),
        )
        if part
    )
    fields: dict[str, bool | None] = {
        "name": fill_first(page, ('input[name="name"]',), full_name),
        "email": fill_first(page, ('input[name="email"]', 'input[type="email"]'), email),
        "phone": fill_first(
            page, ('input[name="phone"]', 'input[type="tel"]'), str(profile.get("phone", ""))
        ),
        "location": _fill_location(page, str(profile.get("location", ""))),
        "current_company": fill_first(
            page, ('input[name="org"]',), str(profile.get("current_company", ""))
        ),
        "linkedin": fill_first(
            page,
            ('input[name="urls[LinkedIn]"]', 'input[name*="LinkedIn" i]'),
            str(profile.get("linkedin", "")),
        ),
        "portfolio": fill_first(
            page,
            ('input[name="urls[Portfolio]"]', 'input[name*="Portfolio" i]'),
            str(profile.get("portfolio", "") or profile.get("website", "")),
        ),
        "resume": _upload_resume(page, resume),
    }
    if cover_letter is not None:
        fields["cover_letter"] = _upload_cover_letter(page, cover_letter)
    return fields


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
            # Lever marks many custom questions as required only visually,
            # with a "✱" glyph in the surrounding text rather than the HTML
            # required attribute, so both signals have to be checked.
            explicitly_required = control.get_attribute(
                "required"
            ) is not None or "✱" in control.evaluate(
                """el => (el.closest(
                        '.application-question, .application-field, li'
                    ) || el.parentElement)?.innerText || ''"""
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
    challenge = page.locator('iframe[src*="captcha" i]:visible, [class*="captcha" i]:visible')
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
    cover_letter: Path | None = None,
) -> dict[str, Any]:
    if not validate_ats_url(url, ATS_NAME):
        raise ValueError("URL must be an absolute Lever HTTPS URL")
    resume = validate_nonempty_file(resume, "resume")
    if cover_letter is not None:
        cover_letter = validate_nonempty_file(cover_letter, "cover letter")
    profile = dict(config["candidate"])
    email = resolve_candidate_email(profile, email_override)

    timeout = int(config.get("navigation_timeout_ms", 30_000))
    screenshot_dir = resolve_project_dir(
        config.get("download_root", OUTPUT_DIR),
        OUTPUT_DIR,
    )
    candidate_evidence = load_candidate_evidence(config)
    with sync_playwright() as playwright:
        # Lever job postings and their application forms are separate pages;
        # the form only lives at the "/apply" path off the posting URL.
        parsed_url = urlparse(url)
        apply_path = parsed_url.path.rstrip("/")
        if not apply_path.endswith("/apply"):
            apply_path += "/apply"
        apply_url = parsed_url._replace(path=apply_path).geturl()
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

            fields = _fill_standard_fields(
                page,
                profile,
                email,
                resume,
                cover_letter,
            )
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
            screenshot = capture_screenshot(page, screenshot_dir, company or "Lever", "prefilled")
            critical_missing = [key for key in ("name", "email", "resume") if not fields.get(key)]
            if fields.get("cover_letter") is False:
                critical_missing.append("cover_letter")
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
                status = "SUBMITTED & CONFIRMED" if confirmed else "SUBMISSION_UNCONFIRMED"
                success = confirmed
                screenshot = capture_screenshot(
                    page, screenshot_dir, company or "Lever", "submitted_verified"
                )

            confirmed = status == "SUBMITTED & CONFIRMED"
            submitted = status in {"SUBMITTED & CONFIRMED", "SUBMISSION_UNCONFIRMED"}
            return {
                "success": success,
                "status": status,
                "ats": ATS_NAME,
                "submitted": submitted,
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
            cover_letter=(
                Path(args.cover_letter).expanduser().resolve() if args.cover_letter else None
            ),
            email_override=args.email,
            config=_load_config(),
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
