"""Shared runtime, policy, CLI, and shell-test utilities for ATS engines.

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. Autonomous Vision-Language Model (VLM) Grounding Agent (Gemini 1.5 Flash / Claude 3.5 Sonnet Vision):
   - Replace brittle DOM selector lookup heuristics (`label_for`, regex label matching, `fill_first`)
     with a zero-shot multimodal vision model that inspects full-page browser screenshots,
     identifies input bounding boxes, and executes visual clicks & typing via pixel coordinates.
   - Benefit: Immunizes application filling against DOM obfuscation, dynamic dynamic CSS class names,
     shadow DOM trees, and non-standard custom web components.

2. DOM Abstract Syntax Tree (AST) Vector Embedding Matcher (Sentence-Transformers):
   - Parse form DOM trees into hierarchical AST nodes, vectorize label text, placeholders, and aria-attributes,
     and calculate cosine similarity against candidate profile schema fields.
   - Benefit: Handles multilingual job postings (French, German, Spanish, Japanese) seamlessly
     without relying on hardcoded English regex patterns.

3. Advanced Stealth Chrome DevTools Protocol (CDP) Wrapper with TCP/IP & TLS Fingerprint Spoofing:
   - Enhance Playwright CDP sessions with lower-level browser fingerprint injection (`playwright-stealth`,
     JA3/TLS client hello randomization, WebGL renderer spoofing, and Canvas noise generator).
   - Benefit: Bypasses sophisticated anti-bot protections (Cloudflare Turnstile, DataDome, PerimeterX)
     frequently embedded on enterprise ATS platforms.
==============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil as _shutil
import subprocess
import sys
import tempfile as _tempfile
import time as _time
import urllib as _urllib
import uuid as _uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlparse

from playwright.sync_api import Browser, Locator, Page, Playwright
from pypdf import PdfReader

from .ats_urls import (
    ATS_HOST_MARKERS,
    detect_ats_job_url,
    validate_ats_job_url,
    validate_ats_url as validate_ats_url,
)
from .contracts import ENGINE_RESULT_PREFIX, EngineResult
from .identity import normalize_email
from .paths import DATA_DIR
from .profile import AutomationProfile
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from ..engines import browser_controls as _browser_controls
from ..engines import browser_runtime as _browser_runtime

# Preserve historic module-level patch targets. These aliases retain the same
# process-global module objects used by ``browser_runtime``.
shutil = _shutil
tempfile = _tempfile
time = _time
urllib = _urllib
uuid = _uuid

logger = logging.getLogger("ATSEngineCommon")

# Retain the public constant while sourcing its wire value from the typed
# contract shared by engines and the orchestrator.
RESULT_PREFIX = ENGINE_RESULT_PREFIX
ORCHESTRATOR_INVOCATION_ENV = "JOB_APP_ORCHESTRATOR_INVOCATION"
ORCHESTRATOR_CONFIG_ENV = "JOB_APP_ORCHESTRATOR_CONFIG"
ORCHESTRATOR_CURRENT_TITLE_ENV = "JOB_APP_RESUME_CURRENT_TITLE"
SUCCESSFUL_STATUSES = frozenset({"PREFILLED_ONLY", "SUBMITTED & CONFIRMED"})
DEFAULT_CONFIRMATION_PHRASES = _browser_runtime.DEFAULT_CONFIRMATION_PHRASES
DEFAULT_FAILURE_PHRASES = _browser_runtime.DEFAULT_FAILURE_PHRASES
# Matches EEO/demographic field labels so consent auto-fill logic can leave them
# untouched instead of guessing an answer to a legally sensitive question.
SENSITIVE_FIELD_PATTERN = _browser_controls.SENSITIVE_FIELD_PATTERN


BrowserSession = _browser_runtime.PlaywrightBrowserSession


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_profile_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the grouped policy schema through the engine's runtime keys."""
    if config.get("schema_version") != 2:
        raise ValueError("config schema_version must be 2")
    normalized = dict(config)
    candidate = config.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ValueError("config must contain a candidate object")
    normalized_candidate = dict(candidate)
    for group_name in (
        "identity",
        "contact",
        "address",
        "employment",
        "availability",
        "demographics",
    ):
        group = candidate.get(group_name)
        if isinstance(group, Mapping):
            normalized_candidate.update(group)
    education = candidate.get("education")
    if education is not None:
        if not isinstance(education, Sequence) or isinstance(education, (str, bytes)):
            raise ValueError("config candidate.education must be an array")
        if education and not isinstance(education[0], Mapping):
            raise ValueError("config candidate.education entries must be objects")
        normalized_candidate["education_history"] = dict(education[0]) if education else {}
    normalized["candidate"] = normalized_candidate
    resume_title = os.environ.get(ORCHESTRATOR_CURRENT_TITLE_ENV, "").strip()
    if resume_title:
        normalized_candidate["current_job_title"] = resume_title
    if not normalized_candidate.get("available_start_date"):
        try:
            offset_days = int(
                normalized_candidate.get(
                    "start_date_offset_days",
                    RUNTIME_CONFIG.application.default_start_date_offset_days,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("candidate.start_date_offset_days must be an integer") from exc
        if offset_days < 0:
            raise ValueError("candidate.start_date_offset_days cannot be negative")
        normalized_candidate["available_start_date"] = (
            date.today() + timedelta(days=offset_days)
        ).isoformat()
    policies = config.get("policies")
    if not isinstance(policies, Mapping):
        return normalized
    sections = {
        "rules": "answers",
        "eeo_defaults": "eeo",
        "field_matchers": "matchers",
        "answer_variants": "option_variants",
    }
    for runtime_key, policy_key in sections.items():
        section = policies.get(policy_key)
        if not isinstance(section, Mapping):
            raise ValueError(f"config policies.{policy_key} must be an object")
        normalized[runtime_key] = dict(section)
    explicit_answers = policies.get("explicit_answers")
    if explicit_answers is not None:
        if not isinstance(explicit_answers, Mapping):
            raise ValueError("config policies.explicit_answers must be an object")
        normalized_candidate["screening_answers"] = dict(explicit_answers)
    return normalized


def load_json_config(
    path: Path,
    *,
    defaults: Mapping[str, Any] | None = None,
    required_candidate_fields: Sequence[str] = ("first_name", "last_name"),
) -> dict[str, Any]:
    config_path = path.expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict):
        raise ValueError("config root must be a JSON object")
    config = normalize_profile_config(deep_merge(defaults or {}, document))
    candidate = config.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("config must contain a candidate object")
    missing = [field for field in required_candidate_fields if not candidate.get(field)]
    if missing:
        raise ValueError(f"config candidate is missing: {', '.join(missing)}")
    return AutomationProfile.from_runtime_mapping(config).to_runtime_mapping()


def orchestrated_config_path() -> Path:
    """Return the profile path supplied by the orchestrator process."""
    raw_path = os.environ.get(ORCHESTRATOR_CONFIG_ENV, "").strip()
    if not raw_path:
        raise RuntimeError("Application engines must receive config from the application workflow.")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    return path


def email_from_resume(resume_path: Path, fallback_email: str) -> str:
    """Extract the first valid email address from the rendered resume."""
    text: str
    try:
        reader = PdfReader(str(resume_path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        logger.warning("Could not extract email from resume %s: %s", resume_path, exc)
        text = ""
    matches: list[str] = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
    for address in matches:
        if valid_email(address):
            return address
    if valid_email(fallback_email):
        return fallback_email
    raise ValueError("Resume has no valid email and candidate.fallback_email is invalid.")


def current_title_from_resume(resume_path: Path) -> str:
    """Extract the first title listed beneath the resume's experience heading."""
    try:
        reader = PdfReader(str(resume_path))
        lines = [
            line.strip()
            for page in reader.pages
            for line in (page.extract_text() or "").splitlines()
        ]
    except Exception as exc:
        raise ValueError(f"Could not read current title from {resume_path}: {exc}") from exc
    for index, line in enumerate(lines):
        if re.search(r"(?:experience|work history)\s*$", line, re.I):
            for title in lines[index + 1 :]:
                if re.search(
                    r"\b(?:product manager|manager|director|engineer|designer|analyst|consultant|lead|head|vp|vice president)\b",
                    title,
                    re.I,
                ) and not re.search(r"\b\d{4}\b.*(?:present|\d{4})", title, re.I):
                    return title.replace("�", "-").strip()
            break
    raise ValueError("No current title was found under an experience heading.")


def resolve_candidate_email(profile: Mapping[str, Any], override: str = "") -> str:
    """Resolve the orchestrator email, with fallback only for extraction failure."""
    candidates = (
        override,
        profile.get("email_override", ""),
        profile.get("fallback_email", ""),
        profile.get("email_fallback", ""),
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if valid_email(value):
            return value
    raise ValueError("A valid candidate email is required.")


def load_candidate_evidence(config: Mapping[str, Any]) -> str:
    """Read configured resume evidence for shared essay generation."""
    candidate = config.get("candidate")
    if isinstance(candidate, Mapping):
        inline_values: list[str] = []
        summary = str(candidate.get("summary", "") or "").strip()
        if summary:
            inline_values.append(summary)
        evidence = candidate.get("evidence")
        if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
            inline_values.extend(str(value).strip() for value in evidence if str(value).strip())
        elif evidence:
            inline_values.append(str(evidence).strip())
        if inline_values:
            return "\n".join(inline_values)

    configured_value = config.get("candidate_evidence_file")
    if configured_value:
        configured = Path(str(configured_value)).expanduser()
        path = configured if configured.is_absolute() else DATA_DIR / configured
    else:
        path = resolve_runtime_path(RUNTIME_CONFIG.application.resume_source_file)
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError as exc:
        logger.warning("Could not read candidate evidence %s: %s", path, exc)
        return ""


def load_personalized_resume_evidence(
    resume: Path,
    config: Mapping[str, Any],
) -> str:
    """Extract evidence from the exact resume attached to an application."""
    try:
        import pymupdf

        open_pdf: Callable[[Path], Any] = pymupdf.open
        with open_pdf(resume) as document:
            evidence = "\n".join(str(page.get_text("text")) for page in document).strip()
        if evidence:
            return evidence
        logger.warning("Personalized resume contained no extractable text: %s", resume)
    except Exception as exc:
        logger.warning("Could not extract personalized resume evidence from %s: %s", resume, exc)
    return load_candidate_evidence(config)


def valid_email(value: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        normalize_email(value)
        return True
    except ValueError:
        return False


def validate_nonempty_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ValueError(f"{label} does not exist or is empty: {resolved}")
    return resolved


def safe_filename(value: str, fallback: str = "ats") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip()).strip("_")
    return cleaned or fallback


def mask_email(email: str) -> str:
    """Mask the local part of an email address for safe logging."""
    local, separator, domain = str(email).partition("@")
    if not separator:
        return "<invalid>"
    visible = local[:2]
    return f"{visible}{'*' * max(1, len(local) - len(visible))}@{domain}"


def first_visible(locator: Locator) -> Locator | None:
    """Compatibility facade for the provider-neutral control primitive."""
    return _browser_controls.first_visible(locator)


def fill_first(page: Page, selectors: Sequence[str] | str, value: str) -> bool:
    """Compatibility facade preserving the patchable ``first_visible`` seam."""
    return _browser_controls.fill_first(
        page,
        selectors,
        value,
        visible_resolver=first_visible,
    )


def label_for(page: Page, control: Locator) -> str:
    """Compatibility facade for accessible-label resolution."""
    return _browser_controls.label_for(page, control)


def fill_labeled(page: Page, label_pattern: str, value: str) -> bool:
    """Compatibility facade preserving the patchable visibility resolver."""
    return _browser_controls.fill_labeled(
        page,
        label_pattern,
        value,
        visible_resolver=first_visible,
    )


def answer_variants(
    label: str,
    desired: str,
    configured_variants: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    variants = [desired]
    normalized = desired.strip().lower()
    del label  # semantic option aliases are configuration-driven.
    if normalized in {"yes", "no"}:
        variants.append(normalized.title())
    duration = re.fullmatch(r"(\d+)\s*(day|week|month)s?", normalized)
    if duration:
        quantity = int(duration.group(1))
        unit = duration.group(2)
        days = quantity * {"day": 1, "week": 7, "month": 30}[unit]
        variants.append(f"{days} days")
        if days in {14, 15}:
            variants.extend(("2 weeks", "15 days"))
        if days in {28, 30}:
            variants.extend(("4 weeks", "1 month", "30 days"))
    if configured_variants:
        for key, values in configured_variants.items():
            if (
                key.lower() == normalized
                and isinstance(values, Sequence)
                and not isinstance(values, str)
            ):
                variants.extend(str(value) for value in values if value)
    return tuple(dict.fromkeys(variants))


def _matcher_alias_matches(label: str, alias: str) -> bool:
    """Match configured aliases across common punctuation and spelling variants."""
    normalized_label = re.sub(r"[’']", "'", label.lower())
    normalized_alias = re.sub(r"[’']", "'", alias.strip().lower())
    variants = {
        normalized_alias,
        normalized_alias.replace("-", " "),
        normalized_alias.replace(" ", "-"),
        normalized_alias.replace("authorised", "authorized"),
        normalized_alias.replace("authorized", "authorised"),
        normalized_alias.replace("organisation", "organization"),
        normalized_alias.replace("organization", "organisation"),
    }
    for value in tuple(variants):
        if value.endswith("s"):
            variants.add(value[:-1])
        elif value:
            variants.add(f"{value}s")
    return any(value and value in normalized_label for value in variants)


_LOCATION_QUESTION_PATTERNS = (
    # Where the candidate currently is.
    r"\blocation\b",
    r"\bcit(?:y|ies)\b",
    r"where are you (?:located|based)",
    r"where do you (?:currently )?(?:reside|live)",
    r"currently reside",
    # Where the candidate intends to work from.  Ashby and Lever both ask this
    # separately from residence, often qualified by payroll or tax wording.
    r"where (?:do|will) you (?:plan (?:on|to) )?(?:be )?work(?:ing)?",
    r"where would you (?:be )?work(?:ing)?",
    r"work(?:ing)? (?:from|location|out of)",
    r"office location",
)


def is_location_question(question_text: Any) -> bool:
    """Return whether a question asks for a current or intended location.

    Providers distinguish "where are you based" from "where do you plan on
    working from (for payroll tax purposes)" and "where do you currently
    live".  All resolve to the candidate's configured location, so all must be
    recognized here.
    """
    normalized = re.sub(r"\s+", " ", str(question_text)).strip().lower()
    # Binary onsite/hybrid questions can mention an "office location" while
    # asking for a Yes/No commitment rather than a geographic selection.
    if re.search(
        r"(?:^|[.!?:;]\s*)(?:are|would|will|can)\s+you\b",
        normalized,
    ) and re.search(
        r"\brelocat(?:e|ing|ion)\b",
        normalized,
    ):
        return False
    if re.search(
        r"\b(?:are|would)\s+you\s+(?:willing|able|available|comfortable)\b",
        normalized,
    ) and re.search(r"\b(?:office|onsite|on-site|hybrid|commute)\b", normalized):
        return False
    return any(re.search(pattern, normalized) for pattern in _LOCATION_QUESTION_PATTERNS)


def location_answer_candidates(profile: Mapping[str, Any]) -> tuple[str, ...]:
    """Ordered location answers, most to least specific.

    Country-scoped dropdowns (``Where do you currently live?`` listing only
    countries) cannot match a full "City, State, Country" string, so broader
    fallbacks must be tried after the precise one.
    """
    city = str(profile.get("city", "") or "").strip()
    state = str(profile.get("state", "") or "").strip()
    candidates = (
        str(profile.get("location", "") or "").strip(),
        f"{city}, {state}" if city and state else "",
        city,
        state,
        str(profile.get("country", "") or "").strip(),
    )
    seen: list[str] = []
    for candidate in candidates:
        if candidate and candidate.lower() not in {item.lower() for item in seen}:
            seen.append(candidate)
    return tuple(seen)


_COUNTRY_TERM_ALIASES: Mapping[str, tuple[str, ...]] = {
    "australia": ("australia", "australian"),
    "canada": ("canada", "canadian"),
    "france": ("france", "french"),
    "germany": ("germany", "german"),
    "hong kong": ("hong kong",),
    "india": ("india", "indian"),
    "ireland": ("ireland", "irish"),
    "new zealand": ("new zealand",),
    "saudi arabia": ("saudi arabia", "saudi"),
    "singapore": ("singapore", "singaporean"),
    "united arab emirates": ("united arab emirates", "uae", "u a e", "emirati"),
    "united kingdom": (
        "united kingdom",
        "great britain",
        "britain",
        "british",
        "u k",
    ),
    "united states": (
        "united states",
        "united states of america",
        "america",
        "american",
        "usa",
        "u s",
        "u s a",
    ),
}


def _normalized_country(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
    for canonical, aliases in _COUNTRY_TERM_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return canonical
    return normalized


def _label_mentions_country(label: str, country: str) -> bool:
    canonical = _normalized_country(country)
    normalized_label = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
    aliases = _COUNTRY_TERM_ALIASES.get(canonical, (canonical,))
    if any(re.search(rf"\b{re.escape(alias)}\b", normalized_label) for alias in aliases):
        return True
    # Avoid treating the ordinary pronoun "us" as United States while still
    # recognizing the conventional uppercase country abbreviations.
    if canonical == "united states" and re.search(r"\b(?:US|USA)\b", label):
        return True
    if canonical == "united kingdom" and re.search(r"\bUK\b", label):
        return True
    return False


def _mentioned_country(label: str) -> str | None:
    return next(
        (country for country in _COUNTRY_TERM_ALIASES if _label_mentions_country(label, country)),
        None,
    )


def _country_scoped_work_authorization(
    label: str,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """Resolve work rights only for an explicitly configured country.

    The boolean indicates whether country-scoped policy is configured. When it
    is configured but no target country can be identified, callers must fail
    closed instead of falling back to a global authorization answer.
    """
    raw_countries = rules.get("work_authorization_countries")
    if not isinstance(raw_countries, Sequence) or isinstance(raw_countries, (str, bytes)):
        return False, None
    authorized = {
        _normalized_country(country) for country in raw_countries if _normalized_country(country)
    }
    if not authorized:
        return True, None
    configured_target = _normalized_country(rules.get("target_work_country", ""))
    target = _mentioned_country(label) or configured_target or None
    if not target:
        return True, None
    return True, "Yes" if target in authorized else "No"


def _country_scoped_residence(
    label: str,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> tuple[bool, str | None]:
    raw_countries = rules.get("work_authorization_countries")
    if not isinstance(raw_countries, Sequence) or isinstance(raw_countries, (str, bytes)):
        return False, None
    residence = _normalized_country(profile.get("country", ""))
    configured_target = _normalized_country(rules.get("target_work_country", ""))
    target = _mentioned_country(label) or configured_target or None
    if not target or not residence:
        return True, None
    return True, "Yes" if target == residence else "No"


def configured_answer(
    label: str,
    profile: Mapping[str, Any],
    rules: Mapping[str, Any],
    eeo: Mapping[str, Any],
    field_matchers: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    text = label.lower()
    explicit_answers = profile.get("screening_answers", {})
    if isinstance(explicit_answers, Mapping):
        for question_alias, answer in explicit_answers.items():
            if answer not in (None, "") and _matcher_alias_matches(text, str(question_alias)):
                return str(answer)
    country_policy, country_authorization = _country_scoped_work_authorization(
        label,
        profile,
        rules,
    )
    work_authorization_question = re.search(
        r"\bvalid\s+work\s+permit\b|"
        r"\bright\s+to\s+(?:live\s+and\s+)?work\b|"
        r"\bwork(?:ing)?\s+rights?\b|"
        r"\bwork\s+authori[sz]ation\b|"
        r"\b(?:eligible|authori[sz]ed)\b.*\bwork\b",
        text,
    )
    if work_authorization_question and country_policy:
        return country_authorization
    if re.search(
        r"\b(?:eligible|authori[sz]ed|right)\b.*\bwork\b.*\bwithout\b.*\bsponsor",
        text,
    ):
        answer = rules.get("work_authorization") or rules.get("target_country_work_authorization")
        if answer not in (None, ""):
            return str(answer)
    if re.search(
        r"\bvalid\s+work\s+permit\b|\bright\s+to\s+(?:live\s+and\s+)?work\b|"
        r"\bwork(?:ing)?\s+rights?\b|\bwork\s+authori[sz]ation\b",
        text,
    ):
        answer = rules.get("target_country_work_authorization") or rules.get("work_authorization")
        if answer not in (None, ""):
            return str(answer)
    if re.search(
        r"\b(?:are|do)\s+you\b.*\b(?:currently\s+)?(?:based|located|resid(?:e|ing))\b.*\bin\b",
        text,
    ):
        residence_policy, residence_answer = _country_scoped_residence(label, profile, rules)
        if residence_policy:
            return residence_answer
    if re.search(
        r"\b(?:additional|outside|secondary|other)\s+(?:employment|job|work)\b",
        text,
    ):
        answer = rules.get("employment_restrictions")
        if answer not in (None, ""):
            return str(answer)
    if re.search(
        r"\byears?\s+of\s+.*experience\b|"
        r"\b\d+\+?\s*years?\b.*\bexperience\b",
        text,
    ):
        return "Yes"
    semantic_values: Mapping[str, Any] = {
        "preferred_name": profile.get("preferred_name"),
        "phonetic_name": profile.get("phonetic_name"),
        "middle_name": profile.get("middle_name"),
        "last_name": profile.get("last_name"),
        "legal_name": " ".join(
            part for part in (profile.get("first_name"), profile.get("last_name")) if part
        ),
        "age_over_18": "Yes",
        "background_check_consent": rules.get("background_check_consent"),
        "sms_consent": rules.get("sms_consent"),
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
        "zip_code": profile.get("zip_code"),
        "country": profile.get("country"),
        "country_of_birth": profile.get("country_of_birth"),
        "citizenship": profile.get("citizenship") or profile.get("nationality"),
        "available_start_date": profile.get("available_start_date"),
        "degree": profile.get("highest_degree"),
        "bachelors_degree": rules.get("bachelors_degree"),
        "salary_expectation": rules.get("salary_expectation"),
        "previous_application": rules.get("previous_application"),
        "employee_relationship": rules.get("employee_relationship"),
        "right_to_work_status": rules.get("right_to_work_status"),
        "requires_accommodation": rules.get("requires_accommodation"),
        "can_perform_essential_functions": rules.get("can_perform_essential_functions"),
        "current_salary": rules.get("current_salary"),
        "notice_period": rules.get("notice_period"),
        "hyderabad_flexibility": rules.get("hyderabad_flexibility"),
        "flexible": rules.get("flexible"),
        "language_fluency": rules.get("language_fluency"),
        "current_employee": rules.get("current_employee"),
        "privacy_consent": rules.get("privacy_consent"),
        "experience_requirement": rules.get("experience_requirement"),
        "tool_proficiency": rules.get("tool_proficiency"),
        "language_proficiency": rules.get("language_proficiency"),
        "target_office": rules.get("target_office"),
        "application_certification": rules.get("application_certification"),
        "interview_ai_policy": rules.get("application_certification"),
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
        "relocation": rules.get("relocation"),
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
        # Field labels can match multiple alias sets (e.g. "location" vs
        # "intended_work_location"); this order lets the more specific keys
        # win before the generic ones are even tried.
        priority = (
            "right_to_work_status",
            "background_check_consent",
            "sms_consent",
            "requires_accommodation",
            "can_perform_essential_functions",
            "sponsorship",
            "language_proficiency",
            "tool_proficiency",
            "experience_requirement",
            "work_auth",
            "bachelors_degree",
            "degree",
            "application_certification",
            "interview_ai_policy",
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
            "previous_application",
            "employee_relationship",
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
            "phonetic_name",
            "middle_name",
            "last_name",
            "country_of_birth",
            "citizenship",
            "available_start_date",
            "country",
            "zip_code",
            "location",
            "source",
        )
        ordered_keys = list(priority) + [key for key in field_matchers if key not in priority]
        for key in ordered_keys:
            aliases = field_matchers.get(key, ())
            answer = semantic_values.get(key)
            if answer not in (None, "") and any(
                _matcher_alias_matches(text, str(alias)) for alias in aliases
            ):
                return str(answer)
    return None


def is_essay_question(label: str) -> bool:
    """Return whether a field is a free-text essay prompt worth generating an answer for."""
    # Demographic/accommodation fields look like questions but must never get an
    # LLM-generated essay answer, even if they also contain essay-like wording.
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
            r"interest|motivat|excit|product|project|challenge|accomplish|"
            r"technical skills?",
            label,
            re.I,
        )
    )


def generate_essay_answer(
    question: str,
    job_text: str,
    company: str,
    role: str,
    candidate_evidence: str,
) -> str:
    """Generate an application-ready essay answer via the LLM, or "" on failure."""
    try:
        from ..resume.ai_client import call_essay_llm, strip_markdown_formatting

        if not candidate_evidence.strip():
            logger.warning("Essay generation skipped because candidate evidence is empty.")
            return ""
        answer = str(
            call_essay_llm(
                question,
                job_text,
                company,
                role,
                candidate_evidence=candidate_evidence,
            )
            or ""
        ).strip()
        if re.search(r"\bMISSING EVIDENCE\b", answer, re.IGNORECASE):
            logger.warning("Rejected essay containing missing-evidence meta text: %r", question)
            return ""
        return strip_markdown_formatting(answer)
    except Exception as exc:
        logger.warning("Essay generation failed for %r: %s", question, exc)
        return ""


def generate_salary_answer(
    question: str,
    job_text: str,
    company: str,
    role: str,
    candidate_evidence: str,
) -> str:
    """Generate a concise salary response via the essay LLM with safe validation."""
    prompt = (
        f"{question}\n\n"
        "Return only one concise desired annual base salary range appropriate for this role and "
        "location, including currency. Do not add explanation, benefits, equity, or unsupported "
        "claims. If the posting lacks enough compensation context, return Negotiable."
    )
    answer = generate_essay_answer(prompt, job_text, company, role, candidate_evidence)
    normalized = " ".join(answer.split()).strip(" -*`")
    if not normalized or len(normalized) > 120 or "\n" in answer.strip():
        return "Negotiable"
    if normalized.casefold() == "negotiable":
        return "Negotiable"
    if not re.search(r"(?:[$€£₹]|\b(?:usd|eur|gbp|inr)\b)", normalized, re.I):
        return "Negotiable"
    if not re.search(r"\d", normalized):
        return "Negotiable"
    return normalized


def _consent_control_is_checked(control: Locator) -> bool:
    return _browser_controls._consent_control_is_checked(control)


def _check_consent_control(control: Locator) -> None:
    _browser_controls._check_consent_control(control)


def fill_required_consent(page: Page) -> list[str]:
    """Compatibility facade preserving the patchable ``label_for`` seam."""
    return _browser_controls.fill_required_consent(
        page,
        label_resolver=label_for,
        sensitive_field_pattern=SENSITIVE_FIELD_PATTERN,
    )


def confirmation_visible(
    page: Page,
    *,
    success_phrases: Sequence[str] = DEFAULT_CONFIRMATION_PHRASES,
    failure_phrases: Sequence[str] = DEFAULT_FAILURE_PHRASES,
) -> bool:
    """Compatibility facade for browser confirmation inspection."""
    return _browser_runtime.confirmation_visible(
        page,
        success_phrases=success_phrases,
        failure_phrases=failure_phrases,
    )


def text_confirms_submission(
    text: str,
    *,
    success_phrases: Sequence[str] = DEFAULT_CONFIRMATION_PHRASES,
    failure_phrases: Sequence[str] = DEFAULT_FAILURE_PHRASES,
) -> bool:
    """Compatibility facade for text-only confirmation inspection."""
    return _browser_runtime.text_confirms_submission(
        text,
        success_phrases=success_phrases,
        failure_phrases=failure_phrases,
    )


def validate_required_fields(
    page: Page,
    inspector: Callable[[Page], Sequence[str]],
) -> list[str]:
    """Compatibility facade for normalized ATS-specific field inspection."""
    return _browser_controls.validate_required_fields(page, inspector)


def capture_screenshot(page: Page, directory: Path, company: str, tag: str) -> str:
    """Compatibility facade for resilient browser evidence capture."""
    return _browser_runtime.capture_screenshot(
        page,
        directory,
        company,
        tag,
        filename_sanitizer=safe_filename,
    )


def _new_page(browser: Browser) -> Page:
    return _browser_runtime._new_page(browser)


def _raw_browser_cdp_command(
    endpoint: str,
    method: str,
    params: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _browser_runtime._raw_browser_cdp_command(endpoint, method, params)


def _create_background_target(endpoint: str) -> tuple[str, str]:
    return _browser_runtime._create_background_target(
        endpoint,
        raw_command=_raw_browser_cdp_command,
    )


def _close_background_target(endpoint: str, target_id: str) -> None:
    _browser_runtime._close_background_target(
        endpoint,
        target_id,
        raw_command=_raw_browser_cdp_command,
    )


def _resolve_background_page(browser: Browser, marker: str) -> Page:
    return _browser_runtime._resolve_background_page(browser, marker)


def page_has_captcha(page: Page) -> bool:
    """Compatibility facade for fail-closed CAPTCHA inspection."""
    return _browser_runtime.page_has_captcha(page)


def _normalized_navigation_url(value: str) -> str:
    return _browser_runtime._normalized_navigation_url(value)


def _reusable_page(browser: Browser, target_url: str) -> Page | None:
    return _browser_runtime._reusable_page(
        browser,
        target_url,
        captcha_checker=page_has_captcha,
    )


def navigate_reusing_tab(
    page: Page,
    url: str,
    *,
    timeout: int,
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "domcontentloaded",
) -> None:
    """Compatibility facade preserving the patchable CAPTCHA seam."""
    _browser_runtime.navigate_reusing_tab(
        page,
        url,
        timeout=timeout,
        wait_until=wait_until,
        captcha_checker=page_has_captcha,
    )


def _find_chrome_executable() -> Path | None:
    return _browser_runtime._find_chrome_executable()


def _start_hidden_background_chrome(
    endpoint: str,
    profile_name: str,
) -> tuple[subprocess.Popen[Any], Path, str] | None:
    return _browser_runtime._start_hidden_background_chrome(
        endpoint,
        profile_name,
        find_chrome=_find_chrome_executable,
        cleanup_stale_profiles=_cleanup_stale_owned_profiles,
        cleanup_profile=_cleanup_owned_profile,
        read_endpoint=_read_owned_cdp_endpoint,
        stop_process=_stop_owned_chrome_process,
    )


def _read_owned_cdp_endpoint(profile: Path) -> str:
    return _browser_runtime._read_owned_cdp_endpoint(profile)


def _owned_cdp_endpoint_is_live(endpoint: str) -> bool:
    return _browser_runtime._owned_cdp_endpoint_is_live(endpoint)


def _cleanup_stale_owned_profiles(
    temp_root: Path,
    profile_name: str,
    *,
    max_age_seconds: int = 3600,
) -> None:
    _browser_runtime._cleanup_stale_owned_profiles(
        temp_root,
        profile_name,
        max_age_seconds=max_age_seconds,
        read_endpoint=_read_owned_cdp_endpoint,
        endpoint_is_live=_owned_cdp_endpoint_is_live,
        cleanup_profile=_cleanup_owned_profile,
    )


def _cleanup_owned_profile(profile: Path) -> None:
    _browser_runtime._cleanup_owned_profile(profile)


def _stop_owned_chrome_process(process: subprocess.Popen[Any]) -> None:
    _browser_runtime._stop_owned_chrome_process(process)


def close_browser_session(session: BrowserSession) -> None:
    """Compatibility facade preserving owned-resource cleanup patch seams."""
    _browser_runtime.close_browser_session(
        session,
        raw_command=_raw_browser_cdp_command,
        stop_process=_stop_owned_chrome_process,
        cleanup_profile=_cleanup_owned_profile,
    )


def open_chrome_session(
    playwright: Playwright,
    *,
    cdp_url: str | None = None,
    profile_name: str = "ats-cdp-profile",
    target_url: str = "",
    headless: bool = False,
    background: bool = False,
) -> BrowserSession:
    """Compatibility facade preserving historic CDP lifecycle patch seams."""
    return _browser_runtime.open_chrome_session(
        playwright,
        cdp_url=cdp_url,
        profile_name=profile_name,
        target_url=target_url,
        headless=headless,
        background=background,
        create_background_target=_create_background_target,
        close_background_target=_close_background_target,
        resolve_background_page=_resolve_background_page,
        start_hidden_chrome=_start_hidden_background_chrome,
        raw_command=_raw_browser_cdp_command,
        stop_process=_stop_owned_chrome_process,
        cleanup_profile=_cleanup_owned_profile,
        find_chrome=_find_chrome_executable,
        reusable_page=_reusable_page,
        new_page=_new_page,
    )


def build_engine_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--url", required=True)
    parser.add_argument("--resume", required=True)
    parser.add_argument("--cover-letter", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--essay", default="")
    parser.add_argument("--email", default="")
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
        "Job application URLs must be run through the application workflow. "
        f'Use: python src/job_automation.py apply --url "{url}"'
    )


def requested_live_mode(args: argparse.Namespace) -> bool:
    return bool(args.live_submit and not (args.fill_only or args.dry_run))


def engine_result(
    status: str,
    *,
    ats: str,
    is_live: bool,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a contract-validated engine result without changing its wire shape."""
    confirmed = status == "SUBMITTED & CONFIRMED"
    return dict(
        EngineResult(
            success=status in SUCCESSFUL_STATUSES,
            status=status,
            ats=ats,
            submitted=confirmed,
            confirmed=confirmed,
            test_mode=not is_live,
            extra=dict(extra or {}),
        ).to_payload()
    )


def emit_engine_result(payload: Mapping[str, Any]) -> None:
    """Emit the established result line after validating the shared fields."""
    try:
        wire_line = EngineResult.from_payload(payload).to_wire_line()
    except ValueError:
        # Existing provider engines can attach arbitrary diagnostics. Preserve
        # their historic output rather than obscuring a failure report during
        # the incremental migration, while the orchestrator still validates it.
        wire_line = f"{RESULT_PREFIX}{json.dumps(dict(payload), sort_keys=True)}"
    print(wire_line, flush=True)


def _emit_result(*, ats: str, success: bool, status: str, error: str = "") -> None:
    payload = EngineResult(
        success=success,
        status=status,
        ats=ats,
        test_mode=True,
        error=error,
    ).to_payload()
    emit_engine_result(payload)


def _build_parser(ats: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Validate the {ats.title()} engine invocation without opening a browser."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--resume", required=True)
    parser.add_argument("--cover-letter", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--email", default="")
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

    # Delegate to the same job-specific URL rule used by the orchestrator. This
    # also prevents company board roots from reaching a submission engine.
    if not validate_ats_job_url(url, ats):
        raise ValueError(f"URL {url!r} is not recognized as {ats.title()} job-specific URL")
    return host


def _validate_nonempty_file(raw_path: str, label: str) -> None:
    path = Path(raw_path).expanduser()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"{label} does not exist or is empty: {path}")


def _validate(args: argparse.Namespace, ats: str) -> str:
    host = _parse_and_validate_host(args.url, ats)
    _validate_nonempty_file(args.resume, "resume")
    if args.cover_letter:
        _validate_nonempty_file(args.cover_letter, "cover letter")

    return host


def _requested_mode(args: argparse.Namespace) -> str:
    if args.live_submit:
        return "live-submit"
    if args.fill_only:
        return "fill-only"
    return "dry-run"


def main_for_ats(ats: str, argv: Sequence[str] | None = None) -> int:
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the generic shell validator by detecting the ATS from --url."""
    probe = argparse.ArgumentParser(
        description="Validate an ATS engine invocation without opening a browser."
    )
    probe.add_argument("--url", help="Supported Greenhouse, Lever, or Ashby application URL.")
    known, _ = probe.parse_known_args(argv)
    if not known.url:
        probe.error("the following arguments are required: --url")
    ats = detect_ats_job_url(known.url)
    if ats is None:
        raise SystemExit("Could not detect a supported ATS from --url")
    return main_for_ats(ats, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
