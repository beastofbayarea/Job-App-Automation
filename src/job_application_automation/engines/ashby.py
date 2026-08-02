"""
Ashby ATS Application Chrome CDP Engine
===================================================================================
A Playwright CDP automation engine for applying to Ashby job postings.
Connects to active Google Chrome debugging session (port 9222).

Usage:
  Internal engine invoked by: python src/job_automation.py apply --url "https://jobs.ashbyhq.com/company/job-id"
"""

import base64
import argparse
import copy
import json
import logging
import os
import random
import re
import signal
import sys
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pypdf import PdfReader
from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
    expect,
    sync_playwright,
)

from .ashby_sections import (
    choice_is_selected,
    configured_screening_answer,
    plan_option_selection,
    required_field_flag,
)
from .browser_controls import (
    click_scrolled_control as _click_scrolled_control,
    fill_scrolled_control as _fill_scrolled_control,
    retry_action as _retry_action,
)
from .browser_runtime import PlaywrightBrowserSession
from .form_sections import (
    CallableSectionHandler,
    FormSectionOutcome,
    FormSectionReport,
    run_section_handlers,
)
from ..core.engine_shared import (
    answer_variants,
    build_engine_parser,
    configured_answer as common_configured_answer,
    deep_merge as _deep_merge,
    emit_engine_result,
    orchestrated_config_path,
    engine_result,
    require_orchestrated_invocation,
    fill_required_consent,
    is_location_question,
    load_json_config,
    mask_email as _mask_email,
    open_chrome_session,
    navigate_reusing_tab,
    requested_live_mode,
    resolve_candidate_email,
    safe_filename,
    text_confirms_submission,
    validate_ats_url,
    validate_required_fields,
)
from ..core.paths import SRC_DIR
from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from ..core.screenshots import active_screenshot_directory

# ==============================================================================
# DEFAULT CONFIGURATION
# ==============================================================================
SCRIPT_DIR = SRC_DIR
ATS_NAME = "ashby"
CDP_URL = RUNTIME_CONFIG.browser.cdp_endpoint
DEFAULT_TIMEOUT_MS = RUNTIME_CONFIG.ashby.default_timeout_ms
NAVIGATION_TIMEOUT_MS = RUNTIME_CONFIG.ashby.navigation_timeout_ms
NETWORK_IDLE_TIMEOUT_MS = RUNTIME_CONFIG.ashby.network_idle_timeout_ms
MAX_FORM_STEPS = RUNTIME_CONFIG.ashby.max_form_steps
MAX_SUBMIT_ATTEMPTS = RUNTIME_CONFIG.ashby.max_submit_attempts
SUBMISSION_CONFIRMATION_PHRASES = RUNTIME_CONFIG.ashby.submission_confirmation_phrases
SUBMISSION_SPAM_PHRASES = RUNTIME_CONFIG.ashby.submission_spam_phrases or (
    "flagged as possible spam",
)
_CONFIGURED_SUBMISSION_FAILURE_PHRASES = RUNTIME_CONFIG.ashby.submission_failure_phrases
SUBMISSION_REJECTION_PHRASES = tuple(
    phrase
    for phrase in _CONFIGURED_SUBMISSION_FAILURE_PHRASES
    if phrase not in SUBMISSION_SPAM_PHRASES
)
SUBMISSION_FAILURE_PHRASES = SUBMISSION_SPAM_PHRASES + SUBMISSION_REJECTION_PHRASES
SUBMISSION_RESULT_TIMEOUT_SECONDS = RUNTIME_CONFIG.ashby.submission_result_timeout_seconds or 15.0
SUBMISSION_RESULT_POLL_SECONDS = RUNTIME_CONFIG.ashby.submission_result_poll_seconds or 0.5
REQUIRED_FIELD_VALIDATION = "REQUIRED_FIELD_VALIDATION"

# Candidate data belongs in candidate_profile_config.json, not in source code. Empty defaults
# make missing data visible rather than silently submitting someone else's details.
DEFAULT_CONFIG = {
    "candidate": {
        "first_name": "",
        "last_name": "",
        "preferred_name": "",
        "phone": "",
        "fallback_email": "",
        "location": "",
        "city": "",
        "state": "",
        "country": "",
        "zip_code": "",
        "nationality": "",
        "citizenship": "",
        "linkedin": "",
        "portfolio": "",
        "website": "",
        "twitter": "",
        "researchgate": "",
        "sciencedirect": "",
        "street_address": "",
        "address_2": "",
        "gender": "",
        "pronouns": "",
        "race": "",
        "age": "",
        "veteran": "",
        "transgender": "",
        "orientation": "",
        "disability": "",
        "communities": [],
        "screening_answers": {},
    },
    "defaults": {"source": "", "salary": "", "product_area_essay": "", "essay": ""},
    "paths": {"ashby_dir": str(resolve_runtime_path(RUNTIME_CONFIG.ashby.screenshot_dir))},
    "action_timeout_ms": DEFAULT_TIMEOUT_MS,
    "navigation_timeout_ms": NAVIGATION_TIMEOUT_MS,
    "network_idle_timeout_ms": NETWORK_IDLE_TIMEOUT_MS,
    "company_overrides": {},
}

_shutdown = False
T = TypeVar("T")


def signal_handler(sig: int, _frame: Any) -> None:
    global _shutdown
    logger.warning("Received signal %s; stopping application process.", sig)
    _shutdown = True


# ==============================================================================
# LOGGING SETUP
# ==============================================================================
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("AshbyAutomationEngine")


