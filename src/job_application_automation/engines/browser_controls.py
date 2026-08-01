"""Provider-neutral Playwright form-control primitives.

The helpers in this module deliberately stop below provider workflow policy. They
operate on locators, fields, and files, while each ATS adapter retains ownership
of navigation, custom widgets, question semantics, and submission decisions.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from playwright.sync_api import Locator, Page


SENSITIVE_FIELD_PATTERN = re.compile(
    r"eeo|gender|race|racial|ethnic|hispanic|latino|veteran|disability|"
    r"sexual|\bsex\b|orientation|transgender|demographic|identity|pronoun",
    re.IGNORECASE,
)


def first_visible(locator: Locator) -> Locator | None:
    """Return the first visible locator, tolerating detached candidates."""
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def first_visible_for(
    page: Page,
    selectors: Sequence[str],
    *,
    visible_resolver: Callable[[Locator], Locator | None] = first_visible,
) -> Locator | None:
    """Return the first visible control found across an ordered selector list."""
    for selector in selectors:
        target = visible_resolver(page.locator(selector))
        if target is not None:
            return target
    return None


def fill_first(
    page: Page,
    selectors: Sequence[str] | str,
    value: str,
    *,
    visible_resolver: Callable[[Locator], Locator | None] = first_visible,
) -> bool:
    """Fill the first visible matching control and verify the resulting value."""
    if not value:
        return False
    selector_list = (
        [selector.strip() for selector in selectors.split(",") if selector.strip()]
        if isinstance(selectors, str)
        else list(selectors)
    )
    for selector in selector_list:
        target = visible_resolver(page.locator(selector))
        if target is None:
            continue
        try:
            target.fill(value)
            return target.input_value().strip() == value.strip()
        except Exception:
            continue
    return False


def fill_all_visible(page: Page, selectors: Sequence[str], value: str) -> bool:
    """Fill every visible duplicate and require all matched controls to verify."""
    if not value:
        return False
    matched = False
    all_filled = True
    for selector in selectors:
        controls = page.locator(selector)
        for index in range(controls.count()):
            control = controls.nth(index)
            try:
                if not control.is_visible():
                    continue
                matched = True
                control.fill(value)
                all_filled = all_filled and control.input_value().strip() == value.strip()
            except Exception:
                all_filled = False
    return matched and all_filled


def label_for(page: Page, control: Locator) -> str:
    """Resolve an accessible label for native and custom controls."""
    control_id = control.get_attribute("id") or ""
    if control_id:
        label = page.locator(f'label[for="{control_id}"]').first
        if label.count():
            return " ".join(label.inner_text().split()).rstrip("* ").strip()
    labelled_by = control.get_attribute("aria-labelledby") or ""
    if labelled_by:
        label = page.locator(f"#{labelled_by}").first
        if label.count():
            return " ".join(label.inner_text().split()).rstrip("* ").strip()
    ancestor = control.locator("xpath=ancestor::label[1]")
    if ancestor.count():
        return " ".join(ancestor.first.inner_text().split()).rstrip("* ").strip()
    return ""


def fill_labeled(
    page: Page,
    label_pattern: str,
    value: str,
    *,
    visible_resolver: Callable[[Locator], Locator | None] = first_visible,
) -> bool:
    """Fill the first visible control whose accessible label matches a regex."""
    if not value:
        return False
    try:
        target = visible_resolver(page.get_by_label(re.compile(label_pattern, re.IGNORECASE)))
        if target is not None:
            target.fill(value)
            return bool(target.input_value().strip())
    except Exception:
        pass
    return False


def upload_first(page: Page, selectors: Sequence[str], path: Path) -> bool:
    """Upload a file to the first usable control in selector order."""
    for selector in selectors:
        controls = page.locator(selector)
        for index in range(controls.count()):
            control = controls.nth(index)
            try:
                control.set_input_files(str(path))
                return True
            except Exception:
                continue
    return False


def upload_matching_file(
    page: Page,
    path: Path,
    *,
    required_terms: Sequence[str],
    context_resolver: Callable[[Locator], str],
) -> bool | None:
    """Upload to a semantically matching file control.

    ``None`` means the form exposes no matching field. ``False`` means a field
    matched but every upload attempt failed, preserving the adapters' existing
    optional-document contract.
    """
    inputs = page.locator('input[type="file"]')
    matched = False
    normalized_terms = tuple(term.casefold().strip() for term in required_terms if term.strip())
    for index in range(inputs.count()):
        target = inputs.nth(index)
        try:
            context = context_resolver(target).casefold().replace("_", " ").replace("-", " ")
            if not all(term in context for term in normalized_terms):
                continue
            matched = True
            target.set_input_files(str(path))
            return True
        except Exception:
            continue
    return False if matched else None


def fill_and_blur(
    page: Page,
    selectors: Sequence[str],
    value: str,
    *,
    control_resolver: Callable[[Page, Sequence[str]], Locator | None] = first_visible_for,
) -> bool:
    """Fill and blur a visible field so client-side validation is committed."""
    if not value:
        return False
    control = control_resolver(page, selectors)
    if control is None:
        return False
    try:
        control.fill(value)
        control.blur()
        page.wait_for_timeout(250)
        return control.input_value().strip() == value.strip()
    except Exception:
        return False


def _consent_control_is_checked(control: Locator) -> bool:
    if (control.get_attribute("role") or "").casefold() == "checkbox":
        return (control.get_attribute("aria-checked") or "").casefold() == "true"
    return control.is_checked()


def _check_consent_control(control: Locator) -> None:
    if (control.get_attribute("role") or "").casefold() == "checkbox":
        control.click(force=True)
    else:
        control.check(force=True)


def fill_required_consent(
    page: Page,
    *,
    label_resolver: Callable[[Page, Locator], str] = label_for,
    sensitive_field_pattern: re.Pattern[str] = SENSITIVE_FIELD_PATTERN,
) -> list[str]:
    """Select explicit required policy consent without guessing declarations."""
    checked: list[str] = []
    boxes = page.locator('input[type="checkbox"], [role="checkbox"]')
    for index in range(boxes.count()):
        box = boxes.nth(index)
        try:
            if _consent_control_is_checked(box):
                continue
            label = label_resolver(page, box)
            context = label or box.evaluate(
                "el => (el.closest('fieldset,div') || el.parentElement)?.innerText || ''"
            )
            explicit_confirm = bool(
                re.search(
                    r"\b(?:i|you)\b.{0,120}"
                    r"\b(?:acknowledge|agree|consent|accept|confirm|declare)\b",
                    context,
                    re.I,
                )
            )
            consent_context = bool(
                re.search(
                    r"\b(?:acknowledge|agree|consent|privacy|terms|policy|"
                    r"data (?:processing|protection)|personal data)\b",
                    context,
                    re.I,
                )
            )
            if not box.is_visible() and not (explicit_confirm or consent_context):
                continue
            if sensitive_field_pattern.search(context) and not explicit_confirm:
                continue
            if re.search(
                r"\b(?:provide|upload|attach|submit)\b.{0,140}"
                r"\b(?:copy|scan|document|attachment|certificate|degree|"
                r"reference|record)\b",
                context,
                re.I,
            ):
                continue
            required = consent_context and (
                explicit_confirm
                or box.get_attribute("required") is not None
                or box.get_attribute("aria-required") == "true"
            )
            if required:
                _check_consent_control(box)
                if _consent_control_is_checked(box):
                    checked.append(" ".join(context.split())[:160])
        except Exception:
            continue
    return checked


def validate_required_fields(
    page: Page,
    inspector: Callable[[Page], Sequence[str]],
) -> list[str]:
    """Run an ATS-specific inspector and return stable, deduplicated issues."""
    return sorted({str(issue).strip() for issue in inspector(page) if str(issue).strip()})
