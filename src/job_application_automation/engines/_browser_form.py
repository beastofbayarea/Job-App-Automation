"""Guarded shared browser-form runtime for the phase-one ATS engines."""

from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin

from playwright.sync_api import Page, sync_playwright
from pypdf import PdfReader

from ..core.engine_shared import (
    answer_variants,
    build_engine_parser,
    capture_screenshot,
    close_browser_session,
    configured_answer,
    confirmation_visible,
    emit_engine_result,
    fill_first,
    fill_required_consent,
    first_visible,
    generate_essay_answer,
    is_essay_question,
    is_location_question,
    load_candidate_evidence,
    load_json_config,
    location_answer_candidates,
    navigate_reusing_tab,
    normalize_profile_config,
    open_chrome_session,
    orchestrated_config_path,
    page_has_captcha,
    requested_live_mode,
    require_orchestrated_invocation,
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
    email_confirmation_selectors: tuple[str, ...] = ()
    address_selectors: tuple[str, ...] = ()
    city_selectors: tuple[str, ...] = ()
    postcode_selectors: tuple[str, ...] = ()
    country_selectors: tuple[str, ...] = ()
    next_selectors: tuple[str, ...] = ()
    background_cdp: bool = False
    resume_parse_wait_ms: int = 0


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
    street_address: str
    city: str
    postcode: str
    country: str

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
    email_override: str | None,
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
        street_address=_first_value(
            candidate,
            _mapping(candidate.get("address")),
            "street_address",
            "address_line1",
            "line1",
        ),
        city=_first_value(candidate, _mapping(candidate.get("address")), "city"),
        postcode=_first_value(
            candidate,
            _mapping(candidate.get("address")),
            "zip_code",
            "postcode",
            "postal_code",
        ),
        country=_first_value(candidate, _mapping(candidate.get("address")), "country"),
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


def _fill_and_blur(page: Page, selectors: Sequence[str], value: str) -> bool:
    """Fill a React-managed text control and force its validation lifecycle."""
    if not value:
        return False
    control = _first_visible_for(page, selectors)
    if control is None:
        return False
    try:
        control.fill(value)
        control.blur()
        page.wait_for_timeout(250)
        return control.input_value().strip() == value.strip()
    except Exception:
        return False


def _stabilize_email_fields(
    page: Page,
    spec: BrowserFormSpec,
    candidate: CandidateFields,
    filled: dict[str, bool],
) -> None:
    """Refill identity email fields after provider-side dynamic rerenders."""
    filled["email"] = _fill_and_blur(page, spec.email_selectors, candidate.email)
    if spec.email_confirmation_selectors:
        filled["email_confirmation"] = _fill_and_blur(
            page,
            spec.email_confirmation_selectors,
            candidate.email,
        )


def _forbidden_characters(control: Any) -> set[str]:
    """Read a provider's inline forbidden-character error for one text control."""
    try:
        context = str(
            control.evaluate(
                """element => {
                    const describedBy = String(
                        element.getAttribute("aria-describedby") || ""
                    ).split(/\\s+/).filter(Boolean);
                    const described = describedBy.map(id => {
                        const node = document.getElementById(id);
                        return node ? node.innerText : "";
                    }).filter(Boolean).join("\\n");
                    if (/cannot contain.+characters?\\s*:/i.test(described)) {
                        return described;
                    }
                    let node = element.parentElement;
                    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                        const text = String(node.innerText || "");
                        if (/cannot contain.+characters?\\s*:/i.test(text)) return text;
                    }
                    return "";
                }"""
            )
        )
    except Exception:
        return set()
    match = re.search(
        r"cannot contain(?:\s+the)?(?:\s+following)?\s+characters?\s*:\s*([^\r\n]+)",
        context,
        re.I,
    )
    if not match:
        return set()
    tokens = re.findall(r"[^\w\s,]+", match.group(1))
    return {character for token in tokens for character in token}


def _repair_forbidden_text_characters(page: Page) -> list[str]:
    """Repair inline character-policy errors without weakening final validation."""
    repaired: list[str] = []
    controls = page.locator("input:not([type='file']), textarea")
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            forbidden = _forbidden_characters(control)
            value = control.input_value()
            if not forbidden or not any(character in value for character in forbidden):
                continue
            replacement = value
            for character in forbidden:
                replacement = replacement.replace(character, "," if character == ";" else " ")
            replacement = re.sub(r"[ \t]+", " ", replacement)
            control.fill(replacement)
            control.blur()
            repaired.append(control.get_attribute("id") or control.get_attribute("name") or "text")
        except Exception:
            continue
    if repaired:
        page.wait_for_timeout(500)
    return repaired


def _dismiss_cookie_banner(page: Page) -> None:
    """Dismiss common ATS cookie overlays without changing application answers."""
    for pattern in (
        r"decline all",
        r"reject all",
        r"only necessary",
        r"accept all",
    ):
        try:
            button = first_visible(page.get_by_role("button", name=re.compile(pattern, re.I)))
            if button is not None:
                button.click(timeout=3000)
                page.wait_for_timeout(250)
                return
        except Exception:
            continue


def _wait_for_form(page: Page, spec: BrowserFormSpec, timeout: int) -> bool:
    """Wait until the provider's asynchronously rendered identity form is visible."""
    selectors = spec.first_name_selectors + spec.full_name_selectors
    if not selectors:
        return True
    deadline = time.monotonic() + min(max(timeout * 2, 30_000), 60_000) / 1000
    while time.monotonic() < deadline:
        if _first_visible_for(page, selectors) is not None:
            return True
        if page_has_captcha(page):
            return False
        page.wait_for_timeout(250)
    return False


def _apply_control_is_ready(control: Any) -> bool:
    try:
        if (control.get_attribute("aria-disabled") or "").casefold() == "true":
            return False
        if not control.is_enabled():
            return False
        tag = str(control.evaluate("element => element.tagName.toLowerCase()")).casefold()
        if tag != "a":
            return True
        return bool(
            (control.get_attribute("href") or "").strip()
            or (control.get_attribute("onclick") or "").strip()
        )
    except Exception:
        return False


def _wait_for_application_entry(page: Page, spec: BrowserFormSpec, timeout: int) -> str:
    """Wait for an actionable apply control or a directly rendered form."""
    deadline = time.monotonic() + min(max(timeout * 2, 30_000), 60_000) / 1000
    form_selectors = spec.first_name_selectors + spec.full_name_selectors
    while time.monotonic() < deadline:
        apply_control = _first_visible_for(page, spec.apply_selectors)
        if apply_control is not None and _apply_control_is_ready(apply_control):
            return "apply"
        if form_selectors and _first_visible_for(page, form_selectors) is not None:
            return "form"
        page.wait_for_timeout(250)
    return ""


def _step_fingerprint(page: Page) -> str:
    """Describe visible controls without retaining candidate-entered values."""
    try:
        return str(
            page.evaluate(
                """() => {
                    const visible = element => {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none" &&
                            rect.width > 0 && rect.height > 0;
                    };
                    const selector = [
                        "input:not([type='hidden'])",
                        "select",
                        "textarea",
                        "[role='combobox']",
                        "[role='checkbox']",
                        "[role='radio']",
                        "button",
                        "spl-button",
                    ].join(",");
                    const controls = [...document.querySelectorAll(selector)]
                        .filter(visible)
                        .map(element => {
                            const label = element.labels?.[0]?.innerText ||
                                element.closest("label, fieldset")?.innerText || "";
                            return [
                                element.tagName,
                                element.getAttribute("type") || "",
                                element.getAttribute("name") || "",
                                element.id || "",
                                element.getAttribute("aria-label") || "",
                                String(label).replace(/\\s+/g, " ").trim().slice(0, 120),
                            ];
                        });
                    return JSON.stringify({url: location.href, controls});
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _wait_for_step_ready(
    page: Page,
    timeout: int,
    previous_fingerprint: str,
) -> bool:
    """Wait for a multistep form to visibly transition before inspecting controls."""
    deadline = time.monotonic() + min(timeout, 30_000) / 1000
    if not previous_fingerprint:
        return False
    saw_busy_state = False
    spinner_selector = (
        '[role="progressbar"]:visible, [aria-busy="true"]:visible, '
        "spl-spinner:visible, spl-loader:visible, "
        '[class*="spinner" i]:visible, [class*="loading" i]:visible'
    )
    control_selectors = (
        'input:not([type="hidden"]):not([type="file"])',
        "select",
        "textarea",
        '[role="combobox"]',
        '[role="checkbox"]',
        'button[type="submit"]',
        "spl-button",
    )
    while time.monotonic() < deadline:
        try:
            busy = page.locator(spinner_selector).count() > 0
            saw_busy_state = saw_busy_state or busy
            current_fingerprint = _step_fingerprint(page)
            if (
                not busy
                and _first_visible_for(page, control_selectors) is not None
                and (
                    saw_busy_state
                    or (current_fingerprint and current_fingerprint != previous_fingerprint)
                )
            ):
                return True
        except Exception:
            pass
        page.wait_for_timeout(250)
    return False


def _clean_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lstrip("* ").rstrip("* ").strip()


def _question_label(control: Any) -> str:
    """Resolve a question label through React/ARIA wrappers and native labels."""
    try:
        return _clean_label(
            control.evaluate(
                """element => {
                    const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                    const referencedText = node => {
                        const raw = node && node.getAttribute &&
                            node.getAttribute("aria-labelledby");
                        if (!raw) return "";
                        const ids = raw.split(/\\s+/).filter(Boolean).filter(
                            id => !/^(?:radio|checkbox)_label_/i.test(id)
                        );
                        return clean(ids.map(id => {
                            const target = document.getElementById(id);
                            return target ? target.innerText : "";
                        }).filter(Boolean).join(" "));
                    };
                    let node = element;
                    for (let depth = 0; node && depth < 12; depth += 1) {
                        const text = referencedText(node);
                        if (text) return text;
                        const labelContent = node.querySelector &&
                            node.querySelector('[slot="label-content"]');
                        if (labelContent && clean(labelContent.innerText)) {
                            return clean(labelContent.innerText);
                        }
                        const ariaLabel = node.getAttribute &&
                            clean(node.getAttribute("aria-label"));
                        if (ariaLabel && !/^select$/i.test(ariaLabel)) {
                            return ariaLabel.replace(/^select\\s+/i, "");
                        }
                        const root = node.getRootNode && node.getRootNode();
                        node = node.parentElement || (root && root.host) || null;
                    }
                    const labels = Array.from(element.labels || [])
                        .map(label => clean(label.innerText))
                        .filter(Boolean);
                    if (labels.length) return labels.join(" ");
                    const container = element.closest(
                        "fieldset, [role='group'], [role='radiogroup'], " +
                        "[data-ui], [class*='question'], [class*='field']"
                    );
                    if (container) {
                        const legend = container.querySelector("legend");
                        if (legend && clean(legend.innerText)) return clean(legend.innerText);
                    }
                    return clean(
                        element.getAttribute("aria-label") ||
                        element.getAttribute("placeholder") ||
                        element.name ||
                        element.id
                    );
                }"""
            )
        )
    except Exception:
        return ""


def _option_label(control: Any) -> str:
    try:
        return _clean_label(
            control.evaluate(
                """element => {
                    const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                    const label = element.closest("label");
                    if (label && clean(label.innerText)) return clean(label.innerText);
                    let node = element.parentElement;
                    for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
                        const raw = node.getAttribute && node.getAttribute("aria-labelledby");
                        if (!raw) continue;
                        const optionIds = raw.split(/\\s+/).filter(
                            id => /^(?:radio|checkbox)_label_/i.test(id)
                        );
                        const text = clean(optionIds.map(id => {
                            const target = document.getElementById(id);
                            return target ? target.innerText : "";
                        }).filter(Boolean).join(" "));
                        if (text) return text;
                    }
                    return clean(element.getAttribute("label") || element.value);
                }"""
            )
        )
    except Exception:
        return ""


def _normalized_option(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _option_matches(option: str, variants: Sequence[str]) -> bool:
    normalized_option = _normalized_option(option)
    for variant in variants:
        normalized_variant = _normalized_option(str(variant))
        if not normalized_variant:
            continue
        if (
            normalized_option == normalized_variant
            or normalized_variant in normalized_option
            or normalized_option in normalized_variant
        ):
            return True
    return False


def _option_strength(value: str) -> tuple[int, int]:
    """Rank experience/rating options independently of provider display order."""
    normalized = _normalized_option(value)
    if re.search(
        r"\b(?:no|none|never|not)\b.{0,30}\b(?:experience|involvement|delivered|used)\b|"
        r"\b(?:not applicable|n/?a)\b",
        normalized,
    ):
        return (-10_000, 0)
    weights = {
        "scaled": 1_000,
        "extensive": 950,
        "expert": 925,
        "significant": 900,
        "led": 875,
        "production": 850,
        "advanced": 800,
        "multiple components": 775,
        "directly": 725,
        "yes": 700,
        "some": 350,
        "basic": 200,
    }
    semantic = max(
        (weight for phrase, weight in weights.items() if phrase in normalized),
        default=0,
    )
    numbers = [int(number) for number in re.findall(r"\b\d+\b", normalized)]
    return (semantic, max(numbers, default=0))


def _select_combobox(
    page: Page,
    control: Any,
    variants: Sequence[str],
    *,
    prefer_maximum: bool = False,
) -> bool:
    """Select a React combobox option, with a guarded strongest-option policy."""
    last_visible: list[str] = []
    try:
        readonly = control.get_attribute("readonly") is not None
        tag = control.evaluate("element => element.tagName.toLowerCase()")
        input_control = tag in {"input", "textarea"}
        requires_option = str(control.get_attribute("aria-autocomplete") or "").casefold() in {
            "list",
            "both",
        }
        queries = (
            tuple(str(variant) for variant in variants if str(variant).strip())
            if input_control and not readonly
            else ("",)
        )
        # Provider search boxes sometimes require exact display text. If every
        # configured alias filters the desired option out, clear the query once
        # and match against the complete list instead of leaving the field blank.
        query_values = (*queries, "") if queries else ("",)
        for query in query_values:
            if query:
                control.fill(query)
                page.wait_for_timeout(350)
            elif input_control and not readonly:
                control.fill("")
                page.wait_for_timeout(350)
            clickable = control
            try:
                root = control.locator(
                    "xpath=ancestor::*[@data-input-type='select' or @role='combobox'][1]"
                )
                if root.count():
                    clickable = root.first
            except Exception:
                pass
            # Autocomplete inputs usually open their menu while typing. Clicking
            # an already-expanded SmartRecruiters control toggles it closed and
            # makes the freshly loaded suggestion impossible to select.
            if control.get_attribute("aria-expanded") != "true":
                clickable.click(timeout=3000)
            page.wait_for_timeout(350)
            listbox_id = (
                control.get_attribute("aria-controls") or control.get_attribute("aria-owns") or ""
            )
            if listbox_id:
                escaped_id = listbox_id.replace("\\", "\\\\").replace('"', '\\"')
                options = page.locator(
                    f'[id="{escaped_id}"] [role="option"], '
                    f'[id="{escaped_id}"] [data-ui="option"], '
                    f'[id="{escaped_id}"] spl-select-option'
                )
            else:
                options = page.locator(
                    '[role="option"]:visible, [data-ui="option"]:visible, spl-select-option:visible'
                )
            try:
                options.first.wait_for(state="visible", timeout=3000)
            except Exception:
                if query != query_values[-1]:
                    continue
            visible: list[tuple[Any, str]] = []
            for index in range(options.count()):
                option = options.nth(index)
                text = _clean_label(option.inner_text())
                if text:
                    visible.append((option, text))
            last_visible = [text for _, text in visible]
            selected = next(
                (option for option, text in visible if _option_matches(text, variants)),
                None,
            )
            if selected is None and prefer_maximum and visible:
                selected = max(visible, key=lambda item: _option_strength(item[1]))[0]
            if selected is None:
                continue
            selected.click(force=True, timeout=3000)
            page.wait_for_timeout(500)
            if (
                input_control
                and control.input_value().strip()
                and (control.get_attribute("aria-invalid") != "true")
            ):
                return True
            backing = clickable.locator("input[aria-hidden='true']")
            if backing.count():
                return bool(backing.first.input_value().strip())
            return True
        if input_control and not readonly and not requires_option:
            return bool(control.input_value().strip()) and (
                control.get_attribute("aria-invalid") != "true"
            )
        if input_control:
            control.press("Escape")
    except Exception as exc:
        logger.debug("Combobox selection failed for %r: %s", variants, exc)
    logger.debug(
        "No combobox option matched configured values=%r; visible options=%r",
        tuple(variants),
        last_visible,
    )
    return False


def _select_native(control: Any, variants: Sequence[str], *, prefer_maximum: bool = False) -> bool:
    try:
        options = control.locator("option")
        available = [
            (
                _clean_label(options.nth(index).inner_text()),
                options.nth(index).get_attribute("value"),
            )
            for index in range(options.count())
        ]
        match = next(
            (
                (text, value)
                for text, value in available
                if text and _option_matches(text, variants)
            ),
            None,
        )
        if match is None and prefer_maximum:
            nonempty = [(text, value) for text, value in available if text and value]
            match = max(nonempty, key=lambda item: _option_strength(item[0])) if nonempty else None
        if match is None:
            return False
        text, value = match
        control.select_option(value=value) if value is not None else control.select_option(
            label=text
        )
        return bool(control.input_value().strip())
    except Exception:
        return False


def _group_controls(page: Page, control: Any, control_type: str) -> Any:
    group = control.locator("xpath=ancestor::*[@role='group' or @role='radiogroup'][1]")
    if group.count():
        return group.first.locator(f'input[type="{control_type}"], [role="{control_type}"]')
    name = control.get_attribute("name") or ""
    if name:
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return page.locator(f'input[type="{control_type}"][name="{escaped}"]')
    if (control.get_attribute("role") or "").casefold() == control_type:
        return page.locator(f'[role="{control_type}"]')
    return control


def _control_group_key(control: Any) -> str:
    try:
        return str(
            control.evaluate(
                """element => {
                    const group = element.closest("[role='group'], [role='radiogroup']");
                    if (group) {
                        return group.getAttribute("data-ui") ||
                            group.getAttribute("aria-labelledby") ||
                            group.id || "";
                    }
                    return element.name || element.id || "";
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _is_combobox_control(control: Any) -> bool:
    if (control.get_attribute("role") or "").casefold() == "combobox":
        return True
    if control.get_attribute("aria-controls") or control.get_attribute("aria-owns"):
        return True
    if (control.get_attribute("aria-autocomplete") or "").casefold() in {"list", "both"}:
        return True
    try:
        return bool(
            control.locator(
                "xpath=ancestor::*[@data-input-type='select' or @role='combobox'][1]"
            ).count()
        )
    except Exception:
        return False


def _choose_group_options(
    page: Page,
    control: Any,
    control_type: str,
    variants: Sequence[str],
    *,
    select_all_positive: bool = False,
) -> bool:
    controls = _group_controls(page, control, control_type)
    question = _question_label(control).casefold()
    candidates: list[tuple[Any, str]] = [
        (controls.nth(index), _option_label(controls.nth(index)))
        for index in range(controls.count())
        if not question or _question_label(controls.nth(index)).casefold() == question
    ]
    if control_type == "checkbox" and select_all_positive:
        selected = [
            item
            for item, label in candidates
            if label and not re.search(r"\b(?:not|none|never|prefer not|do not)\b", label, re.I)
        ]
    else:
        selected = [
            item for item, label in candidates if label and _option_matches(label, variants)
        ][:1]
    if not selected:
        return False
    successes = 0
    for item in selected:
        try:
            role = (item.get_attribute("role") or "").casefold()
            if role in {"radio", "checkbox"}:
                item.click(force=True, timeout=3000)
            else:
                item.check(force=True)
            selected_now = (
                item.get_attribute("aria-checked") == "true" if role else item.is_checked()
            )
            if not selected_now:
                wrapper = item.locator("xpath=ancestor::*[@role='radio' or @role='checkbox'][1]")
                (wrapper.first if wrapper.count() else item).click(force=True, timeout=3000)
                selected_now = (
                    item.get_attribute("aria-checked") == "true" if role else item.is_checked()
                )
            successes += int(selected_now)
        except Exception:
            try:
                wrapper = item.locator("xpath=ancestor::*[@role='radio' or @role='checkbox'][1]")
                (wrapper.first if wrapper.count() else item).click(force=True, timeout=3000)
                successes += int(
                    item.get_attribute("aria-checked") == "true"
                    if item.get_attribute("role")
                    else item.is_checked()
                )
            except Exception:
                continue
    return successes > 0


def _is_professional_binary_question(label: str) -> bool:
    return bool(
        re.search(
            r"^(?:do|does|did|have|has|are|is|can|will|would)\s+(?:you|your)\b",
            label,
            re.I,
        )
        and not re.search(
            r"gender|race|ethnic|veteran|disab|medical|health|sexual|transgender|"
            r"accommodation|criminal|convict",
            label,
            re.I,
        )
    )


def _select_all_positive_checkbox_answers(
    label: str,
    rules: Mapping[str, Any],
) -> bool:
    return bool(
        str(rules.get("interest_checkbox_selection", "")).casefold() == "all"
        and not re.search(
            r"\b(?:create|publish|write|produce)\b.{0,60}\b(?:content|topics?)\b",
            label,
            re.I,
        )
        and re.search(
            r"\b(?:interest|interested|areas?|topics?|disciplines?|specialt(?:y|ies)|"
            r"select all that apply|use .{0,80} tools?)\b",
            label,
            re.I,
        )
    )


def _answer_from_job_context(
    label: str,
    option_labels: Sequence[str],
    job_context: str,
) -> str:
    """Resolve an explicit job-description attention check without guessing."""
    if not job_context or not re.search(
        r"\b(?:favorite|according to .{0,30}description|"
        r"mentioned in .{0,30}description|attention check)\b",
        label,
        re.I,
    ):
        return ""
    matches: list[str] = []
    for option in option_labels:
        cleaned = _clean_label(option)
        if (
            cleaned
            and re.search(
                rf"(?<!\w){re.escape(cleaned)}(?!\w)",
                job_context,
                re.I,
            )
            and cleaned.casefold() not in {match.casefold() for match in matches}
        ):
            matches.append(cleaned)
    return matches[0] if len(matches) == 1 else ""


def _answer_for_binary_options(desired: object, option_labels: Sequence[str]) -> str:
    """Translate a configured substantive answer onto an explicit Yes/No group."""
    options = {_clean_label(option).casefold(): _clean_label(option) for option in option_labels}
    yes = options.get("yes")
    no = options.get("no")
    answer = _clean_label(desired)
    if (
        not yes
        or not no
        or not answer
        or re.search(
            r"\b(?:prefer not|decline|unknown|unspecified)\b",
            answer,
            re.I,
        )
    ):
        return ""
    if re.search(r"^(?:no|false|none|n/?a|not applicable)\b", answer, re.I):
        return no
    return yes


def _repeatable_language_answer(
    label: str,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> str:
    """Map SmartRecruiters repeatable language rows to configured fluency evidence."""
    match = re.search(r"\blanguage input for entry (\d+)\b", label, re.I)
    if match:
        languages = profile.get("languages")
        if isinstance(languages, Sequence) and not isinstance(languages, (str, bytes)):
            index = int(match.group(1)) - 1
            if 0 <= index < len(languages):
                return str(languages[index])
    if re.search(r"\blevel for .*language entry \d+\b", label, re.I):
        return str(rules.get("language_proficiency") or rules.get("language_fluency") or "")
    return ""


def _relocation_target_answers(
    label: str,
    desired: object,
    profile: Mapping[str, Any],
) -> tuple[str, ...]:
    if str(desired or "").strip().casefold() not in {"yes", "true"}:
        return ()
    match = re.search(
        r"\brelocat(?:e|ing)\s+to\s+([^?;,]{2,60})",
        label,
        re.I,
    )
    if not match:
        return ()
    target = _clean_label(match.group(1))
    current_location = str(profile.get("location") or "").casefold()
    if target.casefold() in current_location:
        return (f"Based in {target}",)
    return (
        f"Willing to relocate to {target}",
        f"Relocate to {target}",
    )


def _fill_custom_questions(
    page: Page,
    config: Mapping[str, Any],
    *,
    company: str,
    role: str,
    standard_selectors: Sequence[str] = (),
    job_context: str = "",
) -> dict[str, bool]:
    """Fill configured Workable/SmartRecruiters questions across native and React controls."""
    profile = _mapping(config.get("candidate"))
    rules = _mapping(config.get("rules"))
    eeo = _mapping(config.get("eeo_defaults"))
    matchers = _mapping(config.get("field_matchers"))
    variants = _mapping(config.get("answer_variants"))
    candidate_evidence = load_candidate_evidence(config)
    form_text = page.locator("body").inner_text()
    job_text = f"{job_context}\n{form_text}"[:30_000]
    results: dict[str, bool] = {}
    handled_groups: set[str] = set()
    standard_names = {
        "firstname",
        "first_name",
        "lastname",
        "last_name",
        "email",
        "phone",
        "address",
        "city",
        "postcode",
        "country",
        "summary",
        "cover_letter",
    }
    controls = page.locator(
        "select, textarea, input:not([type='file']):not([type='hidden']):"
        "not([type='submit']):not([type='button']), [role='combobox'], "
        "[role='radio'], [role='checkbox']"
    )
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            tag = control.evaluate("element => element.tagName.toLowerCase()")
            control_type = (control.get_attribute("type") or "").casefold()
            name = (control.get_attribute("name") or "").casefold()
            control_id = (control.get_attribute("id") or "").casefold()
            role_name = (control.get_attribute("role") or "").casefold()
            effective_type = role_name if role_name in {"radio", "checkbox"} else control_type
            combobox_control = _is_combobox_control(control)
            if standard_selectors and control.evaluate(
                "(element, selectors) => selectors.some(selector => element.matches(selector))",
                list(standard_selectors),
            ):
                continue
            if name in standard_names or control_id in {
                "first-name-input",
                "last-name-input",
                "email-input",
                "confirm-email-input",
                "linkedin-input",
                "facebook-input",
                "twitter-input",
                "website-input",
                "hiring-manager-message-input",
            }:
                continue
            if (
                effective_type not in {"radio", "checkbox"}
                and control.get_attribute("aria-hidden") == "true"
            ):
                continue
            if (
                effective_type not in {"radio", "checkbox"}
                and role_name != "combobox"
                and not control.is_visible()
            ):
                continue
            label = _question_label(control)
            if not label:
                continue
            group_key = (
                f"{effective_type}:{label.casefold()}"
                if effective_type in {"radio", "checkbox"}
                else _control_group_key(control)
            )
            if effective_type in {"radio", "checkbox"} and group_key:
                if group_key in handled_groups:
                    continue
                handled_groups.add(group_key)
            if re.search(r"(?:telephone|phone) country code", label, re.I):
                continue
            if re.search(r"phone number|country/region or code", label, re.I):
                continue
            if label.casefold() == "search" and control.get_attribute("required") is None:
                continue
            desired = configured_answer(
                label,
                profile,
                rules,
                eeo,
                matchers,  # type: ignore[arg-type]
            )
            desired = _repeatable_language_answer(label, profile, rules) or desired
            relocation_answers = _relocation_target_answers(label, desired, profile)
            if re.search(r"\byears?\b.*\bexperience\b", label, re.I):
                desired = str(rules.get("management_experience_years") or desired or "")
            if (
                re.search(r"\bsalary|compensation|pay expectation\b", label, re.I)
                and not re.search(r"\d", str(desired or ""))
                and profile.get("compensation")
            ):
                desired = str(profile["compensation"])
            if not desired and re.search(r"\brefer(?:red|ral)\b", label, re.I):
                desired = "N/A"
            if not desired and _is_professional_binary_question(label):
                desired = str(rules.get("experience_requirement") or "")
            option_labels: list[str] = []
            if effective_type in {"radio", "checkbox"}:
                group = _group_controls(page, control, effective_type)
                option_labels = [_option_label(group.nth(item)) for item in range(group.count())]
            if not desired and effective_type in {"radio", "checkbox"}:
                desired = _answer_from_job_context(
                    label,
                    option_labels,
                    job_context,
                )
            if not desired and is_essay_question(label):
                desired = generate_essay_answer(
                    label,
                    job_text,
                    company,
                    role,
                    candidate_evidence,
                )
            desired = _answer_for_binary_options(desired, option_labels) or desired

            preferred = (
                relocation_answers
                if relocation_answers
                else location_answer_candidates(profile)
                if is_location_question(label)
                else answer_variants(
                    label,
                    str(desired or ""),
                    variants,  # type: ignore[arg-type]
                )
            )
            maximum_policy = bool(
                str(rules.get("experience_level_selection", "")).casefold() == "max_value"
                and re.search(
                    r"experience|advanced stage|performance|evaluation|multiple components|"
                    r"proficiency|familiar",
                    label,
                    re.I,
                )
            )
            success = False
            if combobox_control:
                success = _select_combobox(
                    page,
                    control,
                    preferred,
                    prefer_maximum=maximum_policy,
                )
            elif tag == "select":
                success = _select_native(
                    control,
                    preferred,
                    prefer_maximum=maximum_policy,
                )
            elif effective_type in {"radio", "checkbox"}:
                success = _choose_group_options(
                    page,
                    control,
                    effective_type,
                    preferred,
                    select_all_positive=(
                        effective_type == "checkbox"
                        and _select_all_positive_checkbox_answers(label, rules)
                    ),
                )
            elif desired and tag in {"input", "textarea"}:
                control.fill(str(desired))
                success = bool(control.input_value().strip())
            results[label] = success
        except Exception as exc:
            logger.debug("Custom question failed at index %d: %s", index, exc)
    return results


def _required_issues(page: Page) -> list[str]:
    """Inspect invalid native controls and controls explicitly marked as required."""
    try:
        issues = list(
            page.locator("input, select, textarea, [role='radio'], [role='checkbox']").evaluate_all(
                """controls => {
                    const issues = [];
                    const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                    for (const control of controls) {
                        const type = (
                            control.type || control.getAttribute("role") || ""
                        ).toLowerCase();
                        if (control.disabled ||
                            control.getAttribute("aria-disabled") === "true" ||
                            type === "hidden") continue;
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
                        const group = control.closest(
                            "[role='group'], [role='radiogroup'], spl-radio-group"
                        );
                        const required = control.required ||
                            control.getAttribute("aria-required") === "true" ||
                            (group && (
                                group.hasAttribute("required") ||
                                group.getAttribute("aria-required") === "true"
                            ));
                        const ariaInvalid = control.getAttribute("aria-invalid") === "true";
                        const nativelyValid = typeof control.checkValidity !== "function" ||
                            control.checkValidity();
                        if (!required && !ariaInvalid && nativelyValid) continue;
                        let missing = false;
                        if (type === "radio") {
                            if (group) {
                                missing = !group.querySelector(
                                    'input[type="radio"]:checked, ' +
                                    '[role="radio"][aria-checked="true"]'
                                );
                            } else {
                                const escaped = window.CSS && CSS.escape
                                    ? CSS.escape(control.name)
                                    : String(control.name || "").replace(/["\\\\]/g, "\\\\$&");
                                missing = !control.name ||
                                    !document.querySelector(
                                        `input[type="radio"][name="${escaped}"]:checked`
                                    );
                            }
                        } else if (type === "checkbox") {
                            const group = control.closest("[role='group'], fieldset");
                            const grouped = group &&
                                group.querySelectorAll('input[type="checkbox"]').length > 1;
                            missing = grouped
                                ? !group.querySelector('input[type="checkbox"]:checked')
                                : !(control.checked ||
                                    control.getAttribute("aria-checked") === "true");
                        } else if (type === "file") {
                            missing = !control.files || control.files.length === 0;
                        } else {
                            const value = clean(control.value);
                            missing = !value || /^no answer$/i.test(value);
                        }
                        if (missing || ariaInvalid || !nativelyValid) {
                            issues.push(context || control.name || control.id || "Required field missing");
                        }
                    }
                    const errorSelectors = [
                        "[role='alert']",
                        "[aria-live='assertive']",
                        ".MuiFormHelperText-root.Mui-error",
                        "[class*='error-message']",
                        "[class*='errorMessage']",
                        "[class*='field-error']"
                    ].join(",");
                    for (const error of document.querySelectorAll(errorSelectors)) {
                        const style = window.getComputedStyle(error);
                        const visible = style.display !== "none" &&
                            style.visibility !== "hidden" &&
                            (error.getClientRects().length > 0);
                        const text = clean(error.innerText).slice(0, 180);
                        if (visible && text &&
                            /required|provide|invalid|cannot contain|must (?:be|have|contain)/i.test(text)) {
                            issues.push(text);
                        }
                    }
                    return Array.from(new Set(issues));
                }"""
            )
        )
        precise_checkbox_issues: list[str] = []
        boxes = page.locator('input[type="checkbox"]')
        for index in range(boxes.count()):
            box = boxes.nth(index)
            try:
                required = (
                    box.get_attribute("required") is not None
                    or box.get_attribute("aria-required") == "true"
                )
                if required and not box.is_checked():
                    label = _question_label(box)
                    if label:
                        precise_checkbox_issues.append(label)
            except Exception:
                continue
        if precise_checkbox_issues:
            issues = [issue for issue in issues if _clean_label(issue) not in {"", "*"}]
            issues.extend(precise_checkbox_issues)
        return list(dict.fromkeys(issues))
    except Exception as exc:
        logger.warning("Required-field inspection failed: %s", exc)
        return ["Required-field inspection failed"]


def _fill_standard_value(page: Page, selectors: Sequence[str], value: str) -> bool:
    if not value:
        return False
    control = _first_visible_for(page, selectors)
    if control is None:
        return False
    if (control.get_attribute("role") or "").casefold() == "combobox":
        return _select_combobox(page, control, (value,))
    try:
        control.fill(value)
        return bool(control.input_value().strip())
    except Exception:
        return False


def _fill_phone(page: Page, spec: BrowserFormSpec, candidate: CandidateFields) -> bool:
    if not candidate.phone:
        return False
    country_code = first_visible(
        page.locator(
            '[role="combobox"][aria-label*="telephone country code" i], '
            '[aria-label*="phone country code" i]'
        )
    )
    if country_code is not None and candidate.country:
        _select_combobox(page, country_code, (candidate.country,))
    control = _first_visible_for(page, spec.phone_selectors)
    if control is None:
        return False
    value = candidate.phone
    if country_code is not None:
        digits = re.sub(r"\D", "", value)
        try:
            dial_digits = re.sub(r"\D", "", country_code.inner_text())
        except Exception:
            dial_digits = ""
        if value.strip().startswith("+") and dial_digits and digits.startswith(dial_digits):
            digits = digits[len(dial_digits) :]
        value = digits
    try:
        control.fill(value)
        control.blur()
        page.wait_for_timeout(250)
        return bool(control.input_value().strip())
    except Exception:
        return False


def _fill_standard_fields(
    page: Page,
    spec: BrowserFormSpec,
    candidate: CandidateFields,
    resume: Path,
    cover_letter: Path | None,
) -> tuple[dict[str, bool], list[str]]:
    resume_uploaded = _upload_first(page, spec.resume_selectors, resume)
    if resume_uploaded and spec.resume_parse_wait_ms:
        page.wait_for_timeout(spec.resume_parse_wait_ms)

    first_name = fill_first(page, spec.first_name_selectors, candidate.first_name)
    last_name = fill_first(page, spec.last_name_selectors, candidate.last_name)
    full_name = False
    if not (first_name and last_name):
        full_name = fill_first(page, spec.full_name_selectors, candidate.full_name)
    email = fill_first(page, spec.email_selectors, candidate.email)
    email_confirmation = (
        fill_first(page, spec.email_confirmation_selectors, candidate.email)
        if spec.email_confirmation_selectors
        else False
    )

    filled = {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "email": email,
        "email_confirmation": email_confirmation,
        "phone": _fill_phone(page, spec, candidate),
        "headline": fill_first(page, spec.headline_selectors, candidate.headline),
        "linkedin": fill_first(page, spec.linkedin_selectors, candidate.linkedin),
        "github": fill_first(page, spec.github_selectors, candidate.github),
        "portfolio": fill_first(page, spec.portfolio_selectors, candidate.portfolio),
        "address": _fill_standard_value(
            page,
            spec.address_selectors,
            candidate.street_address,
        ),
        "city": _fill_standard_value(page, spec.city_selectors, candidate.city),
        "postcode": _fill_standard_value(
            page,
            spec.postcode_selectors,
            candidate.postcode,
        ),
        "country": _fill_standard_value(
            page,
            spec.country_selectors,
            candidate.country,
        ),
        "resume": resume_uploaded,
        "cover_letter": (
            _fill_cover_letter(page, spec, cover_letter) if cover_letter is not None else False
        ),
    }
    missing_critical = []
    if not ((first_name and last_name) or full_name):
        missing_critical.append("candidate name")
    if not email:
        missing_critical.append("candidate email")
    if spec.email_confirmation_selectors and not email_confirmation:
        missing_critical.append("candidate email confirmation")
    if not filled["resume"]:
        missing_critical.append("resume")
    return filled, missing_critical


def _standard_control_selectors(spec: BrowserFormSpec) -> tuple[str, ...]:
    """Selectors already owned by deterministic standard-field filling."""
    return (
        spec.first_name_selectors
        + spec.last_name_selectors
        + spec.full_name_selectors
        + spec.email_selectors
        + spec.email_confirmation_selectors
        + spec.phone_selectors
        + spec.headline_selectors
        + spec.linkedin_selectors
        + spec.github_selectors
        + spec.portfolio_selectors
        + spec.address_selectors
        + spec.city_selectors
        + spec.postcode_selectors
        + spec.country_selectors
        + spec.cover_letter_text_selectors
    )


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
    custom_questions: Mapping[str, bool] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    payload = {
        "success": status in {"PREFILLED_ONLY", "SUBMITTED & CONFIRMED"},
        "status": status,
        "ats": spec.ats,
        "submitted": submitted,
        "confirmed": confirmed,
        "test_mode": not live_submit,
        "filled_fields": dict(filled_fields),
        "custom_questions": dict(custom_questions or {}),
        "consent_fields": list(consent_fields),
        "missing_critical": list(missing_critical),
        "missing_required": list(missing_required),
        "captcha_present": captcha_present,
        "preexisting_confirmation": preexisting_confirmation,
        "screenshot": screenshot,
    }
    if detail:
        payload["detail"] = detail
    return payload


_CLOSED_JOB_PATTERNS = (
    "sorry, this job has expired",
    "sorry, this position has been filled",
    "job is no longer available",
    "job no longer available",
    "position is no longer available",
    "no longer accepting applications",
    "this job is closed",
)


def _closed_job_reason(page: Page, apply_button: Any | None) -> str:
    try:
        body = _clean_label(page.locator("body").inner_text()).casefold()
    except Exception:
        body = ""
    button_text = ""
    if apply_button is not None:
        try:
            button_text = _clean_label(apply_button.inner_text()).casefold()
        except Exception:
            pass
    combined = f"{button_text}\n{body}"
    marker = next((phrase for phrase in _CLOSED_JOB_PATTERNS if phrase in combined), "")
    if marker:
        return marker
    return ""


def run_browser_form_engine(
    spec: BrowserFormSpec,
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
    """Fill one configured provider form and submit only after all safety gates pass."""
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
            headless=headless,
            background=spec.background_cdp and headless,
        )
        page = session.page
        try:
            navigate_reusing_tab(page, url, timeout=timeout, wait_until="commit")
            if spec.render_wait_ms:
                page.wait_for_timeout(spec.render_wait_ms)
            _dismiss_cookie_banner(page)

            visible_apply_button = _first_visible_for(page, spec.apply_selectors)
            entry_kind = (
                "apply"
                if visible_apply_button is not None
                and _apply_control_is_ready(visible_apply_button)
                else _wait_for_application_entry(page, spec, timeout)
            )
            apply_button = None
            if entry_kind == "apply":
                _dismiss_cookie_banner(page)
                apply_button = _first_visible_for(page, spec.apply_selectors)
                if apply_button is None or not _apply_control_is_ready(apply_button):
                    entry_kind = ""
                    apply_button = None
            closed_reason = _closed_job_reason(
                page,
                apply_button
                or visible_apply_button
                or _first_visible_for(page, spec.apply_selectors),
            )
            if closed_reason:
                screenshot = capture_screenshot(
                    page,
                    screenshot_dir,
                    company or spec.display_name,
                    "closed",
                )
                return _result(
                    spec=spec,
                    status="JOB_CLOSED",
                    live_submit=live_submit,
                    submitted=False,
                    confirmed=False,
                    filled_fields={},
                    consent_fields=[],
                    missing_critical=[],
                    missing_required=[],
                    captcha_present=False,
                    screenshot=screenshot,
                    detail=closed_reason,
                )
            if not entry_kind:
                captcha = page_has_captcha(page)
                screenshot = capture_screenshot(
                    page,
                    screenshot_dir,
                    company or spec.display_name,
                    "captcha" if captcha else "entry-not-ready",
                )
                return _result(
                    spec=spec,
                    status="CAPTCHA_REQUIRED" if captcha else "REQUIRED_FIELDS_NOT_FILLED",
                    live_submit=live_submit,
                    submitted=False,
                    confirmed=False,
                    filled_fields={},
                    consent_fields=[],
                    missing_critical=(
                        [] if captcha else ["application form entry did not become ready"]
                    ),
                    missing_required=[],
                    captcha_present=captcha,
                    screenshot=screenshot,
                    detail=(
                        "CAPTCHA challenge blocked application form entry"
                        if captcha
                        else "application form entry did not become ready"
                    ),
                )
            try:
                job_context = page.locator("body").inner_text()[:30_000]
            except Exception:
                job_context = ""
            if apply_button is not None:
                form_opened = False
                captcha_blocked = False
                for attempt in range(2):
                    href = apply_button.get_attribute("href") or ""
                    if href:
                        navigate_reusing_tab(
                            page,
                            urljoin(page.url, href),
                            timeout=timeout,
                            wait_until="commit",
                        )
                    else:
                        apply_button.click(timeout=min(timeout, 10_000))
                    page.wait_for_timeout(max(1000, spec.render_wait_ms))
                    if _wait_for_form(page, spec, timeout):
                        form_opened = True
                        break
                    if page_has_captcha(page):
                        captcha_blocked = True
                        break
                    if attempt == 0:
                        retry_entry_kind = _wait_for_application_entry(page, spec, timeout)
                        if retry_entry_kind == "form":
                            form_opened = True
                            break
                        if page_has_captcha(page):
                            captcha_blocked = True
                            break
                        if retry_entry_kind != "apply":
                            break
                        apply_button = _first_visible_for(page, spec.apply_selectors)
                        if apply_button is None or not _apply_control_is_ready(apply_button):
                            break
                if not form_opened:
                    screenshot = capture_screenshot(
                        page,
                        screenshot_dir,
                        company or spec.display_name,
                        "captcha" if captcha_blocked else "form-not-open",
                    )
                    return _result(
                        spec=spec,
                        status=(
                            "CAPTCHA_REQUIRED" if captcha_blocked else "REQUIRED_FIELDS_NOT_FILLED"
                        ),
                        live_submit=live_submit,
                        submitted=False,
                        confirmed=False,
                        filled_fields={},
                        consent_fields=[],
                        missing_critical=(
                            [] if captcha_blocked else ["application form did not open"]
                        ),
                        missing_required=[],
                        captcha_present=captcha_blocked,
                        screenshot=screenshot,
                        detail=(
                            "CAPTCHA challenge blocked the application form"
                            if captcha_blocked
                            else "application form did not open"
                        ),
                    )
                _dismiss_cookie_banner(page)

            filled, missing_critical = _fill_standard_fields(
                page,
                spec,
                candidate,
                resume,
                cover_letter,
            )
            custom_questions = _fill_custom_questions(
                page,
                config,
                company=company or spec.display_name,
                role=role,
                standard_selectors=_standard_control_selectors(spec),
                job_context=job_context,
            )
            consent = fill_required_consent(page)
            # Resume parsing, address selection, and custom React controls can
            # rerender the identity block. Refill email fields only after those
            # dynamic interactions, blur them to trigger validation, and repair
            # provider-reported text character restrictions before gating submit.
            _stabilize_email_fields(page, spec, candidate, filled)
            _repair_forbidden_text_characters(page)
            consent = sorted(set(consent + fill_required_consent(page)))
            captcha = page_has_captcha(page)
            missing_required = validate_required_fields(page, _required_issues)
            for _ in range(5):
                if missing_critical or missing_required or captcha or not spec.next_selectors:
                    break
                next_button = _first_visible_for(page, spec.next_selectors)
                if next_button is None:
                    break
                try:
                    if not next_button.is_enabled():
                        missing_required = validate_required_fields(page, _required_issues)
                        break
                    previous_step = _step_fingerprint(page)
                    next_button.click(timeout=min(timeout, 10_000))
                    page.wait_for_timeout(max(1000, spec.render_wait_ms))
                    if not _wait_for_step_ready(page, timeout, previous_step):
                        captcha = page_has_captcha(page)
                        if not captcha:
                            missing_required = ["Application step did not become ready"]
                        break
                except Exception as exc:
                    logger.debug("%s intermediate step failed: %s", spec.display_name, exc)
                    missing_required = ["Application step could not be advanced"]
                    break
                custom_questions.update(
                    _fill_custom_questions(
                        page,
                        config,
                        company=company or spec.display_name,
                        role=role,
                        standard_selectors=_standard_control_selectors(spec),
                        job_context=job_context,
                    )
                )
                if cover_letter is not None and not filled["cover_letter"]:
                    filled["cover_letter"] = _fill_cover_letter(page, spec, cover_letter)
                _repair_forbidden_text_characters(page)
                consent = sorted(set(consent + fill_required_consent(page)))
                captcha = page_has_captcha(page)
                missing_required = validate_required_fields(page, _required_issues)
            screenshot = capture_screenshot(
                page,
                screenshot_dir,
                company or spec.display_name,
                "prefill",
            )

            if captcha:
                return _result(
                    spec=spec,
                    status="CAPTCHA_REQUIRED",
                    live_submit=live_submit,
                    submitted=False,
                    confirmed=False,
                    filled_fields=filled,
                    consent_fields=consent,
                    missing_critical=missing_critical,
                    missing_required=missing_required,
                    captcha_present=True,
                    screenshot=screenshot,
                    custom_questions=custom_questions,
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
                    custom_questions=custom_questions,
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
                    custom_questions=custom_questions,
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
                    custom_questions=custom_questions,
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
                    custom_questions=custom_questions,
                )

            if page_has_captcha(page):
                screenshot = capture_screenshot(
                    page,
                    screenshot_dir,
                    company or spec.display_name,
                    "captcha",
                )
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
                    custom_questions=custom_questions,
                    detail="CAPTCHA challenge appeared before submit",
                )

            submitted = True
            click_error = ""
            try:
                submit_button.click()
            except Exception as exc:
                click_error = f"{type(exc).__name__}: {exc}"[:300]
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
                custom_questions=custom_questions,
                detail=(
                    f"Submit click raised after dispatch may have occurred: {click_error}"
                    if click_error and not confirmed
                    else ""
                ),
            )
        finally:
            close_browser_session(session)


def engine_parser(spec: BrowserFormSpec) -> argparse.ArgumentParser:
    return build_engine_parser(f"{spec.display_name} application engine")


def load_orchestrated_config() -> dict[str, Any]:
    return normalize_profile_config(load_json_config(orchestrated_config_path()))


def main_for_browser_form_engine(
    spec: BrowserFormSpec,
    argv: Sequence[str] | None = None,
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