# ==============================================================================
# CORE HELPERS & UTILITIES
# ==============================================================================
def expand(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()


def human_delay(a: float = 0.18, b: float = 0.55) -> None:
    time.sleep(random.uniform(a, b))


def retry(
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 1.1,
    label: str = "action",
) -> T:
    return _retry_action(
        fn,
        attempts=attempts,
        base_delay=base_delay,
        label=label,
        sleep=time.sleep,
        on_error=lambda action, attempt, total, exc: logger.warning(
            "%s failed (%d/%d): %s",
            action,
            attempt,
            total,
            exc,
        ),
    )


def load_config(path: Path) -> dict[str, Any]:
    """Load Ashby defaults through the shared schema-v2 profile loader."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    cfg = load_json_config(path, defaults=DEFAULT_CONFIG)
    logger.info("Loaded profile configuration: %s", path)

    cfg.setdefault("defaults", {})
    cfg.setdefault("paths", {})
    cfg.setdefault("company_overrides", {})
    return cfg


def _start_date_from_offset(
    *,
    offset_days: int = 14,
    base_date: date | None = None,
) -> str:
    """Return an ISO start date relative to the run date."""
    return ((base_date or date.today()) + timedelta(days=offset_days)).isoformat()


def is_ashby_url(url: str) -> bool:
    """Return whether *url* is a supported HTTPS Ashby job URL."""
    return validate_ats_url(url, ATS_NAME)


def _safe_filename_part(value: str, fallback: str = "Company") -> str:
    return safe_filename(value, fallback)[:100]


def extract_lowest_salary(comp_str: str, location_str: str = "") -> str:
    compensation = str(comp_str or "").strip()
    location = str(location_str or "")
    if not compensation or compensation.lower() in ("n/a", "none", "unknown"):
        return "Competitive / Market rate"
    loc_lower = f"{location} {compensation}".lower()
    currency = "$"
    if any(k in loc_lower for k in ("uk", "london", "united kingdom", "gbp", "£")):
        currency = "£"
    elif any(k in loc_lower for k in ("eu", "europe", "germany", "france", "eur", "€")):
        currency = "€"
    elif any(k in loc_lower for k in ("inr", "india", "₹")):
        currency = "₹"

    matches = re.findall(r"(\d[\d,]*\.?\d*)\s*([kK])?", compensation)
    vals = []
    for num_str, k_flag in matches:
        clean_num = num_str.replace(",", "")
        try:
            val = float(clean_num)
            # Compensation strings often shorten thousands ("120k" or bare "120");
            # treat any bare number under 1000 as thousands too. The 30000 floor
            # then screens out unrelated numbers (e.g. years, hours) that survive
            # that scaling.
            if k_flag.lower() == "k" or val < 1000:
                val *= 1000
            if val >= 30000:
                vals.append(int(val))
        except ValueError:
            pass

    if vals:
        return f"{currency}{min(vals):,}"
    return compensation


def ss(page: Page, directory: Path, company: str, tag: str) -> str:
    """Capture a debug screenshot, preferring a raw CDP capture around submit/error states."""
    ts = datetime.now().strftime("%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    is_post_submit = "submitted" in tag or "5sec" in tag or "FATAL" in tag
    company_part = _safe_filename_part(company)
    tag_part = _safe_filename_part(tag, fallback="screenshot")

    try:
        if page.is_closed():
            return "N/A"

        if is_post_submit:
            # page.screenshot() can hang or fail while the page is mid-navigation
            # right after a submit/error, so capture via the CDP session directly
            # instead of Playwright's higher-level (and more fragile) API.
            cdp_path = directory / f"{company_part}_{tag_part}_{ts}_cdp.png"
            try:
                cdp = page.context.new_cdp_session(page)
                res = cdp.send("Page.captureScreenshot", {"format": "png"})
                if res and "data" in res:
                    cdp_path.write_bytes(base64.b64decode(res["data"]))
                    logger.info(
                        "Screenshot [%s] (CDP hardware capture): %s",
                        tag,
                        cdp_path.name,
                    )
                    return str(cdp_path)
            except Exception as cdp_exc:
                logger.debug("CDP capture attempt notice [%s]: %s", tag, cdp_exc)

        try:
            page.evaluate("""() => {
                const el = document.createElement('style');
                el.innerText = '* { font-family: Arial, sans-serif !important; font-display: swap !important; animation: none !important; transition: none !important; }';
                (document.head || document.documentElement).appendChild(el);
            }""")
        except Exception:
            pass

        jpg_path = directory / f"{company_part}_{tag_part}_{ts}.jpg"
        fp = not is_post_submit

        try:
            page.screenshot(
                path=str(jpg_path),
                type="jpeg",
                quality=85,
                full_page=fp,
                animations="disabled",
                timeout=2500,
            )
            logger.info("Screenshot [%s]: %s", tag, jpg_path.name)
            return str(jpg_path)
        except Exception as primary_exc:
            try:
                page.screenshot(
                    path=str(jpg_path),
                    type="jpeg",
                    quality=85,
                    full_page=False,
                    animations="disabled",
                    timeout=2000,
                )
                logger.info("Screenshot [%s] (viewport fallback): %s", tag, jpg_path.name)
                return str(jpg_path)
            except Exception as fallback_exc:
                logger.warning(
                    "Screenshot capture skipped [%s]: %s / %s",
                    tag,
                    primary_exc,
                    fallback_exc,
                )
                return "N/A"
    except Exception as exc:
        logger.warning("Screenshot setup failed [%s]: %s", tag, exc)
        return "N/A"


def fill(page: Page, loc: Any, value: str, timeout: int = 7000) -> bool:
    return _fill_scrolled_control(
        loc,
        value,
        timeout_ms=timeout,
        visibility_waiter=lambda control, timeout_ms: expect(control).to_be_visible(
            timeout=timeout_ms
        ),
        before_primary_fill=lambda: human_delay(0.08, 0.2),
        fallback_delay_ms=lambda: random.randint(25, 55),
        on_failure=lambda exc: logger.debug("fill failed: %s", exc),
    )


def click(loc: Any, desc: str = "", timeout: int = 5000) -> bool:
    return _click_scrolled_control(
        loc,
        timeout_ms=timeout,
        visibility_waiter=lambda control, timeout_ms: expect(control).to_be_visible(
            timeout=timeout_ms
        ),
        before_click=lambda: human_delay(0.12, 0.28),
        on_success=lambda: logger.info("Clicked: %s", desc),
        on_failure=lambda exc: logger.debug("click [%s] failed: %s", desc, exc),
    )


def smooth_mouse_move(page: Page, loc: Any) -> None:
    try:
        box = loc.bounding_box()
        if box:
            target_x = box["x"] + box["width"] / 2
            target_y = box["y"] + box["height"] / 2
            page.mouse.move(target_x, target_y, steps=random.randint(12, 22))
            human_delay(0.05, 0.15)
    except Exception:
        pass


def select_ashby_combobox(
    page: Page,
    inp: Any,
    value: str,
    fallback_value: str | None = None,
) -> bool:
    """Type into an Ashby autocomplete combobox and pick a matching option.

    Falls through Playwright locators, then raw DOM text matching, then a
    keyboard-driven acceptance of Ashby's highlighted suggestion.
    """
    target = str(value or fallback_value or "").strip()
    if not target:
        return False

    try:
        inp.click()
        human_delay(0.2, 0.4)
        inp.fill("")
        human_delay(0.2, 0.3)
        search_term = target.split(",")[0].strip() or target
        inp.press_sequentially(search_term, delay=50)
        human_delay(0.8, 1.2)
        try:
            page.locator('[role="option"]:visible').first.wait_for(
                state="visible",
                timeout=5000,
            )
        except Exception:
            logger.debug(
                "No visible combobox option appeared for [%s] within 5 seconds.",
                search_term,
            )

        no_results = page.get_by_text(re.compile(r"^\s*No results\s*$", re.I))
        fallback = str(fallback_value or "").strip()
        if (
            fallback
            and fallback.lower() != search_term.lower()
            and any(item.is_visible() for item in no_results.all())
        ):
            inp.fill("")
            inp.press_sequentially(fallback, delay=50)
            human_delay(0.8, 1.2)

        # Tier 1: Playwright locators work with portal-rendered Ashby menus.
        candidates: list[str] = []
        for candidate in (target, fallback_value, search_term):
            candidate = str(candidate or "").strip()
            if candidate and candidate.lower() not in {item.lower() for item in candidates}:
                candidates.append(candidate)
        option_selector = (
            '[role="option"], '
            '[role="listbox"] [role="menuitem"], '
            'div[class*="_option_"], '
            'div[class*="_result_"]'
        )
        for candidate in candidates:
            options = page.locator(option_selector).filter(
                has_text=re.compile(re.escape(candidate), re.I)
            )
            for index in range(min(options.count(), 8)):
                option = options.nth(index)
                if option.is_visible() and click(option, f"Combobox option: {candidate}"):
                    human_delay(0.3, 0.6)
                    return True

        # Tier 2: match the visible option's rendered text.  Do not reject nested
        # option nodes; Ashby frequently wraps labels, icons, and helper text.
        clicked = page.evaluate(
            """values => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const wanted = values.map(norm).filter(Boolean);
            const nodes = Array.from(document.querySelectorAll(
                '[role="listbox"] [role="option"], [role="option"], '
                + '[role="listbox"] [role="menuitem"], '
                + 'div[class*="_option_"], div[class*="_result_"]'
            )).filter(node => {
                const r = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            });
            const texts = nodes.map(node => ({ node, text: norm(node.innerText) })).filter(x => x.text && x.text.length < 160);
            for (const wantedText of wanted) {
                const exact = texts.find(x => x.text === wantedText || x.text.startsWith(wantedText));
                if (exact) { exact.node.click(); return exact.text; }
            }
            for (const wantedText of wanted) {
                const contains = texts.find(x => x.text.includes(wantedText));
                if (contains) { contains.node.click(); return contains.text; }
            }
            return null;
        }""",
            candidates,
        )

        if clicked:
            logger.info(f"Combobox selected option: {clicked}")
            human_delay(0.4, 0.7)
            return True

        # Tier 3: accept Ashby's highlighted suggestion only when the listbox is visible.
        listbox = page.locator('[role="listbox"]:visible').first
        if not listbox.count():
            return False
        inp.press("ArrowDown")
        human_delay(0.2, 0.4)
        inp.press("Enter")
        human_delay(0.4, 0.7)
        return bool(inp.input_value().strip())
    except Exception as e:
        logger.debug(f"Combobox selection failed for [{target}]: {e}")
        return False


def verify_value(loc: Any, expected_substr: str, name: str) -> bool:
    try:
        actual = loc.input_value().strip()
        if expected_substr.lower() in actual.lower():
            logger.info(f"Verified {name}")
            return True
        logger.warning(f"{name} mismatch: expected~'{expected_substr}' got='{actual}'")
        return False
    except Exception as e:
        logger.warning(f"Could not verify {name}: {e}")
        return False


def generate_essay_safely(question: str, jd_text: str, company: str, role: str) -> str:
    """Load the optional essay generator only when an unanswered essay is encountered."""
    try:
        from ..resume.ai_client import call_essay_llm, strip_markdown_formatting
    except Exception as exc:
        logger.warning("Essay generator is unavailable; leaving the field for review: %s", exc)
        return ""

    try:
        evidence_path = resolve_runtime_path(RUNTIME_CONFIG.application.resume_source_file)
        candidate_evidence = (
            evidence_path.read_text(encoding="utf-8") if evidence_path.is_file() else ""
        )
        return strip_markdown_formatting(
            call_essay_llm(
                question,
                jd_text,
                company,
                role,
                candidate_evidence=candidate_evidence,
            )
        )
    except Exception as exc:
        logger.warning("Essay generation failed; leaving the field for review: %s", exc)
        return ""


# ==============================================================================
# FORM FILLING ENGINE
# ==============================================================================
def _configured_answer(
    profile: Mapping[str, Any],
    question_text: str,
) -> str | None:
    """Return an explicit configured answer for a screening question, if one exists."""
    answers = profile.get("screening_answers", {})
    if not isinstance(answers, dict):
        return None
    explicit_answer = configured_screening_answer(answers, question_text)
    if explicit_answer is not None:
        return explicit_answer
    normalized_question = re.sub(r"\s+", " ", question_text).strip().lower()
    return common_configured_answer(
        normalized_question,
        profile,
        profile.get("_rules", {}),
        profile.get("_eeo_defaults", {}),
        profile.get("_field_matchers", {}),
    )


def _boolean_rule_answer(value: Any) -> str | None:
    """Normalize a configured rule to an Ashby Yes/No button label."""
    normalized = str(value or "").strip().lower()
    if normalized in {"yes", "true", "1"} or normalized.startswith("yes,"):
        return "Yes"
    if normalized in {"no", "false", "0"} or normalized.startswith("no,"):
        return "No"
    return None


def _merge_repository_rules(
    profile: dict[str, Any],
    defaults: dict[str, Any],
    resolved_config: Mapping[str, Any],
) -> None:
    """Expose repository-level rules through the engine's question matcher."""
    rules = resolved_config.get("rules", {})
    if not isinstance(rules, Mapping):
        raise ValueError("Configuration field 'rules' must be an object.")

    existing = profile.get("screening_answers", {})
    if existing is None:
        existing = {}
    if not isinstance(existing, Mapping):
        raise ValueError("Candidate field 'screening_answers' must be an object.")
    answers = {str(key): value for key, value in existing.items()}

    rule_fragments = {
        "work_authorization": (
            "legally authorized",
            "authorized to work",
            "work authorization",
            "eligible to work",
        ),
        "visa_sponsorship": (
            "require work sponsorship",
            "require sponsorship",
            "future sponsorship",
            "visa sponsorship",
        ),
        "relocation": (
            "willing to relocate",
            "and/or willing to relocate",
            "open to relocating",
        ),
        "are_you_comfortable_with": (
            "willing to work on-site",
            "willing to work onsite",
            "comfortable working on-site",
            "comfortable working onsite",
            "work is in office",
            "working from office",
            "work from office",
            "working in the office",
            "in-office schedule",
            "office days per week",
        ),
    }
    for rule_name, fragments in rule_fragments.items():
        answer = _boolean_rule_answer(rules.get(rule_name))
        if not answer:
            continue
        for fragment in fragments:
            answers.setdefault(fragment, answer)

    profile["screening_answers"] = answers
    source = str(rules.get("source_channel") or "").strip()
    if source and not defaults.get("source"):
        defaults["source"] = source


def _fill_yesno_groups(page: Page, profile: Mapping[str, Any]) -> None:
    yesno_groups = page.locator("div[class*='_yesno']").all()
    if not yesno_groups:
        return
    logger.info(f"[_fill_yesno_groups] Found {len(yesno_groups)} yes/no group(s)")

    for group in yesno_groups:
        try:
            q_lower = group.evaluate("""el => {
                let curr = el;
                for (let i = 0; i < 5 && curr; i++) {
                    let label = curr.querySelector('label, [class*="label" i], [class*="title" i], [class*="heading" i], [class*="question" i]');
                    if (label) {
                        let txt = (label.innerText || "").trim();
                        if (txt.length > 5 && !txt.toLowerCase().startsWith("yes") && !txt.toLowerCase().startsWith("no")) {
                            return txt.toLowerCase();
                        }
                    }
                    curr = curr.parentElement;
                }
                return (el.innerText || "").toLowerCase();
            }""")

            hidden_cb = group.locator('input[type="checkbox"]').first
            configured = _configured_answer(profile, q_lower)
            logger.info(
                "Yes/No candidate: question=%s | configured=%s",
                q_lower[:160],
                configured or "<none>",
            )
            if configured is None:
                logger.info("Skipping unconfigured yes/no question: %s", q_lower[:120])
                continue
            should_yes = configured.lower() in ("yes", "true", "1")
            if configured.lower() not in ("yes", "true", "1", "no", "false", "0"):
                logger.warning(
                    "Skipping non-boolean configured answer for question: %s", q_lower[:120]
                )
                continue

            answer_label = "Yes" if should_yes else "No"

            target_btn = group.locator("button").filter(has_text=answer_label).first
            if target_btn.count() and target_btn.is_visible():
                try:
                    cls = target_btn.get_attribute("class") or ""
                    pressed = target_btn.get_attribute("aria-pressed")
                    if "_active_" not in cls and "_selected_" not in cls and pressed != "true":
                        target_btn.scroll_into_view_if_needed()
                        human_delay(0.1, 0.25)
                        target_btn.click()
                        human_delay(0.15, 0.3)
                        logger.info(
                            "Selected Yes/No option: %s",
                            answer_label,
                        )
                    continue
                except Exception as btn_err:
                    logger.debug(f"[_fill_yesno_groups] Button click failed: {btn_err}")

            if hidden_cb.count():
                try:
                    if should_yes:
                        hidden_cb.check(force=True, timeout=3000)
                    else:
                        hidden_cb.uncheck(force=True, timeout=3000)
                except Exception as cb_err:
                    logger.debug(f"[_fill_yesno_groups] Checkbox manipulation failed: {cb_err}")

        except Exception as e:
            logger.debug(f"[_fill_yesno_groups] Error processing group: {e}")


