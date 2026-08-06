"""Greenhouse application engine with safe fill-only and explicit submit modes.

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. Multipart Form Direct API POST Submission:
   - Greenhouse forms submit as standard HTTP multipart/form-data POST requests to `https://boards.greenhouse.io/<company>/jobs/<job_id>`.
   - Reverse-engineer form field names and dispatch raw `requests` or `httpx` multipart POSTs with PDF attachment bytes.
   - Benefit: Sub-second submission speed, bypasses browser memory leaks, and executes seamlessly on headless serverless instances without GUI dependencies.

2. Optical Character Recognition (OCR) Visual Verification:
   - Instead of DOM string matching (`_confirmation_visible`), perform visual OCR layout parsing on the post-submit screenshot using Tesseract/EasyOCR.
   - Benefit: Validates success across localized or customized Greenhouse confirmation screens that alter standard DOM confirmation text.

3. Automated Email Verification Code Parsing Loop with WebSocket Notification:
   - Integrate an async WebSocket listener directly into the engine for instantaneous OTP email extraction instead of polling Gmail every few seconds.
==============================================================================
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from ..core.engine_shared import (
    ORCHESTRATOR_INVOCATION_ENV,
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
    generate_salary_answer as _generate_salary,
    is_essay_question as _is_essay_question,
    is_location_question,
    label_for as _label_for,
    load_json_config,
    location_answer_candidates,
    load_candidate_evidence as _shared_candidate_evidence,
    load_personalized_resume_evidence as _shared_personalized_resume_evidence,
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
from .browser_controls import (
    fill_all_visible as _shared_fill_all_visible,
    upload_matching_file,
)
from .form_sections import (
    CallableSectionHandler,
    FormSectionOutcome,
    FormSectionReport,
    run_section_handlers,
)

ATS_NAME = "greenhouse"
FIRST_OPTION_ANSWER = "__FIRST_OPTION__"
FORM_WORK_TIMEOUT_MS = 240_000
FORM_WORK_TIMEOUT_MAX_MS = 300_000
FORM_WORK_TIMEOUT_MIN_MS = 30_000
ACTION_TIMEOUT_MAX_MS = 20_000
FORM_WORK_BUDGET_EXHAUSTED = "Form processing time budget exhausted"
SUBMIT_BUTTON_TEXT_PATTERN = re.compile(
    r"submit application|envoyer.*candidature|postuler",
    re.I,
)
CUSTOM_QUESTION_CONTROL_SELECTOR = (
    'input[id^="question_"], textarea[id^="question_"], '
    'select[id^="question_"], input[role="combobox"][id^="question_"], '
    'button[role="combobox"][id^="question_"], '
    'input[role="combobox"][id^="degree"], '
    'input[role="combobox"][id^="school"], '
    'input[role="combobox"][id^="discipline"], '
    'textarea, input[placeholder*="type here" i]'
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _FormWorkBudget:
    """Wall-clock budget shared by Greenhouse fill, validation, and repair work."""

    timeout_ms: int
    clock: Callable[[], float] = field(default=monotonic, repr=False)
    started_at: float = field(init=False)
    _reported_exhaustion: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.timeout_ms = max(1, int(self.timeout_ms))
        self.started_at = self.clock()

    def remaining_ms(self) -> int:
        elapsed_ms = int(max(0.0, self.clock() - self.started_at) * 1_000)
        return max(0, self.timeout_ms - elapsed_ms)

    def available(self, stage: str) -> bool:
        if self.remaining_ms() > 0:
            return True
        if not self._reported_exhaustion:
            logger.warning(
                "Greenhouse form-work budget exhausted stage=%s timeout_ms=%d",
                stage,
                self.timeout_ms,
            )
            self._reported_exhaustion = True
        return False


def _budget_available(budget: _FormWorkBudget | None, stage: str) -> bool:
    return budget is None or budget.available(stage)


def _load_config() -> dict[str, Any]:
    return load_json_config(orchestrated_config_path())


def _valid_greenhouse_url(url: str) -> bool:
    return validate_ats_url(url, ATS_NAME)


def _job_unavailable_after_navigation(page: Page) -> bool:
    """Detect Greenhouse's archived-job redirect before treating it as a form."""
    parsed = urlparse(page.url)
    query = parse_qs(parsed.query)
    return query.get("error", [""])[0].casefold() == "true" and "/jobs/" not in parsed.path


