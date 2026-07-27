"""Shared runtime, policy, CLI, and shell-test utilities for ATS engines."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Browser, Locator, Page, Playwright


RESULT_PREFIX = "ENGINE_RESULT_JSON:"
ORCHESTRATOR_INVOCATION_ENV = "JOB_APP_ORCHESTRATOR_INVOCATION"
SUCCESSFUL_STATUSES = frozenset({"PREFILLED_ONLY", "SUBMITTED & CONFIRMED"})
DEFAULT_CONFIRMATION_PHRASES = (
    "application submitted",
    "application has been submitted",
    "thank you for applying",
    "thank you so much for your interest",
    "thanks for applying",
    "application received",
    "application has been received",
    "successfully submitted",
)
DEFAULT_FAILURE_PHRASES = (
    "flagged as possible spam",
    "flagged as potential bot traffic",
    "couldn't submit",
    "submission failed",
)
SENSITIVE_FIELD_PATTERN = re.compile(
    r"eeo|gender|race|racial|ethnic|hispanic|latino|veteran|disability|"
    r"sexual|\bsex\b|orientation|transgender|demographic|identity|pronoun",
    re.IGNORECASE,
)

ATS_HOST_MARKERS: Mapping[str, tuple[str, ...]] = {
    "ashby": ("ashbyhq.com",),
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
}


@dataclass
class BrowserSession:
    browser: Browser
    page: Page
    close_browser_on_exit: bool


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_json_config(
    path: Path,
    *,
    defaults: Optional[Mapping[str, Any]] = None,
    required_candidate_fields: Sequence[str] = ("first_name", "last_name"),
) -> dict[str, Any]:
    config_path = path.expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict):
        raise ValueError("config root must be a JSON object")
    config = deep_merge(defaults or {}, document)
    candidate = config.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("config must contain a candidate object")
    missing = [field for field in required_candidate_fields if not candidate.get(field)]
    if missing:
        raise ValueError(f"config candidate is missing: {', '.join(missing)}")
    return config


def validate_ats_url(url: str, ats: str) -> bool:
    if ats not in ATS_HOST_MARKERS:
        return False
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    greenhouse_job_id = parse_qs(parsed.query).get("gh_jid", [])
    custom_greenhouse_url = (
        ats == "greenhouse"
        and len(greenhouse_job_id) == 1
        and greenhouse_job_id[0].isdigit()
    )
    return (
        parsed.scheme.lower() == "https"
        and bool(host)
        and (
            any(_host_matches(host, marker) for marker in ATS_HOST_MARKERS[ats])
            or custom_greenhouse_url
        )
    )


def valid_email(value: str) -> bool:
    local, separator, domain = str(value).strip().partition("@")
    return bool(separator and local and "." in domain and not any(char.isspace() for char in value))


def validate_nonempty_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ValueError(f"{label} does not exist or is empty: {resolved}")
    return resolved


def safe_filename(value: str, fallback: str = "ats") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip()).strip("_")
    return cleaned or fallback


def first_visible(locator: Locator) -> Optional[Locator]:
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def fill_first(page: Page, selectors: Sequence[str], value: str) -> bool:
    if not value:
        return False
    for selector in selectors:
        target = first_visible(page.locator(selector))
        if target is None:
            continue
        try:
            target.fill(value)
            return target.input_value().strip() == value.strip()
        except Exception:
            continue
    return False


def label_for(page: Page, control: Locator) -> str:
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
    return ""


def fill_labeled(page: Page, label_pattern: str, value: str) -> bool:
    if not value:
        return False
    try:
        target = first_visible(page.get_by_label(re.compile(label_pattern, re.IGNORECASE)))
        if target is not None:
            target.fill(value)
            return bool(target.input_value().strip())
    except Exception:
        pass
    return False


def answer_variants(
    label: str,
    desired: str,
    configured_variants: Optional[Mapping[str, Sequence[str]]] = None,
) -> tuple[str, ...]:
    variants = [desired]
    normalized = desired.strip().lower()
    del label  # semantic option aliases are configuration-driven.
    if normalized in {"yes", "no"}:
        variants.append(normalized.title())
    if configured_variants:
        for key, values in configured_variants.items():
            if key.lower() == normalized and isinstance(values, Sequence) and not isinstance(values, str):
                variants.extend(str(value) for value in values if value)
    return tuple(dict.fromkeys(variants))


def configured_answer(
    label: str,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
    eeo: Mapping[str, Any],
    field_matchers: Optional[Mapping[str, Sequence[str]]] = None,
) -> Optional[str]:
    text = label.lower()
    if re.search(
        r"\byears?\s+of\s+.*experience\b|"
        r"\b\d+\+?\s*years?\b.*\bexperience\b",
        text,
    ):
        return "Yes"
    semantic_values: Mapping[str, Any] = {
        "preferred_name": profile.get("preferred_name"),
        "legal_name": " ".join(
            part for part in (profile.get("first_name"), profile.get("last_name")) if part
        ),
        "age_over_18": "Yes",
        "sanctions_residency": "No",
        "api_product_experience": "Yes",
        "intended_work_location": profile.get("location"),
        "current_company": profile.get("current_company"),
        "current_title": profile.get("current_job_title"),
        "government_entity_details": "N/A",
        "human_attestation": "I am a human being",
        "linkedin": profile.get("linkedin"),
        "portfolio": profile.get("website") or profile.get("portfolio"),
        "public_username": str(profile.get("twitter", "")).rsplit("/", 1)[-1],
        "location": profile.get("location"),
        "country": profile.get("country"),
        "degree": profile.get("highest_degree"),
        "bachelors_degree": rules.get("bachelors_degree"),
        "salary_expectation": rules.get("salary_expectation"),
        "current_salary": rules.get("current_salary"),
        "notice_period": rules.get("notice_period"),
        "hyderabad_flexibility": rules.get("hyderabad_flexibility"),
        "flexible": rules.get("flexible"),
        "language_fluency": rules.get("language_fluency"),
        "current_employee": rules.get("current_employee"),
        "privacy_consent": rules.get("privacy_consent"),
        "eight_plus_product_years": rules.get("eight_plus_product_years"),
        "target_office": rules.get("target_office"),
        "application_certification": rules.get("application_certification"),
        "target_country_work_auth": rules.get("target_country_work_authorization"),
        "based_in_target_country": rules.get("based_in_target_country"),
        "international_travel": rules.get("international_travel"),
        "led_product_implementation": rules.get("led_product_implementation"),
        "regional_experience": rules.get("regional_experience"),
        "management_experience_years": rules.get("management_experience_years"),
        "selected_essay_questions": rules.get("selected_essay_questions"),
        "sponsorship": rules.get("visa_sponsorship"),
        "work_auth": rules.get("work_authorization"),
        "comfortable": rules.get("are_you_comfortable_with"),
        "relocation": rules.get("relocation_bay_area"),
        "employment_restrictions": rules.get("employment_restrictions"),
        "previous_employment": rules.get("previous_employment"),
        "source": rules.get("source_channel"),
        "pronouns": profile.get("pronouns"),
        "gender": eeo.get("gender") or profile.get("gender"),
        "hispanic_latino": eeo.get("hispanic_latino"),
        "race": eeo.get("race") or profile.get("race"),
        "veteran": eeo.get("veteran_status") or profile.get("veteran"),
        "disability": eeo.get("disability_status") or profile.get("disability"),
        "transgender": eeo.get("transgender_status") or profile.get("transgender"),
        "orientation": profile.get("orientation"),
    }
    if field_matchers:
        priority = (
            "sponsorship",
            "work_auth",
            "bachelors_degree",
            "degree",
            "application_certification",
            "salary_expectation",
            "target_country_work_auth",
            "based_in_target_country",
            "international_travel",
            "led_product_implementation",
            "regional_experience",
            "management_experience_years",
            "selected_essay_questions",
            "sanctions_residency",
            "api_product_experience",
            "intended_work_location",
            "employment_restrictions",
            "previous_employment",
            "comfortable",
            "relocation",
            "transgender",
            "orientation",
            "gender",
            "hispanic_latino",
            "race",
            "veteran",
            "disability",
            "pronouns",
            "age_over_18",
            "legal_name",
            "current_company",
            "current_title",
            "government_entity_details",
            "human_attestation",
            "linkedin",
            "portfolio",
            "public_username",
            "preferred_name",
            "country",
            "location",
            "source",
        )
        ordered_keys = list(priority) + [
            key for key in field_matchers if key not in priority
        ]
        for key in ordered_keys:
            aliases = field_matchers.get(key, ())
            answer = semantic_values.get(key)
            if answer not in (None, "") and any(str(alias).lower() in text for alias in aliases):
                return str(answer)
    return None


def is_essay_question(label: str) -> bool:
    if re.search(
        r"accommodation|adjustment|disability|demographic|gender|race|veteran|"
        r"sexual|transgender|if you answered yes",
        label,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"\bwhy\b|describe|explain|tell us|experience|achievement|"
            r"interest|motivat|excit|product|project|challenge|accomplish",
            label,
            re.I,
        )
    )


def fill_required_consent(page: Page) -> list[str]:
    checked: list[str] = []
    boxes = page.locator('input[type="checkbox"]')
    for index in range(boxes.count()):
        box = boxes.nth(index)
        try:
            if box.is_checked():
                continue
            label = label_for(page, box)
            context = label or box.evaluate(
                "el => (el.closest('fieldset,div') || el.parentElement)?.innerText || ''"
            )
            explicit_confirm = bool(
                re.search(r"\bi\s+(?:hereby\s+)?confirm\b", context, re.I)
            )
            if not box.is_visible() and not explicit_confirm:
                continue
            if SENSITIVE_FIELD_PATTERN.search(context) and not explicit_confirm:
                continue
            required = explicit_confirm or bool(
                re.search(r"\b(?:agree|acknowledge|consent|privacy)\b", context, re.I)
            )
            if required:
                box.check(force=True)
                if box.is_checked():
                    checked.append(" ".join(context.split())[:160])
        except Exception:
            continue
    return checked


def confirmation_visible(
    page: Page,
    *,
    success_phrases: Sequence[str] = DEFAULT_CONFIRMATION_PHRASES,
    failure_phrases: Sequence[str] = DEFAULT_FAILURE_PHRASES,
) -> bool:
    text = page.locator("body").inner_text().lower()
    return text_confirms_submission(
        text, success_phrases=success_phrases, failure_phrases=failure_phrases
    )


def text_confirms_submission(
    text: str,
    *,
    success_phrases: Sequence[str] = DEFAULT_CONFIRMATION_PHRASES,
    failure_phrases: Sequence[str] = DEFAULT_FAILURE_PHRASES,
) -> bool:
    text = text.lower()
    return (
        any(phrase.lower() in text for phrase in success_phrases)
        and not any(phrase.lower() in text for phrase in failure_phrases)
    )


def validate_required_fields(
    page: Page,
    inspector: Callable[[Page], Sequence[str]],
) -> list[str]:
    """Run an ATS-specific required-field adapter and normalize its result."""
    return sorted({str(issue).strip() for issue in inspector(page) if str(issue).strip()})


def capture_screenshot(page: Page, directory: Path, company: str, tag: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{safe_filename(company, 'ats')}_{safe_filename(tag, 'capture')}.png"
    try:
        page.screenshot(path=str(target), full_page=True)
        return str(target)
    except Exception:
        return ""


def _new_page(browser: Browser) -> Page:
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return context.new_page()


def page_has_captcha(page: Page) -> bool:
    """Return whether a visible CAPTCHA is present without interacting with it."""
    try:
        return page.locator(
            'iframe[src*="captcha" i]:visible, iframe[title*="captcha" i]:visible, '
            '[class*="captcha" i]:visible, [id*="captcha" i]:visible'
        ).count() > 0
    except Exception:
        return False


def _normalized_navigation_url(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def _reusable_page(browser: Browser, target_url: str) -> Optional[Page]:
    """Reuse an existing tab for the same application, excluding CAPTCHA tabs."""
    target = _normalized_navigation_url(target_url)
    blank: Optional[Page] = None
    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            if page.url in ("", "about:blank", "chrome://newtab/"):
                blank = blank or page
                continue
            try:
                if (
                    _normalized_navigation_url(page.url) == target
                    and not page_has_captcha(page)
                ):
                    return page
            except Exception:
                continue
    return blank


def navigate_reusing_tab(
    page: Page,
    url: str,
    *,
    timeout: int,
    wait_until: str = "domcontentloaded",
) -> None:
    """Refresh a matching tab; navigate only when it is not already on the target."""
    current = _normalized_navigation_url(page.url) if page.url not in ("", "about:blank") else ""
    target = _normalized_navigation_url(url)
    if current == target:
        if page_has_captcha(page):
            raise RuntimeError("CAPTCHA_REQUIRED: existing tab was left open")
        page.reload(wait_until=wait_until, timeout=timeout)
    else:
        page.goto(url, wait_until=wait_until, timeout=timeout)


def _find_chrome_executable() -> Optional[Path]:
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    )
    return next((path for path in candidates if path.is_file()), None)


def open_chrome_session(
    playwright: Playwright,
    *,
    cdp_url: str = "http://localhost:9222",
    profile_name: str = "ats-cdp-profile",
    target_url: str = "",
) -> BrowserSession:
    force_fresh = os.environ.get("JOB_APP_FRESH_BROWSER") == "1"
    if not force_fresh:
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            page = _reusable_page(browser, target_url) if target_url else None
            return BrowserSession(browser, page or _new_page(browser), False)
        except Exception:
            pass

    if force_fresh:
        browser = playwright.chromium.launch(headless=False)
        return BrowserSession(browser, _new_page(browser), True)

    chrome = _find_chrome_executable()
    if chrome:
        profile = Path(os.environ.get("TEMP", str(Path.cwd()))) / profile_name
        subprocess.Popen(
            [
                str(chrome),
                f"--remote-debugging-port={urlparse(cdp_url).port or 9222}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        for _ in range(15):
            time.sleep(0.4)
            try:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
                page = _reusable_page(browser, target_url) if target_url else None
                return BrowserSession(browser, page or _new_page(browser), False)
            except Exception:
                continue

    browser = playwright.chromium.launch(headless=False)
    page = _reusable_page(browser, target_url) if target_url else None
    return BrowserSession(browser, page or _new_page(browser), True)


def build_engine_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--url", required=True)
    parser.add_argument("--resume", required=True)
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--essay", default="")
    parser.add_argument("--email", default="")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--live-submit", action="store_true")
    parser.add_argument("--fill-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headed", action="store_true")
    return parser


def require_orchestrated_invocation(url: str) -> None:
    """Prevent job URLs from bypassing the repository orchestrator."""
    if os.environ.get(ORCHESTRATOR_INVOCATION_ENV) == "1":
        return
    raise RuntimeError(
        "Job application URLs must be run through job_application_orchestrator.py. "
        f'Use: python src/job_application_orchestrator.py --url "{url}"'
    )


def requested_live_mode(args: argparse.Namespace) -> bool:
    return bool(args.live_submit and not (args.fill_only or args.dry_run))


def engine_result(
    status: str,
    *,
    ats: str,
    is_live: bool,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    confirmed = status == "SUBMITTED & CONFIRMED"
    payload: dict[str, Any] = {
        "success": status in SUCCESSFUL_STATUSES,
        "status": status,
        "ats": ats,
        "submitted": confirmed,
        "confirmed": confirmed,
        "test_mode": not is_live,
    }
    if extra:
        payload.update(extra)
    return payload


def emit_engine_result(payload: Mapping[str, Any]) -> None:
    print(f"{RESULT_PREFIX}{json.dumps(dict(payload), sort_keys=True)}", flush=True)


def _host_matches(host: str, marker: str) -> bool:
    return host == marker or host.endswith(f".{marker}")


def _emit_result(*, ats: str, success: bool, status: str, error: str = "") -> None:
    payload: dict[str, Any] = {
        "success": success,
        "status": status,
        "ats": ats,
        "submitted": False,
        "confirmed": False,
        "test_mode": True,
    }
    if error:
        payload["error"] = error
    print(f"{RESULT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _build_parser(ats: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Validate the {ats.title()} engine invocation without opening a browser."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--resume", required=True)
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--email", default="")
    parser.add_argument("--config")
    parser.add_argument("--live-submit", action="store_true")
    parser.add_argument("--fill-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headed", action="store_true")
    return parser


def _parse_and_validate_host(url: str, ats: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host:
        raise ValueError("job URL must be an absolute HTTPS URL")

    markers = ATS_HOST_MARKERS[ats]
    if not any(_host_matches(host, marker) for marker in markers):
        raise ValueError(f"URL host {host!r} is not recognized as {ats.title()}")
    return host


def _validate_nonempty_file(raw_path: str, label: str) -> None:
    path = Path(raw_path).expanduser()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"{label} does not exist or is empty: {path}")


def _validate(args: argparse.Namespace, ats: str) -> str:
    host = _parse_and_validate_host(args.url, ats)
    _validate_nonempty_file(args.resume, "resume")

    if args.config:
        config = Path(args.config).expanduser()
        if not config.is_file():
            raise ValueError(f"config file does not exist: {config}")
    return host


def _requested_mode(args: argparse.Namespace) -> str:
    if args.live_submit:
        return "live-submit"
    if args.fill_only:
        return "fill-only"
    return "dry-run"


def main_for_ats(ats: str, argv: Optional[Sequence[str]] = None) -> int:
    """Validate an ATS engine invocation and emit the standard result record."""
    if ats not in ATS_HOST_MARKERS:
        raise ValueError(f"unsupported ATS: {ats}")

    args = _build_parser(ats).parse_args(argv)
    try:
        require_orchestrated_invocation(args.url)
        host = _validate(args, ats)
    except (OSError, ValueError) as exc:
        _emit_result(
            ats=ats,
            success=False,
            status="SHELL_TEST_VALIDATION_FAILED",
            error=str(exc),
        )
        return 2

    print(
        f"{ats.title()} shell test validated {host}; "
        f"requested mode={_requested_mode(args)}. No browser was opened.",
        flush=True,
    )
    _emit_result(ats=ats, success=True, status="SHELL_TEST_VALIDATED")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the generic shell validator by detecting the ATS from --url."""
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--url", required=True)
    known, _ = probe.parse_known_args(argv)
    ats = next(
        (name for name in ATS_HOST_MARKERS if validate_ats_url(known.url, name)),
        None,
    )
    if ats is None:
        raise SystemExit("Could not detect a supported ATS from --url")
    return main_for_ats(ats, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