def _fill_radio_groups(page: Page, profile: Mapping[str, Any]) -> None:
    # Ashby visually hides many native radio inputs and renders their labels as
    # styled controls. Playwright can still check those inputs with force=True.
    radio_inputs = page.locator('input[type="radio"]').all()
    if not radio_inputs:
        return

    radio_groups: OrderedDict[str, list[Locator]] = OrderedDict()
    for radio in radio_inputs:
        name = radio.get_attribute("name") or ""
        if name not in radio_groups:
            radio_groups[name] = []
        radio_groups[name].append(radio)

    if not radio_groups:
        return

    for name, radios in radio_groups.items():
        try:
            container_text = radios[0].evaluate("""el => {
                const c = el.closest('.ashby-application-form-field-entry, fieldset, [role="group"], div[class*="Question"], div[class*="field"], div[class*="Field"], div[class*="Form"]') || el.parentElement?.parentElement;
                if (!c) return '';
                const title = c.querySelector('.ashby-application-form-question-title');
                return title ? title.innerText : c.innerText.substring(0, 300);
            }""")
            q_lower = container_text.lower()

            # Self-rating scales (e.g. "rate your proficiency 1-5") have no fixed
            # configured answer, so default to the most favorable (highest) option.
            if re.search(r"\b(?:rate|rating)\b", q_lower):
                numbered: list[tuple[float, Any]] = []
                for radio in radios:
                    radio_id = radio.get_attribute("id") or ""
                    label = page.locator(f'label[for="{radio_id}"]').first
                    text = (
                        label.inner_text().strip()
                        if label.count()
                        else radio.evaluate(
                            "el => el.closest('label,div')?.innerText || ''"
                        ).strip()
                    )
                    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)", text)
                    if match:
                        numbered.append((float(match.group(1)), radio))
                if numbered:
                    _, highest = max(numbered, key=lambda item: item[0])
                    highest.check(force=True)
                    logger.info("Selected highest numeric rating option.")
                    continue

            target_text = None
            if any(
                k in q_lower
                for k in (
                    "level",
                    "experience",
                    "proficiency",
                    "scale",
                    "rating",
                    "skill",
                    "knowledge",
                    "seniority",
                    "ability",
                    "competency",
                )
            ):
                target_text = _configured_answer(profile, q_lower)
            elif "pronoun" in q_lower:
                target_text = profile.get("pronouns")
            elif any(k in q_lower for k in ("lgbtq", "lgbt", "queer", "diversity")):
                target_text = profile.get("lgbtq")
            elif any(k in q_lower for k in ("sponsor", "visa", "immigration")):
                target_text = _configured_answer(profile, q_lower)
            elif any(k in q_lower for k in ("relocat", "onsite", "on-site")):
                target_text = _configured_answer(profile, q_lower)
            elif "location" in q_lower or "city" in q_lower or "based" in q_lower:
                target_text = (
                    _configured_answer(profile, q_lower)
                    or profile.get("location")
                    or profile.get("city")
                )
            elif any(k in q_lower for k in ("authorized", "legally", "eligible")):
                target_text = _configured_answer(profile, q_lower)
            elif "transgender" in q_lower:
                target_text = profile.get("transgender")
            elif any(k in q_lower for k in ("sexual orientation", "orientation")):
                target_text = profile.get("orientation")
            elif any(k in q_lower for k in ("gender", "sex")):
                target_text = profile.get("gender")
            elif any(k in q_lower for k in ("race", "ethnicity")):
                target_text = profile.get("race")
            elif any(k in q_lower for k in ("nationality", "citizenship")):
                target_text = profile.get("nationality") or profile.get("citizenship")
            else:
                target_text = _configured_answer(profile, q_lower)

            logger.info(
                "Radio group candidate: question=%s | configured=%s",
                q_lower[:160],
                target_text or "<none>",
            )
            if not target_text:
                logger.info("Skipping unconfigured radio question: %s", q_lower[:120])
                continue

            selected = False
            targets = list(plan_option_selection(target_text).candidates)

            for radio in radios:
                radio_id = radio.get_attribute("id") or ""
                label_for = page.locator(f'label[for="{radio_id}"]').first
                if label_for.count():
                    lbl_text = label_for.inner_text().strip()
                    normalized_label = re.sub(r"\s+", " ", lbl_text).strip().lower()
                    matched_target = any(
                        normalized_label == t.lower()
                        or normalized_label.startswith(t.lower())
                        or (
                            len(t.strip()) > 3
                            and re.search(
                                rf"(?<!\w){re.escape(t.lower())}(?!\w)",
                                normalized_label,
                            )
                        )
                        for t in targets
                    )
                    if matched_target:
                        if label_for.is_visible():
                            label_for.click()
                        if not radio.is_checked():
                            radio.check(force=True)
                        radio.dispatch_event("input")
                        radio.dispatch_event("change")
                        selected = radio.is_checked()
                        if selected:
                            logger.info("Selected radio option: %s", lbl_text)
                            break

            if not selected:
                for radio in radios:
                    parent_opt = radio.evaluate("""el => {
                        const opt = el.closest('div[class*="option"]') || el.parentElement;
                        return opt ? { text: opt.innerText.trim(), tag: opt.tagName } : null;
                    }""")
                    if parent_opt and any(
                        t.lower() in parent_opt.get("text", "").lower() for t in targets
                    ):
                        radio.check(force=True)
                        selected = True
                        break

            if not selected:
                page.evaluate(
                    """(params) => {
                    const radios = document.querySelectorAll(`input[type="radio"][name="${params.name}"]`);
                    for (const radio of radios) {
                        const label = radio.closest('div')?.innerText || radio.parentElement?.innerText || '';
                        if (params.targets.some(t => label.toLowerCase().includes(t.toLowerCase()))) {
                            radio.checked = true;
                            radio.dispatchEvent(new Event('change', { bubbles: true }));
                            radio.dispatchEvent(new Event('input', { bubbles: true }));
                            break;
                        }
                    }
                }""",
                    {"name": name, "targets": targets},
                )

        except Exception as e:
            logger.debug(f"[_fill_radio_groups] Error: {e}")


def _refresh_selected_choice_groups(page: Page) -> None:
    """Force React to register selected custom choices after validation rerenders."""
    for group in page.locator("div[class*='_yesno']").all():
        try:
            active = group.locator("button[class*='_active_']").first
            if not active.count():
                continue
            intended = active.inner_text().strip()
            alternate = (
                group.locator("button")
                .filter(has_not_text=re.compile(rf"^\s*{re.escape(intended)}\s*$", re.I))
                .first
            )
            if alternate.count() and alternate.is_visible():
                alternate.click()
                human_delay(0.1, 0.2)
            target = (
                group.locator("button")
                .filter(has_text=re.compile(rf"^\s*{re.escape(intended)}\s*$", re.I))
                .first
            )
            target.click()
            human_delay(0.1, 0.2)
            logger.info("Refreshed Yes/No selection: %s", intended)
        except Exception as exc:
            logger.debug("Could not refresh a Yes/No selection: %s", exc)

    radio_groups: OrderedDict[str, list[Any]] = OrderedDict()
    for radio in page.locator('input[type="radio"]').all():
        radio_groups.setdefault(radio.get_attribute("name") or "", []).append(radio)
    for radios in radio_groups.values():
        try:
            selected = next((radio for radio in radios if radio.is_checked()), None)
            radio_alternate = next(
                (radio for radio in radios if selected is not None and radio != selected),
                None,
            )
            if selected is None or radio_alternate is None:
                continue
            radio_alternate.check(force=True)
            human_delay(0.1, 0.2)
            selected.check(force=True)
            human_delay(0.1, 0.2)
            logger.info("Refreshed selected radio option.")
        except Exception as exc:
            logger.debug("Could not refresh a radio selection: %s", exc)


def _fill_configured_checkbox_groups(
    page: Page,
    profile: Mapping[str, Any],
) -> None:
    """Select explicitly configured options in custom checkbox groups."""
    groups = page.locator(".ashby-application-form-field-entry, fieldset, [role='group']").all()
    for group in groups:
        try:
            checkboxes = group.locator('input[type="checkbox"]')
            if not checkboxes.count():
                continue
            question = group.inner_text(timeout=1000)
            normalized_question = re.sub(r"\s+", " ", question).strip().lower()
            title = group.locator(".ashby-application-form-question-title").first
            semantic_question = (
                title.inner_text().strip() if title.count() else question.splitlines()[0].strip()
            )

            # "Why are you interested" checkbox groups are multi-select with no
            # single configured answer; checking every option is the safest way
            # to avoid understating interest instead of guessing which apply.
            if "why" in normalized_question and "interested" in normalized_question:
                for index in range(checkboxes.count()):
                    checkbox = checkboxes.nth(index)
                    if not checkbox.is_checked():
                        checkbox.check(force=True)
                logger.info("Selected all checkbox options for interest question.")
                continue

            if "preferred work location" in normalized_question:
                for index in range(checkboxes.count()):
                    checkbox = checkboxes.nth(index)
                    checkbox_id = checkbox.get_attribute("id") or ""
                    label = (
                        page.locator(f'label[for="{checkbox_id}"]').first
                        if checkbox_id
                        else checkbox.locator("xpath=ancestor::label[1]")
                    )
                    option_text = (
                        label.inner_text().strip()
                        if label.count()
                        else checkbox.evaluate(
                            "el => el.closest('label,div')?.innerText || ''"
                        ).strip()
                    )
                    if _is_preferred_work_region(option_text) and not checkbox.is_checked():
                        checkbox.check(force=True)
                        logger.info(
                            "Selected preferred work location: %s",
                            option_text,
                        )
                continue

            configured = _configured_answer(profile, semantic_question)
            if not configured:
                continue
            requested = [value.strip() for value in re.split(r"[,;]", configured) if value.strip()]
            for value in requested:
                label = (
                    group.locator("label").filter(has_text=re.compile(re.escape(value), re.I)).first
                )
                if not label.count() or not label.is_visible():
                    continue
                checkbox = label.locator('input[type="checkbox"]').first
                if not checkbox.count():
                    checkbox_id = label.get_attribute("for") or ""
                    if checkbox_id:
                        checkbox = group.locator(
                            f'input[type="checkbox"][id="{checkbox_id}"]'
                        ).first
                if checkbox.count() and not checkbox.is_checked():
                    checkbox.check(force=True)
                    logger.info("Configured checkbox selected: %s", value)
                elif not checkbox.count():
                    label.click()
                    logger.info(
                        "Configured checkbox selected through its label: %s",
                        value,
                    )
        except Exception as exc:
            logger.debug("Configured checkbox group failed: %s", exc)


def _is_preferred_work_region(option_text: str) -> bool:
    """Return whether a location is in the configured US/Europe/Asia/Australia scope."""
    normalized = re.sub(r"\s+", " ", option_text).strip().lower()
    region_terms = (
        # United States
        "united states",
        "remote us",
        "u.s.",
        "usa",
        "san francisco",
        "new york",
        "seattle",
        "raleigh",
        "austin",
        "boston",
        "chicago",
        "denver",
        "los angeles",
        "miami",
        "portland",
        "washington",
        # Europe
        "europe",
        "emea",
        "london",
        "amsterdam",
        "berlin",
        "paris",
        "dublin",
        "madrid",
        "munich",
        "zurich",
        "stockholm",
        "warsaw",
        # Asia
        "asia",
        "apac",
        "singapore",
        "tokyo",
        "seoul",
        "hong kong",
        "bangalore",
        "bengaluru",
        "hyderabad",
        "mumbai",
        "delhi",
        # Australia
        "australia",
        "sydney",
        "melbourne",
        "brisbane",
        "perth",
    )
    return any(term in normalized for term in region_terms)


def _select_highest_numeric_combobox(page: Page, inp: Any) -> bool:
    """Open a rating combobox and choose the option with the largest number."""
    try:
        inp.click()
        human_delay(0.2, 0.4)
        options = page.locator(
            '[role="option"]:visible, div[class*="_option_"]:visible, '
            'div[class*="_result_"]:visible'
        )
        options.first.wait_for(state="visible", timeout=3000)
        numbered: list[tuple[float, Any]] = []
        for index in range(options.count()):
            option = options.nth(index)
            text = option.inner_text().strip()
            match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)", text)
            if match:
                numbered.append((float(match.group(1)), option))
        if not numbered:
            page.keyboard.press("Escape")
            return False
        _, highest = max(numbered, key=lambda item: item[0])
        highest.click()
        logger.info("Selected highest numeric rating dropdown option.")
        return True
    except Exception as exc:
        logger.debug("Highest-rating combobox selection failed: %s", exc)
        return False