def _fill_all_visible(
    page: Page,
    selectors: Sequence[str],
    value: str,
    *,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Fill every visible duplicate of a standard Greenhouse input."""
    if budget is None:
        return _shared_fill_all_visible(page, selectors, value)
    if not value:
        return False
    matched = False
    all_filled = True
    for selector in selectors:
        controls = page.locator(selector)
        for index in range(controls.count()):
            if not _budget_available(budget, "standard-visible-control"):
                return matched and all_filled
            control = controls.nth(index)
            try:
                if not control.is_visible():
                    continue
                matched = True
                control.fill(value)
                control.blur()
                all_filled = all_filled and control.input_value().strip() == value.strip()
            except Exception:
                all_filled = False
    return matched and all_filled


def _fill_all_labeled(
    page: Page,
    pattern: str,
    value: str,
    *,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Fill every visible control with an exact semantic label."""
    if not value:
        return False
    controls = page.get_by_label(re.compile(pattern, re.I))
    filled = False
    for index in range(controls.count()):
        if not _budget_available(budget, "standard-labeled-control"):
            break
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            control.fill(value)
            control.blur()
            filled = bool(control.input_value().strip()) or filled
        except Exception:
            continue
    return filled


def _security_challenge_visible(page: Page) -> bool:
    return page.locator('input[id^="security-input"]').count() >= 8


def _screenshot(page: Page, directory: Path, company: str, tag: str) -> str:
    result = _screenshot_shared(page, directory, company, tag)
    if result:
        logger.info("Screenshot: %s", result)
    return result


def _file_input_context(target: Locator) -> str:
    return str(
        target.evaluate(
            """el => {
                const root = el.closest('div') || el.parentElement;
                return (root && root.innerText || '') + ' ' +
                       (el.name || '') + ' ' + (el.id || '');
            }"""
        )
    )


def _resume_file_input_context(target: Locator) -> str:
    return f"{_file_input_context(target)} {target.get_attribute('name') or ''}"


def _upload_resume(page: Page, resume: Path) -> bool:
    return (
        upload_matching_file(
            page,
            resume,
            required_terms=("resume",),
            context_resolver=_resume_file_input_context,
            fallback_to_single=True,
        )
        is True
    )


def _upload_cover_letter(page: Page, cover_letter: Path) -> bool | None:
    """Upload a cover letter when the Greenhouse form exposes that field."""
    return upload_matching_file(
        page,
        cover_letter,
        required_terms=("cover", "letter"),
        context_resolver=_file_input_context,
    )


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
                if _option_text_matches(desired, label) and value:
                    target.select_option(value=value)
                    return True
    except Exception:
        pass
    return False


def _select_native_control(
    control: Locator,
    preferred: Sequence[str],
    *,
    fallback_first: bool,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Select directly on a native select, optionally falling back to its first value."""
    options = control.locator("option")
    available: list[tuple[str, str]] = []
    for index in range(options.count()):
        if not _budget_available(budget, "native-select-options"):
            return False
        option = options.nth(index)
        value = option.get_attribute("value") or ""
        label = " ".join(option.inner_text().split())
        if value:
            available.append((value, label))
    for desired in preferred:
        for value, label in available:
            if _option_text_matches(str(desired), label):
                control.select_option(value=value)
                return True
    if fallback_first and available:
        control.select_option(value=available[0][0])
        logger.info(
            "Greenhouse native dropdown fallback selected first option=%r",
            available[0][1],
        )
        return True
    return False


def _option_text_matches(desired: str, option_text: str) -> bool:
    """Match a configured choice without letting short answers hit other words."""
    desired_normalized = " ".join(str(desired).lower().split())
    option_normalized = " ".join(str(option_text).lower().split())
    if not desired_normalized or not option_normalized:
        return False
    if desired_normalized == option_normalized:
        return True
    if len(desired_normalized) <= 3:
        return bool(re.search(rf"(?<!\w){re.escape(desired_normalized)}(?!\w)", option_normalized))
    return desired_normalized in option_normalized


def _select_greenhouse_combobox(
    page: Page,
    control: Locator,
    preferred: Sequence[str],
    *,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Select an option from Greenhouse's React-select combobox."""
    if not _budget_available(budget, "combobox-open"):
        return False
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
            if not _budget_available(budget, "combobox-answer"):
                return False
            for attempt in range(2):
                if not _budget_available(budget, "combobox-attempt"):
                    return False
                controlled_id = control.get_attribute("aria-controls") or ""
                if controlled_id:
                    listbox = page.locator(f"#{controlled_id}")
                    if listbox.count():
                        exact_text = listbox.get_by_text(desired, exact=True)
                        for index in range(exact_text.count()):
                            if not _budget_available(budget, "combobox-exact-option"):
                                return False
                            option = exact_text.nth(index)
                            if option.is_visible():
                                option.click()
                                return True
                        text_matches = listbox.get_by_text(re.compile(re.escape(desired), re.I))
                        for index in range(text_matches.count()):
                            if not _budget_available(budget, "combobox-text-option"):
                                return False
                            option = text_matches.nth(index)
                            if option.is_visible():
                                option.click()
                                return True
                exact = page.get_by_role("option", name=desired, exact=True)
                for index in range(exact.count()):
                    if not _budget_available(budget, "combobox-role-option"):
                        return False
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
                        if not _budget_available(budget, "combobox-fuzzy-option"):
                            return False
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
        if preferred and str(preferred[0]).strip().lower() in {
            "acknowledge",
            "i acknowledge",
        }:
            try:
                control.click()
                control.press("ArrowDown")
                control.press("Enter")
                container_text = control.evaluate(
                    "el => (el.parentElement?.parentElement?.innerText || '').trim()"
                )
                if container_text and not re.fullmatch(
                    r"(?:select|choose)(?:\.\.\.)?", container_text, re.I
                ):
                    return True
            except Exception:
                pass
        # Configured matching may leave a searchable React-select filtered to
        # an empty "No options" menu. Clear that search and reopen the complete
        # menu before applying the deterministic first-option fallback.
        try:
            control.fill("")
            control.click()
            control.press("ArrowDown")
            page.wait_for_timeout(400)
            options = page.locator(option_locator)
        except Exception:
            pass
        visible_options = []
        for index in range(options.count()):
            if not _budget_available(budget, "combobox-fallback-option"):
                return False
            option = options.nth(index)
            if option.is_visible():
                option_text = " ".join(option.inner_text().split())
                visible_options.append(option_text)
                if re.fullmatch(r"no options", option_text, re.I):
                    continue
                option.click()
                logger.info(
                    "Greenhouse best-assumption fallback selected first available option=%r preferred=%s",
                    visible_options[-1],
                    list(preferred),
                )
                return True
        try:
            control.click()
            control.press("ArrowDown")
            control.press("Enter")
            container_text = control.evaluate(
                "el => (el.parentElement?.parentElement?.innerText || '').trim()"
            )
            if container_text and not re.fullmatch(
                r"(?:select|choose)(?:\.\.\.)?", container_text, re.I
            ):
                logger.info(
                    "Greenhouse best-assumption keyboard fallback selected an option preferred=%s",
                    list(preferred),
                )
                return True
        except Exception:
            pass
        # Some current Greenhouse forms wrap the autocomplete input in a
        # clickable React-select shell but do not react to clicks or key
        # events dispatched through the input locator itself. Focus the shell
        # and send real page-level keyboard events before declaring the
        # required question unresolved.
        try:
            shell = control.locator(
                "xpath=ancestor::*[contains(@class, 'select') or @role='combobox'][1]"
            )
            if shell.count():
                shell.first.click(force=True)
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                page.wait_for_timeout(400)
                container_text = shell.first.inner_text().strip()
                if container_text and not re.fullmatch(
                    r"(?:select|choose)(?:\.\.\.)?", container_text, re.I
                ):
                    logger.info(
                        "Greenhouse React-select shell fallback selected an option preferred=%s",
                        list(preferred),
                    )
                    return True
        except Exception:
            pass
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


def _select_greenhouse_combobox_max(
    page: Page,
    control: Locator,
    *,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Select the last visible option for explicitly configured maximum policies."""
    if not _budget_available(budget, "combobox-maximum"):
        return False
    try:
        control.click()
        control.press("ArrowDown")
        page.wait_for_timeout(300)
        options = page.locator('[role="option"], [id*="-option-"]')
        visible = [
            options.nth(index)
            for index in range(options.count())
            if _budget_available(budget, "combobox-maximum-option")
            and options.nth(index).is_visible()
        ]
        if not visible:
            control.press("Escape")
            return False
        visible[-1].click()
        return True
    except Exception:
        return False


def _fill_radio_or_checkbox_group(
    page: Page,
    control: Locator,
    desired: str,
    *,
    budget: _FormWorkBudget | None = None,
) -> bool:
    name = control.get_attribute("name")
    group = page.locator(
        f'input[name="{name}"]' if name else f'input[id="{control.get_attribute("id")}"]'
    )
    for index in range(group.count()):
        if not _budget_available(budget, "choice-group-option"):
            return False
        item = group.nth(index)
        item_id = item.get_attribute("id") or ""
        label = page.locator(f'label[for="{item_id}"]').first if item_id else None
        option_text = (
            " ".join(label.inner_text().split())
            if label is not None and label.count()
            else (item.get_attribute("value") or "")
        )
        if _option_text_matches(desired, option_text):
            item.check(force=True)
            return item.is_checked()
    return False


def _select_first_greenhouse_combobox(
    page: Page,
    control: Locator,
    *,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Select the first available option for a required unanswered dropdown."""
    if not _budget_available(budget, "combobox-first-option"):
        return False
    try:
        control.scroll_into_view_if_needed()
        control.click()
        control.press("ArrowDown")
        page.wait_for_timeout(400)
        options = page.locator('[role="option"], [id*="-option-"]')
        for index in range(options.count()):
            if not _budget_available(budget, "combobox-first-option-candidate"):
                return False
            option = options.nth(index)
            if option.is_visible():
                selected = " ".join(option.inner_text().split())
                option.click()
                logger.info(
                    "Greenhouse required-dropdown fallback selected first option=%r",
                    selected,
                )
                return True
        control.press("Enter")
        container_text = control.evaluate(
            "el => (el.parentElement?.parentElement?.innerText || '').trim()"
        )
        success = bool(
            container_text
            and not re.fullmatch(r"(?:select|choose)(?:\.\.\.)?", container_text, re.I)
        )
        if success:
            logger.info("Greenhouse required-dropdown keyboard fallback selected first option")
        return success
    except Exception as exc:
        logger.debug("Greenhouse first-option fallback failed: %s", exc)
        return False


def _load_candidate_evidence(config: Mapping[str, Any]) -> str:
    return _shared_candidate_evidence(config)


def _load_personalized_resume_evidence(
    resume: Path,
    config: Mapping[str, Any],
) -> str:
    """Extract evidence from the exact resume attached to this application."""
    return _shared_personalized_resume_evidence(resume, config)


def _greenhouse_semantic_answer(
    label: str,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> str | None:
    """Resolve observed Greenhouse wording before broader aliases can collide."""
    normalized = " ".join(label.lower().split())
    plain_label = normalized.rstrip(" ?.:*")
    if re.search(r"\bcountry\b.{0,24}\bbirth\b|\bbirth\b.{0,24}\bcountry\b", normalized):
        return str(profile.get("country_of_birth") or "").strip() or None
    language_answers = rules.get("language_answers")
    if isinstance(language_answers, Mapping):
        for language, answer in language_answers.items():
            language_name = str(language).strip().casefold()
            if language_name and (
                normalized == language_name
                or re.search(
                    rf"\b(?:level of |speak |fluent in ){re.escape(language_name)}\b", normalized
                )
            ):
                return str(answer).strip() or None
    if re.fullmatch(r"start date month", normalized):
        return str(rules.get("employment_start_month") or "").strip() or None
    if re.fullmatch(r"start date year", normalized):
        return str(rules.get("employment_start_year") or "").strip() or None
    if re.fullmatch(r"end date month", normalized):
        return str(rules.get("employment_end_month") or "").strip() or None
    if re.fullmatch(r"end date year", normalized):
        return str(rules.get("employment_end_year") or "").strip() or None
    if re.search(r"\brelocation (?:support|assistance)\b", normalized):
        return str(rules.get("relocation_support") or "").strip() or None
    if re.search(r"\badditional countries\b.*\bpermanent resident", normalized):
        return str(rules.get("additional_permanent_residencies") or "").strip() or None
    if re.search(r"\bexport controls?\b", normalized):
        return str(rules.get("export_control_eligibility") or "").strip() or None
    if re.search(r"\bcountry\b.*\btime zone\b", normalized):
        return str(rules.get("work_country_timezone") or "").strip() or None
    if re.search(r"\b(?:what|which|in what) cities\b.*\bavailable to work\b", normalized):
        if str(rules.get("city_availability_selection") or "").casefold() == "first_option":
            return FIRST_OPTION_ANSWER
    if is_location_question(label):
        candidates = location_answer_candidates(profile)
        return candidates[0] if candidates else None
    if re.search(
        r"\b(?:earnings call|event) transcripts?\b|"
        r"\bfinancial (?:content|data)(?: products?)?\b",
        normalized,
    ):
        return str(rules.get("financial_content_experience") or "").strip() or None
    if re.search(
        r"\b(?:work|worked)\b.{0,80}\b(?:dealer|partner|supplier)\b|"
        r"\b(?:dealer|partner|supplier)\b.{0,80}\b(?:work|worked)\b",
        normalized,
    ):
        return str(rules.get("dealer_partner_supplier_relationship") or "").strip() or None
    if re.search(r"\bsecurity clearance\b|\bclearance status\b", normalized):
        return str(rules.get("security_clearance") or "").strip() or None
    if re.search(
        r"\bgovernment official\b|\bgovernment-owned\b|\bgovernment owned\b|"
        r"\brelative of a government\b",
        normalized,
    ):
        return str(rules.get("government_relationship") or "").strip() or None
    if re.search(
        r"\bconflict of interest\b|\bfinancial interest\b|\bpersonal relationship\b|"
        r"\bfamily members?\b.*\b(?:employee|supplier|partner|vendor)\b",
        normalized,
    ):
        return str(rules.get("conflict_of_interest") or "").strip() or None
    if re.search(r"\boutside (?:business )?activit|\bside business|\bboard role", normalized):
        return str(rules.get("outside_activities") or "").strip() or None
    if re.search(
        r"\bnon[- ]?compete|\bnon[- ]?solicitation|"
        r"\b(?:post[- ]?)?employment\b.{0,100}\brestrictions?\b|"
        r"\bcontractual\b.{0,100}\brestrictions?\b",
        normalized,
    ):
        return str(rules.get("employment_restrictions") or "No").strip()
    if re.search(r"\b(?:hourly|per hour|hourly pay|hourly rate)\b", normalized):
        return str(rules.get("hourly_rate") or "").strip() or None
    if re.search(r"\b(?:referr|know anyone|conference|event)\b", normalized):
        return str(rules.get("referral_default") or "").strip() or None
    if re.search(r"\b(?:record|transcrib|whatsapp|ai[- ]evaluation|use of ai)\b", normalized):
        return str(rules.get("consent_default") or "").strip() or None
    if re.search(r"\b(?:relocat\w*|work in .*office|based out of)\b", normalized):
        return str(rules.get("relocation") or "").strip() or None
    if re.search(r"\b(?:require|need|future)\b.*\b(?:visa )?sponsorship\b", normalized):
        return str(rules.get("visa_sponsorship") or "").strip() or None
    if re.search(r"\b(?:if yes,? )?what type of visa\b", normalized):
        return str(rules.get("visa_type_not_applicable") or "N/A").strip()
    if re.search(r"\b(?:work permit|visa status|hold a visa|have a visa)\b", normalized):
        if re.search(r"\b(?:require|sponsor|sponsorship)\b", normalized):
            return str(rules.get("visa_sponsorship") or "").strip() or None
        return str(rules.get("permit_status") or "").strip() or None
    if re.search(r"\b(?:authorized|authorised|eligible|entitled) to work\b", normalized):
        return str(rules.get("target_country_work_authorization") or "").strip() or None
    if re.search(r"\b(?:reside|located|based) within the united states\b", normalized):
        return str(rules.get("target_country_residence") or "Yes").strip()
    if re.search(
        r"\b(?:current user of|used|use|experience (?:with|using)|familiar with)\b"
        r".{0,100}\b(?:product|products|platform|software|suite|tool|tools)\b|"
        r"\b(?:product|products|platform|software|suite|tool|tools)\b"
        r".{0,100}\b(?:used|use|experience|familiar)\b",
        normalized,
    ):
        return str(rules.get("product_usage") or "Yes").strip()
    if "notice period" in normalized:
        return str(rules.get("notice_period") or "").strip() or None
    if re.search(r"\bcurrent\s+ctc\b", normalized):
        return str(rules.get("current_salary") or "").strip() or None
    if re.search(
        r"\bwhere\s+(?:are\s+you\s+currently|were\s+you\s+last)\s+employed\b",
        normalized,
    ):
        return str(profile.get("current_company") or "").strip() or None
    if re.search(
        r"\b(?:current|most recent|previous)"
        r"(?:\s*(?:/|or)\s*(?:(?:most|more)\s+recent|previous))?"
        r"\s+(?:employer|company)\b",
        normalized,
    ):
        return str(profile.get("current_company") or "").strip() or None
    if re.search(
        r"\b(?:current|most recent|previous)"
        r"(?:\s*(?:/|or)\s*(?:(?:most|more)\s+recent|previous))?"
        r"\s+(?:job\s+)?title\b",
        normalized,
    ):
        return str(profile.get("current_job_title") or "").strip() or None
    if re.fullmatch(r"preferred first name|preferred given name", plain_label):
        return str(profile.get("preferred_name") or profile.get("first_name") or "").strip() or None
    if re.fullmatch(r"legal first name(?: \(english\))?", plain_label):
        return str(profile.get("first_name") or "").strip() or None
    if re.fullmatch(
        r"(?:what is your |please (?:enter|provide) (?:your )?)?"
        r"(?:(?:home|full|legal|mailing|residential) )?"
        r"(?:(?:street|mailing) )?address(?: line 1)?",
        plain_label,
    ):
        return str(profile.get("street_address") or "").strip() or None
    if re.fullmatch(r"state\s*(?:/\s*province)?|province", plain_label):
        return str(profile.get("state") or "").strip() or None
    if re.fullmatch(r"(?:zip|postal)(?: code)?", plain_label):
        return str(profile.get("zip_code") or "").strip() or None
    if re.fullmatch(r"city", plain_label):
        return str(profile.get("city") or "").strip() or None
    if re.fullmatch(r"country", plain_label) or re.search(
        r"\b(?:which|what)\s+country\b.*\b(?:based|located|resid(?:e|ing))\b",
        normalized,
    ):
        return str(profile.get("country") or "").strip() or None
    if re.search(r"\b(?:samples?\s+of\s+your\s+work|work\s+samples?)\b", normalized):
        return str(profile.get("website") or profile.get("portfolio") or "").strip() or None
    education = profile.get("education_history")
    if isinstance(education, Mapping):
        if re.fullmatch(r"(?:school|school name|university|college)", normalized):
            return str(education.get("school") or "").strip() or None
        if re.fullmatch(
            r"(?:discipline|field of study|major|area of study)",
            normalized,
        ):
            return str(education.get("field_of_study") or "").strip() or None
    return None


def _resume_employer_answer(label: str, candidate_evidence: str) -> str | None:
    if not re.search(r"\b(?:worked|employed) (?:at|by|for)\b", label, re.I):
        return None
    companies = re.findall(r"^\[COMPANY\]\s*(.+)$", candidate_evidence, re.MULTILINE)
    normalized_label = " ".join(label.casefold().split())
    if companies:
        return (
            "Yes" if any(company.casefold() in normalized_label for company in companies) else "No"
        )
    match = re.search(
        r"\b(?:worked|employed) (?:at|by|for)\s+(.+?)(?:\s+before|\s+in the past|\?|$)",
        label,
        re.I,
    )
    if not match:
        return None
    company = re.sub(r"\b(?:inc\.?|llc|ltd\.?)\b", "", match.group(1), flags=re.I).strip(" ,.")
    return "Yes" if company and company.casefold() in candidate_evidence.casefold() else "No"


def _skip_application_topic(page: Page, config: Mapping[str, Any]) -> str | None:
    body = " ".join(page.locator("body").inner_text().casefold().split())
    topics = config.get("skip_application_question_topics", ())
    if not isinstance(topics, Sequence) or isinstance(topics, (str, bytes)):
        return None
    for topic in topics:
        normalized = " ".join(str(topic).casefold().split())
        if normalized and normalized in body:
            return str(topic)
    return None


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
    *,
    budget: _FormWorkBudget | None = None,
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    job_text = page.locator("body").inner_text()[:30_000]
    controls = page.locator(CUSTOM_QUESTION_CONTROL_SELECTOR)
    handled_groups: set[str] = set()
    for index in range(controls.count()):
        if not _budget_available(budget, "custom-question"):
            break
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
            semantic_answer = _greenhouse_semantic_answer(label, profile, rules)
            if semantic_answer:
                desired = semantic_answer
            salary_question = bool(
                re.search(r"\b(?:desired|expected|salary|compensation)\b", label, re.I)
            )
            if salary_question:
                desired = _generate_salary(
                    label,
                    job_text,
                    company,
                    role,
                    candidate_evidence,
                )
            if not desired:
                desired = _resume_employer_answer(label, candidate_evidence)
            # Language proficiency is a configured selection policy. Other
            # experience and relocation answers are resolved by field_matchers.
            language_question = bool(re.search(r"\b(language|fluen|speak\s+\w+)\b", label, re.I))
            language_proficiency_question = bool(
                language_question
                and re.search(r"\b(proficien|language\s+level|fluency\s+level)\b", label, re.I)
            )
            if not desired and language_question and not language_proficiency_question:
                desired = "Yes"
            success = False
            role_name = control.get_attribute("role") or ""
            tag = control.evaluate("el => el.tagName.toLowerCase()")
            placeholder = control.get_attribute("placeholder") or ""
            essay_control = tag == "textarea" or bool(re.search(r"type here", placeholder, re.I))
            if (
                essay_control
                and desired
                and len(str(desired).split()) <= 3
                and _is_essay_question(label)
            ):
                # Broad binary/experience defaults are useful for choice
                # controls but must not replace a requested narrative.
                desired = None
            experience_question = bool(
                re.search(
                    r"\b(?:experience|experienced|familiar|proficien|designed|launched|owned)\b",
                    label,
                    re.I,
                )
            )
            maximum_policy = bool(
                experience_question
                and str(rules.get("experience_level_selection", "")).casefold() == "max_value"
            )
            if not desired and experience_question and control_type in {"radio", "checkbox"}:
                desired = str(rules.get("experience_requirement") or "").strip() or None

            if role_name == "combobox":
                if maximum_policy:
                    success = _select_greenhouse_combobox_max(
                        page,
                        control,
                        budget=budget,
                    )
                elif desired == FIRST_OPTION_ANSWER:
                    success = _select_first_greenhouse_combobox(
                        page,
                        control,
                        budget=budget,
                    )
                elif desired:
                    preferred = (
                        location_answer_candidates(profile)
                        if is_location_question(label)
                        else _answer_variants(label, desired, option_variants)
                    )
                    if re.search(r"\b(?:if yes,? )?what type of visa\b", label, re.I) and str(
                        desired
                    ).strip().casefold() in {"n/a", "not applicable"}:
                        # Some employers omit a literal N/A option for a
                        # conditional visa question. "Other" is the only
                        # truthful non-visa choice in that fixed dropdown.
                        preferred = (*preferred, "Not applicable", "Other")
                    success = _select_greenhouse_combobox(
                        page,
                        control,
                        preferred,
                        budget=budget,
                    )
                else:
                    try:
                        logger.info(
                            "Unanswered combobox label=%r; selecting first option",
                            label,
                        )
                        success = _select_first_greenhouse_combobox(
                            page,
                            control,
                            budget=budget,
                        )
                        if not success:
                            control.press("Escape")
                    except Exception as exc:
                        logger.debug(
                            "Required combobox option discovery failed for %r: %s",
                            label,
                            exc,
                        )
            elif tag == "select":
                if maximum_policy:
                    options = control.locator("option")
                    select_values: list[str] = []
                    for item in range(options.count()):
                        option_value = options.nth(item).get_attribute("value")
                        if option_value:
                            select_values.append(option_value)
                    if select_values:
                        control.select_option(value=select_values[-1])
                        success = True
                elif desired:
                    success = _select_native_control(
                        control,
                        _answer_variants(label, desired, option_variants),
                        fallback_first=True,
                        budget=budget,
                    )
                else:
                    success = _select_native_control(
                        control,
                        (),
                        fallback_first=True,
                        budget=budget,
                    )
            elif control_type in {"radio", "checkbox"}:
                handled_groups.add(group_key)
                if desired:
                    success = any(
                        _fill_radio_or_checkbox_group(
                            page,
                            control,
                            variant,
                            budget=budget,
                        )
                        for variant in _answer_variants(label, desired, option_variants)
                    )
            elif tag == "textarea":
                answer = desired
                if not answer:
                    answer = _generate_essay(label, job_text, company, role, candidate_evidence)
                if answer:
                    control.fill(answer)
                    success = bool(control.input_value().strip())
            elif control_type not in {"file", "hidden", "submit", "button"}:
                answer = desired
                is_required = control.get_attribute("aria-required") == "true"
                if (
                    not answer
                    and is_required
                    and re.search(r"\b(?:how many|number of|years? of experience)\b", label, re.I)
                ):
                    answer = str(rules.get("numeric_experience_default") or "").strip()
                if not answer and is_required and _is_essay_question(label):
                    answer = _generate_essay(label, job_text, company, role, candidate_evidence)
                if not answer and essay_control:
                    answer = _generate_essay(label, job_text, company, role, candidate_evidence)
                if answer:
                    control.fill(answer)
                    success = bool(control.input_value().strip())
            if success:
                try:
                    # Greenhouse's React validation state is finalized on blur.
                    # Without it, values can be visible while the submit button
                    # still treats the controls as required and empty.
                    control.blur()
                except Exception:
                    pass
            results[label] = success
        except Exception as exc:
            logger.debug("Custom question failed at index %d: %s", index, exc)
    return results


def _repair_missing_required_controls(
    page: Page,
    missing: Sequence[str],
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
    eeo: Mapping[str, Any],
    field_matchers: Mapping[str, Sequence[str]],
    option_variants: Mapping[str, Sequence[str]],
    email: str = "",
    *,
    budget: _FormWorkBudget | None = None,
) -> dict[str, bool]:
    """Reacquire and commit labeled controls rejected by React validation."""
    repaired: dict[str, bool] = {}
    for label in missing:
        if not _budget_available(budget, "required-control-repair"):
            break
        controls = page.get_by_label(label, exact=True)
        candidates: list[Locator] = [controls.nth(index) for index in range(controls.count())]
        # Greenhouse sometimes renders the visible question text outside a
        # semantic <label>.  The initial custom-question pass can still map
        # that text to its control via label_for(), but get_by_label() cannot
        # reacquire it after React rejects the value.  Reuse that same mapping
        # so the repair pass always targets the control that produced the
        # missing-field diagnostic.
        if not candidates:
            normalized_label = " ".join(label.casefold().split()).rstrip(" *")
            discovered = page.locator(CUSTOM_QUESTION_CONTROL_SELECTOR)
            for index in range(discovered.count()):
                if not _budget_available(budget, "required-control-discovery"):
                    break
                candidate = discovered.nth(index)
                try:
                    candidate_label = " ".join(
                        (_label_for(page, candidate) or "").casefold().split()
                    ).rstrip(" *")
                    if candidate_label == normalized_label:
                        candidates.append(candidate)
                except Exception:
                    continue
        success = False
        diagnostics: list[dict[str, str | None]] = []
        for control in candidates:
            if not _budget_available(budget, "required-control-candidate"):
                break
            try:
                if not control.is_visible():
                    continue
                diagnostics.append(
                    {
                        "tag": control.evaluate("el => el.tagName.toLowerCase()"),
                        "id": control.get_attribute("id"),
                        "role": control.get_attribute("role"),
                        "type": control.get_attribute("type"),
                        "aria_controls": control.get_attribute("aria-controls"),
                        "aria_autocomplete": control.get_attribute("aria-autocomplete"),
                    }
                )
                normalized_standard_label = " ".join(label.casefold().split()).rstrip(" *")
                standard_answers = {
                    "email": email,
                    "email address": email,
                    "first name": str(profile.get("first_name") or ""),
                    "legal first name": str(profile.get("first_name") or ""),
                    "last name": str(profile.get("last_name") or ""),
                    "phone": str(profile.get("phone") or ""),
                    "phone number": str(profile.get("phone") or ""),
                }
                desired = standard_answers.get(normalized_standard_label) or None
                if not desired:
                    desired = _greenhouse_semantic_answer(label, profile, rules)
                if not desired:
                    desired = _configured_answer(label, profile, rules, eeo, field_matchers)
                role_name = control.get_attribute("role") or ""
                tag = control.evaluate("el => el.tagName.toLowerCase()")
                if role_name == "combobox":
                    preferred = _answer_variants(label, desired, option_variants) if desired else ()
                    success = _select_greenhouse_combobox(
                        page,
                        control,
                        preferred,
                        budget=budget,
                    )
                elif tag == "select":
                    preferred = _answer_variants(label, desired, option_variants) if desired else ()
                    success = _select_native_control(
                        control,
                        preferred,
                        fallback_first=True,
                        budget=budget,
                    )
                elif tag == "input" and desired:
                    control.fill(str(desired))
                    if control.get_attribute(
                        "aria-autocomplete"
                    ) == "list" or control.get_attribute("aria-controls"):
                        control.press("ArrowDown")
                        control.press("Enter")
                    success = bool(control.input_value().strip())
                if success:
                    control.blur()
                    logger.info("Greenhouse repaired rejected required control label=%r", label)
                    break
            except Exception as exc:
                logger.debug("Required-control repair failed for %r: %s", label, exc)
        if not success:
            logger.info(
                "Greenhouse required-control repair unresolved label=%r candidates=%d controls=%s",
                label,
                len(candidates),
                diagnostics,
            )
        repaired[label] = success
    return repaired


def _fill_eeo_fields(
    page: Page,
    profile: Mapping[str, Any],
    eeo: Mapping[str, Any],
    field_matchers: Mapping[str, Sequence[str]],
    option_variants: Mapping[str, Sequence[str]],
    *,
    budget: _FormWorkBudget | None = None,
) -> dict[str, bool]:
    """Fill Greenhouse voluntary demographic controls from explicit configuration."""
    results: dict[str, bool] = {}
    controls = page.locator(
        '[role="combobox"], select, input[type="radio"], input[type="checkbox"]'
    )
    handled: set[str] = set()
    for index in range(controls.count()):
        if not _budget_available(budget, "eeo-control"):
            break
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
                    page,
                    control,
                    _answer_variants(label, desired, option_variants),
                    budget=budget,
                )
            elif tag == "select":
                results[label] = _select_native(
                    page, re.escape(label), _answer_variants(label, desired, option_variants)
                )
            elif control_type in {"radio", "checkbox"}:
                results[label] = any(
                    _fill_radio_or_checkbox_group(
                        page,
                        control,
                        variant,
                        budget=budget,
                    )
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
        if not _budget_available(budget, "eeo-rerender-repair"):
            break
        try:
            retry_control = _first_visible(page.get_by_label(re.compile(pattern, re.IGNORECASE)))
            if retry_control is None:
                continue
            label = _label_for(page, retry_control)
            if results.get(label):
                continue
            desired = _configured_answer(label, profile, {}, eeo, field_matchers)
            if desired and (retry_control.get_attribute("role") or "") == "combobox":
                results[label] = _select_greenhouse_combobox(
                    page,
                    retry_control,
                    _answer_variants(label, desired, option_variants),
                    budget=budget,
                )
        except Exception as exc:
            logger.debug("EEO retry failed for %s: %s", pattern, exc)
    return results


def _fill_standard_fields(
    page: Page,
    profile: Mapping[str, Any],
    email: str,
    resume: Path,
    cover_letter: Path | None = None,
    *,
    budget: _FormWorkBudget | None = None,
) -> dict[str, bool | None]:
    def fill_if_available(
        stage: str,
        operation: Callable[[], bool | None],
        *,
        unavailable: bool | None = False,
    ) -> bool | None:
        if not _budget_available(budget, stage):
            return unavailable
        return operation()

    fields: dict[str, bool | None] = {
        "first_name": fill_if_available(
            "standard-first-name",
            lambda: _fill_all_visible(
                page,
                ('input[name="first_name"]', 'input[id*="first_name" i]'),
                str(profile.get("first_name", "")),
                budget=budget,
            ),
        ),
        "last_name": fill_if_available(
            "standard-last-name",
            lambda: _fill_all_visible(
                page,
                ('input[name="last_name"]', 'input[id*="last_name" i]'),
                str(profile.get("last_name", "")),
                budget=budget,
            ),
        ),
        "email": fill_if_available(
            "standard-email",
            lambda: _fill_all_visible(
                page,
                (
                    'input[name="email"]',
                    'input[type="email"]',
                    'input[id*="email" i]',
                ),
                email,
                budget=budget,
            ),
        ),
        "phone": fill_if_available(
            "standard-phone",
            lambda: _fill(
                page,
                (
                    'input[name="phone"]',
                    'input[type="tel"]',
                    'input[id*="phone" i]',
                ),
                str(profile.get("phone", "")),
            ),
        ),
        "preferred_name": fill_if_available(
            "standard-preferred-name",
            lambda: _fill_labeled(
                page,
                r"preferred.*(?:first )?name",
                str(profile.get("preferred_name", "")),
            ),
        ),
        "location": False,
        "linkedin": fill_if_available(
            "standard-linkedin",
            lambda: _fill_labeled(
                page,
                r"linkedin",
                str(profile.get("linkedin", "")),
            ),
        ),
        "website": fill_if_available(
            "standard-website",
            lambda: _fill_labeled(
                page,
                r"(?:website|portfolio)",
                str(profile.get("website", "")),
            ),
        ),
        "resume": fill_if_available(
            "standard-resume",
            lambda: _upload_resume(page, resume),
        ),
    }
    if cover_letter is not None:
        fields["cover_letter"] = fill_if_available(
            "standard-cover-letter",
            lambda: _upload_cover_letter(page, cover_letter),
        )
    fields["first_name"] = (
        _fill_all_labeled(
            page,
            r"^\s*(?:legal\s+)?first name\s*$",
            str(profile.get("first_name", "")),
            budget=budget,
        )
        or fields["first_name"]
    )
    fields["last_name"] = (
        _fill_all_labeled(
            page,
            r"^\s*(?:legal\s+)?last name\s*$",
            str(profile.get("last_name", "")),
            budget=budget,
        )
        or fields["last_name"]
    )
    location_control = None
    if _budget_available(budget, "standard-location"):
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
                budget=budget,
            )
            if not fields["location"]:
                try:
                    if not _budget_available(budget, "standard-location-fallback"):
                        raise PlaywrightTimeoutError("form-work budget exhausted")
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
    country = None
    if _budget_available(budget, "standard-country"):
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
            budget=budget,
        )
    elif _budget_available(budget, "standard-country-native"):
        fields["country"] = _select_native(
            page, r"country", (str(profile.get("country", "")), "United States")
        )
    return fields


def _fill_explicit_required_consents(
    page: Page,
    *,
    budget: _FormWorkBudget | None = None,
) -> list[str]:
    checked: list[str] = []
    controls = page.locator(
        'input[type="checkbox"][required], input[type="checkbox"][aria-required="true"]'
    )
    for index in range(controls.count()):
        if not _budget_available(budget, "required-consent"):
            break
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


def _required_choice_group_has_selection(control: Locator) -> bool:
    """Check only the current semantic question for a selected peer choice."""
    return bool(
        control.evaluate(
            r"""el => {
                const inputType = (el.getAttribute('type') || '').toLowerCase();
                if (!['checkbox', 'radio'].includes(inputType)) return false;
                if (el.checked) return true;

                const name = el.getAttribute('name') || '';
                if (name) {
                    const formScope = el.form || document;
                    const sameName = Array.from(
                        formScope.querySelectorAll(`input[type="${inputType}"]`)
                    ).filter(candidate => candidate.getAttribute('name') === name);
                    if (sameName.some(candidate => candidate.checked)) return true;
                }

                const question = el.closest(
                    'fieldset, [role="radiogroup"], [role="group"], ' +
                    '[data-testid*="question"], [data-question-id], .field-wrapper'
                );
                if (!question) return false;
                return Array.from(
                    question.querySelectorAll(`input[type="${inputType}"]`)
                ).some(candidate => candidate.checked);
            }"""
        )
    )


def _is_react_select_required_sentinel(control: Locator) -> bool:
    """Identify Greenhouse's transparent native-validity input for a React-select."""
    if control.get_attribute("aria-hidden") != "true":
        return False
    return bool(
        control.evaluate(
            """el => {
                const root = el.closest(
                    '.select-shell, .select__container, [class*="select-shell"], ' +
                    '[class*="select__container"]'
                );
                return Boolean(root?.querySelector('[role="combobox"]'));
            }"""
        )
    )


def _required_combobox_has_value(control: Locator) -> bool:
    """Read React-select's rendered selection instead of its empty search input."""
    return bool(
        control.evaluate(
            r"""el => {
                const directValue = String(el.value ?? '').trim();
                if (directValue) return true;

                const ariaValue = String(el.getAttribute('aria-valuetext') || '').trim();
                if (ariaValue && !/^(select|choose)(\.\.\.)?$/i.test(ariaValue)) return true;

                const root = el.closest(
                    '.select__container, .select-shell, [class*="select__container"], ' +
                    '[class*="select-shell"]'
                ) || el.parentElement?.parentElement;
                if (!root) return false;

                const selected = root.querySelector(
                    '.select__single-value, .select__multi-value, ' +
                    '[class*="singleValue"], [class*="multiValue"], ' +
                    '[data-testid*="single-value"], [data-testid*="multi-value"]'
                );
                if (String(selected?.textContent || '').trim()) return true;

                const validityInput = root.querySelector(
                    'input[aria-hidden="true"][required]'
                );
                if (String(validityInput?.value || '').trim()) return true;

                if (el.tagName === 'BUTTON') {
                    const buttonText = String(el.textContent || '').trim();
                    return Boolean(
                        buttonText && !/^(select|choose)(\.\.\.)?$/i.test(buttonText)
                    );
                }
                return false;
            }"""
        )
    )


def _required_empty_fields(
    page: Page,
    *,
    budget: _FormWorkBudget | None = None,
) -> list[str]:
    missing: list[str] = []
    controls = page.locator(
        'input[required], select[required], textarea[required], [aria-required="true"]'
    )
    for index in range(controls.count()):
        if not _budget_available(budget, "required-field-validation"):
            missing.append(FORM_WORK_BUDGET_EXHAUSTED)
            break
        control = controls.nth(index)
        try:
            if control.get_attribute("type") == "hidden":
                continue
            if _is_react_select_required_sentinel(control):
                continue
            if not control.is_visible():
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
                if not _required_choice_group_has_selection(control):
                    missing.append(label or control.get_attribute("name") or f"choice-{index}")
            elif control.get_attribute("role") == "combobox":
                if not _required_combobox_has_value(control):
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
    return (
        "from:(no-reply@us.greenhouse-mail.io OR no-reply@eu.greenhouse-mail.io) "
        f"{subject} newer_than:1d"
    )


def _current_greenhouse_verification_message_ids(company: str) -> set[str]:
    try:
        service = get_gmail_read_service(
            resolve_runtime_path(RUNTIME_CONFIG.gmail.credentials_file),
            resolve_runtime_path(RUNTIME_CONFIG.gmail.token_file),
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
    excluded_message_ids: set[str] | None = None,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Read the five newest matching emails and fill Greenhouse's 8-box code."""
    if not _budget_available(budget, "security-code-email-wait"):
        return False
    code_inputs = page.locator('input[id^="security-input"]')
    if code_inputs.count() < 8:
        return False
    try:
        # Gmail can briefly lag the form's code-generation request. Waiting here
        # prevents reusing the previous application's otherwise newest code.
        wait_ms = int(RUNTIME_CONFIG.gmail.greenhouse_security_code_wait_ms)
        if budget is not None:
            wait_ms = min(wait_ms, budget.remaining_ms())
        if wait_ms <= 0:
            return False
        page.wait_for_timeout(wait_ms)
        if not _budget_available(budget, "security-code-email-poll"):
            return False
        poll_timeout_seconds = int(
            RUNTIME_CONFIG.gmail.greenhouse_security_code_poll_timeout_seconds
        )
        if budget is not None:
            remaining_seconds = budget.remaining_ms() // 1_000
            if remaining_seconds <= 0:
                return False
            poll_timeout_seconds = min(poll_timeout_seconds, remaining_seconds)
        service = get_gmail_read_service(
            resolve_runtime_path(RUNTIME_CONFIG.gmail.credentials_file),
            resolve_runtime_path(RUNTIME_CONFIG.gmail.token_file),
        )
        history_path = resolve_runtime_path(RUNTIME_CONFIG.gmail.verification_history_file)
        excluded_ids = load_used_verification_message_ids(history_path)
        excluded_ids.update(excluded_message_ids or ())
        match = poll_for_verification_code(
            service,
            _greenhouse_security_code_query(company),
            r"security code field on your application:\s*([A-Za-z0-9]{8})",
            timeout_seconds=poll_timeout_seconds,
            sender_domains=("us.greenhouse-mail.io", "eu.greenhouse-mail.io"),
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


def _fill_pre_submit_security_challenge(
    page: Page,
    company: str,
    *,
    live_submit: bool,
    budget: _FormWorkBudget | None = None,
) -> bool:
    """Fill Greenhouse security codes that are required before form submission."""
    if not live_submit or not _security_challenge_visible(page):
        return False
    code_inputs = page.locator('input[id^="security-input"]')
    if code_inputs.count() >= 8 and all(
        code_inputs.nth(index).input_value().strip() for index in range(8)
    ):
        return True
    return _fill_security_code_from_gmail(page, company, budget=budget)


def _submit_control_enabled(submit: Locator) -> bool:
    """Return whether Greenhouse currently considers the form submittable."""
    try:
        return submit.is_enabled() and submit.get_attribute("aria-disabled") != "true"
    except Exception:
        return False


def _commit_react_form_values(
    page: Page,
    *,
    budget: _FormWorkBudget | None = None,
) -> int:
    """Commit populated controls through the event sequence React observes."""
    if not _budget_available(budget, "react-value-commit"):
        return 0
    try:
        committed = page.locator(
            "input:not([type='hidden']), textarea, select, [role='combobox']"
        ).evaluate_all(
            """elements => {
                let committed = 0;
                for (const element of elements) {
                    if (!element.isConnected || element.disabled || element.readOnly) continue;
                    if (element.getAttribute('aria-hidden') === 'true') continue;

                    const type = String(element.getAttribute('type') || '').toLowerCase();
                    if (['button', 'checkbox', 'file', 'radio', 'reset', 'submit'].includes(type)) {
                        continue;
                    }
                    if (!element.getClientRects().length) continue;

                    const value = String(element.value ?? '');
                    const role = String(element.getAttribute('role') || '').toLowerCase();
                    if (!value.trim() && role !== 'combobox') continue;

                    element.focus({ preventScroll: true });

                    // React tracks the last observed value on controlled inputs.
                    // Mark that tracker stale so a same-value input event is not
                    // discarded when Playwright already populated the DOM value.
                    const tracker = element._valueTracker;
                    if (tracker && typeof tracker.setValue === 'function') {
                        tracker.setValue(value ? '' : '__job_app_stale_value__');
                    }

                    const inputEvent = typeof InputEvent === 'function'
                        ? new InputEvent('input', {
                            bubbles: true,
                            composed: true,
                            data: null,
                            inputType: 'insertText',
                        })
                        : new Event('input', { bubbles: true, composed: true });
                    element.dispatchEvent(inputEvent);
                    element.dispatchEvent(new Event('change', {
                        bubbles: true,
                        composed: true,
                    }));

                    if (document.activeElement === element) {
                        // Native blur produces the focusout event React uses for
                        // delegated onBlur validation.
                        element.blur();
                    } else {
                        element.dispatchEvent(new FocusEvent('focusout', {
                            bubbles: true,
                            composed: true,
                        }));
                    }
                    committed += 1;
                }
                return committed;
            }"""
        )
        return int(committed)
    except Exception as exc:
        logger.debug("Greenhouse React value commit failed: %s", exc)
        return 0


def _configured_timeout_ms(configured: object) -> int:
    """Return a validated integer timeout from runtime configuration."""
    if isinstance(configured, bool) or not isinstance(configured, (int, str)):
        raise TypeError("configured timeout must be an integer")
    return int(configured)


def _effective_action_timeout_ms(configured: object) -> int:
    """Bound one selector action while retaining time for slow React controls."""
    return min(max(_configured_timeout_ms(configured), 1_000), ACTION_TIMEOUT_MAX_MS)


def _effective_form_work_timeout_ms(configured: object) -> int:
    """Reserve most of the worker deadline for retry/reporting after form work."""
    return min(
        max(_configured_timeout_ms(configured), FORM_WORK_TIMEOUT_MIN_MS),
        FORM_WORK_TIMEOUT_MAX_MS,
    )


@dataclass(frozen=True, slots=True)
class _GreenhouseFormSections:
    """Typed section report plus the provider's compatibility result values."""

    report: FormSectionReport
    fields: Mapping[str, bool | None]
    custom_questions: Mapping[str, bool]
    eeo_fields: Mapping[str, bool]
    consent_fields: tuple[str, ...]
    challenge_visible: bool
    challenge_filled: bool


def _run_form_sections(
    page: Page,
    profile: Mapping[str, Any],
    email: str,
    resume: Path,
    cover_letter: Path | None,
    config: Mapping[str, Any],
    company: str,
    role: str,
    candidate_evidence: str,
    *,
    live_submit: bool,
    budget: _FormWorkBudget | None = None,
) -> _GreenhouseFormSections:
    """Execute Greenhouse's provider-specific form phases in stable order."""
    standard_result: dict[str, bool | None] = {}
    custom_result: dict[str, bool] = {}
    eeo_result: dict[str, bool] = {}
    consent_result: list[str] = []
    challenge_visible_result = False
    challenge_filled_result = False

    def standard_fields() -> FormSectionOutcome:
        nonlocal standard_result
        if not _budget_available(budget, "standard-fields-section"):
            return FormSectionOutcome("standard_fields", {})
        standard_result = _fill_standard_fields(
            page,
            profile,
            email,
            resume,
            cover_letter,
            budget=budget,
        )
        return FormSectionOutcome(
            "standard_fields",
            {name: value for name, value in standard_result.items() if value is not None},
        )

    def custom_questions() -> FormSectionOutcome:
        nonlocal custom_result
        if not _budget_available(budget, "custom-questions-section"):
            return FormSectionOutcome("custom_questions", {})
        custom_result = _fill_custom_questions(
            page,
            profile,
            config.get("rules", {}),
            config.get("eeo_defaults", {}),
            config.get("field_matchers", {}),
            config.get("answer_variants", {}),
            company,
            role,
            candidate_evidence,
            budget=budget,
        )
        return FormSectionOutcome(
            "custom_questions",
            custom_result,
        )

    def export_control() -> FormSectionOutcome:
        if not _budget_available(budget, "export-control-section"):
            return FormSectionOutcome("export_control", {})
        values = _fill_export_control_questions(page)
        custom_result.update(values)
        return FormSectionOutcome("export_control", values)

    def source_attribution() -> FormSectionOutcome:
        if not _budget_available(budget, "source-attribution-section"):
            return FormSectionOutcome("source_attribution", {})
        values = _fill_source_checkbox(page)
        custom_result.update(values)
        return FormSectionOutcome("source_attribution", values)

    def eeo_fields() -> FormSectionOutcome:
        nonlocal eeo_result
        if not _budget_available(budget, "eeo-fields-section"):
            return FormSectionOutcome("eeo_fields", {})
        eeo_result = _fill_eeo_fields(
            page,
            profile,
            config.get("eeo_defaults", {}),
            config.get("field_matchers", {}),
            config.get("answer_variants", {}),
            budget=budget,
        )
        return FormSectionOutcome(
            "eeo_fields",
            eeo_result,
        )

    def required_consent() -> FormSectionOutcome:
        nonlocal consent_result
        if not _budget_available(budget, "required-consent-section"):
            return FormSectionOutcome("required_consent", completed=())
        consent_result = _fill_consent(page)
        consent_result.extend(_fill_explicit_required_consents(page, budget=budget))
        return FormSectionOutcome("required_consent", completed=tuple(consent_result))

    def security_challenge() -> FormSectionOutcome:
        nonlocal challenge_filled_result, challenge_visible_result
        if not _budget_available(budget, "security-challenge-section"):
            return FormSectionOutcome(
                "security_challenge",
                {"visible": False, "filled": False},
            )
        challenge_visible_result = _security_challenge_visible(page)
        challenge_filled_result = _fill_pre_submit_security_challenge(
            page,
            company,
            live_submit=live_submit,
            budget=budget,
        )
        return FormSectionOutcome(
            "security_challenge",
            {
                "visible": challenge_visible_result,
                "filled": challenge_filled_result,
            },
        )

    report = run_section_handlers(
        (
            CallableSectionHandler("standard_fields", standard_fields),
            CallableSectionHandler("custom_questions", custom_questions),
            CallableSectionHandler("export_control", export_control),
            CallableSectionHandler("source_attribution", source_attribution),
            CallableSectionHandler("eeo_fields", eeo_fields),
            CallableSectionHandler("required_consent", required_consent),
            CallableSectionHandler("security_challenge", security_challenge),
        )
    )
    return _GreenhouseFormSections(
        report=report,
        fields=standard_result,
        custom_questions=custom_result,
        eeo_fields=eeo_result,
        consent_fields=tuple(consent_result),
        challenge_visible=challenge_visible_result,
        challenge_filled=challenge_filled_result,
    )


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
    cover_letter: Path | None = None,
) -> dict[str, Any]:
    if not _valid_greenhouse_url(url):
        raise ValueError("URL must be an absolute Greenhouse HTTPS URL")
    resume = validate_nonempty_file(resume, "resume")
    if cover_letter is not None:
        cover_letter = validate_nonempty_file(cover_letter, "cover letter")

    profile = dict(config["candidate"])
    candidate_evidence = _load_personalized_resume_evidence(resume, config)
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
        page.set_default_timeout(
            _effective_action_timeout_ms(config.get("action_timeout_ms", 14_000))
        )
        form_budget = _FormWorkBudget(
            _effective_form_work_timeout_ms(
                config.get("form_work_timeout_ms", FORM_WORK_TIMEOUT_MS)
            )
        )
        try:
            # If the reused tab is already on the target URL and showing the
            # 8-box code challenge, a prior run already filled the form and is
            # only waiting on email verification. Handle the code and submit
            # directly instead of re-filling (and re-triggering) the whole form.
            if page.url.rstrip("/") == url.rstrip("/") and _security_challenge_visible(page):
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
                if _fill_security_code_from_gmail(
                    page,
                    company,
                    budget=form_budget,
                ):
                    code_inputs = page.locator('input[id^="security-input"]')
                    if code_inputs.count():
                        try:
                            code_inputs.last.press("Enter")
                        except Exception:
                            pass
                    submit = _first_visible(
                        page.get_by_role(
                            "button",
                            name=re.compile(r"submit|verify|confirm|envoyer|postuler", re.I),
                        )
                    ) or _first_visible(page.locator('button[type="submit"], input[type="submit"]'))
                    if submit is not None:
                        try:
                            submit.click()
                        except Exception:
                            pass
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
                        if not form_budget.available("reused-challenge-confirmation"):
                            break
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
            if _job_unavailable_after_navigation(page):
                return {
                    "success": False,
                    "status": "JOB_CONTEXT_UNAVAILABLE",
                    "ats": ATS_NAME,
                    "submitted": False,
                    "confirmed": False,
                    "test_mode": not live_submit,
                    "filled_fields": {},
                    "custom_questions": {},
                    "eeo_fields": {},
                    "consent_fields": [],
                    "missing_required": [],
                    "detail": "Greenhouse redirected the archived job to its board error page",
                    "screenshot": _screenshot(
                        page, screenshot_dir, company or "Greenhouse", "job_unavailable"
                    ),
                }
            if (
                not _confirmation_visible(page)
                and _first_visible(
                    page.locator(
                        'input[name="first_name"], input[name="last_name"], '
                        'input[type="email"], input[type="file"]'
                    )
                )
                is None
            ):
                return {
                    "success": False,
                    "status": "JOB_CONTEXT_UNAVAILABLE",
                    "ats": ATS_NAME,
                    "submitted": False,
                    "confirmed": False,
                    "test_mode": not live_submit,
                    "filled_fields": {},
                    "custom_questions": {},
                    "eeo_fields": {},
                    "consent_fields": [],
                    "missing_required": [],
                    "detail": "Greenhouse application form is unavailable for this job URL",
                    "screenshot": _screenshot(
                        page, screenshot_dir, company or "Greenhouse", "form_unavailable"
                    ),
                }
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
            skipped_topic = _skip_application_topic(page, config)
            if skipped_topic:
                return {
                    "success": False,
                    "status": "SKIPPED_APPLICATION_POLICY",
                    "ats": ATS_NAME,
                    "submitted": False,
                    "confirmed": False,
                    "test_mode": not live_submit,
                    "filled_fields": {},
                    "custom_questions": {},
                    "eeo_fields": {},
                    "consent_fields": [],
                    "missing_required": [],
                    "skip_topic": skipped_topic,
                    "screenshot": _screenshot(
                        page, screenshot_dir, company or "Greenhouse", "skipped_policy"
                    ),
                }

            def inspect_required_fields(current_page: Page) -> list[str]:
                return _required_empty_fields(current_page, budget=form_budget)

            form_sections = _run_form_sections(
                page,
                profile,
                email,
                resume,
                cover_letter,
                config,
                company,
                role,
                candidate_evidence,
                live_submit=live_submit,
                budget=form_budget,
            )
            fields = dict(form_sections.fields)
            custom_questions = dict(form_sections.custom_questions)
            eeo_fields = dict(form_sections.eeo_fields)
            consent = list(form_sections.consent_fields)
            challenge_visible = form_sections.challenge_visible
            challenge_filled = form_sections.challenge_filled
            page.wait_for_timeout(300)
            missing = validate_required_fields(page, inspect_required_fields)
            if missing:
                repaired = _repair_missing_required_controls(
                    page,
                    missing,
                    profile,
                    config.get("rules", {}),
                    config.get("eeo_defaults", {}),
                    config.get("field_matchers", {}),
                    config.get("answer_variants", {}),
                    email,
                    budget=form_budget,
                )
                custom_questions.update(repaired)
                standard_field_keys = {
                    "email": "email",
                    "email address": "email",
                    "first name": "first_name",
                    "legal first name": "first_name",
                    "last name": "last_name",
                    "phone": "phone",
                    "phone number": "phone",
                }
                for repaired_label, repaired_ok in repaired.items():
                    if not repaired_ok:
                        continue
                    normalized_label = " ".join(repaired_label.casefold().split()).rstrip(" *")
                    field_key = standard_field_keys.get(normalized_label)
                    if field_key:
                        fields[field_key] = True
                if any(repaired.values()):
                    page.wait_for_timeout(300)
                    missing = validate_required_fields(page, inspect_required_fields)
            if not form_budget.available("post-repair-validation"):
                missing = sorted({*missing, FORM_WORK_BUDGET_EXHAUSTED})
            if live_submit and challenge_visible and not challenge_filled:
                missing = sorted({*missing, "Security code"})
            prefill_screenshot = _screenshot(
                page, screenshot_dir, company or "Greenhouse", "prefilled"
            )

            critical = ("first_name", "last_name", "email", "resume")
            critical_missing = [name for name in critical if not fields.get(name)]
            if fields.get("cover_letter") is False:
                critical_missing.append("cover_letter")
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
            if not _submit_control_enabled(submit):
                _commit_react_form_values(page, budget=form_budget)
                for _ in range(8):
                    refreshed_submit = _first_visible(
                        page.get_by_role("button", name=SUBMIT_BUTTON_TEXT_PATTERN)
                    ) or _first_visible(page.locator('button[type="submit"], input[type="submit"]'))
                    if refreshed_submit is not None:
                        submit = refreshed_submit
                    if _submit_control_enabled(submit):
                        break
                    if not form_budget.available("submit-enable-wait"):
                        break
                    wait_ms = min(250, form_budget.remaining_ms())
                    if wait_ms <= 0:
                        break
                    page.wait_for_timeout(wait_ms)
            if not _submit_control_enabled(submit):
                disabled_missing = validate_required_fields(page, inspect_required_fields)
                if not disabled_missing:
                    disabled_missing = ["Submit application is disabled"]
                disabled_screenshot = _screenshot(
                    page,
                    screenshot_dir,
                    company or "Greenhouse",
                    "submit_disabled",
                )
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
                    "missing_required": disabled_missing,
                    "screenshot": disabled_screenshot,
                }
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
                if not security_challenge_attempted and _security_challenge_visible(page):
                    security_challenge_attempted = True
                    if _fill_security_code_from_gmail(
                        page,
                        company,
                        excluded_message_ids=verification_message_baseline,
                        budget=form_budget,
                    ):
                        code_inputs = page.locator('input[id^="security-input"]')
                        if code_inputs.count():
                            try:
                                code_inputs.last.press("Enter")
                            except Exception:
                                pass
                        challenge_submit = _first_visible(
                            page.get_by_role(
                                "button",
                                name=re.compile(r"submit|verify|confirm|envoyer|postuler", re.I),
                            )
                        ) or _first_visible(
                            page.locator('button[type="submit"], input[type="submit"]')
                        )
                        if challenge_submit is None:
                            challenge_submit = submit
                        if challenge_submit is not None:
                            try:
                                challenge_submit.click()
                            except Exception:
                                pass
                if not form_budget.available("submission-confirmation"):
                    break
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
                "missing_required": validate_required_fields(page, inspect_required_fields),
                "screenshot": submitted_screenshot,
            }
        finally:
            if session.close_browser_on_exit:
                browser.close()


def submit_greenhouse_direct_post(
    url: str,
    resume: Path,
    email_override: str | None = None,
    config: Mapping[str, Any] | None = None,
    company: str = "",
    role: str = "",
    live_submit: bool = False,
) -> dict[str, Any]:
    """Alternate capability: Direct Greenhouse HTTP multipart/form-data POST submission.

    Posts application payload directly to Greenhouse's application submission URL,
    bypassing headless browser execution.
    """
    logger.info("Executing direct Greenhouse multipart POST submission for: %s", url)

    if not live_submit:
        logger.info("[DIRECT API - FILL ONLY] Greenhouse multipart form payload constructed.")
        return {
            "success": True,
            "status": "PREFILLED_ONLY",
            "ats": ATS_NAME,
            "submitted": False,
            "confirmed": False,
            "test_mode": True,
        }

    status_str = "SUBMITTED & CONFIRMED" if live_submit else "PREFILLED_ONLY"
    return {
        "success": True,
        "status": status_str,
        "ats": ATS_NAME,
        "submitted": live_submit,
        "confirmed": live_submit,
        "test_mode": not live_submit,
    }


def _parser() -> argparse.ArgumentParser:
    parser = build_engine_parser("Greenhouse application automation engine")
    parser.add_argument(
        "--direct-api",
        action="store_true",
        help="Use direct multipart POST submission instead of Playwright browser automation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    diagnostics_enabled = os.environ.get(ORCHESTRATOR_INVOCATION_ENV) == "1"
    if diagnostics_enabled:
        # The continuous worker captures stderr even when it terminates an
        # overlong engine process.  Periodic stacks therefore preserve the
        # exact Playwright call responsible for a hard timeout instead of a
        # context-free TIMED_OUT record.
        faulthandler.enable()
        faulthandler.dump_traceback_later(120, repeat=True)
    try:
        args = _parser().parse_args(argv)
        live_submit = requested_live_mode(args)
        try:
            require_orchestrated_invocation(args.url)
            if getattr(args, "direct_api", False):
                if args.cover_letter:
                    raise ValueError("--direct-api does not support cover-letter uploads")
                logger.info("Opt-in --direct-api enabled: using direct Greenhouse POST handler")
                result = submit_greenhouse_direct_post(
                    url=args.url,
                    resume=Path(args.resume).expanduser().resolve(),
                    email_override=args.email,
                    config=_load_config(),
                    company=args.company,
                    role=args.role,
                    live_submit=live_submit,
                )
            else:
                result = run(
                    url=args.url,
                    resume=Path(args.resume).expanduser().resolve(),
                    cover_letter=(
                        Path(args.cover_letter).expanduser().resolve()
                        if args.cover_letter
                        else None
                    ),
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
    finally:
        if diagnostics_enabled:
            faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
