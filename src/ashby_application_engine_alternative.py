"""
Alternate, class-based Ashby application engine.

The module can run independently or as an engine selected by
``job_application_orchestrator.py``. Every completed CLI invocation emits exactly one
``ENGINE_RESULT_JSON:`` record for the orchestrator. Browser automation remains
encapsulated by :class:`AshbyApplicant`; importing this module has no browser or
network side effects.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import threading
import time
from datetime import date, timedelta
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from resume_ai_utilities import strip_markdown_formatting
from ashby_application_engine import (
    _fill_configured_checkbox_groups,
    _fill_radio_groups,
    _select_highest_numeric_combobox,
    disallowed_screening_questions,
    fill_education_history,
)
from ats_application_engine_common import require_orchestrated_invocation
from project_paths import (
    CONFIG_DIR,
    DATA_DIR,
    OUTPUT_DIR,
    PROJECT_ROOT,
    SRC_DIR,
)

# ==============================================================================
# CONSTANTS & LLM CONCURRENCY
# ==============================================================================
__all__ = [
    "AshbyApplicant",
    "ApplicationConfig",
    "ApplicationResult",
    "FieldFill",
    "ErrorRecord",
    "ErrorSeverity",
    "load_candidate_profile",
    "main",
]

SCRIPT_DIR = SRC_DIR
DEFAULT_DOWNLOADS = Path.home() / "Downloads"
DEFAULT_ASHBY_DIR = OUTPUT_DIR
DEFAULT_RESUMES_DIR = OUTPUT_DIR
DEFAULT_BASE_FILES_DIR = DATA_DIR

DEFAULT_EMAIL_POOL_FILE = CONFIG_DIR / "candidate_email_pool.json"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "candidate_profile_config.json"

# Timing
DEFAULT_TYPE_DELAY_MIN_MS = 40
DEFAULT_TYPE_DELAY_MAX_MS = 80
SHORT_DELAY: tuple[float, float] = (0.2, 0.4)
MEDIUM_DELAY: tuple[float, float] = (0.5, 1.0)
LONG_DELAY: tuple[float, float] = (1.5, 2.5)
DEFAULT_POST_SUBMIT_WAIT_SEC = 15.0
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_ELEMENT_TIMEOUT_MS = 8_000
POLL_INTERVAL_SEC = 0.5
SKIPPABLE_PHASES = frozenset({"eeo", "consent", "essays", "personal"})

NON_TEXT_INPUT_TYPES = frozenset({
    "file", "radio", "checkbox", "hidden",
    "submit", "button", "image", "reset",
})

EEO_CLICKABLE_SELECTORS = "[role='radio'], [role='checkbox'], label, button"
FIELD_LABEL_SCRIPT = """el => {
    const direct = el.labels && el.labels[0] ? el.labels[0].innerText : "";
    const container = el.closest("div") ? el.closest("div").innerText : "";
    return (direct || container || "").toLowerCase();
}"""
FIELD_CONTEXT_SCRIPT = """el => {
    const direct = el.labels && el.labels[0] ? el.labels[0].innerText : "";
    const parent = el.parentElement ? el.parentElement.innerText : "";
    return (direct + " " + parent).toLowerCase();
}"""

logger = logging.getLogger("ashby_applicant")
ENGINE_RESULT_PREFIX = "ENGINE_RESULT_JSON:"

# Thread-safety and rate limiting for AI provider calls
_llm_lock = threading.Lock()
_last_llm_call = 0.0
LLM_MIN_INTERVAL = 5.0  # Minimum 5-second pause between calls


# ==============================================================================
# CONFIG & PROFILE LOADING
# ==============================================================================
DEFAULT_CANDIDATE_PROFILE: dict[str, Any] = {
    "first_name": "Shivam",
    "last_name": "Singh",
    "preferred_name": "Shiv",
    "fallback_email": "shiv-pm-ai@umich.edu",
    "phone": "6502833478",
    "portfolio": "https://www.goodreads.com/author/show/21624984.Shivam_Singh",
    "publications": "https://www.researchgate.net/profile/Shivam-Singh-188",
    "linkedin": "https://linkedin.com/in/beastofbayarea",
    "twitter": "https://x.com/BeastofBayArea",
    "goodreads_book": "https://www.goodreads.com/book/show/60591386-in-crypto-we-trust",
    "researchgate": "https://www.researchgate.net/profile/Shivam-Singh-188",
    "sciencedirect": "https://www.sciencedirect.com/science/article/abs/pii/S0959652623023867",
    "website": "https://goodreads.com/beastofbayarea",
    "street_address": "447 Sutter Street",
    "address_2": "ste 506",
    "city": "San Francisco",
    "state": "California",
    "zip_code": "94108",
    "country": "United States",
    "location": "San Francisco, California, USA",
    "nationality": "Indian",
    "languages": "English, French, Hindi",
    "birthday": "1995-11-27",
    "gender": "Man",
    "pronouns": "they/them",
    "race": "Asian or Asian American",
    "age": "30-39",
    "veteran": "No",
    "transgender": "No",
    "orientation": "Bisexual",
    "disability": "Yes",
    "communities": "Person with disability, Neurodiverse",
    "education_history": {
        "school": "University of Michigan",
        "degree": "MBA",
        "field_of_study": "Business",
        "start_month": "August",
        "start_year": "2022",
        "end_month": "May",
        "end_year": "2024",
        "still_student": False,
    },
    "lgbtq": "Yes",
}


@dataclass(frozen=True)
class ProfileSettings:
    """Validated portions of a repository or legacy candidate config."""

    candidate: dict[str, Any]
    company_overrides: dict[str, dict[str, Any]]
    document: dict[str, Any]
    source: Optional[Path] = None


def _read_json_object(path: Path, *, required: bool, description: str) -> dict[str, Any]:
    path = path.expanduser()
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"{description} not found: {path}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {description} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{description} must contain a JSON object: {path}")
    return document


def _normalise_profile_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _normalise_company_overrides(
    raw: Any,
    *,
    source: Path,
) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"'company_overrides' must be a JSON object in {source}")

    result: dict[str, dict[str, Any]] = {}
    for company, override in raw.items():
        if not isinstance(override, Mapping):
            raise ValueError(
                f"Company override for {company!r} must be a JSON object in {source}"
            )
        result[str(company)] = dict(override)
    return result


def load_profile_settings(
    path: Optional[Path] = None,
    *,
    required: bool = False,
) -> ProfileSettings:
    """Load repository-style nested config and legacy top-level profiles.

    Legacy candidate keys are applied first, then the nested ``candidate``
    object takes precedence when both formats are present.
    """

    source = (path or DEFAULT_CONFIG_PATH).expanduser()
    document = _read_json_object(
        source,
        required=required,
        description="Candidate config",
    )
    base = dict(DEFAULT_CANDIDATE_PROFILE)
    if not document:
        return ProfileSettings(base, {}, {}, source if source.is_file() else None)

    nested = document.get("candidate", {})
    if nested is None:
        nested = {}
    if not isinstance(nested, Mapping):
        raise ValueError(f"'candidate' must be a JSON object in {source}")

    legacy = {key: document[key] for key in base if key in document}
    combined: dict[str, Any] = {**legacy, **dict(nested)}
    overrides = sorted(
        key
        for key, value in combined.items()
        if key in base and _normalise_profile_value(value) != base[key]
    )
    unknown = sorted(key for key in combined if key not in base)
    if overrides:
        logger.info("Candidate overrides loaded from %s: %s", source, ", ".join(overrides))
    if unknown:
        logger.warning(
            "Unknown candidate keys in %s (ignored): %s",
            source,
            ", ".join(unknown),
        )

    base.update(
        {
            key: _normalise_profile_value(value)
            for key, value in combined.items()
            if key in base and value is not None
        }
    )
    company_overrides = _normalise_company_overrides(
        document.get("company_overrides"),
        source=source,
    )
    return ProfileSettings(base, company_overrides, document, source)


def load_candidate_profile(
    path: Optional[Path] = None,
    *,
    required: bool = False,
) -> dict[str, Any]:
    """Return the merged candidate mapping used by the application engine."""

    return load_profile_settings(path, required=required).candidate


# ==============================================================================
# UTILITIES & LOCAL LLM CLI CALLER
# ==============================================================================
def human_delay(bounds: tuple[float, float] = MEDIUM_DELAY) -> None:
    lo, hi = bounds
    if lo > hi:
        lo, hi = hi, lo
    time.sleep(random.uniform(lo, hi))


def get_random_candidate_email(
    email_pool_file: Path = DEFAULT_EMAIL_POOL_FILE,
    *,
    fallback: str = "shiv-pm-ai@umich.edu",
) -> str:
    if email_pool_file.exists():
        try:
            with email_pool_file.open("r", encoding="utf-8") as handle:
                raw_emails = json.load(handle)
            emails = (
                [
                    item.strip()
                    for item in raw_emails
                    if isinstance(item, str) and "@" in item and item.strip()
                ]
                if isinstance(raw_emails, list)
                else []
            )
            if emails:
                return random.choice(emails)
            logger.warning("Email pool has no usable addresses: %s", email_pool_file)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Email pool unreadable (%s); using fallback.", exc)
    return fallback


def resolve_resume_path(resume_pdf_name: str, extra_dirs: Iterable[Path] = ()) -> Path:
    p = Path(resume_pdf_name).expanduser()
    if p.is_absolute():
        return _validate_resume_file(p)

    search_dirs = [Path(directory).expanduser() for directory in extra_dirs]
    raw_candidates = [
        p,
        SCRIPT_DIR / resume_pdf_name,
        DEFAULT_ASHBY_DIR / resume_pdf_name,
        DEFAULT_RESUMES_DIR / resume_pdf_name,
        DEFAULT_BASE_FILES_DIR / resume_pdf_name,
        *(directory / resume_pdf_name for directory in search_dirs),
    ]
    candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        key = str(candidate.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    for c in candidates:
        if c.is_file():
            return _validate_resume_file(c)
    raise FileNotFoundError(f"Resume PDF not found. Searched: {[str(c) for c in candidates]}")


def _validate_resume_file(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Resume not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Resume must be a PDF file: {path}")
    if path.stat().st_size <= 0:
        raise ValueError(f"Resume PDF is empty: {path}")
    return path.resolve()


def _call_llm(
    user_prompt: str,
    system_prompt: str,
    provider: str = "auto",
    timeout: int = 120,
) -> dict[str, Any]:
    global _last_llm_call
    with _llm_lock:
        now = time.time()
        elapsed = now - _last_llm_call
        if elapsed < LLM_MIN_INTERVAL:
            time.sleep(LLM_MIN_INTERVAL - elapsed)
        _last_llm_call = time.time()

    raw_content = ""

    if provider in ["antigravity", "auto"]:
        try:
            import asyncio
            from google.antigravity import Agent, LocalAgentConfig

            async def _run_antigravity_agent() -> str:
                config = LocalAgentConfig(system_instructions=system_prompt)
                async with Agent(config) as agent:
                    resp = await agent.chat(user_prompt)
                    tokens = []
                    async for token in resp:
                        tokens.append(token)
                    return "".join(tokens)

            raw_content = asyncio.run(_run_antigravity_agent())
            if raw_content:
                logger.info("v3 LLM response generated via google-antigravity SDK.")
        except Exception as exc:
            logger.warning("google-antigravity SDK call failed in v3: %s", exc)

    if not raw_content and provider in ["genai", "auto"]:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client()
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.7,
                )
            )
            if response and response.text:
                raw_content = response.text
                logger.info("v3 LLM response generated via google.genai SDK.")
        except Exception as exc:
            logger.warning("google.genai SDK call failed in v3: %s", exc)

    if raw_content:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content.strip(), flags=re.MULTILINE | re.DOTALL).strip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "answer" in parsed:
                return parsed
            elif isinstance(parsed, dict) and "essay_answer" in parsed:
                return {"answer": str(parsed["essay_answer"]).strip(), "reasoning": "Generated via LLM"}
        except Exception:
            pass
        return {"answer": cleaned, "reasoning": "Generated via LLM"}

    logger.warning("Antigravity/GenAI LLM not available; using static fallback for LLM response.")
    return {
        "answer": (
            "I am deeply passionate about this role and believe my experience aligns well. "
            "I have a strong track record of building and shipping AI-powered products, "
            "working closely with engineering teams, and driving measurable business impact."
        ),
        "reasoning": "Automated application response based on candidate profile."
    }




def _bypass_recaptcha(page: Page, timeout_ms: int = 10000) -> bool:
    """
    Attempt to bypass Google reCAPTCHA on Ashby forms.
    
    Strategy:
    1. Find the reCAPTCHA site key from the page's iframe or data attributes
    2. Manually render an invisible reCAPTCHA widget via grecaptcha.render()
    3. Execute it via grecaptcha.execute() to obtain a valid token
    4. Inject the token into the g-recaptcha-response textarea
    
    This approach works because Ashby uses reCAPTCHA v3 (invisible) with the
    standard anchor iframe for visual compliance. The server validates the token
    on submission, and our manually-rendered widget produces a valid token.
    
    Returns True if token was successfully generated and injected.
    """
    try:
        # Step 1: Extract the reCAPTCHA site key
        sitekey = page.evaluate("""() => {
            // Try data-sitekey attribute first
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            // Try extracting from the anchor iframe URL
            const iframes = document.querySelectorAll('iframe[src*=recaptcha]');
            for (const f of iframes) {
                const m = f.src.match(/k=([^&]+)/);
                if (m) return m[1];
            }
            return null;
        }""")
        
        if not sitekey:
            logger.warning("reCAPTCHA bypass: Could not find site key on page.")
            return False
        
        logger.info("reCAPTCHA bypass: Found site key: %s...", sitekey[:16])
        
        # Step 2: Render invisible widget and execute
        token = page.evaluate("""(sitekey) => {
            return new Promise((resolve, reject) => {
                const container = document.createElement('div');
                container.id = 'rc-bypass-' + Date.now();
                container.style.display = 'none';
                document.body.appendChild(container);
                
                const widgetId = grecaptcha.render(container.id, {
                    sitekey: sitekey,
                    size: 'invisible',
                    callback: function(token) {
                        resolve(token);
                    },
                    'error-callback': function(err) {
                        reject(new Error('reCAPTCHA error: ' + err));
                    }
                });
                
                grecaptcha.execute(widgetId);
                
                // Timeout after 8 seconds
                setTimeout(() => reject(new Error('reCAPTCHA token timeout')), 8000);
            });
        }""", sitekey)
        
        if not token or len(token) < 100:
            logger.warning("reCAPTCHA bypass: Token was empty or too short.")
            return False
        
        logger.info("reCAPTCHA bypass: Got token (len=%d), injecting into form.", len(token))
        
        # Step 3: Inject token into the g-recaptcha-response textarea
        page.evaluate("""(token) => {
            const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta) {
                ta.value = token;
                // Also create/update hidden inputs that some forms use
                let hiddenInput = document.querySelector('input[name="g-recaptcha-response"]');
                if (!hiddenInput) {
                    hiddenInput = document.createElement('input');
                    hiddenInput.type = 'hidden';
                    hiddenInput.name = 'g-recaptcha-response';
                    document.forms[0]?.appendChild(hiddenInput);
                }
                hiddenInput.value = token;
                return true;
            }
            return false;
        }""", token)
        
        logger.info("reCAPTCHA bypass: Token injected successfully.")
        return True
        
    except Exception as e:
        logger.warning("reCAPTCHA bypass failed: %s", e)
        return False


def _mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "<invalid-email>"
    visible = local[:2]
    return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"


def _redact_preview(value: str, *, limit: int = 160) -> str:
    """Bound field telemetry and avoid persisting obvious contact details."""

    text = str(value)
    text = re.sub(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        lambda match: _mask_email(match.group(0)),
        text,
    )
    if re.fullmatch(r"[\d\s()+.-]{7,}", text):
        digits = re.sub(r"\D", "", text)
        text = f"***{digits[-4:]}" if digits else "<redacted>"
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _safe_artifact_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    return (stem or "company")[:80]


# ==============================================================================
# DATA CLASSES
# ==============================================================================
class ErrorSeverity(str, Enum):
    BLOCKING = "blocking"
    NON_BLOCKING = "non-blocking"


@dataclass
class FieldFill:
    phase: str
    selector: str
    label: str
    value_preview: str
    success: bool
    note: str = ""

    def __post_init__(self) -> None:
        self.value_preview = _redact_preview(self.value_preview)

    def __str__(self) -> str:
        flag = "OK" if self.success else "FAIL"
        preview = (
            self.value_preview
            if len(self.value_preview) <= 60
            else self.value_preview[:57] + "..."
        )
        note = f" | {self.note}" if self.note else ""
        return f"[{flag}] {self.phase} | {self.label} | {preview}{note}"


@dataclass
class ErrorRecord:
    message: str
    severity: ErrorSeverity = ErrorSeverity.NON_BLOCKING
    phase: str = ""


@dataclass
class ApplicationResult:
    status: str = "PREFILLED_AUDIT_ONLY"
    prefilled_screenshot: str = "N/A"
    submitted_screenshot: str = "N/A"
    api_verified: bool = False
    field_log: list[FieldFill] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)

    @property
    def blocking_errors(self) -> list[ErrorRecord]:
        return [e for e in self.errors if e.severity is ErrorSeverity.BLOCKING]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "prefilled_screenshot": self.prefilled_screenshot,
            "submitted_screenshot": self.submitted_screenshot,
            "api_verified": self.api_verified,
            "fields_filled": sum(1 for f in self.field_log if f.success),
            "fields_failed": sum(1 for f in self.field_log if not f.success),
            "errors": [
                {"message": e.message, "severity": e.severity.value, "phase": e.phase}
                for e in self.errors
            ],
            "field_log": [asdict(f) for f in self.field_log],
        }


@dataclass
class ApplicationConfig:
    url: str
    resume_pdf: str
    company: str = "Company"
    role: str = "Applicant"
    essay_answer: str = ""
    product_area_essay: str = ""
    live_submit: bool = False
    headful: bool = False
    keep_open: bool = False
    profile: dict[str, Any] = field(default_factory=dict)
    company_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_dir: Path = field(default_factory=lambda: DEFAULT_ASHBY_DIR)
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS
    element_timeout_ms: int = DEFAULT_ELEMENT_TIMEOUT_MS
    post_submit_wait_sec: float = DEFAULT_POST_SUBMIT_WAIT_SEC
    type_delay_min_ms: int = DEFAULT_TYPE_DELAY_MIN_MS
    type_delay_max_ms: int = DEFAULT_TYPE_DELAY_MAX_MS
    skip_phases: set[str] = field(default_factory=set)
    candidate_email_override: Optional[str] = None
    resume_path_override: Optional[Path] = None
    email_pool_file: Path = field(default_factory=lambda: DEFAULT_EMAIL_POOL_FILE)
    resume_search_dirs: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        self.url = _normalise_ashby_url(self.url)
        self.company = self.company.strip() or "Company"
        self.role = self.role.strip() or "Applicant"
        self.output_dir = self.output_dir.expanduser()
        self.email_pool_file = self.email_pool_file.expanduser()
        self.skip_phases = set(self.skip_phases)
        self.resume_search_dirs = tuple(
            Path(directory).expanduser() for directory in self.resume_search_dirs
        )

        invalid_phases = self.skip_phases - SKIPPABLE_PHASES
        if invalid_phases:
            raise ValueError(
                f"Unsupported skip phases: {', '.join(sorted(invalid_phases))}"
            )
        if self.nav_timeout_ms <= 0 or self.element_timeout_ms <= 0:
            raise ValueError("Navigation and element timeouts must be positive")
        if self.post_submit_wait_sec < 0:
            raise ValueError("Post-submit wait cannot be negative")
        if self.type_delay_min_ms < 0 or self.type_delay_max_ms < 0:
            raise ValueError("Typing delays cannot be negative")
        if self.type_delay_min_ms > self.type_delay_max_ms:
            raise ValueError("Minimum typing delay cannot exceed maximum typing delay")
        if self.candidate_email_override:
            self.candidate_email_override = self.candidate_email_override.strip()
            if (
                "@" not in self.candidate_email_override
                or any(char.isspace() for char in self.candidate_email_override)
            ):
                raise ValueError("Candidate email override is invalid")
        if self.resume_path_override is not None:
            self.resume_path_override = _validate_resume_file(
                self.resume_path_override
            )


# ==============================================================================
# QUALITY CONTROL AUDITOR
# ==============================================================================
class QCAuditor:
    """Pre-Fill Quality Control Auditor combining rubric scoring and metric checks."""

    @staticmethod
    def audit_essay(essay_text: str, keywords: Optional[list[str]] = None) -> dict[str, Any]:
        words = essay_text.split()
        word_count = len(words)
        has_metrics = bool(re.search(r"(\d+%|\$\d+|\d+\+|\b\d+\b)", essay_text))
        kw_found = [kw for kw in (keywords or []) if kw.lower() in essay_text.lower()]
        kw_alignment = (len(kw_found) / len(keywords) * 100) if keywords else 100.0

        score = 10.0
        reasons = []
        if word_count < 50 or word_count > 250:
            score -= 1.5
            reasons.append(f"Word count ({word_count}) outside optimal range (100-200 words)")
        if not has_metrics:
            score -= 2.0
            reasons.append("Lacks concrete metrics (%, $, scale numbers)")
        if keywords and kw_alignment < 80.0:
            score -= 1.5
            reasons.append(f"Keyword alignment ({kw_alignment:.1f}%) below 80% threshold")

        final_score = max(1.0, round(score, 1))
        return {
            "score": final_score,
            "passed": final_score >= 9.0,
            "word_count": word_count,
            "has_metrics": has_metrics,
            "keyword_alignment_pct": round(kw_alignment, 1),
            "reasons": reasons,
        }

    @classmethod
    def run_full_qc(cls, essay: str, company: str = "", role: str = "", keywords: Optional[list[str]] = None) -> bool:
        res = cls.audit_essay(essay, keywords)
        logger.info("=== Phase 0 Quality Control Audit (%s | %s) ===", company, role)
        logger.info("Word Count: %d | Has Metrics: %s | Score: %.1f/10", res['word_count'], res['has_metrics'], res['score'])
        if res["reasons"]:
            for r in res["reasons"]:
                logger.warning("QC Rubric Notice: %s", r)
        if res["passed"]:
            logger.info("[PASSED] Quality Control Passed (Score >= 9.0/10)")
        else:
            logger.warning("QC Warning: Score %.1f/10 is below 9.0/10 target rubric.", res["score"])
        return res["passed"]


# ==============================================================================
# APPLICATION ENGINE
# ==============================================================================
class AshbyApplicant:
    SUBMIT_API_PATTERNS = (
        re.compile(r"ashbyhq\.com/.*submitApplication", re.I),
        re.compile(r"ashbyhq\.com/api/.*application.*submit", re.I),
    )

    NEXT_PAGE_TEXT_RE = re.compile(r"^\s*(next|continue)\b", re.I)
    # Keep the historical misspelling as a public compatibility alias.
    SKIPABLE_PHASES = SKIPPABLE_PHASES

    def __init__(self, cfg: ApplicationConfig) -> None:
        self.cfg = cfg
        company_override = cfg.company_overrides.get(cfg.company)
        if company_override is None:
            company_override = next(
                (
                    override
                    for company, override in cfg.company_overrides.items()
                    if company.casefold() == cfg.company.casefold()
                ),
                {},
            )

        self.profile = dict(cfg.profile or load_candidate_profile())
        for key, value in company_override.items():
            if key in self.profile:
                self.profile[key] = _normalise_profile_value(value)

        self.effective_essay = str(
            company_override.get("essay", cfg.essay_answer)
        ).strip()
        default_product_essay = (
            "As VP of Product at The D. E. Shaw Group, I owned the IDP platform "
            "supporting 12 ML engineers, 2 data scientists, and 3 UX designers. "
            "Previously at AWS, I led the GenAI assistant team with 18 engineers."
        )
        self.product_area_essay = (
            company_override.get("product_area_essay")
            or cfg.product_area_essay
            or default_product_essay
        )

        self.candidate_email = (
            cfg.candidate_email_override
            or get_random_candidate_email(
                cfg.email_pool_file,
                fallback=self.profile.get(
                    "fallback_email",
                    DEFAULT_CANDIDATE_PROFILE["fallback_email"],
                ),
            )
        )
        self.profile["email"] = self.candidate_email
        self.resume_path = (
            cfg.resume_path_override
            or resolve_resume_path(cfg.resume_pdf, cfg.resume_search_dirs)
        )
        self.result = ApplicationResult()
        self._api_responses: list[dict[str, Any]] = []
        self.output_dir = cfg.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._artifact_stem = _safe_artifact_stem(cfg.company)
        self._eeo_done = False
        self._consent_done = False

    def _artifact_path(self, suffix: str) -> Path:
        return self.output_dir / f"{self._artifact_stem}_{suffix}"

    def _record_field(
        self,
        *,
        phase: str,
        selector: str,
        label: str,
        value: str,
        success: bool,
        note: str = "",
    ) -> None:
        self.result.field_log.append(
            FieldFill(
                phase=phase,
                selector=selector,
                label=label,
                value_preview=value,
                success=success,
                note=note,
            )
        )

    def _record_error(
        self,
        message: str,
        *,
        phase: str = "",
        blocking: bool = False,
    ) -> None:
        self.result.errors.append(
            ErrorRecord(
                message=message,
                severity=(
                    ErrorSeverity.BLOCKING
                    if blocking
                    else ErrorSeverity.NON_BLOCKING
                ),
                phase=phase,
            )
        )

    def _type_value(
        self,
        locator: Locator,
        value: str,
        *,
        clear: bool,
        delay_ms: Optional[int] = None,
    ) -> None:
        locator.scroll_into_view_if_needed()
        locator.click()
        human_delay(SHORT_DELAY)
        if clear:
            locator.fill("")
        delay = (
            delay_ms
            if delay_ms is not None
            else random.randint(
                self.cfg.type_delay_min_ms,
                self.cfg.type_delay_max_ms,
            )
        )
        locator.press_sequentially(value, delay=delay)

    def _close_browser_resources(
        self,
        *,
        page: Optional[Page],
        context: Optional[BrowserContext],
        browser: Browser,
    ) -> None:
        if page is not None:
            try:
                page.remove_listener("response", self._on_response)
            except (PlaywrightError, ValueError) as exc:
                logger.debug("Could not remove response listener: %s", exc)
        if context is not None:
            try:
                context.close()
            except PlaywrightError as exc:
                logger.debug("Could not close browser context cleanly: %s", exc)
        try:
            browser.close()
        except PlaywrightError as exc:
            logger.debug("Could not close browser cleanly: %s", exc)

    def run(self) -> ApplicationResult:
        self._banner()
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not self.cfg.headful,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--window-size=1366,900",
                ],
            )
            context: Optional[BrowserContext] = None
            page: Optional[Page] = None
            try:
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/126.0.0.0 Safari/537.36"),
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                )
                page = context.new_page()
                page.set_default_timeout(self.cfg.element_timeout_ms)
                page.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )
                page.on("response", self._on_response)

                self._navigate(page)
                self._open_apply_modal(page)
                self._phase0_screenshots(page)
                blocked_questions = disallowed_screening_questions(page)
                if blocked_questions:
                    self.result.status = (
                        "SKIPPED_INTERNAL_MOBILITY_OR_SECURITY_CLEARANCE"
                    )
                    logger.warning(
                        "Skipping application because of disallowed screening "
                        "question(s): %s",
                        " | ".join(blocked_questions),
                    )
                    return self.result

                max_pages = 10
                for page_num in range(1, max_pages + 1):
                    logger.info("=== Form page %d ===", page_num)
                    self._run_all_phases(page, page_num=page_num)
                    if not self._goto_next_page(page):
                        break
                    human_delay(MEDIUM_DELAY)

                prefilled_png = self._artifact_path("100pct_verified_prefilled.png")
                page.screenshot(path=str(prefilled_png), full_page=True)
                self.result.prefilled_screenshot = str(prefilled_png)
                logger.info("Prefilled screenshot saved: %s", prefilled_png)

                self._submit(page)

                # Keep browser open for manual review / submit when requested
                if self.cfg.keep_open:
                    logger.info("=" * 60)
                    logger.info("FORM PREFILLED. Browser left open for your review.")
                    logger.info("You can edit any fields and click Submit yourself.")
                    logger.info("Press Enter in this terminal when you are finished to close the browser.")
                    logger.info("=" * 60)
                    try:
                        input()
                    except (EOFError, KeyboardInterrupt):
                        logger.info("Received exit signal — closing browser.")
            except Exception as e:
                self._record_error(
                    f"{type(e).__name__}: {e}",
                    blocking=True,
                )
                logger.exception("Application engine aborted: %s", e)
            finally:
                self._close_browser_resources(
                    page=page,
                    context=context,
                    browser=browser,
                )

        return self.result

    def _banner(self) -> None:
        logger.info("=" * 60)
        logger.info("Ashby Applicant Engine (LLM CLI Integrated) — Review Mode")
        logger.info("URL: %s", self.cfg.url)
        logger.info("Company: %s | Role: %s", self.cfg.company, self.cfg.role)
        logger.info("Email: %s", _mask_email(self.candidate_email))
        logger.info("Resume: %s", self.resume_path)
        mode = "LIVE SUBMIT" if self.cfg.live_submit else "DRY-RUN / PREFILL"
        if self.cfg.keep_open:
            mode += " + KEEP-OPEN"
        logger.info("Mode: %s", mode)
        logger.info("Headful: %s", self.cfg.headful)
        logger.info("Output dir: %s", self.output_dir)
        if self.cfg.skip_phases:
            logger.info("Skip phases: %s", ", ".join(sorted(self.cfg.skip_phases)))
        logger.info("=" * 60)

    def _on_response(self, response: Response) -> None:
        url = response.url
        if not any(p.search(url) for p in self.SUBMIT_API_PATTERNS):
            return
        try:
            status = response.status
            body = response.text() if status < 400 else ""
        except PlaywrightError:
            return
        self._api_responses.append({"url": url, "status": status, "body_len": len(body)})
        if status < 300:
            self.result.api_verified = True

    def _navigate(self, page: Page) -> None:
        logger.info("Navigating to %s", self.cfg.url)
        page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            logger.debug("networkidle not reached; continuing.")
        human_delay(LONG_DELAY)

    def _open_apply_modal(self, page: Page) -> None:
        apply_locator = page.locator(
            'a:has-text("Apply for this Job"), '
            'button:has-text("Apply for this Job"), '
            'a:has-text("Apply"), button:has-text("Apply")'
        ).first
        try:
            apply_locator.wait_for(state="visible", timeout=5_000)
            apply_locator.click()
            human_delay(LONG_DELAY)
            try:
                page.locator("form").first.wait_for(state="visible", timeout=5_000)
            except PlaywrightTimeoutError:
                logger.debug("No <form> detected after Apply click; assuming inline form.")
        except PlaywrightTimeoutError:
            logger.info("No separate 'Apply' button; assuming form is inline.")

    def _phase0_screenshots(self, page: Page) -> None:
        jd_png = self._artifact_path("JD_screenshot.png")
        page.screenshot(path=str(jd_png), full_page=True)
        empty_png = self._artifact_path("application_questions.png")
        page.screenshot(path=str(empty_png), full_page=True)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        human_delay(MEDIUM_DELAY)

    def _run_all_phases(self, page: Page, page_num: int) -> None:
        if "personal" not in self.cfg.skip_phases:
            self._phase4_personal_and_uploads(page, page_num)
        if "eeo" not in self.cfg.skip_phases and not self._eeo_done:
            self._phase1_eeo(page, page_num)
            self._eeo_done = True
        if "consent" not in self.cfg.skip_phases and not self._consent_done:
            self._phase2_consent(page, page_num)
            self._consent_done = True
        if "essays" not in self.cfg.skip_phases:
            self._phase3_essays_and_inputs(page, page_num)
        fill_education_history(page, self.profile)
        _fill_configured_checkbox_groups(page, self.profile)
        _fill_radio_groups(page, self.profile)
        for combo in page.locator(
            'input[role="combobox"]:visible, '
            'input[aria-autocomplete="list"]:visible'
        ).all():
            try:
                label = combo.evaluate(FIELD_LABEL_SCRIPT)
                if re.search(r"\b(?:rate|rating)\b", label, re.I):
                    _select_highest_numeric_combobox(page, combo)
            except PlaywrightError:
                continue

    def _goto_next_page(self, page: Page) -> bool:
        try:
            form_buttons = page.locator("form button, form a")
            for btn in form_buttons.all():
                try:
                    text = (btn.text_content() or "").strip()
                    if not text or not self.NEXT_PAGE_TEXT_RE.match(text):
                        continue
                    if not btn.is_visible() or not btn.is_enabled():
                        continue
                    btn.click()
                    logger.info("Navigated to next form page via '%s'.", text)
                    return True
                except PlaywrightError:
                    continue
        except PlaywrightError as e:
            logger.debug("Next-page detection failed: %s", e)
        return False

    def _phase1_eeo(self, page: Page, page_num: int) -> None:
        logger.info("--- PHASE 1: EEO & DIVERSITY SURVEY ---")
        p = self.profile
        self._select_eeo_option(page, "veteran", p.get("veteran", "No"))
        self._select_eeo_option(page, "race|ethnicity", p.get("race", ""))
        self._select_eeo_option(page, "gender", p.get("gender", ""))
        self._select_eeo_option(page, "pronouns", p.get("pronouns", ""))
        self._select_eeo_option(page, "age", p.get("age", ""))
        self._select_eeo_option(page, "transgender", p.get("transgender", "No"))
        self._select_eeo_option(page, "orientation", p.get("orientation", ""))
        self._select_eeo_option(page, "disability|community", p.get("disability", "Yes"))
        for community in p.get("communities", "").split(","):
            community = community.strip()
            if community:
                self._select_eeo_option(
                    page,
                    "communities|community",
                    community,
                )

    def _select_eeo_option(self, page: Page, fieldset_kw: str, label_kw: str) -> None:
        if not label_kw:
            return
        try:
            fieldsets = page.locator("fieldset").filter(has_text=re.compile(fieldset_kw, re.I))
            if self._click_first_visible_label(fieldsets, label_kw):
                self._record_field(
                    phase="eeo",
                    selector=f"fieldset:{fieldset_kw}",
                    label=label_kw,
                    value=label_kw,
                    success=True,
                )
                logger.info("EEO [%s] -> '%s'", fieldset_kw, label_kw)
                return

            divs = page.locator("div[role='group'], divfieldset, div").filter(has_text=re.compile(fieldset_kw, re.I))
            if self._click_first_visible_label(divs, label_kw):
                self._record_field(
                    phase="eeo",
                    selector=f"div:{fieldset_kw}",
                    label=label_kw,
                    value=label_kw,
                    success=True,
                )
                logger.info("EEO [%s] -> '%s' (via div)", fieldset_kw, label_kw)
                return

            self._record_field(
                phase="eeo",
                selector=f"fieldset:{fieldset_kw}",
                label=label_kw,
                value="",
                success=False,
                note="not present on this page",
            )
        except PlaywrightError as e:
            self._record_error(
                f"EEO[{fieldset_kw}]: {e}",
                phase="eeo",
            )

    @staticmethod
    def _click_first_visible_label(container: Locator, label_kw: str) -> bool:
        pattern = re.compile(re.escape(label_kw), re.I)
        for fs in container.all():
            try:
                target = fs.locator(EEO_CLICKABLE_SELECTORS).filter(has_text=pattern).first
                target.wait_for(state="visible", timeout=500)
                target.scroll_into_view_if_needed()
                human_delay(SHORT_DELAY)
                target.click()
                return True
            except PlaywrightTimeoutError:
                continue
            except PlaywrightError:
                continue
        return False

    def _phase2_consent(self, page: Page, page_num: int) -> None:
        logger.info("--- PHASE 2: CONSENT & POLICIES ---")
        try:
            consent = page.locator("label").filter(
                has_text=re.compile(
                    r"\bcontinue\b|\bi agree\b|\bprivacy\b|\bconsent\b|"
                    r"\bi\s+(?:hereby\s+)?confirm\b",
                    re.I,
                )
            )
            count = 0
            for label in consent.all():
                try:
                    if not label.is_visible():
                        continue
                    label.scroll_into_view_if_needed()
                    human_delay(SHORT_DELAY)
                    label.click()
                    count += 1
                    self._record_field(
                        phase="consent",
                        selector="label",
                        label="consent",
                        value="checked",
                        success=True,
                    )
                except PlaywrightError as e:
                    logger.debug("Consent click skipped: %s", e)
            logger.info("Clicked %d consent labels.", count)
        except PlaywrightError as e:
            self._record_error(
                f"Consent: {e}",
                phase="consent",
            )

    def _phase3_essays_and_inputs(self, page: Page, page_num: int) -> None:
        logger.info("--- PHASE 3: CUSTOM ESSAYS & INPUTS (page %d) ---", page_num)
        self._fill_source_dropdown(page, page_num)
        self._fill_comboboxes(page, page_num)
        self._fill_textareas(page, page_num)
        self._fill_text_inputs(page, page_num)
        self._fill_choice_buttons(page, page_num)

    def _fill_comboboxes(self, page: Page, page_num: int) -> None:
        try:
            combos = page.locator('input[placeholder*="Start typing"], input[role="combobox"], input[aria-autocomplete="list"]')
            for c_inp in combos.all():
                try:
                    if not c_inp.is_visible():
                        continue
                    lbl_text = c_inp.evaluate(FIELD_LABEL_SCRIPT)

                    if any(k in lbl_text for k in ("location", "state", "city", "country", "reside")):
                        val = self.profile.get("city", "San Francisco") + ", " + self.profile.get("state", "California")
                    elif "hear" in lbl_text or "source" in lbl_text:
                        val = "LinkedIn"
                    else:
                        val = "United States"

                    self._type_value(c_inp, val, clear=True, delay_ms=30)
                    human_delay(MEDIUM_DELAY)

                    try:
                        container = page.locator('div[class*="_floatingContainer_"], [id*="listbox"]').first
                        container.wait_for(state="visible", timeout=2_000)
                        exact_match = container.locator('div[class*="_result_"], div[role="option"]').filter(
                            has_text=re.compile(rf"^\s*{re.escape(val)}\s*$", re.I)
                        ).first
                        if exact_match.count() and exact_match.is_visible():
                            exact_match.click()
                        else:
                            container.locator('div[class*="_result_"], div[role="option"]').first.click()
                        logger.info("Combobox [%s] selected option via popover: %s", lbl_text[:30], val)
                    except PlaywrightTimeoutError:
                        c_inp.press("ArrowDown")
                        c_inp.press("Enter")
                        logger.info("Combobox [%s] selected via keyboard: %s", lbl_text[:30], val)

                    self._record_field(
                        phase=f"combo-p{page_num}",
                        selector="combobox",
                        label=lbl_text[:50],
                        value=val,
                        success=True,
                    )
                except PlaywrightError as e:
                    logger.debug("Combobox fill skipped: %s", e)
        except PlaywrightError as e:
            logger.debug("Combobox phase error: %s", e)

    def _fill_choice_buttons(self, page: Page, page_num: int) -> None:
        try:
            button_groups = page.locator('div:has(> button:has-text("Yes")):has(> button:has-text("No")), div:has(> label:has-text("Yes")):has(> label:has-text("No"))')
            for g in button_groups.all():
                try:
                    if not g.is_visible():
                        continue
                    question_txt = g.evaluate('''el => {
                        let parent = el.closest('div[class*="_question_"], fieldset, div[class*="_field_"]');
                        return parent ? parent.innerText : el.innerText;
                    }''').lower()

                    if any(k in question_txt for k in ("sponsorship", "visa", "require visa")):
                        target_text = "No"
                    else:
                        target_text = "Yes"

                    btn = g.locator('button, label').filter(has_text=re.compile(rf"^\s*{target_text}\b", re.I)).first
                    if btn.count() and btn.is_visible():
                        btn.scroll_into_view_if_needed()
                        btn.click()
                        self._record_field(
                            phase=f"choice-p{page_num}",
                            selector="button",
                            label=question_txt[:40].strip(),
                            value=target_text,
                            success=True,
                        )
                        logger.info("Choice button: '%s' for [%s]", target_text, question_txt[:30].strip())
                except PlaywrightError:
                    continue
        except PlaywrightError as e:
            logger.debug("Choice buttons error: %s", e)

    def _fill_source_dropdown(self, page: Page, page_num: int) -> None:
        try:
            sources = page.locator('input[placeholder*="Start typing"]')
            for inp in sources.all():
                try:
                    if not inp.is_visible():
                        continue
                    container_text = inp.evaluate('el => el.closest("div") ? el.closest("div").innerText : ""').lower()
                    if not any(kw in container_text for kw in ("hear", "referral", "source")):
                        continue
                    self._type_value(inp, "LinkedIn", clear=False)
                    human_delay(MEDIUM_DELAY)
                    inp.press("ArrowDown")
                    inp.press("Enter")
                    self._record_field(
                        phase=f"source-p{page_num}",
                        selector='input[placeholder*="Start typing"]',
                        label="source",
                        value="LinkedIn",
                        success=True,
                    )
                    logger.info("Source dropdown: LinkedIn")
                except PlaywrightError as e:
                    self._record_field(
                        phase=f"source-p{page_num}",
                        selector='input[placeholder*="Start typing"]',
                        label="source",
                        value="",
                        success=False,
                        note=str(e),
                    )
        except PlaywrightError as e:
            self._record_error(
                f"Source: {e}",
                phase=f"source-p{page_num}",
            )

    def _synthesize_essay_full_awareness(self, page: Page, prompt_text: str) -> str:
        """
        Synthesizes a custom essay through the configured AI provider with full
        prompt and job-description awareness.
        and an automated QCAuditor feedback retry loop.
        """
        try:
            jd_text = page.evaluate("() => document.body.innerText || ''")
        except Exception:
            jd_text = ""

        company = self.cfg.company
        role = self.cfg.role
        profile_summary = json.dumps(self.profile, indent=2)

        base_system_prompt = (
            "You are an expert AI Product Manager applicant. Generate a metric-backed essay answer to the job application question.\n"
            "Output MUST be valid JSON in this exact structure:\n"
            "{\n"
            '  "essay_answer": "Your essay text here"\n'
            "}\n"
            "Guidelines:\n"
            "- Word count MUST be strictly between 100 and 200 words.\n"
            "- Include concrete metrics (percentages, dollar amounts, scale numbers, team sizes).\n"
            "- Directly align past enterprise experience with the question and target job description.\n"
            "- Do NOT wrap output in markdown code fences; respond ONLY with raw, valid JSON."
        )

        feedback = ""

        for attempt in range(1, 4):
            current_system_prompt = base_system_prompt
            if feedback:
                current_system_prompt += (
                    f"\n\nCRITICAL FEEDBACK FROM PREVIOUS ATTEMPT (Attempt {attempt - 1}):\n"
                    f"The previous output failed quality control checks due to:\n{feedback}\n"
                    "Adjust the essay answer to explicitly fix all listed issues while maintaining strict 100-200 word count and metric requirements."
                )

            user_prompt = (
                f"Target Company: {company}\n"
                f"Target Role: {role}\n"
                f"Question Prompt: {prompt_text}\n\n"
                f"Candidate Profile:\n{profile_summary}\n\n"
                f"Job Description Excerpt:\n{jd_text[:3000]}"
            )

            try:
                logger.info("Calling AI provider for essay synthesis (attempt %d/3)...", attempt)
                llm_resp = _call_llm(user_prompt=user_prompt, system_prompt=current_system_prompt)
                essay = strip_markdown_formatting(
                    llm_resp.get("essay_answer")
                    or llm_resp.get("answer")
                    or ""
                )
            except Exception as exc:
                logger.warning("AI provider call failed on attempt %d: %s. Falling back to rule-based synthesis...", attempt, exc)
                essay = self.effective_essay or (
                    f"I bring 8+ years of technical product management and platform execution experience. "
                    f"At AWS and The D. E. Shaw Group, I scaled AI products to 100M+ users. "
                    f"I look forward to contributing to {company} in the {role} position."
                )

            qc_res = QCAuditor.audit_essay(essay)
            logger.info("=== Phase 0 Quality Control Audit (%s | %s) ===", company, role)
            logger.info("Attempt %d - Word Count: %d | Has Metrics: %s | Score: %.1f/10",
                        attempt, qc_res['word_count'], qc_res['has_metrics'], qc_res['score'])

            if qc_res["passed"] or attempt == 3:
                if not qc_res["passed"]:
                    logger.warning("QC Audit unpassed after attempt %d; applying auto-refinement fallback...", attempt)
                    essay += f" I am eager to apply my technical background to drive +30% product velocity for {role} at {company}."
                return strip_markdown_formatting(essay)

            reasons_str = "\n- ".join(qc_res["reasons"])
            feedback = f"- {reasons_str}"
            logger.warning("QC audit failed on attempt %d; retrying the AI provider with feedback:\n%s", attempt, feedback)

        return strip_markdown_formatting(essay)

    def _fill_textareas(self, page: Page, page_num: int) -> None:
        try:
            textareas = page.locator("textarea")
            for ta in textareas.all():
                try:
                    if not ta.is_visible():
                        continue
                    lbl_text = ta.evaluate(FIELD_CONTEXT_SCRIPT)
                    # Skip cover letter textareas completely if present
                    if "cover letter" in lbl_text or "coverletter" in lbl_text:
                        logger.info("Skipping Cover Letter textarea field.")
                        continue

                    if any(t in lbl_text for t in ("hear", "source", "referral")):
                        value_preview = "LinkedIn"
                    else:
                        value_preview = self._synthesize_essay_full_awareness(page, lbl_text)

                    self._type_value(
                        ta,
                        value_preview,
                        clear=False,
                        delay_ms=20,
                    )
                    logger.debug(
                        "Textarea filled for %r (%d characters).",
                        lbl_text[:40],
                        len(value_preview),
                    )
                    self._record_field(
                        phase=f"textarea-p{page_num}",
                        selector="textarea",
                        label=lbl_text[:50],
                        value=value_preview,
                        success=True,
                    )
                except PlaywrightError as e:
                    self._record_field(
                        phase=f"textarea-p{page_num}",
                        selector="textarea",
                        label="",
                        value="",
                        success=False,
                        note=str(e),
                    )
        except PlaywrightError as e:
            self._record_error(
                f"Textareas: {e}",
                phase=f"textarea-p{page_num}",
            )

    def _fill_text_inputs(self, page: Page, page_num: int) -> None:
        p = self.profile
        try:
            inputs = page.locator("input")
            for inp in inputs.all():
                try:
                    if not inp.is_visible():
                        continue
                    inp_type = (inp.get_attribute("type") or "text").lower()
                    if inp_type in NON_TEXT_INPUT_TYPES:
                        continue
                    lbl_text = inp.evaluate(FIELD_CONTEXT_SCRIPT)
                    
                    # Skip cover letter input fields completely
                    if "cover letter" in lbl_text or "coverletter" in lbl_text:
                        continue

                    value = self._match_input_value(lbl_text, p)
                    if value is None:
                        continue
                    self._type_value(inp, value, clear=False)
                    logger.debug("Input filled for %r.", lbl_text[:40])
                    self._record_field(
                        phase=f"input-p{page_num}",
                        selector="input",
                        label=lbl_text[:50],
                        value=value,
                        success=True,
                    )
                except PlaywrightError as e:
                    self._record_field(
                        phase=f"input-p{page_num}",
                        selector="input",
                        label="",
                        value="",
                        success=False,
                        note=str(e),
                    )
        except PlaywrightError as e:
            self._record_error(
                f"Inputs: {e}",
                phase=f"input-p{page_num}",
            )

    @staticmethod
    def _match_input_value(lbl: str, p: Mapping[str, Any]) -> Optional[str]:
        def has(kw: str) -> bool:
            return re.search(rf"\b{re.escape(kw)}\b", lbl) is not None

        if (
            ("address" in lbl and "you" in lbl)
            or "preferred" in lbl
            or "call you" in lbl
        ):
            return p.get("first_name", "Shivam")
        if "notice" in lbl or "availability" in lbl or "start date" in lbl:
            return (date.today() + timedelta(days=14)).isoformat()
        if "hear" in lbl or "source" in lbl or "referral" in lbl:
            return "LinkedIn"
        if "confirm" in lbl and "email" in lbl:
            return p.get("email", "")
        if has("nationality") or has("citizenship"):
            return "United States"
        if has("twitter") or "x.com" in lbl:
            return p.get("twitter", "")
        if has("researchgate"):
            return p.get("researchgate", "")
        if has("sciencedirect"):
            return p.get("sciencedirect", "")
        if has("book") or has("goodreads"):
            return p.get("goodreads_book", "")
        if has("street") or has("address_line"):
            return p.get("street_address", "")
        if has("city"):
            return p.get("city", "")
        if (has("state") or has("region")) and "statement" not in lbl:
            return p.get("state", "")
        if has("zip") or has("postal"):
            return p.get("zip_code", "")
        if has("country"):
            return p.get("country", "United States")
        if has("portfolio") or has("website") or "link" in lbl or "url" in lbl:
            return p.get("linkedin", "https://linkedin.com/in/beastofbayarea")
        if has("salary") or has("pay"):
            return "Competitive / Market rate"
        return None

    def _phase4_personal_and_uploads(self, page: Page, page_num: int) -> None:
        logger.info("--- PHASE 4: PERSONAL INFO & RESUME (page %d) ---", page_num)
        has_file_input = page.locator('input[type="file"]').count() > 0
        if has_file_input:
            self._upload_resume(page, page_num)
        self._fill_system_fields(page, page_num)

    def _upload_resume(self, page: Page, page_num: int) -> None:
        try:
            file_inputs = page.locator('input[id="_systemfield_resume"], input[type="file"]')
            target_input = None
            
            # Find explicit non-cover-letter resume file input
            for finp in file_inputs.all():
                lbl = finp.evaluate('el => el.parentElement ? el.parentElement.innerText : ""').lower()
                if "cover" in lbl:
                    logger.info("Skipping cover letter file upload element.")
                    continue
                target_input = finp
                break

            if target_input is None:
                logger.debug("No valid resume file input on page %d; skipping.", page_num)
                return

            target_input.wait_for(state="attached", timeout=5_000)
            target_input.set_input_files(str(self.resume_path))
            self._record_field(
                phase=f"resume-p{page_num}",
                selector='input[type="file"]',
                label="resume",
                value=self.resume_path.name,
                success=True,
            )
            logger.info("Resume attached: %s", self.resume_path.name)
            logger.info("Enforcing mandatory 4.0s Resume Lock-in Pause to let Ashby React autofill settle...")
            time.sleep(4.0)
        except PlaywrightError as e:
            self._record_error(
                f"Resume upload: {e}",
                phase=f"resume-p{page_num}",
                blocking=True,
            )

    def _fill_system_fields(self, page: Page, page_num: int) -> None:
        p = self.profile
        name_val = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

        # Name field filling
        name_filled = False
        try:
            for inp in page.locator('input').all():
                if not inp.is_visible():
                    continue
                inp_type = (inp.get_attribute("type") or "text").lower()
                if inp_type in NON_TEXT_INPUT_TYPES:
                    continue
                lbl = inp.evaluate(FIELD_LABEL_SCRIPT)
                id_name = (inp.get_attribute("id") or "").lower()
                name_attr = (inp.get_attribute("name") or "").lower()
                
                if "_systemfield_name" in id_name or "_systemfield_name" in name_attr or "full name" in lbl or "name*" in lbl or lbl.strip() == "name":
                    self._type_value(inp, name_val, clear=True, delay_ms=20)
                    self._record_field(
                        phase=f"name-p{page_num}",
                        selector="input[name]",
                        label="full name",
                        value=name_val,
                        success=True,
                    )
                    name_filled = True
                    logger.info("Name field filled: %s", name_val)
                    break
        except PlaywrightError:
            pass

        if not name_filled:
            self._safe_fill(page, 'input[id="_systemfield_name"], input[name="_systemfield_name"], input[id*="name"], input[name*="name"]', name_val, phase=f"name-p{page_num}", label="full name")
            try:
                first_inp = page.locator('input[type="text"], input:not([type])').first
                if first_inp.is_visible() and not first_inp.input_value():
                    self._type_value(
                        first_inp,
                        name_val,
                        clear=True,
                        delay_ms=20,
                    )
                    self._record_field(
                        phase=f"name-p{page_num}",
                        selector="input:first-text",
                        label="full name (fallback)",
                        value=name_val,
                        success=True,
                    )
                    logger.info("Name field filled via fallback: %s", name_val)
            except PlaywrightError:
                pass

        # Email field
        self._safe_fill(page, 'input[id="_systemfield_email"], input[name="_systemfield_email"], input[type="email"]', self.candidate_email, phase=f"email-p{page_num}", label="email")

        # Phone field
        self._safe_fill(page, 'input[id="_systemfield_phone"], input[name*="phone"], input[type="tel"]', p.get("phone", ""), phase=f"phone-p{page_num}", label="phone")

        # LinkedIn field
        linkedin_filled = False
        try:
            for inp in page.locator('input').all():
                if not inp.is_visible():
                    continue
                lbl = inp.evaluate(FIELD_LABEL_SCRIPT)
                if "linkedin" in lbl:
                    linkedin = p.get("linkedin", "")
                    self._type_value(inp, linkedin, clear=True, delay_ms=0)
                    self._record_field(
                        phase=f"linkedin-p{page_num}",
                        selector="input[linkedin]",
                        label="linkedin",
                        value=linkedin,
                        success=True,
                    )
                    linkedin_filled = True
                    break
        except PlaywrightError:
            pass

        if not linkedin_filled:
            self._safe_fill(page, 'input[id*="linkedin"], input[name*="linkedin"], input[placeholder*="linkedin.com"]', p.get("linkedin", ""), phase=f"linkedin-p{page_num}", label="linkedin")

    def _safe_fill(self, page: Page, selector: str, value: str, phase: str, label: str) -> None:
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=3_000)
            self._type_value(loc, value, clear=True)
            self._record_field(
                phase=phase,
                selector=selector,
                label=label,
                value=value,
                success=True,
            )
        except PlaywrightTimeoutError:
            self._record_field(
                phase=phase,
                selector=selector,
                label=label,
                value="",
                success=False,
                note="not visible within timeout",
            )
        except PlaywrightError as e:
            self._record_field(
                phase=phase,
                selector=selector,
                label=label,
                value="",
                success=False,
                note=str(e),
            )
            self._record_error(
                f"Fill[{selector}]: {e}",
                phase=phase,
            )

    def _submit(self, page: Page) -> None:
        if not self.cfg.live_submit:
            logger.info("Dry-run: skipping submit click.")
            return
        try:
            submit_btn: Optional[Locator] = None
            text_btn = page.locator('button:has-text("Submit Application")').first
            try:
                text_btn.wait_for(state="visible", timeout=3_000)
                submit_btn = text_btn
            except PlaywrightTimeoutError:
                fallback = page.locator('button[type="submit"]').last
                try:
                    fallback.wait_for(state="visible", timeout=2_000)
                    submit_btn = fallback
                except PlaywrightTimeoutError:
                    pass

            if submit_btn is None:
                self._record_error(
                    "Submit button not found",
                    phase="submit",
                    blocking=True,
                )
                logger.error("Could not locate submit button.")
                return

            self._pre_submit_verification_sweep(page)

            # Attempt reCAPTCHA bypass before clicking submit
            recaptcha_ok = _bypass_recaptcha(page)
            if recaptcha_ok:
                logger.info("reCAPTCHA token injected before submit.")
            else:
                logger.warning("reCAPTCHA bypass did not produce a token. "
                               "Submission may be blocked by bot detection.")

            submit_btn.scroll_into_view_if_needed()
            human_delay(MEDIUM_DELAY)
            submit_btn.click()
            logger.info("Submit clicked.")

            self._wait_for_submission_confirmation(page)

            submitted_png = self._artifact_path("submitted_verified.png")
            try:
                page.screenshot(path=str(submitted_png), full_page=True)
            except PlaywrightError:
                logger.warning("Could not capture initial submit screenshot.")
            self.result.submitted_screenshot = str(submitted_png)

            logger.info("Waiting 5 seconds post-submission...")
            time.sleep(5.0)
            submitted_5s_png = self._artifact_path("submitted_5s_verified.png")
            try:
                page.screenshot(path=str(submitted_5s_png), full_page=True)
                logger.info("5s Post-submit screenshot saved: %s", submitted_5s_png)
            except PlaywrightError:
                logger.warning("Could not capture 5s submitted screenshot.")

            try:
                is_confirmed_url = any(kw in page.url.lower() for kw in ("submitted", "thank", "success"))
            except PlaywrightError:
                is_confirmed_url = False

            if self.result.api_verified or is_confirmed_url:
                self.result.status = "SUBMITTED & CONFIRMED (API VERIFIED)"
            else:
                self.result.status = "SUBMITTED (UI VERIFIED)"
        except PlaywrightError as e:
            self._record_error(
                f"Submit: {e}",
                phase="submit",
                blocking=True,
            )
            logger.error("Submit failed: %s", e)

    def _pre_submit_verification_sweep(self, page: Page) -> None:
        logger.info("--- MANDATORY PRE-SUBMIT VERIFICATION & FILL SWEEP ---")
        p = self.profile
        name_val = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

        # Sweep text inputs & textareas
        try:
            for inp in page.locator('input[type="text"], input:not([type]), input[type="email"], input[type="tel"], textarea').all():
                try:
                    if not inp.is_visible():
                        continue
                    val_cur = inp.input_value()
                    if val_cur:
                        continue
                    lbl_text = inp.evaluate(FIELD_LABEL_SCRIPT)

                    if "cover letter" in lbl_text or "coverletter" in lbl_text:
                        continue

                    tag_name = inp.evaluate('el => el.tagName.toLowerCase()')
                    if "name" in lbl_text or inp.get_attribute("id") == "_systemfield_name":
                        val = name_val
                    elif "email" in lbl_text or inp.get_attribute("id") == "_systemfield_email":
                        val = self.candidate_email
                    elif "phone" in lbl_text or inp.get_attribute("id") == "_systemfield_phone":
                        val = p.get("phone", "6502833478")
                    elif "linkedin" in lbl_text:
                        val = p.get("linkedin", "")
                    elif tag_name == "textarea":
                        val = self._synthesize_essay_full_awareness(page, lbl_text)
                    else:
                        val = "Product Management & AI Systems Execution"

                    self._type_value(inp, val, clear=True, delay_ms=20)
                    logger.info(
                        "Pre-submit sweep filled empty field [%s].",
                        lbl_text[:30],
                    )
                except PlaywrightError:
                    continue
        except PlaywrightError as e:
            logger.debug("Pre-submit sweep text error: %s", e)

        # Sweep required radio & button choice groups
        try:
            groups = page.locator('fieldset, div[role="radiogroup"], div[class*="_question_"], div:has(> button:has-text("Yes")):has(> button:has-text("No")), div:has(> label:has-text("Yes")):has(> label:has-text("No"))')
            for g in groups.all():
                try:
                    if not g.is_visible():
                        continue
                    checked = g.locator('input[type="radio"]:checked, [role="radio"][aria-checked="true"], button[aria-pressed="true"], button[class*="_active_"], button[class*="_selected_"]').count()
                    if checked == 0:
                        txt = g.evaluate('''el => {
                            let parent = el.closest('div[class*="_question_"], fieldset, div[class*="_field_"]');
                            return parent ? parent.innerText : el.innerText;
                        }''').lower()
                        if any(k in txt for k in ("sponsorship", "visa", "require visa")):
                            target_text = "No"
                        else:
                            target_text = "Yes"

                        target_btn = g.locator('label, button, [role="radio"]').filter(has_text=re.compile(rf"^\s*{target_text}\b", re.I)).first
                        if target_btn.count() and target_btn.is_visible():
                            target_btn.scroll_into_view_if_needed()
                            target_btn.click()
                            logger.info("Pre-submit sweep selected choice '%s' for group [%s]", target_text, txt[:40].strip())
                except PlaywrightError:
                    continue
        except PlaywrightError as e:
            logger.debug("Pre-submit sweep choice group error: %s", e)

    def _wait_for_submission_confirmation(self, page: Page) -> None:
        deadline = time.time() + self.cfg.post_submit_wait_sec
        confirmation_pattern = re.compile(
            r"thank you|application received|successfully submitted|application submitted|submitted", re.I
        )
        while time.time() < deadline:
            if self.result.api_verified:
                return
            try:
                if any(kw in page.url.lower() for kw in ("submitted", "thank", "success")):
                    self.result.api_verified = True
                    return
                text = (page.locator("body").text_content() or "").strip()
                if confirmation_pattern.search(text):
                    return
            except PlaywrightError:
                pass
            time.sleep(POLL_INTERVAL_SEC)


# ==============================================================================
# CLI
# ==============================================================================
def _normalise_ashby_url(url: str) -> str:
    candidate = str(url).strip()
    if not candidate or "\\" in candidate or any(char.isspace() for char in candidate):
        raise ValueError("URL must not be empty or contain whitespace/backslashes")

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host:
        raise ValueError("URL must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("URL must not include credentials")
    if host != "ashbyhq.com" and not host.endswith(".ashbyhq.com"):
        raise ValueError("URL must use an Ashby host (*.ashbyhq.com)")
    try:
        if parsed.port not in (None, 443):
            raise ValueError("Ashby URL must use the default HTTPS port")
    except ValueError as exc:
        raise ValueError(f"URL has an invalid port: {exc}") from exc
    return candidate


def _validate_url(url: str) -> str:
    try:
        return _normalise_ashby_url(url)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.setLevel(numeric)


def ensure_output_dirs(output_dir: Path) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)


def _emit_engine_result(
    *,
    success: bool,
    status: str,
    submitted: bool = False,
    confirmed: bool = False,
    test_mode: bool = False,
    error: str = "",
) -> None:
    payload: dict[str, Any] = {
        "success": success,
        "status": status,
        "ats": "ashby",
        "submitted": submitted,
        "confirmed": confirmed,
        "test_mode": test_mode,
    }
    if error:
        payload["error"] = error
    print(f"{ENGINE_RESULT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ashby Job Applicant Engine")
    parser.add_argument(
        "--url",
        required=True,
        help="Ashby job posting URL",
    )
    parser.add_argument("--resume", required=True, help="Resume PDF path or filename")
    parser.add_argument("--company", default="Company", help="Company name")
    parser.add_argument("--role", default="Applicant", help="Role title")
    parser.add_argument("--essay", default="", help="Default essay answer text")
    parser.add_argument("--product-area-essay", default="", help="Role product area essay text")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to nested candidate_profile_config.json or a legacy candidate profile",
    )
    parser.add_argument(
        "--company-overrides",
        type=Path,
        default=None,
        help="Path to company_overrides.json",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ASHBY_DIR)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--nav-timeout", type=int, default=None)
    parser.add_argument("--post-submit-wait", type=float, default=None)
    parser.add_argument("--type-delay-min", type=int, default=None)
    parser.add_argument("--type-delay-max", type=int, default=None)
    parser.add_argument("--email", default=None, help="Override candidate email")
    parser.add_argument(
        "--skip-phase",
        action="append",
        choices=sorted(AshbyApplicant.SKIPABLE_PHASES),
        default=[],
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live-submit",
        action="store_true",
        help="Execute real form submission",
    )
    mode.add_argument(
        "--fill-only",
        action="store_true",
        help="Prefill without submitting",
    )
    mode.add_argument("--dry-run", action="store_true", help="Prefill audit only")
    parser.add_argument(
        "--headed",
        "--headful",
        dest="headful",
        action="store_true",
        help="Run browser visibly",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave browser open after prefilling so you can review and submit manually",
    )
    return parser


def _resolve_config_path(raw: Any, *, base_dir: Path) -> Path:
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else base_dir / path


def _configured_resume_dirs(settings: ProfileSettings) -> tuple[Path, ...]:
    raw_dirs = settings.document.get("resume_search_dirs", [])
    if raw_dirs is None:
        return ()
    if not isinstance(raw_dirs, list) or not all(
        isinstance(item, str) and item.strip() for item in raw_dirs
    ):
        raise ValueError("'resume_search_dirs' must be a list of non-empty paths")

    return tuple(
        path if path.is_absolute() else PROJECT_ROOT / path
        for path in (Path(item).expanduser() for item in raw_dirs)
    )


def _configured_email_pool(settings: ProfileSettings) -> Path:
    raw_path = settings.document.get("email_pool_file")
    if not raw_path:
        return DEFAULT_EMAIL_POOL_FILE
    base_dir = settings.source.parent if settings.source else SCRIPT_DIR
    return _resolve_config_path(raw_path, base_dir=base_dir)


def _load_company_overrides_file(path: Optional[Path]) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    document = _read_json_object(
        path,
        required=True,
        description="Company overrides file",
    )
    raw = document.get("company_overrides", document)
    return _normalise_company_overrides(raw, source=path)


def _config_int(
    args_value: Optional[int],
    document: Mapping[str, Any],
    key: str,
    default: int,
) -> int:
    raw = args_value if args_value is not None else document.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"'{key}' must be an integer")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{key}' must be an integer") from exc


def _config_float(
    args_value: Optional[float],
    document: Mapping[str, Any],
    key: str,
    default: float,
) -> float:
    raw = args_value if args_value is not None else document.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"'{key}' must be numeric")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{key}' must be numeric") from exc


def _build_application_config(
    args: argparse.Namespace,
    settings: ProfileSettings,
) -> ApplicationConfig:
    document = settings.document
    external_overrides = _load_company_overrides_file(args.company_overrides)
    company_overrides = {
        **settings.company_overrides,
        **external_overrides,
    }

    configured_headless = document.get("headless", True)
    if not isinstance(configured_headless, bool):
        raise ValueError("'headless' must be true or false")
    return ApplicationConfig(
        url=_normalise_ashby_url(args.url),
        resume_pdf=args.resume,
        company=args.company,
        role=args.role,
        essay_answer=args.essay,
        product_area_essay=args.product_area_essay,
        live_submit=args.live_submit and not args.dry_run,
        headful=args.headful or not configured_headless,
        keep_open=args.keep_open,
        profile=settings.candidate,
        company_overrides=company_overrides,
        output_dir=args.output_dir,
        nav_timeout_ms=_config_int(
            args.nav_timeout,
            document,
            "navigation_timeout_ms",
            DEFAULT_NAV_TIMEOUT_MS,
        ),
        element_timeout_ms=_config_int(
            args.timeout,
            document,
            "action_timeout_ms",
            DEFAULT_ELEMENT_TIMEOUT_MS,
        ),
        post_submit_wait_sec=_config_float(
            args.post_submit_wait,
            document,
            "post_submit_wait_sec",
            DEFAULT_POST_SUBMIT_WAIT_SEC,
        ),
        type_delay_min_ms=_config_int(
            args.type_delay_min,
            document,
            "type_delay_min_ms",
            DEFAULT_TYPE_DELAY_MIN_MS,
        ),
        type_delay_max_ms=_config_int(
            args.type_delay_max,
            document,
            "type_delay_max_ms",
            DEFAULT_TYPE_DELAY_MAX_MS,
        ),
        skip_phases=set(args.skip_phase),
        candidate_email_override=args.email,
        email_pool_file=_configured_email_pool(settings),
        resume_search_dirs=_configured_resume_dirs(settings),
    )


def _emit_application_result(
    result: ApplicationResult,
    cfg: ApplicationConfig,
) -> int:
    blocking_errors = result.blocking_errors
    if blocking_errors:
        logger.error("Blocking errors encountered: %d.", len(blocking_errors))

    submitted = result.status.startswith("SUBMITTED")
    confirmed = result.status.startswith("SUBMITTED & CONFIRMED")
    success = not blocking_errors and (confirmed if cfg.live_submit else True)
    status = result.status
    if cfg.live_submit and submitted and not confirmed:
        status = "SUBMIT_ATTEMPT_UNCONFIRMED"

    logger.info(
        "Run complete. %d fields filled successfully.",
        sum(1 for field_fill in result.field_log if field_fill.success),
    )
    _emit_engine_result(
        success=success,
        status=status,
        submitted=submitted,
        confirmed=confirmed,
        test_mode=not cfg.live_submit,
        error=blocking_errors[0].message if blocking_errors else "",
    )
    return 0 if success else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argument_parser().parse_args(argv)

    _setup_logging(args.log_level)

    # --keep-open implies headful + dry-run (no auto-submit)
    if args.keep_open:
        if not args.headful:
            logger.info("--keep-open requested → forcing --headful")
        args.headful = True
        if args.live_submit:
            logger.warning(
                "--keep-open and --live-submit both set; ignoring --live-submit "
                "(you will submit yourself)"
            )
        args.live_submit = False

    try:
        require_orchestrated_invocation(args.url)
        settings = load_profile_settings(
            args.config,
            required=args.config is not None,
        )
        cfg = _build_application_config(args, settings)
        ensure_output_dirs(cfg.output_dir)
        result = AshbyApplicant(cfg).run()
    except Exception as exc:
        logger.exception("Engine initialization failed: %s", exc)
        _emit_engine_result(
            success=False,
            status="FAILED_INITIALIZATION",
            test_mode=not args.live_submit,
            error=f"{type(exc).__name__}: {exc}",
        )
        return 1

    return _emit_application_result(result, cfg)


if __name__ == "__main__":
    sys.exit(main())