def fill_eeo(page: Page, profile: Mapping[str, Any]) -> None:
    """Fill the EEO/self-identification section, if present, without overwriting existing selections."""
    eeo_section = page.locator(
        "h1, h2, h3, h4, legend, [class*='heading' i], [class*='title' i]"
    ).filter(
        has_text=re.compile(
            r"equal employment opportunity|self[- ]identification|"
            r"demographic information|veteran status",
            re.I,
        )
    )
    if not eeo_section.count():
        logger.debug("No EEO/self-identification section detected.")
        return

    def _sel(kw: str, val: str) -> None:
        if not val:
            return

        if val.lower() in ("male", "man"):
            regex_pat = r"\b(male|man)\b"
        elif val.lower() in ("asian", "asian or asian american"):
            regex_pat = r"\basian\b"
        else:
            regex_pat = rf"^{re.escape(val)}$|{re.escape(val)}"

        groups = page.locator(
            "fieldset, [role='group'], div[class*='field'], div[class*='Field']"
        ).filter(has_text=re.compile(kw, re.I))
        for i in range(min(groups.count(), 5)):
            grp = groups.nth(i)
            lab = (
                grp.locator("label, [role='option'], button")
                .filter(has_text=re.compile(regex_pat, re.I))
                .first
            )
            if lab.count() and lab.is_visible():
                selected_input = lab.locator("input[type='radio'], input[type='checkbox']").first
                if not selected_input.count():
                    input_id = lab.get_attribute("for")
                    if input_id:
                        selected_input = page.locator(
                            f'[id="{input_id}"][type="radio"], [id="{input_id}"][type="checkbox"]'
                        ).first
                if selected_input.count() and selected_input.is_checked():
                    logger.info("EEO %s already selected; preserving selection.", kw)
                    return
                click(lab, f"EEO {kw}={val}")
                return

            # Current Ashby forms render EEO values as custom dropdowns. Open
            # the field-local trigger and select from the portal listbox.
            trigger = grp.locator(
                'button:visible, [role="combobox"]:visible, input[role="combobox"]:visible'
            ).first
            if trigger.count():
                variants = answer_variants(
                    kw,
                    val,
                    profile.get("_answer_variants", {}),
                )
                if click(trigger, f"Open EEO {kw} dropdown"):
                    human_delay(0.2, 0.4)
                    options = page.locator(
                        '[role="option"]:visible, [role="menuitem"]:visible, '
                        'div[class*="_option_"]:visible'
                    )
                    for variant in variants:
                        option = options.filter(has_text=re.compile(re.escape(variant), re.I)).first
                        if option.count() and click(option, f"EEO {kw}={variant}"):
                            return
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass

        logger.info("EEO option not found for %s; leaving the field for review.", kw)

    try:
        _sel("veteran", str(profile.get("veteran") or ""))
        _sel(r"race|ethnicity", str(profile.get("race") or ""))
        _sel("gender", str(profile.get("gender") or ""))
        _sel("transgender", str(profile.get("transgender") or ""))
        _sel(r"orientation|sexual", str(profile.get("orientation") or ""))
        _sel(r"disability|disabled", str(profile.get("disability") or ""))

        comm_groups = page.locator("fieldset, [role='group'], div[class*='field']").filter(
            has_text=re.compile(r"community|communities|identity|neurodiverse", re.I)
        )
        for i in range(min(comm_groups.count(), 3)):
            grp = comm_groups.nth(i)
            for opt in profile.get("communities", []):
                cb = grp.locator("label").filter(has_text=re.compile(re.escape(opt), re.I)).first
                if cb.count() and cb.is_visible():
                    inp = cb.locator("input[type='checkbox']")
                    if not inp.count() or not inp.is_checked():
                        click(cb, f"Community option: {opt}")
    except Exception as e:
        logger.warning(f"EEO partial failure: {e}")


def _attach_file(
    page: Page,
    field: Locator,
    file_input: Locator,
    path: Path,
    label: str,
) -> bool:
    """Attach one file and wait for Ashby's widget to confirm the upload."""
    try:
        existing_text = field.inner_text() if field.count() else ""
        if path.name.lower() in existing_text.lower():
            logger.info("%s attachment already present.", label)
            return True
        upload_button = field.get_by_text(re.compile(r"^\s*Upload File\s*$", re.I)).first
        used_file_chooser = False
        if upload_button.count() and upload_button.is_visible():
            try:
                with page.expect_file_chooser(timeout=5000) as chooser_info:
                    upload_button.click()
                chooser_info.value.set_files(str(path))
                used_file_chooser = True
            except Exception as chooser_exc:
                logger.debug("%s file chooser path failed: %s", label, chooser_exc)
        if not used_file_chooser:
            file_input.set_input_files(str(path))

        for _ in range(20):
            time.sleep(0.5)
            field_text = field.inner_text() if field.count() else ""
            if path.name.lower() in field_text.lower():
                logger.info("%s uploaded and attached: %s", label, path.name)
                return True
        logger.warning(
            "%s input accepted the file, but Ashby did not render an attachment confirmation.",
            label,
        )
    except Exception as exc:
        logger.error("%s upload error: %s", label, exc)
    return False


def _extract_cover_letter_text(path: Path) -> str:
    """Extract the generated letter for Ashby forms that use a text area."""
    try:
        return "\n\n".join(
            text
            for page in PdfReader(str(path)).pages
            if (text := (page.extract_text() or "").strip())
        ).strip()
    except Exception as exc:
        logger.warning("Could not extract cover-letter PDF text: %s", exc)
        return ""


def fill_personal_and_files(
    page: Page,
    profile: Mapping[str, Any],
    email: str,
    resume: Path,
    cover_letter: Path | None = None,
) -> dict[str, bool]:
    """Fill identity fields and attach the prepared resume and cover letter."""
    flags = {
        "name": False,
        "email": False,
        "phone": False,
        "resume": False,
        "cover_letter": cover_letter is None,
    }

    resume_loc = page.locator(
        'input[id="_systemfield_resume"], input[name="_systemfield_resume"]'
    ).first
    resume_field = resume_loc.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
        "' ashby-application-form-field-entry ')][1]"
    )
    if not resume_loc.count():
        resume_field = (
            page.locator(".ashby-application-form-field-entry")
            .filter(has_text=re.compile(r"^\s*Resume\b", re.I))
            .first
        )
        resume_loc = resume_field.locator('input[type="file"]').first
    if not resume_loc.count():
        for finp in page.locator('input[type="file"]').all():
            lbl = finp.evaluate(
                "el => (el.closest('div')||el.parentElement||el).innerText||''"
            ).lower()
            if "autofill" not in lbl and "cover" not in lbl:
                resume_loc = finp
                resume_field = finp.locator(
                    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                    "' ashby-application-form-field-entry ')][1]"
                )
                break

    if resume_loc.count():
        flags["resume"] = _attach_file(
            page,
            resume_field,
            resume_loc,
            resume,
            "Resume",
        )
    else:
        # Some Ashby applications intentionally omit a resume field. Absence
        # is not equivalent to a failed required upload.
        flags["resume"] = True
        logger.info("This application does not request a resume upload.")

    if cover_letter is not None:
        cover_field = (
            page.locator(".ashby-application-form-field-entry")
            .filter(has_text=re.compile(r"\bcover\s+letter\b", re.I))
            .first
        )
        cover_input = cover_field.locator('input[type="file"]').first
        if cover_input.count():
            flags["cover_letter"] = _attach_file(
                page,
                cover_field,
                cover_input,
                cover_letter,
                "Cover letter",
            )
        else:
            flags["cover_letter"] = True
            logger.info("This application does not request a cover-letter upload.")

    fn_loc = page.locator(
        'input[id*="first" i], input[name*="first" i], input[aria-label*="First Name" i]'
    ).first
    ln_loc = page.locator(
        'input[id*="last" i], input[name*="last" i], input[aria-label*="Last Name" i]'
    ).first
    pref_loc = page.locator(
        'input[id*="preferred" i], input[name*="preferred" i], input[aria-label*="Preferred" i]'
    ).first

    if not fn_loc.count() or not ln_loc.count():
        for inp in page.locator("input:visible").all():
            lbl = inp.evaluate(
                "el => (el.labels?.[0]?.innerText || el.closest('div') || el.parentElement || el).innerText || ''"
            ).lower()
            if (
                not fn_loc.count()
                and any(k in lbl for k in ("first name", "given name"))
                and "last" not in lbl
            ):
                fn_loc = inp
            elif (
                not ln_loc.count()
                and any(k in lbl for k in ("last name", "family name", "surname"))
                and "first" not in lbl
            ):
                ln_loc = inp
            elif not pref_loc.count() and any(
                k in lbl for k in ("preferred name", "nickname", "goes by", "alias")
            ):
                pref_loc = inp

    if fn_loc.count() and fn_loc.is_visible():
        first_name = profile.get("first_name", "")
        if first_name:
            fill(page, fn_loc, first_name)
            flags["first_name"] = verify_value(fn_loc, first_name, "First Name")
    if ln_loc.count() and ln_loc.is_visible():
        last_name = profile.get("last_name", "")
        if last_name:
            fill(page, ln_loc, last_name)
            flags["last_name"] = verify_value(ln_loc, last_name, "Last Name")
    if pref_loc.count() and pref_loc.is_visible():
        preferred_name = profile.get("preferred_name", "")
        if preferred_name:
            fill(page, pref_loc, preferred_name)
            flags["preferred_name"] = verify_value(pref_loc, preferred_name, "Preferred Name")

    name_loc = page.locator('input[id="_systemfield_name"], input[name="_systemfield_name"]').first
    if not name_loc.count():
        for inp in page.locator("input:visible").all():
            lbl = inp.evaluate(
                "el => (el.closest('div')||el.parentElement||el).innerText||''"
            ).lower()
            if (
                "name" in lbl
                and "company" not in lbl
                and "file" not in lbl
                and "first" not in lbl
                and "last" not in lbl
                and "preferred" not in lbl
            ):
                name_loc = inp
                break
    if name_loc.count() and name_loc.is_visible():
        full = " ".join(
            part for part in (profile.get("first_name", ""), profile.get("last_name", "")) if part
        )
        if full:
            fill(page, name_loc, full)
            flags["name"] = verify_value(name_loc, profile.get("first_name", ""), "Name")
    else:
        flags["name"] = flags.get("first_name", False) and flags.get("last_name", False)

    email_loc = page.locator(
        'input[id="_systemfield_email"], input[name="_systemfield_email"], input[type="email"]'
    ).first
    if not email_loc.count():
        for inp in page.locator("input:visible").all():
            lbl = inp.evaluate(
                "el => (el.closest('div')||el.parentElement||el).innerText||''"
            ).lower()
            if "email" in lbl:
                email_loc = inp
                break
    if email_loc.count():
        fill(page, email_loc, email)
        flags["email"] = verify_value(email_loc, email.split("@")[0], "Email")

    phone_loc = page.locator(
        'input[id="_systemfield_phone"], input[name*="phone" i], input[type="tel"]'
    ).first
    if not phone_loc.count():
        for inp in page.locator("input:visible").all():
            lbl = inp.evaluate(
                "el => (el.closest('div')||el.parentElement||el).innerText||''"
            ).lower()
            if "phone" in lbl:
                phone_loc = inp
                break
    if phone_loc.count():
        phone = profile.get("phone", "")
        if phone:
            fill(page, phone_loc, phone)
            flags["phone"] = verify_value(phone_loc, phone[-4:], "Phone")

    loc_input = page.locator(
        'input[id="_systemfield_location"], input[name="_systemfield_location"], input[id*="location" i], input[name*="location" i], input[placeholder*="location" i], input[placeholder*="city" i], input[aria-label*="location" i]'
    ).first
    if not loc_input.count() or not loc_input.is_visible():
        location_field = (
            page.locator(".ashby-application-form-field-entry")
            .filter(
                has_text=re.compile(
                    r"where are you currently located|current location|"
                    r"where are you located|where are you based",
                    re.I,
                )
            )
            .first
        )
        if location_field.count():
            loc_input = location_field.locator('input[type="text"], input[role="combobox"]').first
    if not loc_input.count() or not loc_input.is_visible():
        for inp in page.locator("input:visible").all():
            lbl = inp.evaluate("""el => {
                const direct = el.labels?.[0]?.innerText || '';
                const container = (
                    el.closest('.ashby-application-form-field-entry')
                    || el.parentElement
                    || el
                ).innerText || '';
                return `${direct} ${container}`.toLowerCase();
            }""")
            if any(
                k in lbl
                for k in (
                    "where are you currently located",
                    "current location",
                    "where are you located",
                    "where are you based",
                    "location",
                )
            ):
                loc_input = inp
                break
    if loc_input.count() and loc_input.is_visible():
        if not loc_input.input_value().strip():
            location = profile.get("location") or profile.get("city", "")
            if location:
                selected = select_ashby_combobox(
                    page,
                    loc_input,
                    location,
                    fallback_value=profile.get("country") or profile.get("city"),
                )
                flags["location"] = selected and verify_value(
                    loc_input,
                    location.split(",")[0],
                    "Location",
                )
                if not flags["location"]:
                    logger.warning("Location remains unselected after combobox attempt.")

    li = page.locator(
        'input[id*="linkedin" i], input[name*="linkedin" i], input[placeholder*="linkedin" i]'
    ).first
    if not li.count() or not li.is_visible():
        for inp in page.locator("input:visible").all():
            lbl = inp.evaluate("""el => {
                const l = el.labels?.[0]?.innerText || '';
                const p = (el.closest('div[class*="Container"], div[class*="field"], div[class*="Field"], div[class*="Form"]')||el.parentElement||el).innerText || '';
                return (l + ' ' + p).toLowerCase();
            }""")
            if "linkedin" in lbl:
                li = inp
                break
    if not li.count() or not li.is_visible():
        for inp in page.locator('input[type="text"]:visible').all():
            ctx = inp.evaluate("""el => {
                let node = el;
                for (let i = 0; i < 5 && node; i++) {
                    const txt = node.innerText || '';
                    if (txt.length > 5 && txt.length < 200) return txt;
                    node = node.parentElement;
                }
                return '';
            }""")
            if "linkedin" in ctx.lower():
                li = inp
                break
    if li.count() and li.is_visible():
        li_type = (li.get_attribute("type") or "text").lower()
        if li_type not in ("checkbox", "radio", "hidden", "submit", "button"):
            linkedin = profile.get("linkedin", "")
            if linkedin:
                fill(page, li, linkedin)
                flags["linkedin"] = verify_value(li, "linkedin.com", "LinkedIn")

    for inp in page.locator("input:visible").all():
        try:
            context = inp.evaluate("""el => {
                const label = el.labels?.[0]?.innerText || '';
                const field = (
                    el.closest('.ashby-application-form-field-entry')
                    || el.parentElement
                    || el
                ).innerText || '';
                return `${label} ${field}`.toLowerCase();
            }""")
            if "confirm your email" in context or "confirm email" in context:
                fill(page, inp, email)
                flags["email_confirmation"] = verify_value(
                    inp,
                    email,
                    "Email confirmation",
                )
                break
        except Exception as exc:
            logger.debug("Email confirmation fill failed: %s", exc)

    return flags


