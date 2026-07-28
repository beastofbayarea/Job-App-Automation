"""Greenhouse application engine with safe fill-only and explicit submit modes."""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ..core.engine_shared import (
    SENSITIVE_FIELD_PATTERN as SENSITIVE_EEO,
    answer_variants as _answer_variants,
    build_engine_parser,
    capture_screenshot as _screenshot_shared,
    configured_answer as _configured_answer,
    confirmation_visible as _confirmation_visible,
    emit_engine_result,
    require_orchestrated_invocation,
    fill_first as _fill,
    fill_labeled as _fill_labeled,
    fill_required_consent as _fill_consent,
    first_visible as _first_visible,
    generate_essay_answer as _generate_essay,
    is_essay_question as _is_essay_question,
    label_for as _label_for,
    load_json_config,
    load_candidate_evidence as _shared_candidate_evidence,
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
from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from ..mail.gmail_client import (
    fetch_messages,
    get_gmail_read_service,
    load_used_verification_message_ids,
    poll_for_verification_code,
    record_used_verification_message,
)

ATS_NAME = "greenhouse"
SUBMIT_BUTTON_TEXT_PATTERN = re.compile(
    r"submit application|envoyer.*candidature|postuler",
    re.I,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    return load_json_config(orchestrated_config_path())


def _valid_greenhouse_url(url: str) -> bool:
    return validate_ats_url(url, ATS_NAME)


def _security_challenge_visible(page: Page) -> bool:
    return page.locator('input[id^="security-input"]').count() >= 8


def _screenshot(page: Page, directory: Path, company: str, tag: str) -> str:
    result = _screenshot_shared(page, directory, company, tag)
    if result:
        logger.info("Screenshot: %s", result)
    return result


def _upload_resume(page: Page, resume: Path) -> bool:
    inputs = page.locator('input[type="file"]')
    for index in range(inputs.count()):
        target = inputs.nth(index)
        try:
            context = target.evaluate(
                """el => {
                    const root = el.closest('div') || el.parentElement;
                    return (root && root.innerText || '') + ' ' +
                           (el.name || '') + ' ' + (el.id || '');
                }"""
            ).lower()
            if (
                "resume" not in context
                and "resume" not in (target.get_attribute("name") or "").lower()
            ):
                continue
            target.set_input_files(str(resume))
            return True
        except Exception:
            continue

    if inputs.count() == 1:
        try:
            inputs.first.set_input_files(str(resume))
            return True
        except Exception:
            pass
    return False


def _open_application_form(page: Page, timeout: int, source_url: str, company: str) -> None:
    """Follow a custom career site's Apply CTA before filling Greenhouse fields."""
    if (
        _first_visible(
            page.locator('input[name="first_name"], input[type="email"], input[type="file"]')
        )
        is not None
    ):
        return
    apply_cta = _first_visible(
        page.get_by_role(
            "link",
            name=re.compile(r"^(?:apply|apply now|apply for this job)$", re.I),
        )
    ) or _first_visible(
        page.get_by_role(
            "button",
            name=re.compile(r"^(?:apply|apply now|apply for this job)$", re.I),
        )
    )
    if apply_cta is None:
        apply_cta = _first_visible(
            page.get_by_text(
                re.compile(r"^(?:apply|apply now|apply for this job)$", re.I),
                exact=True,
            )
        )
    if apply_cta is None:
        logger.info("No Apply CTA found on custom Greenhouse page")
    else:
        apply_cta.click()
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeoutError:
            pass
    if (
        _first_visible(
            page.locator('input[name="first_name"], input[type="email"], input[type="file"]')
        )
        is not None
    ):
        return
    parsed = urlparse(source_url)
    job_ids = parse_qs(parsed.query).get("gh_jid", [])
    if "greenhouse.io" in (parsed.hostname or "").lower() or not job_ids:
        return
    board = re.sub(r"[^a-z0-9]+", "", company.lower())
    if not board:
        return
    # Some custom career sites embed the Greenhouse widget via JS that can fail
    # to render (or the CTA above only linked out without loading the form).
    # The gh_jid query param plus the company's board slug is enough to build
    # the underlying Greenhouse embed URL directly and bypass the broken widget.
    embed_url = "https://job-boards.greenhouse.io/embed/job_app?" + urlencode(
        {"for": board, "token": job_ids[0]}
    )
    navigate_reusing_tab(page, embed_url, wait_until="domcontentloaded", timeout=timeout)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def _select_native(page: Page, label_pattern: str, preferred: Sequence[str]) -> bool:
    try:
        target = _first_visible(page.get_by_label(re.compile(label_pattern, re.IGNORECASE)))
    except Exception:
        target = None
    if target is None:
        return False
    try:
        options = target.locator("option")
        available = [
            ((options.nth(i).get_attribute("value") or ""), options.nth(i).inner_text())
            for i in range(options.count())
        ]
        for desired in preferred:
            for value, label in available:
                if desired.lower() in label.lower() and value:
                    target.select_option(value=value)
                    return True
    except Exception:
        pass
    return False


def _select_greenhouse_combobox(
    page: Page,
    control: Locator,
    preferred: Sequence[str],
) -> bool:
    """Select an option from Greenhouse's React-select combobox."""
    try:
        control.scroll_into_view_if_needed()
        control.click()
        # Some Greenhouse React-select variants do not mount their option
        # portal until keyboard navigation begins.
        option_locator = '[role="option"], [id*="-option-"]'
        if page.locator(option_locator).count() == 0:
            control.press("ArrowDown")
        try:
            page.locator(option_locator).first.wait_for(state="visible", timeout=2_000)
        except Exception:
            pass
        options = page.locator(option_locator)
        for desired in (item for item in preferred if item):
            for attempt in range(2):
                controlled_id = control.get_attribute("aria-controls") or ""
                if controlled_id:
                    listbox = page.locator(f"#{controlled_id}")
                    if listbox.count():
                        exact_text = listbox.get_by_text(desired, exact=True)
                        for index in range(exact_text.count()):
                            option = exact_text.nth(index)
                            if option.is_visible():
                                option.click()
                                return True
                        text_matches = listbox.get_by_text(re.compile(re.escape(desired), re.I))
                        for index in range(text_matches.count()):
                            option = text_matches.nth(index)
                            if option.is_visible():
                                option.click()
                                return True
                exact = page.get_by_role("option", name=desired, exact=True)
                for index in range(exact.count()):
                    option = exact.nth(index)
                    if option.is_visible():
                        option.click()
                        return True
                matches = options.filter(has_text=re.compile(re.escape(desired), re.I))
                # Fuzzy substring matching picks the wrong option for these values:
                # "Asian" also matches "Asian (Non-Hispanic)"-style compound race
                # options, and "San Francisco" matches multiple Bay Area location
                # entries. Require an exact match instead of falling through to
                # the fuzzy match below.
                exact_only = desired.lower() == "asian" or desired.lower().startswith(
                    "san francisco"
                )
                if not exact_only:
                    for index in range(matches.count()):
                        option = matches.nth(index)
                        if option.is_visible():
                            option.click()
                            return True
                if attempt == 0:
                    try:
                        control.fill(desired)
                        page.wait_for_timeout(500)
                        options = page.locator(option_locator)
                    except Exception:
                        break
            try:
                control.fill(desired)
                page.wait_for_timeout(400)
                control.press("ArrowDown")
                control.press("Enter")
                container_text = control.evaluate(
                    "el => (el.parentElement?.parentElement?.innerText || '').trim()"
                )
                if desired.lower() in container_text.lower():
                    return True
            except Exception:
                pass
        # Do not guess when no configured option matches.
        visible_options = []
        for index in range(options.count()):
            option = options.nth(index)
            if option.is_visible():
                visible_options.append(" ".join(option.inner_text().split()))
        visible_listboxes = []
        listboxes = page.locator('[role="listbox"]')
        for index in range(listboxes.count()):
            listbox = listboxes.nth(index)
            if listbox.is_visible():
                visible_listboxes.append(" ".join(listbox.inner_text().split())[:1000])
        logger.info(
            "No Greenhouse option matched preferred=%s available=%s control=%s expanded=%s controls=%s listboxes=%s listbox_text=%s",
            list(preferred),
            visible_options,
            control.get_attribute("id"),
            control.get_attribute("aria-expanded"),
            control.get_attribute("aria-controls"),
            page.locator('[role="listbox"]').count(),
            visible_listboxes,
        )
        control.press("Escape")
        control.press("Tab")
    except Exception as exc:
        logger.debug("Greenhouse combobox selection failed: %s", exc)
    return False


def _fill_radio_or_checkbox_group(
    page: Page,
    control: Locator,
    desired: str,
) -> bool:
    name = control.get_attribute("name")
    group = page.locator(
        f'input[name="{name}"]' if name else f'input[id="{control.get_attribute("id")}"]'
    )
    for index in range(group.count()):
        item = group.nth(index)
        item_id = item.get_attribute("id") or ""
        label = page.locator(f'label[for="{item_id}"]').first if item_id else None
        option_text = (
            " ".join(label.inner_text().split())
            if label is not None and label.count()
            else (item.get_attribute("value") or "")
        )
        if desired.lower() in option_text.lower():
            item.check(force=True)
            return item.is_checked()
    return False


def _load_candidate_evidence(config: Mapping[str, Any]) -> str:
    return _shared_candidate_evidence(config)


def _fill_custom_questions(
    page: Page,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
    eeo: Mapping[str, Any],
    field_matchers: Mapping[str, Sequence[str]],
    option_variants: Mapping[str, Sequence[str]],
    company: str,
    role: str,
    candidate_evidence: str,
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    job_text = page.locator("body").inner_text()[:30_000]
    controls = page.locator(
        'input[id^="question_"], textarea[id^="question_"], '
        'select[id^="question_"], input[role="combobox"][id^="question_"], '
        'input[role="combobox"][id^="degree"], '
        'input[role="combobox"][id^="school"], '
        'input[role="combobox"][id^="discipline"]'
    )
    handled_groups: set[str] = set()
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            control_id = control.get_attribute("id") or f"question-{index}"
            control_type = (control.get_attribute("type") or "").lower()
            group_key = control.get_attribute("name") or control_id
            if control_type in {"radio", "checkbox"} and group_key in handled_groups:
                continue
            label = _label_for(page, control)
            if not label:
                continue
            desired = _configured_answer(label, profile, rules, eeo, field_matchers)
            # Language proficiency is a configured selection policy. Other
            # experience and relocation answers are resolved by field_matchers.
            language_question = bool(re.search(r"\b(language|fluen|speak\s+\w+)\b", label, re.I))
            language_proficiency_question = bool(
                language_question
                and re.search(r"\b(proficien|language\s+level|fluency\s+level)\b", label, re.I)
            )
            if language_question and not language_proficiency_question:
                desired = "Yes"
            success = False
            role_name = control.get_attribute("role") or ""
            tag = control.evaluate("el => el.tagName.toLowerCase()")

            if role_name == "combobox":
                if desired:
                    success = _select_greenhouse_combobox(
                        page, control, _answer_variants(label, desired, option_variants)
                    )
                elif control.get_attribute("aria-required") == "true":
                    try:
                        control.click()
                        control.press("ArrowDown")
                        page.wait_for_timeout(400)
                        available = [
                            " ".join(page.get_by_role("option").nth(i).inner_text().split())
                            for i in range(page.get_by_role("option").count())
                            if page.get_by_role("option").nth(i).is_visible()
                        ]
                        logger.info(
                            "Unconfigured required combobox label=%r options=%s",
                            label,
                            available,
                        )
                        control.press("Escape")
                    except Exception as exc:
                        logger.debug(
                            "Required combobox option discovery failed for %r: %s",
                            label,
                            exc,
                        )
            elif tag == "select":
                if desired:
                    success = _select_native(
                        page, re.escape(label), _answer_variants(label, desired, option_variants)
                    )
            elif control_type in {"radio", "checkbox"}:
                handled_groups.add(group_key)
                if desired:
                    success = any(
                        _fill_radio_or_checkbox_group(page, control, variant)
                        for variant in _answer_variants(label, desired, option_variants)
                    )
            elif tag == "textarea":
                answer = desired
                if not answer and _is_essay_question(label):
                    answer = _generate_essay(label, job_text, company, role, candidate_evidence)
                if answer:
                    control.fill(answer)
                    success = bool(control.input_value().strip())
            elif control_type not in {"file", "hidden", "submit", "button"}:
                answer = desired
                is_required = control.get_attribute("aria-required") == "true"
                if not answer and is_required and _is_essay_question(label):
                    answer = _generate_essay(label, job_text, company, role, candidate_evidence)
                if answer:
                    control.fill(answer)
                    success = bool(control.input_value().strip())
            results[label] = success
        except Exception as exc:
            logger.debug("Custom question failed at index %d: %s", index, exc)
    return results


def _fill_eeo_fields(
    page: Page,
    profile: Mapping[str, Any],
    eeo: Mapping[str, Any],
    field_matchers: Mapping[str, Sequence[str]],
    option_variants: Mapping[str, Sequence[str]],
) -> dict[str, bool]:
    """Fill Greenhouse voluntary demographic controls from explicit configuration."""
    results: dict[str, bool] = {}
    controls = page.locator(
        '[role="combobox"], select, input[type="radio"], input[type="checkbox"]'
    )
    handled: set[str] = set()
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            label = _label_for(page, control)
            if not label or not SENSITIVE_EEO.search(label):
                continue
            group_key = control.get_attribute("name") or control.get_attribute("id") or label
            if group_key in handled:
                continue
            handled.add(group_key)
            desired = _configured_answer(label, profile, {}, eeo, field_matchers)
            if not desired:
                results[label] = False
                continue
            role_name = control.get_attribute("role") or ""
            tag = control.evaluate("el => el.tagName.toLowerCase()")
            control_type = (control.get_attribute("type") or "").lower()
            if role_name == "combobox":
                results[label] = _select_greenhouse_combobox(
                    page, control, _answer_variants(label, desired, option_variants)
                )
            elif tag == "select":
                results[label] = _select_native(
                    page, re.escape(label), _answer_variants(label, desired, option_variants)
                )
            elif control_type in {"radio", "checkbox"}:
                results[label] = any(
                    _fill_radio_or_checkbox_group(page, control, variant)
                    for variant in _answer_variants(label, desired, option_variants)
                )
        except Exception as exc:
            logger.debug("EEO field failed at index %d: %s", index, exc)

    # Greenhouse can re-render the lower disability control while earlier
    # React-select fields are being selected. Re-resolve known EEO labels once.
    for pattern in (
        r"gender",
        r"hispanic|latino",
        r"race|ethnic",
        r"veteran",
        r"disability",
        r"transgender",
        r"sexual orientation",
    ):
        try:
            control = _first_visible(page.get_by_label(re.compile(pattern, re.IGNORECASE)))
            if control is None:
                continue
            label = _label_for(page, control)
            if results.get(label):
                continue
            desired = _configured_answer(label, profile, {}, eeo, field_matchers)
            if desired and (control.get_attribute("role") or "") == "combobox":
                results[label] = _select_greenhouse_combobox(
                    page, control, _answer_variants(label, desired, option_variants)
                )
        except Exception as exc:
            logger.debug("EEO retry failed for %s: %s", pattern, exc)
    return results


def _fill_standard_fields(
    page: Page,
    profile: Mapping[str, Any],
    email: str,
    resume: Path,
) -> dict[str, bool]:
    fields = {
        "first_name": _fill(
            page,
            ('input[name="first_name"]', 'input[id*="first_name" i]'),
            str(profile.get("first_name", "")),
        ),
        "last_name": _fill(
            page,
            ('input[name="last_name"]', 'input[id*="last_name" i]'),
            str(profile.get("last_name", "")),
        ),
        "email": _fill(
            page,
            ('input[name="email"]', 'input[type="email"]', 'input[id*="email" i]'),
            email,
        ),
        "phone": _fill(
            page,
            ('input[name="phone"]', 'input[type="tel"]', 'input[id*="phone" i]'),
            str(profile.get("phone", "")),
        ),
        "preferred_name": _fill_labeled(
            page, r"preferred.*(?:first )?name", str(profile.get("preferred_name", ""))
        ),
        "location": False,
        "linkedin": _fill_labeled(page, r"linkedin", str(profile.get("linkedin", ""))),
        "website": _fill_labeled(page, r"(?:website|portfolio)", str(profile.get("website", ""))),
        "resume": _upload_resume(page, resume),
    }
    location_control = _first_visible(
        page.get_by_label(re.compile(r"(?:location|city)", re.IGNORECASE))
    )
    if location_control is not None:
        location_value = str(profile.get("location", ""))
        if (location_control.get_attribute("role") or "") == "combobox":
            fields["location"] = _select_greenhouse_combobox(
                page,
                location_control,
                (
                    location_value,
                    str(profile.get("city", "")),
                ),
            )
            if not fields["location"]:
                try:
                    location_control.fill(location_value or str(profile.get("city", "")))
                    page.wait_for_timeout(700)
                    location_control.press("ArrowDown")
                    location_control.press("Enter")
                    fields["location"] = bool(
                        location_control.evaluate(
                            "el => (el.parentElement?.parentElement?.innerText || '').trim()"
                        )
                    )
                except Exception:
                    fields["location"] = False
        else:
            try:
                location_control.fill(location_value)
                fields["location"] = bool(location_control.input_value().strip())
            except Exception:
                fields["location"] = False
    country = _first_visible(
        page.locator('input[role="combobox"][id="country"]')
    ) or _first_visible(page.get_by_label(re.compile(r"^(?:country|pays)", re.IGNORECASE)))
    if country is not None:
        fields["country"] = _select_greenhouse_combobox(
            page,
            country,
            (
                str(profile.get("country", "")),
                "United States",
                "États-Unis",
                "USA",
            ),
        )
    else:
        fields["country"] = _select_native(
            page, r"country", (str(profile.get("country", "")), "United States")
        )
    return fields


def _fill_explicit_required_consents(page: Page) -> list[str]:
    checked: list[str] = []
    controls = page.locator(
        'input[type="checkbox"][required], input[type="checkbox"][aria-required="true"]'
    )
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible() or control.is_checked():
                continue
            label = _label_for(page, control)
            if not re.search(r"\b(consent|agree|privacy|process(?:ing)?)\b", label, re.I):
                continue
            control.check(force=True)
            if control.is_checked():
                checked.append(" ".join(label.split())[:160])
        except Exception:
            continue
    return checked


def _fill_export_control_questions(page: Page) -> dict[str, bool]:
    """Answer the recurring Databricks sanctions checkbox groups consistently."""
    body = page.locator("body").inner_text()
    if "U.S. sanctions and export controls" not in body:
        return {}
    answers: dict[str, bool] = {}
    for pattern in (
        r"^None of the above$",
        r"^Not applicable \(i\.e\., I selected .none of the above. for the prior question\)$",
    ):
        control = _first_visible(page.get_by_label(re.compile(pattern, re.I)))
        if control is None:
            continue
        control.check(force=True)
        label = _label_for(page, control)
        answers[label] = control.is_checked()
    return answers


def _fill_source_checkbox(page: Page) -> dict[str, bool]:
    """Answer Twilio's recurring "how did you hear about us" checkbox with LinkedIn."""
    body = page.locator("body").inner_text()
    if not re.search(r"how did you hear about (?:us|twilio)", body, re.I):
        return {}
    control = _first_visible(page.get_by_label(re.compile(r"^LinkedIn$", re.I)))
    if control is None or (control.get_attribute("type") or "").lower() != "checkbox":
        return {}
    control.check(force=True)
    return {"How did you hear about this job? LinkedIn": control.is_checked()}


def _required_empty_fields(page: Page) -> list[str]:
    missing: list[str] = []
    controls = page.locator(
        'input[required], select[required], textarea[required], [aria-required="true"]'
    )
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible() or control.get_attribute("type") == "hidden":
                continue
            label = _label_for(page, control)
            if (
                not label
                and not control.get_attribute("id")
                and not control.get_attribute("name")
                and control.evaluate(
                    """el => {
                        const parent = el.parentElement?.parentElement;
                        return Boolean(parent?.querySelector('[role="combobox"]'));
                    }"""
                )
            ):
                # React-select maintains a second internal input for the same
                # labeled combobox. The labeled control is validated separately.
                continue
            control_type = (control.get_attribute("type") or "").lower()
            if control_type in {"checkbox", "radio"}:
                if not control.is_checked():
                    name = control.get_attribute("name")
                    if name and page.locator(f'input[name="{name}"]:checked').count():
                        continue
                    missing.append(label or name or f"choice-{index}")
            elif control.get_attribute("role") == "combobox":
                container_text = control.evaluate(
                    "el => (el.parentElement?.parentElement?.innerText || '').trim()"
                )
                placeholder = re.fullmatch(r"(?:select|choose)(?:\.\.\.)?", container_text, re.I)
                if not container_text or placeholder:
                    missing.append(label or control.get_attribute("id") or f"field-{index}")
            elif not control.input_value().strip():
                missing.append(
                    label
                    or control.get_attribute("name")
                    or control.get_attribute("id")
                    or f"field-{index}"
                )
        except Exception:
            continue
    return sorted(set(missing))


def _greenhouse_security_code_query(company: str) -> str:
    safe_company = company.replace('"', " ").strip()
    subject = (
        f'subject:"Security code for your application to {safe_company}"'
        if safe_company
        else 'subject:"Security code for your application"'
    )
    return f"from:no-reply@us.greenhouse-mail.io {subject} newer_than:1d"


def _current_greenhouse_verification_message_ids(company: str) -> set[str]:
    try:
        service = get_gmail_read_service(
            resolve_runtime_path(RUNTIME_CONFIG.gmail["credentials_file"]),
            resolve_runtime_path(RUNTIME_CONFIG.gmail["token_file"]),
        )
        return {
            record.message_id
            for record in fetch_messages(
                service,
                _greenhouse_security_code_query(company),
                20,
                False,
            )
            if record.message_id
        }
    except Exception as exc:
        logger.warning("Unable to snapshot existing Greenhouse security messages: %s", exc)
        return set()


def _fill_security_code_from_gmail(
    page: Page,
    company: str,
    *,
    excluded_message_ids: Optional[set[str]] = None,
) -> bool:
    """Read the five newest matching emails and fill Greenhouse's 8-box code."""
    code_inputs = page.locator('input[id^="security-input"]')
    if code_inputs.count() < 8:
        return False
    try:
        # Gmail can briefly lag the form's code-generation request. Waiting here
        # prevents reusing the previous application's otherwise newest code.
        page.wait_for_timeout(int(RUNTIME_CONFIG.gmail["greenhouse_security_code_wait_ms"]))
        service = get_gmail_read_service(
            resolve_runtime_path(RUNTIME_CONFIG.gmail["credentials_file"]),
            resolve_runtime_path(RUNTIME_CONFIG.gmail["token_file"]),
        )
        history_path = resolve_runtime_path(RUNTIME_CONFIG.gmail["verification_history_file"])
        excluded_ids = load_used_verification_message_ids(history_path)
        excluded_ids.update(excluded_message_ids or ())
        match = poll_for_verification_code(
            service,
            _greenhouse_security_code_query(company),
            r"security code field on your application:\s*([A-Za-z0-9]{8})",
            timeout_seconds=int(RUNTIME_CONFIG.gmail["verification_poll_timeout_seconds"]),
            sender_domains=("us.greenhouse-mail.io",),
            expected_recipient="",
            excluded_message_ids=excluded_ids,
        )
        if not match:
            logger.info("No unused Greenhouse security code found before timeout")
            return False
        for index, character in enumerate(match.code):
            code_inputs.nth(index).fill(character)
        if any(
            code_inputs.nth(index).input_value() != character
            for index, character in enumerate(match.code)
        ):
            logger.warning("Greenhouse security code was not filled completely")
            return False
        record_used_verification_message(history_path, match)
        logger.info("Filled Greenhouse security code from the newest matching Gmail message")
        return True
    except Exception as exc:
        logger.warning("Unable to read or fill Greenhouse security code: %s", exc)
        return False


def run(
    *,
    url: str,
    resume: Path,
    email_override: str,
    config: Mapping[str, Any],
    company: str,
    role: str,
    headed: bool,
    live_submit: bool,
) -> dict[str, Any]:
    if not _valid_greenhouse_url(url):
        raise ValueError("URL must be an absolute Greenhouse HTTPS URL")
    resume = validate_nonempty_file(resume, "resume")

    profile = dict(config["candidate"])
    candidate_evidence = _load_candidate_evidence(config)
    email = resolve_candidate_email(profile, email_override)

    timeout = int(config.get("navigation_timeout_ms", 30_000))
    screenshot_dir = resolve_project_dir(
        config.get("download_root", OUTPUT_DIR),
        OUTPUT_DIR,
    )
    with sync_playwright() as playwright:
        del headed  # shared runtime is visible Chrome/CDP-first for every ATS.
        session = open_chrome_session(
            playwright,
            profile_name="greenhouse-cdp-profile",
            target_url=url,
        )
        browser, page = session.browser, session.page
        page.set_default_timeout(int(config.get("action_timeout_ms", 14_000)))
        try:
            # If the reused tab is already on the target URL and showing the
            # 8-box code challenge, a prior run already filled the form and is
            # only waiting on email verification. Handle the code and submit
            # directly instead of re-filling (and re-triggering) the whole form.
            if (
                page.url.rstrip("/") == url.rstrip("/")
                and _security_challenge_visible(page)
            ):
                if not live_submit:
                    return {
                        "success": True,
                        "status": "PREFILLED_ONLY",
                        "ats": ATS_NAME,
                        "submitted": False,
                        "confirmed": False,
                        "test_mode": True,
                        "filled_fields": {},
                        "custom_questions": {},
                        "eeo_fields": {},
                        "consent_fields": [],
                        "missing_required": [],
                        "screenshot": "",
                    }
                if _fill_security_code_from_gmail(page, company):
                    submit = _first_visible(
                        page.get_by_role(
                            "button",
                            name=re.compile(r"submit application", re.I),
                        )
                    )
                    if submit is not None:
                        submit.click()
                        for _ in range(20):
                            if _confirmation_visible(page):
                                screenshot = _screenshot(
                                    page,
                                    screenshot_dir,
                                    company or "Greenhouse",
                                    "submitted_verified",
                                )
                                return {
                                    "success": True,
                                    "status": "SUBMITTED & CONFIRMED",
                                    "ats": ATS_NAME,
                                    "submitted": True,
                                    "confirmed": True,
                                    "test_mode": False,
                                    "filled_fields": {},
                                    "custom_questions": {},
                                    "eeo_fields": {},
                                    "consent_fields": [],
                                    "missing_required": [],
                                    "screenshot": screenshot,
                                }
                            page.wait_for_timeout(1_000)
                        screenshot = _screenshot(
                            page,
                            screenshot_dir,
                            company or "Greenhouse",
                            "submitted_verified",
                        )
                        return {
                            "success": False,
                            "status": "SUBMISSION_UNCONFIRMED",
                            "ats": ATS_NAME,
                            "submitted": True,
                            "confirmed": False,
                            "test_mode": False,
                            "filled_fields": {},
                            "custom_questions": {},
                            "eeo_fields": {},
                            "consent_fields": [],
                            "missing_required": [],
                            "screenshot": screenshot,
                        }
            navigate_reusing_tab(
                page,
                url,
                wait_until="domcontentloaded",
                timeout=timeout,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=timeout)
            except PlaywrightTimeoutError:
                logger.info("Initial network-idle wait timed out; continuing with loaded DOM")
            _open_application_form(page, timeout, url, company)
            if _confirmation_visible(page):
                confirmed_screenshot = _screenshot(
                    page, screenshot_dir, company or "Greenhouse", "submitted_verified"
                )
                return {
                    "success": True,
                    "status": "SUBMITTED & CONFIRMED",
                    "ats": ATS_NAME,
                    "submitted": True,
                    "confirmed": True,
                    "test_mode": False,
                    "filled_fields": {},
                    "custom_questions": {},
                    "eeo_fields": {},
                    "consent_fields": [],
                    "missing_required": [],
                    "screenshot": confirmed_screenshot,
                }
            fields = _fill_standard_fields(page, profile, email, resume)
            custom_questions = _fill_custom_questions(
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
            custom_questions.update(_fill_export_control_questions(page))
            custom_questions.update(_fill_source_checkbox(page))
            eeo_fields = _fill_eeo_fields(
                page,
                profile,
                config.get("eeo_defaults", {}),
                config.get("field_matchers", {}),
                config.get("answer_variants", {}),
            )
            consent = _fill_consent(page)
            consent.extend(_fill_explicit_required_consents(page))
            missing = validate_required_fields(page, _required_empty_fields)
            prefill_screenshot = _screenshot(
                page, screenshot_dir, company or "Greenhouse", "prefilled"
            )

            critical = ("first_name", "last_name", "email", "resume")
            critical_missing = [name for name in critical if not fields.get(name)]
            if critical_missing:
                return {
                    "success": False,
                    "status": "REQUIRED_FIELDS_NOT_FILLED",
                    "ats": ATS_NAME,
                    "submitted": False,
                    "confirmed": False,
                    "test_mode": not live_submit,
                    "filled_fields": fields,
                    "custom_questions": custom_questions,
                    "eeo_fields": eeo_fields,
                    "consent_fields": consent,
                    "missing_critical": critical_missing,
                    "missing_required": missing,
                    "screenshot": prefill_screenshot,
                }

            if not live_submit:
                return {
                    "success": True,
                    "status": "PREFILLED_ONLY",
                    "ats": ATS_NAME,
                    "submitted": False,
                    "confirmed": False,
                    "test_mode": True,
                    "filled_fields": fields,
                    "custom_questions": custom_questions,
                    "eeo_fields": eeo_fields,
                    "consent_fields": consent,
                    "missing_required": missing,
                    "screenshot": prefill_screenshot,
                }

            if missing:
                return {
                    "success": False,
                    "status": "REQUIRED_FIELDS_NOT_FILLED",
                    "ats": ATS_NAME,
                    "submitted": False,
                    "confirmed": False,
                    "test_mode": False,
                    "filled_fields": fields,
                    "custom_questions": custom_questions,
                    "eeo_fields": eeo_fields,
                    "consent_fields": consent,
                    "missing_required": missing,
                    "screenshot": prefill_screenshot,
                }

            submit = _first_visible(
                page.get_by_role(
                    "button",
                    name=SUBMIT_BUTTON_TEXT_PATTERN,
                )
            )
            if submit is None:
                submit = _first_visible(page.locator('button[type="submit"], input[type="submit"]'))
            if submit is None:
                if _confirmation_visible(page):
                    confirmed_screenshot = _screenshot(
                        page,
                        screenshot_dir,
                        company or "Greenhouse",
                        "submitted_verified",
                    )
                    return {
                        "success": True,
                        "status": "SUBMITTED & CONFIRMED",
                        "ats": ATS_NAME,
                        "submitted": True,
                        "confirmed": True,
                        "test_mode": False,
                        "filled_fields": fields,
                        "custom_questions": custom_questions,
                        "eeo_fields": eeo_fields,
                        "consent_fields": consent,
                        "missing_required": [],
                        "screenshot": confirmed_screenshot,
                    }
                raise RuntimeError("submit button was not found")
            verification_message_baseline = _current_greenhouse_verification_message_ids(company)
            submit.click()
            try:
                page.wait_for_load_state("networkidle", timeout=timeout)
            except PlaywrightTimeoutError:
                pass
            confirmed = False
            security_challenge_attempted = False
            for _ in range(15):
                if _confirmation_visible(page):
                    confirmed = True
                    break
                if (
                    not security_challenge_attempted
                    and _security_challenge_visible(page)
                ):
                    security_challenge_attempted = True
                    if _fill_security_code_from_gmail(
                        page,
                        company,
                        excluded_message_ids=verification_message_baseline,
                    ):
                        challenge_submit = _first_visible(
                            page.get_by_role(
                                "button",
                                name=SUBMIT_BUTTON_TEXT_PATTERN,
                            )
                        )
                        if challenge_submit is None:
                            challenge_submit = _first_visible(
                                page.locator('button[type="submit"], input[type="submit"]')
                            )
                        if challenge_submit is not None:
                            challenge_submit.click()
                page.wait_for_timeout(1_000)
            submitted_screenshot = _screenshot(
                page, screenshot_dir, company or "Greenhouse", "submitted_verified"
            )
            return {
                "success": confirmed,
                "status": "SUBMITTED & CONFIRMED" if confirmed else "SUBMISSION_UNCONFIRMED",
                "ats": ATS_NAME,
                "submitted": True,
                "confirmed": confirmed,
                "test_mode": False,
                "filled_fields": fields,
                "custom_questions": custom_questions,
                "eeo_fields": eeo_fields,
                "consent_fields": consent,
                "missing_required": validate_required_fields(page, _required_empty_fields),
                "screenshot": submitted_screenshot,
            }
        finally:
            if session.close_browser_on_exit:
                browser.close()


def _parser() -> argparse.ArgumentParser:
    return build_engine_parser("Greenhouse application automation engine")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    live_submit = requested_live_mode(args)
    try:
        require_orchestrated_invocation(args.url)
        result = run(
            url=args.url,
            resume=Path(args.resume).expanduser().resolve(),
            email_override=args.email,
            config=_load_config(),
            company=args.company,
            role=args.role,
            headed=args.headed,
            live_submit=live_submit,
        )
    except Exception as exc:
        logger.exception("Greenhouse engine failed")
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