def fill_consent_checkboxes(
    page: Page,
    profile: Mapping[str, Any] | None = None,
) -> None:
    """Check only explicit consent/legal acknowledgements, never EEO selections."""
    if page.is_closed():
        return
    selected = fill_required_consent(page)
    if selected:
        logger.info("Checked %d consent/legal checkbox(es).", len(selected))
    if profile is None:
        return
    radio_names = {
        radio.get_attribute("name") or "" for radio in page.locator('input[type="radio"]').all()
    }
    for name in radio_names:
        if not name:
            continue
        radios = page.locator(f'input[type="radio"][name="{name}"]')
        try:
            context = radios.first.evaluate("""el => (
                el.closest('.ashby-application-form-field-entry, fieldset')
                || el.parentElement
                || el
            ).innerText || ''""")
            if "text message" not in context.lower():
                continue
            configured = _configured_answer(profile, context)
            answer = _boolean_rule_answer(configured)
            if not answer:
                continue
            for index in range(radios.count()):
                radio = radios.nth(index)
                label_text = radio.evaluate("el => el.closest('label')?.innerText || ''")
                if re.search(rf"\b{answer}\b", label_text, re.I):
                    radio.check(force=True)
                    logger.info("Selected SMS consent response: %s", answer)
                    break
        except Exception as exc:
            logger.debug("SMS consent selection failed: %s", exc)


def fill_secondary(
    page: Page,
    profile: Mapping[str, Any],
    defaults: Mapping[str, Any],
    essay: str,
    company: str = "",
    role: str = "",
) -> None:
    """Fill remaining non-identity fields: dates, comboboxes, essay questions, and other free-text/number screening inputs."""
    if page.is_closed():
        return

    try:
        jd_text = page.evaluate("() => document.body.innerText || ''")
    except Exception as exc:
        logger.debug("Could not read job description text: %s", exc)
        jd_text = ""

    start_date_str = str(profile.get("available_start_date") or "").strip()

    for inp in page.locator(
        'input[placeholder*="date" i]:visible, input[placeholder*="pick" i]:visible, input[type="date"]:visible, input[id*="start" i]:visible, input[name*="start" i]:visible'
    ).all():
        try:
            ctx = inp.evaluate("""el => {
                const field = el.closest(
                    '.ashby-application-form-field-entry, fieldset, [role="group"]'
                );
                if (field) {
                    const text = (field.innerText || "").trim();
                    if (text) return text.replace(/\\n/g, " ").toLowerCase();
                }
                let curr = el;
                for (let i = 0; i < 5 && curr; i++) {
                    let t = (curr.innerText || "").trim();
                    if (t.length > 2 && t.length < 300) return t.replace(/\\n/g, " ").toLowerCase();
                    curr = curr.parentElement;
                }
                return "";
            }""")
            if start_date_str and any(
                k in ctx for k in ("start", "available", "when can you", "start date", "date")
            ):
                if not inp.input_value().strip():
                    fill(page, inp, start_date_str)
                    human_delay(0.2, 0.4)
                    inp.press("Enter")
                    logger.info("Filled configured start date.")
        except Exception as exc:
            logger.debug("Start-date field processing failed: %s", exc)

    for inp in page.locator(
        'input[placeholder*="Start typing"]:visible, input[placeholder*="type"]:visible, input[role="combobox"]:visible, input[name*="location" i]:visible, input[id*="location" i]:visible, input[placeholder*="location" i]:visible, input[placeholder*="city" i]:visible'
    ).all():
        try:
            ctx = inp.evaluate("""el => {
                const field = el.closest(
                    '.ashby-application-form-field-entry, fieldset, [role="group"]'
                );
                if (field) {
                    const text = (field.innerText || "").trim();
                    if (text) return text.replace(/\\n/g, " ").toLowerCase();
                }
                let curr = el;
                for (let i = 0; i < 5 && curr; i++) {
                    let t = (curr.innerText || "").trim();
                    if (t.length > 2 && t.length < 300) return t.replace(/\\n/g, " ").toLowerCase();
                    curr = curr.parentElement;
                }
                return "";
            }""")
            configured_combobox = _configured_answer(profile, ctx)
            if configured_combobox and not inp.input_value().strip():
                if select_ashby_combobox(page, inp, configured_combobox):
                    continue
            if re.search(r"\b(?:rate|rating)\b", ctx):
                if _select_highest_numeric_combobox(page, inp):
                    continue
            if any(k in ctx for k in ("hear", "referral", "source", "how did you")):
                source = defaults.get("source", "")
                if source:
                    select_ashby_combobox(page, inp, source)
            elif is_location_question(ctx):
                if not inp.input_value().strip():
                    location = (
                        _configured_answer(profile, ctx)
                        or profile.get("location")
                        or profile.get("city", "")
                    )
                    if location:
                        select_ashby_combobox(
                            page, inp, location, fallback_value=profile.get("city")
                        )
            elif any(k in ctx for k in ("nationality", "citizenship", "country of citizenship")):
                if not inp.input_value().strip():
                    nationality = profile.get("nationality") or profile.get("citizenship", "")
                    if nationality:
                        select_ashby_combobox(page, inp, nationality)
        except Exception as exc:
            logger.debug("Secondary combobox processing failed: %s", exc)

    essay_dict: dict[str, Any] = {}
    default_essay = essay.strip() or defaults.get("product_area_essay", "")
    if essay.strip().startswith("{") and essay.strip().endswith("}"):
        try:
            parsed_essay = json.loads(essay.strip())
            if not isinstance(parsed_essay, dict):
                raise ValueError("Essay JSON must be an object.")
            essay_dict = parsed_essay
            default_essay = essay_dict.get("default", default_essay)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Ignoring invalid essay JSON: %s", exc)

    essay_controls = page.locator(
        'textarea:visible, input[placeholder*="type here" i]:visible'
    ).all()
    for idx, ta in enumerate(essay_controls, 1):
        try:
            # Ashby commonly gives both identity inputs and long-answer inputs
            # the placeholder "Type here". Never overwrite a value already
            # populated by the personal-information pass.
            if ta.input_value().strip():
                continue
            lbl = ta.evaluate("""el => {
                const l = el.labels?.[0]?.innerText || '';
                const p = (
                    el.closest('.ashby-application-form-field-entry, fieldset, [role="group"]')
                    || el.closest('div[class*="Container"], div[class*="field"], div[class*="Form"]')
                    || el.parentElement
                    || el
                ).innerText || '';
                return (l + ' ' + p).toLowerCase();
            }""")
            if any(t in lbl for t in ("hear", "source", "referral")):
                source = defaults.get("source", "")
                if source:
                    fill(page, ta, source)
            else:
                configured_essay = _configured_answer(profile, lbl)
                val = (
                    configured_essay
                    or (profile.get("_cover_letter_text") if "cover letter" in lbl else None)
                    or (
                        essay_dict.get("why")
                        if any(w in lbl for w in ("why", "interest", "mission", "fit", "inspire"))
                        else None
                    )
                    or (
                        essay_dict.get("product")
                        if any(
                            w in lbl
                            for w in (
                                "product",
                                "build",
                                "excellence",
                                "signal",
                                "achievement",
                                "project",
                                "experience",
                            )
                        )
                        else None
                    )
                    or essay_dict.get(f"essay_{idx}")
                    or essay_dict.get("default")
                    or default_essay
                    or generate_essay_safely(lbl, jd_text, company, role)
                )
                if val:
                    logger.info("Filling essay response for field %d.", idx)
                    fill(page, ta, val)
                else:
                    logger.info("Leaving unconfigured essay field %d for review.", idx)
        except Exception as exc:
            logger.debug("Essay field %d processing failed: %s", idx, exc)

    for inp in page.locator("input:visible").all():
        try:
            itype = (inp.get_attribute("type") or "text").lower()
            if itype in ("file", "radio", "checkbox", "hidden", "submit", "button"):
                continue
            # Re-selecting an already populated Ashby combobox causes React to
            # rebuild the form. Browser file inputs cannot survive that rebuild,
            # so a later fill pass used to remove an attached resume.
            if inp.input_value().strip():
                continue
            lbl = inp.evaluate("""el => {
                let curr = el;
                for (let i = 0; i < 5 && curr; i++) {
                    let t = (curr.innerText || "").trim();
                    if (t.length > 2 && t.length < 300) return t.replace(/\\n/g, " ").toLowerCase();
                    curr = curr.parentElement;
                }
                return "";
            }""")
            salary_raw = profile.get("compensation") or defaults.get("salary", "")
            configured_salary = (
                _configured_answer(profile, lbl)
                if any(
                    marker in lbl
                    for marker in (
                        "salary",
                        "compensation",
                        "pay expectation",
                        "desired pay",
                    )
                )
                else None
            )
            salary_val = configured_salary or (
                extract_lowest_salary(
                    salary_raw,
                    profile.get("location", ""),
                )
                if salary_raw
                else ""
            )
            languages = profile.get("languages", "")
            if isinstance(languages, list):
                languages = ", ".join(str(language) for language in languages)
            mapping = [
                (
                    (
                        "current/most recent company",
                        "current/last company",
                        "current company",
                    ),
                    profile.get("current_company"),
                ),
                (
                    ("current/most recent job title", "current job title"),
                    profile.get("current_job_title"),
                ),
                (
                    ("phonetic spelling", "pronounce"),
                    _configured_answer(profile, lbl),
                ),
                (
                    ("pronoun", "pronouns"),
                    profile.get("pronouns") if "pronounce" not in lbl else None,
                ),
                (("lgbtq", "lgbt", "sexual orientation"), profile.get("lgbtq")),
                (("birth", "dob", "date of birth"), profile.get("birthday")),
                (("preferred", "nickname", "goes by", "alias"), profile.get("preferred_name")),
                (("first name", "given name"), profile.get("first_name")),
                (("last name", "family name", "surname"), profile.get("last_name")),
                (
                    ("nationality", "citizenship"),
                    profile.get("nationality") or profile.get("citizenship"),
                ),
                (("country",), profile.get("country")),
                (("linkedin",), profile.get("linkedin")),
                (("other url", "other website", "website", "goodreads"), profile.get("website")),
                (
                    ("portfolio", "researchgate"),
                    profile.get("portfolio") or profile.get("researchgate"),
                ),
                (("twitter", "x.com"), profile.get("twitter")),
                (("sciencedirect",), profile.get("sciencedirect")),
                (
                    ("street address", "address_line_1", "address line 1"),
                    profile.get("street_address"),
                ),
                (("suite", "apt", "unit", "address line 2"), profile.get("address_2")),
                ((r"\bcity\b|\blocation\b",), profile.get("location") or profile.get("city")),
                (
                    (r"\bstate\b|\bregion\b",),
                    profile.get("state") if "statement" not in lbl else None,
                ),
                ((r"\bzip\b|\bpostal\b",), profile.get("zip_code")),
                (("language", "languages"), languages),
                (("salary", "compensation", "pay expectation", "desired pay"), salary_val),
            ]
            matched = False
            for keys, val in mapping:
                if val and any(re.search(k, lbl) if k.startswith("\\") else k in lbl for k in keys):
                    if any(
                        loc_k in lbl
                        for loc_k in (
                            "location",
                            "where are you",
                            "city",
                            "state",
                            "region",
                            "country",
                        )
                    ):
                        if not inp.input_value().strip():
                            configured_location = _configured_answer(profile, lbl)
                            select_ashby_combobox(
                                page,
                                inp,
                                configured_location or val,
                                fallback_value=profile.get("city"),
                            )
                    else:
                        fill(page, inp, val)
                    matched = True
                    break

            if not matched and not inp.input_value().strip():
                if itype == "number":
                    rating_numbers = inp.evaluate("""el => {
                        const field = el.closest(
                            '.ashby-application-form-field-entry, fieldset, [role="group"]'
                        );
                        const text = field ? (field.innerText || '') : '';
                        if (!/\\b(?:rate|rating)\\b/i.test(text)) return [];
                        return Array.from(
                            text.matchAll(/(?:^|\\n)\\s*(\\d+(?:\\.\\d+)?)\\s*[–—-]/g),
                            match => Number(match[1])
                        ).filter(Number.isFinite);
                    }""")
                    if rating_numbers:
                        highest_rating = str(max(rating_numbers))
                        fill(page, inp, highest_rating)
                        logger.info(
                            "Selected highest numeric rating: %s",
                            highest_rating,
                        )
                        continue
                configured = _configured_answer(profile, lbl)
                if configured:
                    if itype == "number":
                        numeric = re.search(r"-?\d+(?:\.\d+)?", configured)
                        configured = numeric.group(0) if numeric else ""
                    if configured:
                        fill(page, inp, configured)
                elif any(
                    w in lbl for w in ("hear", "source", "referral", "how did")
                ) and defaults.get("source"):
                    fill(page, inp, defaults["source"])
        except Exception as exc:
            logger.debug("Secondary text input processing failed: %s", exc)

    def _button_is_selected(btn: Any) -> bool:
        try:
            pressed = btn.get_attribute("aria-pressed")
            classes = (btn.get_attribute("class") or "").lower()
            return pressed == "true" or "selected" in classes or "_active_" in classes
        except Exception as exc:
            logger.debug("Could not verify yes/no button state: %s", exc)
            return False

    _fill_yesno_groups(page, profile)
    _fill_radio_groups(page, profile)
    _fill_configured_checkbox_groups(page, profile)

    # The passes above only handle groups backed by native radio/checkbox
    # inputs. Some Ashby questions render as plain buttons/[role="radio"]
    # elements with no underlying input, so re-scan and handle those here,
    # skipping anything already covered (filtered out below).
    processed_texts = set()
    for field in page.locator(
        "fieldset, [role='group'], div[class*='field'], div[class*='Field'], div[class*='Question'], div[class*='_yesno']"
    ).all():
        try:
            txt = field.evaluate("el => el.innerText || ''").strip()
            if not txt or txt in processed_texts:
                continue
            processed_texts.add(txt)
            q_lower = txt.lower()

            cls = field.get_attribute("class") or ""
            if (
                "_yesno" in cls
                or field.locator('input[type="radio"], input[type="checkbox"]').count()
            ):
                continue

            no_btn = field.locator("button:text-is('No'), [role='radio']:has-text('No')").first
            yes_btn = field.locator("button:text-is('Yes'), [role='radio']:has-text('Yes')").first
            configured = _configured_answer(profile, q_lower)
            exact_option = (
                field.locator("[role='radio'], button, label")
                .filter(
                    has_text=re.compile(
                        rf"^\s*{re.escape(configured)}\s*$",
                        re.I,
                    )
                )
                .first
                if configured
                else None
            )
            if exact_option is not None and exact_option.count():
                if click(exact_option, f"Configured option: {configured}"):
                    human_delay(0.15, 0.3)
            elif configured and configured.lower() in ("yes", "true", "1") and yes_btn.count():
                if click(yes_btn, "Configured yes/no answer"):
                    human_delay(0.15, 0.3)
                    if not _button_is_selected(yes_btn):
                        logger.debug("Yes button did not expose selected state.")
            elif configured and configured.lower() in ("no", "false", "0") and no_btn.count():
                if click(no_btn, "Configured yes/no answer"):
                    human_delay(0.15, 0.3)
                    if not _button_is_selected(no_btn):
                        logger.debug("No button did not expose selected state.")
            elif not configured:
                logger.info("Skipping unconfigured yes/no question: %s", q_lower[:120])
        except Exception as exc:
            logger.debug("Button-style yes/no processing failed: %s", exc)


def _locate_submit_btn(page: Page) -> Locator:
    btn = page.locator('button[class*="_submitButton"], button[class*="submit-button"]').first
    if btn.count() and btn.is_visible():
        return btn
    btn = page.locator('button:has-text("Submit Application"):visible').first
    if btn.count() and btn.is_visible():
        return btn
    btn = (
        page.locator('button[type="submit"]:visible')
        .filter(
            has_not=page.locator(
                'button[class*="_option"], button[class*="_remove"], button[class*="_secondary"]'
            )
        )
        .first
    )
    if btn.count() and btn.is_visible():
        return btn
    btn = page.locator('button:has-text("Submit"):visible').first
    return btn


def can_advance(page: Page) -> bool:
    if _shutdown:
        return False
    next_btn = page.locator(
        'button:has-text("Next"):visible, button:has-text("Continue"):visible, button:has-text("Save and continue"):visible, button:has-text("Save & Continue"):visible'
    ).first
    submit_btn = _locate_submit_btn(page)
    if submit_btn.count() and submit_btn.is_visible():
        return False
    if next_btn.count() and next_btn.is_visible():
        click(next_btn, "Next/Continue")
        try:
            page.wait_for_load_state("networkidle", timeout=7000)
        except PlaywrightTimeout:
            pass
        time.sleep(1.2)
        human_delay(0.5, 0.9)
        return True
    return False


def _open_browser_session(
    playwright: Any,
    target_url: str = "",
) -> PlaywrightBrowserSession:
    """Open the shared visible Chrome/CDP-first ATS browser session."""
    return open_chrome_session(
        playwright,
        cdp_url=CDP_URL,
        profile_name="ashby-cdp-profile",
        target_url=target_url,
    )


def _education_field_container(group: Any, label_pattern: str) -> Any:
    label = (
        group.locator("label")
        .filter(has_text=re.compile(rf"^\s*{label_pattern}\s*\*?\s*$", re.I))
        .first
    )
    if not label.count():
        return (
            group.locator(".ashby-application-form-field-entry")
            .filter(has_text=re.compile(label_pattern, re.I))
            .first
        )
    # Education is one repeatable Ashby field entry containing several nested
    # controls. Returning the outer field entry makes every lookup resolve to
    # the first control (School), so Degree and the date selectors accidentally
    # overwrite the school combobox. The immediate label wrapper is the
    # control-local container in current Ashby forms.
    return label.locator("xpath=..")


def _choose_education_option(
    page: Page,
    container: Any,
    value: str,
    *,
    control_index: int = 0,
) -> bool:
    select = container.locator("select").nth(control_index)
    if select.count():
        try:
            select.select_option(label=value)
            return True
        except Exception:
            pass

    controls = container.locator(
        'input[role="combobox"], input[aria-autocomplete="list"], '
        'button[aria-haspopup="listbox"], button'
    )
    if controls.count() <= control_index:
        return False
    control = controls.nth(control_index)
    if control.evaluate("el => el.tagName.toLowerCase()") == "input":
        return select_ashby_combobox(page, control, value)
    try:
        control.click()
        option = (
            page.locator(
                '[role="option"]:visible, [role="menuitem"]:visible, '
                'div[class*="_option_"]:visible, div[class*="_result_"]:visible'
            )
            .filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I))
            .first
        )
        option.wait_for(state="visible", timeout=3000)
        option.click()
        return True
    except Exception:
        return False


def fill_education_history(
    page: Page,
    profile: Mapping[str, Any],
) -> None:
    """Fill the configured primary education record on Ashby forms."""
    education = profile.get("education_history", {})
    if not isinstance(education, Mapping) or not education:
        return
    group = page.locator(
        '.ashby-application-form-field-entry[data-field-path="_systemfield_education_history"]'
    ).first
    if not group.count():
        group = (
            page.locator(".ashby-application-form-field-entry")
            .filter(has_text=re.compile(r"\bEducation History\b", re.I))
            .first
        )
    if not group.count():
        return

    school = _education_field_container(group, "School")
    _choose_education_option(page, school, str(education.get("school", "")))

    for label_pattern, key in (
        ("Degree", "degree"),
        ("Field of Study", "field_of_study"),
    ):
        value = str(education.get(key, "")).strip()
        if not value:
            continue
        container = _education_field_container(group, label_pattern)
        inp = container.locator('input:not([type="hidden"])').first
        if inp.count():
            fill(page, inp, value)

    start = _education_field_container(group, "Start Date")
    end = _education_field_container(group, "End Date")
    _choose_education_option(page, start, str(education.get("start_month", "")), control_index=0)
    _choose_education_option(page, start, str(education.get("start_year", "")), control_index=1)
    _choose_education_option(page, end, str(education.get("end_month", "")), control_index=0)
    _choose_education_option(page, end, str(education.get("end_year", "")), control_index=1)

    student_label = group.locator("label").filter(has_text=re.compile(r"Still Student", re.I)).first
    still_student = student_label.locator('input[type="checkbox"]').first
    if still_student.count() and still_student.is_checked() != bool(
        education.get("still_student", False)
    ):
        still_student.set_checked(bool(education.get("still_student", False)))


def _page_body_lower(page: Page, *, timeout_ms: float | None = None) -> str:
    try:
        if timeout_ms is None:
            return page.inner_text("body").lower()
        return page.inner_text("body", timeout=max(1, int(timeout_ms))).lower()
    except Exception as exc:
        logger.debug("Could not read page body: %s", exc)
        return ""


def disallowed_screening_questions(page: Page) -> list[str]:
    """Return form questions that require skipping this application."""
    patterns = (
        re.compile(
            r"\binternal\s+(?:mobility|candidate|transfer|employee application)\b",
            re.I,
        ),
        re.compile(
            r"\b(?:active\s+|current\s+|obtain\s+|eligible\s+for\s+)?"
            r"security\s+clearance\b|\bclearance\s+level\b",
            re.I,
        ),
    )
    matches: list[str] = []
    fields = page.locator(".ashby-application-form-field-entry, fieldset, [role='group']")
    for index in range(fields.count()):
        field = fields.nth(index)
        try:
            if not field.is_visible():
                continue
            text = " ".join(field.inner_text(timeout=1000).split())
            if text and any(pattern.search(text) for pattern in patterns):
                matches.append(text[:240])
        except Exception:
            continue
    return list(dict.fromkeys(matches))


def _submission_confirmed(body_text: str) -> bool:
    return text_confirms_submission(
        body_text,
        success_phrases=SUBMISSION_CONFIRMATION_PHRASES,
        failure_phrases=SUBMISSION_FAILURE_PHRASES,
    )


def _submission_failure_status(body_text: str) -> str | None:
    """Classify an explicit Ashby rejection without collapsing all failures into spam."""
    if any(phrase in body_text for phrase in SUBMISSION_SPAM_PHRASES):
        return "FLAGGED_POSSIBLE_SPAM"
    if any(phrase in body_text for phrase in SUBMISSION_REJECTION_PHRASES):
        return "SUBMISSION_REJECTED"
    return None


def _submission_page_outcome(
    page: Page,
    *,
    timeout_ms: float | None = None,
) -> tuple[str | None, str]:
    """Observe one Ashby post-submit state, prioritizing terminal outcomes."""
    body_text = _page_body_lower(page, timeout_ms=timeout_ms)
    failure_status = _submission_failure_status(body_text)
    if failure_status is not None:
        return failure_status, body_text
    if _submission_confirmed(body_text):
        try:
            if not _locate_submit_btn(page).count():
                return "SUBMITTED & CONFIRMED", body_text
        except Exception as exc:
            logger.debug("Post-submit button inspection failed: %s", exc)
    if "missing entry for required field" in body_text:
        return REQUIRED_FIELD_VALIDATION, body_text
    return None, body_text


def _wait_for_submission_outcome(
    page: Page,
    *,
    timeout_seconds: float = SUBMISSION_RESULT_TIMEOUT_SECONDS,
    poll_seconds: float = SUBMISSION_RESULT_POLL_SECONDS,
) -> tuple[str, str]:
    """Poll without clicking until Ashby exposes a definitive post-submit state."""
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("submission outcome timeout and poll interval must be positive")
    deadline = time.monotonic() + timeout_seconds
    last_body_text = ""
    while True:
        if _shutdown or page.is_closed():
            break
        remaining_seconds = max(0.0, deadline - time.monotonic())
        outcome, last_body_text = _submission_page_outcome(
            page,
            timeout_ms=max(1.0, remaining_seconds * 1000),
        )
        if outcome is not None:
            return outcome, last_body_text
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        time.sleep(min(poll_seconds, remaining_seconds))
    return "SUBMIT_ATTEMPT_UNCONFIRMED", last_body_text


def _run_form_sections(
    page: Page,
    profile: Mapping[str, Any],
    defaults: Mapping[str, Any],
    essay: str,
    company: str,
    role: str,
    email: str,
    resume: Path,
    cover_letter: Path | None = None,
    *,
    refresh_selected_choices: bool = False,
) -> FormSectionReport:
    """Execute Ashby's provider-specific form phases in stable order."""

    def personal_and_files() -> FormSectionOutcome:
        return FormSectionOutcome(
            "personal_and_files",
            fill_personal_and_files(page, profile, email, resume, cover_letter),
        )

    def secondary() -> FormSectionOutcome:
        fill_secondary(page, profile, defaults, essay, company, role)
        return FormSectionOutcome("secondary")

    def education_history() -> FormSectionOutcome:
        fill_education_history(page, profile)
        return FormSectionOutcome("education_history")

    def consent() -> FormSectionOutcome:
        fill_consent_checkboxes(page, profile)
        return FormSectionOutcome("consent")

    def eeo() -> FormSectionOutcome:
        fill_eeo(page, profile)
        return FormSectionOutcome("eeo")

    def selected_choice_refresh() -> FormSectionOutcome:
        _refresh_selected_choice_groups(page)
        return FormSectionOutcome("selected_choice_refresh")

    handlers: tuple[CallableSectionHandler, ...] = (
        CallableSectionHandler("personal_and_files", personal_and_files),
        CallableSectionHandler("secondary", secondary),
        CallableSectionHandler("education_history", education_history),
        CallableSectionHandler("consent", consent),
        CallableSectionHandler("eeo", eeo),
    )
    if refresh_selected_choices:
        handlers += (CallableSectionHandler("selected_choice_refresh", selected_choice_refresh),)
    return run_section_handlers(handlers)


def _fill_current_form(
    page: Page,
    profile: Mapping[str, Any],
    defaults: Mapping[str, Any],
    essay: str,
    company: str,
    role: str,
    email: str,
    resume: Path,
    cover_letter: Path | None = None,
) -> dict[str, bool]:
    """Run every fill helper once against the currently visible Ashby form step."""
    return _run_form_sections(
        page,
        profile,
        defaults,
        essay,
        company,
        role,
        email,
        resume,
        cover_letter,
    ).fields


def _repair_dynamic_form(
    page: Page,
    profile: Mapping[str, Any],
    defaults: Mapping[str, Any],
    essay: str,
    company: str,
    role: str,
    email: str,
    resume: Path,
    cover_letter: Path | None = None,
) -> None:
    """Replay Ashby sections after required-field validation rerenders the form."""
    _run_form_sections(
        page,
        profile,
        defaults,
        essay,
        company,
        role,
        email,
        resume,
        cover_letter,
        refresh_selected_choices=True,
    )


def _required_field_issues(page: Page) -> list[str]:
    """Return labels for visible Ashby fields that are required but unanswered."""
    issues: list[str] = []
    fields = page.locator(".ashby-application-form-field-entry").all()
    for field in fields:
        try:
            if not field.is_visible():
                continue
            label = field.locator("label").first
            label_text = (
                label.inner_text().strip()
                if label.count()
                else field.inner_text().strip().splitlines()[0]
            )
            if not label_text:
                label_text = "Unnamed required field"

            label_class = ""
            pseudo_content: object = ""
            if label.count():
                label_class = (label.get_attribute("class") or "").lower()
                if "required" not in label_class:
                    pseudo_content = label.evaluate(
                        "el => getComputedStyle(el, '::after').content || ''"
                    )
            is_required = required_field_flag(
                label_class=label_class,
                pseudo_content=pseudo_content,
            )
            if not is_required:
                has_required_control = (
                    field.locator(
                        "input[required], textarea[required], select[required], "
                        '[aria-required="true"]'
                    ).count()
                    > 0
                )
                is_required = required_field_flag(
                    label_class=label_class,
                    pseudo_content=pseudo_content,
                    has_required_control=has_required_control,
                )
            if not is_required:
                continue

            yes_no_buttons = field.locator("button").filter(
                has_text=re.compile(r"^\s*(Yes|No)\s*$", re.I)
            )
            if yes_no_buttons.count() >= 2:
                selected = False
                for index in range(yes_no_buttons.count()):
                    button = yes_no_buttons.nth(index)
                    classes = (button.get_attribute("class") or "").lower()
                    if choice_is_selected(
                        aria_pressed=button.get_attribute("aria-pressed"),
                        class_name=classes,
                    ):
                        selected = True
                        break
                if not selected:
                    issues.append(label_text)
                continue

            file_inputs = field.locator('input[type="file"]')
            if file_inputs.count():
                has_file = any(
                    file_inputs.nth(index).input_value().strip()
                    for index in range(file_inputs.count())
                )
                if not has_file:
                    issues.append(label_text)
                continue

            radios = field.locator('input[type="radio"]')
            if radios.count():
                if not any(radios.nth(index).is_checked() for index in range(radios.count())):
                    issues.append(label_text)
                continue

            checkboxes = field.locator('input[type="checkbox"]')
            if checkboxes.count():
                if not any(
                    checkboxes.nth(index).is_checked() for index in range(checkboxes.count())
                ):
                    issues.append(label_text)
                continue

            controls = field.locator(
                'input:not([type="hidden"]):not([type="button"]):not([type="submit"]), '
                "textarea, select"
            )
            if controls.count() and not any(
                controls.nth(index).input_value().strip() for index in range(controls.count())
            ):
                issues.append(label_text)
        except Exception as exc:
            logger.debug("Required-field audit failed for one field: %s", exc)
    # Repeatable compound fields such as Education History have a non-required
    # outer label and required nested labels. Audit those nested controls
    # explicitly because the outer field-entry pass above cannot see them.
    nested_required = page.locator(".ashby-application-form-field-entry label[class*='required']")
    for index in range(nested_required.count()):
        label = nested_required.nth(index)
        try:
            if not label.is_visible():
                continue
            label_text = label.inner_text().strip() or "Unnamed required field"
            container = label.locator("xpath=..")
            controls = container.locator('input:not([type="hidden"]), textarea, select')
            if controls.count() and not any(
                controls.nth(control_index).input_value().strip()
                for control_index in range(controls.count())
            ):
                if label_text not in issues:
                    issues.append(label_text)
        except Exception as exc:
            logger.debug("Nested required-field audit failed: %s", exc)
    return issues


def _capture_submission_outcome(
    page: Page,
    directory: Path,
    company: str,
    status: str,
) -> None:
    outcome_label = {
        "SUBMITTED & CONFIRMED": "submitted_verified",
        "FLAGGED_POSSIBLE_SPAM": "rejected_possible_spam",
        "SUBMISSION_REJECTED": "submission_rejected",
    }.get(status, "submit_unconfirmed")
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    screenshot_path = directory / (
        f"{_safe_filename_part(company)}_{outcome_label}_{timestamp}.png"
    )
    try:
        cdp = page.context.new_cdp_session(page)
        data = cdp.send(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        screenshot_path.write_bytes(base64.b64decode(data["data"]))
    except Exception as cdp_exc:
        logger.debug("Submission-outcome CDP screenshot failed: %s", cdp_exc)
        try:
            if not page.is_closed():
                page.screenshot(
                    path=str(screenshot_path),
                    full_page=False,
                    animations="disabled",
                    timeout=2000,
                )
        except Exception as screenshot_exc:
            logger.warning(
                "Submission-outcome screenshot failed: %s",
                screenshot_exc,
            )


def _submit_application(
    page: Page,
    directory: Path,
    company: str,
    repair_dynamic_fields: Callable[[], None] | None = None,
) -> str:
    """Submit once the form is complete and return a structured status string."""
    if _shutdown:
        return "ABORTED_SIGNAL_RECEIVED"

    submit = _locate_submit_btn(page)
    expect(submit).to_be_visible(timeout=7000)
    status = "SUBMIT_ATTEMPT_UNCONFIRMED"

    for submit_attempt in range(1, MAX_SUBMIT_ATTEMPTS + 1):
        if _shutdown:
            return "ABORTED_SIGNAL_RECEIVED"
        if page.is_closed():
            break

        try:
            submit = _locate_submit_btn(page)
            pre_click_failure = _submission_failure_status(_page_body_lower(page))
            if pre_click_failure is not None:
                status = pre_click_failure
                logger.warning(
                    "Ashby already exposes rejection status %s; not clicking.",
                    status,
                )
                break
            if (not submit.count() or not submit.is_visible()) and _submission_confirmed(
                _page_body_lower(page)
            ):
                status = "SUBMITTED & CONFIRMED"
                break
        except Exception as locate_exc:
            logger.debug("Submit button inspection failed: %s", locate_exc)

        click_uncertain = False
        try:
            smooth_mouse_move(page, submit)
            human_delay(0.5, 1.0)
            submit.click(timeout=15000)
            logger.info(
                "Clicked Submit Application button (attempt %d).",
                submit_attempt,
            )
        except Exception as click_exc:
            logger.warning(
                "Submit click attempt %d had an uncertain outcome; not re-clicking: %s",
                submit_attempt,
                click_exc,
            )
            click_uncertain = True

        outcome, _ = _wait_for_submission_outcome(page)
        if outcome == "SUBMITTED & CONFIRMED":
            status = outcome
            break
        if outcome in {"FLAGGED_POSSIBLE_SPAM", "SUBMISSION_REJECTED"}:
            status = outcome
            logger.warning(
                "Ashby rejected the submission with status %s; not re-clicking.",
                status,
            )
            break
        if outcome == REQUIRED_FIELD_VALIDATION:
            if click_uncertain:
                logger.warning(
                    "Required-field validation followed a click exception; "
                    "the outcome is ambiguous and will not be re-clicked."
                )
                status = "SUBMIT_ATTEMPT_UNCONFIRMED"
                break
            if repair_dynamic_fields is None or submit_attempt >= MAX_SUBMIT_ATTEMPTS:
                status = "ABORTED_MISSING_REQUIRED_FIELDS"
                break
            logger.warning(
                "Ashby validation rerendered a required field; reapplying "
                "dynamic answers before retry."
            )
            repair_dynamic_fields()
            time.sleep(1.0)
            continue
        status = "SUBMIT_ATTEMPT_UNCONFIRMED"
        logger.warning(
            "Ashby did not expose a definitive result after submit attempt %d "
            "within %.1f seconds; "
            "not re-clicking an ambiguous submission.",
            submit_attempt,
            SUBMISSION_RESULT_TIMEOUT_SECONDS,
        )
        break

    if _shutdown:
        return "ABORTED_SIGNAL_RECEIVED"

    if status == "SUBMIT_ATTEMPT_UNCONFIRMED":
        final_outcome, _ = _submission_page_outcome(page)
        if final_outcome in {
            "SUBMITTED & CONFIRMED",
            "FLAGGED_POSSIBLE_SPAM",
            "SUBMISSION_REJECTED",
        }:
            status = final_outcome
    _capture_submission_outcome(page, directory, company, status)
    return status


# ==============================================================================
# MAIN EXECUTION RUNNER
# ==============================================================================
def _positive_config_int(config: Mapping[str, Any], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Configuration field {key!r} must be a positive integer.")
    return value


def run_job(
    url: str,
    resume_path: str,
    company: str = "",
    role: str = "",
    essay: str = "",
    live: bool = False,
    cfg: Mapping[str, Any] | None = None,
    cover_letter_path: str = "",
) -> str:
    """Open the job, fill every form step, validate required fields, and submit if `live`; return the run's outcome status string."""
    if cfg is None:
        resolved_config = copy.deepcopy(DEFAULT_CONFIG)
    else:
        resolved_config = _deep_merge(DEFAULT_CONFIG, cfg)

    if not is_ashby_url(url):
        raise ValueError(
            "This engine supports HTTPS Ashby URLs only. "
            "Select the matching ATS engine for this job."
        )

    candidate_config = resolved_config.get("candidate")
    if not isinstance(candidate_config, Mapping):
        raise ValueError("Configuration field 'candidate' must be an object.")
    profile = dict(candidate_config)
    defaults_config = resolved_config.get("defaults", {})
    paths = resolved_config.get("paths", {})
    company_overrides = resolved_config.get("company_overrides", {})
    if not isinstance(defaults_config, Mapping) or not isinstance(paths, Mapping):
        raise ValueError("Configuration fields 'defaults' and 'paths' must be objects.")
    defaults = dict(defaults_config)
    if not isinstance(company_overrides, Mapping):
        raise ValueError("Configuration field 'company_overrides' must be an object.")
    action_timeout_ms = _positive_config_int(resolved_config, "action_timeout_ms")
    navigation_timeout_ms = _positive_config_int(resolved_config, "navigation_timeout_ms")
    network_idle_timeout_ms = _positive_config_int(resolved_config, "network_idle_timeout_ms")
    overrides = company_overrides.get(company, {})
    if not isinstance(overrides, Mapping):
        raise ValueError(f"Company override for {company!r} must be an object.")

    # Restrict company overrides to keys that already exist on the candidate
    # profile so a typo'd override key can't silently introduce a new field.
    profile.update({k: v for k, v in overrides.items() if k in profile})
    profile["_rules"] = resolved_config.get("rules", {})
    profile["_eeo_defaults"] = resolved_config.get("eeo_defaults", {})
    profile["_field_matchers"] = resolved_config.get("field_matchers", {})
    profile["_answer_variants"] = resolved_config.get("answer_variants", {})
    # Keep the legacy engine keys aligned with repository-level EEO defaults.
    eeo_defaults = profile["_eeo_defaults"]
    if isinstance(eeo_defaults, Mapping):
        profile["gender"] = eeo_defaults.get("gender") or profile.get("gender")
        profile["race"] = eeo_defaults.get("race") or profile.get("race")
        profile["veteran"] = eeo_defaults.get("veteran_status") or profile.get("veteran")
        profile["disability"] = eeo_defaults.get("disability_status") or profile.get("disability")
    _merge_repository_rules(profile, defaults, resolved_config)
    missing_identity = [field for field in ("first_name", "last_name") if not profile.get(field)]
    if missing_identity:
        raise ValueError("Config missing required candidate fields: " + ", ".join(missing_identity))
    essay = str(overrides.get("essay", essay or defaults.get("essay", "")) or "")

    ashby_dir = active_screenshot_directory(
        expand(str(paths.get("ashby_dir", RUNTIME_CONFIG.ashby.screenshot_dir)))
    )
    ashby_dir.mkdir(parents=True, exist_ok=True)

    resume = Path(resume_path).expanduser().resolve()
    if not resume.is_file():
        raise FileNotFoundError(f"Resume PDF file not found at: {resume_path}")
    if resume.stat().st_size == 0:
        raise ValueError(f"Resume file is empty: {resume}")
    cover_letter = Path(cover_letter_path).expanduser().resolve() if cover_letter_path else None
    if cover_letter is not None:
        if not cover_letter.is_file():
            raise FileNotFoundError(f"Cover-letter PDF file not found at: {cover_letter_path}")
        if cover_letter.stat().st_size == 0:
            raise ValueError(f"Cover-letter file is empty: {cover_letter}")
        profile["_cover_letter_text"] = _extract_cover_letter_text(cover_letter)

    email = resolve_candidate_email(profile)

    logger.info("=" * 66)
    logger.info("JOB URL: %s", url)
    logger.info(
        "Company: %s | Role: %s | Email: %s",
        company or "Inferred",
        role,
        _mask_email(email),
    )
    logger.info("Resume: %s", resume.name)
    logger.info("=" * 66)

    status = "FAILED"
    with sync_playwright() as p:
        session = _open_browser_session(p, url)
        browser = session.browser
        page = session.page
        page.set_default_timeout(action_timeout_ms)
        critical: dict[str, bool] = {}

        try:
            if _shutdown:
                status = "ABORTED_SIGNAL_RECEIVED"
                return status

            def _nav() -> None:
                navigate_reusing_tab(
                    page,
                    url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms,
                )
                page.wait_for_load_state(
                    "networkidle",
                    timeout=network_idle_timeout_ms,
                )

            retry(_nav, label="Navigation")

            comp_name = company or page.title().split("-")[0].strip() or "Company"

            ss(page, ashby_dir, comp_name, "00_JD")
            apply = page.locator(
                'a:has-text("Apply for this Job"), button:has-text("Apply for this Job"), a:has-text("Apply"), button:has-text("Apply")'
            ).first
            if apply.count() and apply.is_visible():
                click(apply, "Apply")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    logger.debug("Apply navigation did not reach network idle.")
                human_delay(1.0, 1.8)

            ss(page, ashby_dir, comp_name, "01_form")

            blocked_questions = disallowed_screening_questions(page)
            if blocked_questions:
                status = "SKIPPED_INTERNAL_MOBILITY_OR_SECURITY_CLEARANCE"
                logger.warning(
                    "Skipping application because of disallowed screening question(s): %s",
                    " | ".join(blocked_questions),
                )
                return status

            for step in range(1, MAX_FORM_STEPS + 1):
                if _shutdown:
                    status = "ABORTED_SIGNAL_RECEIVED"
                    break
                critical = _fill_current_form(
                    page,
                    profile,
                    defaults,
                    essay,
                    comp_name,
                    role,
                    email,
                    resume,
                    cover_letter,
                )
                ss(page, ashby_dir, comp_name, f"step{step}")
                if not can_advance(page):
                    break

            if _shutdown:
                status = "ABORTED_SIGNAL_RECEIVED"
                return status

            # A subset of Ashby country/location comboboxes remount the form
            # after selection and silently detach an already uploaded resume.
            # Reattach personal files after all dynamic controls have settled.
            refresh_report = run_section_handlers(
                (
                    CallableSectionHandler(
                        "visible_form_steps",
                        lambda: FormSectionOutcome("visible_form_steps", critical),
                    ),
                    CallableSectionHandler(
                        "personal_and_files_refresh",
                        lambda: FormSectionOutcome(
                            "personal_and_files_refresh",
                            fill_personal_and_files(
                                page,
                                profile,
                                email,
                                resume,
                                cover_letter,
                            ),
                        ),
                    ),
                )
            )
            critical = refresh_report.fields

            # Every visible step is filled inside the loop above. Re-running
            # the terminal step here used to rebuild Ashby's React form after
            # file selection, which clears the browser-held resume attachment.
            time.sleep(1.0)
            ss(page, ashby_dir, comp_name, "prefilled")

            missing = [
                k
                for k, ok in critical.items()
                if not ok and k in ("name", "email", "resume", "cover_letter")
            ]
            if missing:
                status = f"ABORTED_MISSING_{'_'.join(missing).upper()}"
                raise RuntimeError(f"Aborting submit: missing {missing}")

            required_issues = validate_required_fields(page, _required_field_issues)
            if required_issues:
                status = "ABORTED_MISSING_REQUIRED_FIELDS"
                logger.error(
                    "Required fields remain unanswered: %s",
                    "; ".join(required_issues),
                )
                raise RuntimeError("Aborting completion: required fields remain unanswered")

            status = "PREFILLED_ONLY"

            if live:
                status = _submit_application(
                    page,
                    ashby_dir,
                    comp_name,
                    repair_dynamic_fields=lambda: _repair_dynamic_form(
                        page,
                        profile,
                        defaults,
                        essay,
                        comp_name,
                        role,
                        email,
                        resume,
                        cover_letter,
                    ),
                )

        except Exception as exc:
            logger.error("Fatal exception: %s", exc)
            try:
                ss(page, ashby_dir, "Error", "FATAL")
            except Exception as screenshot_exc:
                logger.debug("Fatal screenshot failed: %s", screenshot_exc)
            if not status.startswith("ABORTED"):
                status = f"FAILED: {type(exc).__name__}"
        finally:
            if not live and status == "PREFILLED_ONLY":
                logger.info("=" * 66)
                logger.info("✅ Pre-filling complete!")
                logger.info("Tab is available for manual review when connected to Chrome over CDP.")
                logger.info("=" * 66)

            try:
                if session.close_browser_on_exit:
                    browser.close()
            except Exception as cleanup_exc:
                logger.warning("Browser cleanup failed: %s", cleanup_exc)

            logger.info("Final Outcome -> %s", status)
        return status


# ==============================================================================
# ENTRY POINT
# ==============================================================================
def build_argument_parser() -> argparse.ArgumentParser:
    parser = build_engine_parser("Ashby Job Application Automation Engine")
    parser.set_defaults(role="AI Product Manager")
    return parser


def _engine_result(status: str, is_live: bool) -> dict[str, Any]:
    result = engine_result(status, ats=ATS_NAME, is_live=is_live)
    if is_live and status == "SUBMIT_ATTEMPT_UNCONFIRMED":
        # The click may have reached Ashby even when the browser never observed
        # the response. Preserve that ambiguity so every orchestrator
        # quarantines the job instead of treating it as safe to retry.
        result["submitted"] = True
    return result


def _emit_engine_result(result: Mapping[str, Any]) -> None:
    emit_engine_result(result)


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    is_live = requested_live_mode(args)

    try:
        require_orchestrated_invocation(args.url)
        config = load_config(orchestrated_config_path())
        if args.email:
            config["candidate"]["email_override"] = args.email

        final_status = run_job(
            url=args.url,
            resume_path=args.resume,
            cover_letter_path=args.cover_letter,
            company=args.company,
            role=args.role,
            essay=args.essay,
            live=is_live,
            cfg=config,
        )
    except Exception as exc:
        logger.error("Engine initialization failed: %s", exc)
        final_status = f"FAILED: {type(exc).__name__}"

    result = _engine_result(final_status, is_live)
    _emit_engine_result(result)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    raise SystemExit(main())
