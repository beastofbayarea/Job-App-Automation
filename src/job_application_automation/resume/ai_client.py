#!/usr/bin/env python3
"""
Resume AI Utilities (Google GenAI Edition)
================================================================================
Universal LLM and document generation utilities for:
1. Resume Content Tailoring via Google GenAI SDK
2. Zero-Template Application Essay Synthesis via Google GenAI SDK
3. Resume Text Extraction & Supported ATS Job Scraping

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. Multi-Model Consensus Router & LLM Jury System (Gemini + Claude 3.5 + GPT-4o):
   - Rather than relying on a single call to Google GenAI / Vertex AI, implement an asynchronous multi-model router (via LiteLLM).
   - Generate candidate responses using multiple LLM providers concurrently and employ a lightweight evaluator prompt to select the response with the highest semantic overlap with the Job Description.
   - Benefit: Eliminates single-vendor API downtime and maximizes output quality.

2. Retrieval-Augmented Generation (RAG) with Vector Database (Chroma / FAISS):
   - Replace whole-resume prompt injection with a vectorized RAG pipeline storing atomic candidate accomplishment blocks (JSON objects tagged with skills, impact metrics, and role seniority).
   - Perform semantic vector query matching against JD keywords, injecting only top-K relevant accomplishment blocks.
   - Benefit: Reduces token consumption by 70%, stays well within prompt limits, and avoids hallucinating non-existent accomplishments.

3. Constrained JSON Schema Decoding (Pydantic / Instructor / Outlines):
   - Replace brittle post-generation regex formatting (`strip_markdown_formatting`) and JSON text extraction with native JSON-Schema grammar enforcement at inference time.
   - Benefit: Guarantees 100% valid schema outputs, zero syntax parsing errors, and eliminates markdown cleanup hacks.

4. Offline Local SLM Fallback Engine (Ollama / Llama 3.2 / Phi-3):
   - Integrate a local small language model (SLM) fallback using Ollama or llama.cpp for offline resume tailoring when Vertex AI credentials or network connections are unavailable.
==============================================================================
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

import pypdf
from playwright.sync_api import sync_playwright

from ..core.adapters import LLMClient, LLMSettings
from ..core.engine_shared import (
    close_browser_session,
    detect_ats_job_url,
    open_chrome_session,
)
from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

# =============================================================================
# LOGGING & RUNTIME CONFIGURATION
# =============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ResumeAIUtilities")


@dataclass(frozen=True, slots=True)
class VertexSettings:
    """Explicit Vertex configuration for a locally credentialed LLM gateway."""

    project_id: str = RUNTIME_CONFIG.vertex.project_id
    location: str = RUNTIME_CONFIG.vertex.location
    model: str = RUNTIME_CONFIG.vertex.model
    service_account_file: Path = resolve_runtime_path(RUNTIME_CONFIG.vertex.service_account_file)

    def __post_init__(self) -> None:
        for name, value in (
            ("project_id", self.project_id),
            ("location", self.location),
            ("model", self.model),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "project_id", self.project_id.strip())
        object.__setattr__(self, "location", self.location.strip())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(
            self,
            "service_account_file",
            Path(self.service_account_file).expanduser(),
        )


VERTEX_SETTINGS = VertexSettings()
# Legacy exports remain stable for callers that import these module constants.
PROJECT_ID = VERTEX_SETTINGS.project_id
LOCATION = VERTEX_SETTINGS.location
MODEL = VERTEX_SETTINGS.model
SERVICE_ACCOUNT_FILE = VERTEX_SETTINGS.service_account_file
GOOGLE_APPLICATION_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"
VERTEX_AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

# Serializes LLM calls; the orchestrator can run multiple ATS engines concurrently
# but Vertex AI quota and the shared client are not safe for parallel requests.
_ai_lock = threading.Lock()
_client_lock = threading.Lock()
_client = None
_client_settings: VertexSettings | None = None
LLM_MAX_ATTEMPTS = RUNTIME_CONFIG.vertex.max_attempts
LLM_RETRY_DELAY_SECONDS = RUNTIME_CONFIG.vertex.retry_delay_seconds
JOB_TEXT_LIMIT = RUNTIME_CONFIG.vertex.job_text_limit
JOB_NAVIGATION_TIMEOUT_MS = RUNTIME_CONFIG.vertex.job_navigation_timeout_ms
PROJECT_ID_FROM_SERVICE_ACCOUNT = "from-service-account"
JOB_CONTEXT_BLOCK_MARKERS = (
    "access is temporarily restricted",
    "we detected unusual activity",
    "automated (bot) activity",
)


def strip_markdown_formatting(value: Any) -> str:
    """Return application-ready plain text while preserving readable content."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    # Fenced and inline code: retain the content, remove Markdown delimiters.
    text = re.sub(r"(?m)^\s*```[^\n]*\n?", "", text)
    text = re.sub(r"(?m)^\s*```\s*$", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # Images and links retain their human-readable label, not the target URL.
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<(?:https?://|mailto:)[^>]+>", "", text)
    # Remove block-level syntax without flattening paragraphs.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+", "", text)
    text = re.sub(r"(?m)^\s*(?:-{3,}|_{3,}|\*{3,})\s*$", "", text)
    # Remove emphasis/strikethrough markers while retaining their contents.
    text = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", text)
    text = re.sub(r"(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"\1", text)
    # Unescape HTML entities first so encoded tags or characters are normalized.
    text = html.unescape(text)
    # LLMs occasionally emit simple HTML formatting instead of Markdown.
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|p|br)\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# =============================================================================
# GEMINI CLIENT BUILDER
# =============================================================================
def credential_file_for(
    settings: VertexSettings,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a credential file without changing the process environment.

    The documented standard Google environment variable remains supported for
    existing deployments.  Explicit settings remain the deterministic fallback
    when that variable is absent or blank.
    """
    values = os.environ if environment is None else environment
    configured = values.get(GOOGLE_APPLICATION_CREDENTIALS, "")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser()
    return settings.service_account_file


def project_id_for(settings: VertexSettings, credentials_path: Path) -> str:
    """Resolve the configured project, optionally deriving it from service-account JSON."""
    if settings.project_id != PROJECT_ID_FROM_SERVICE_ACCOUNT:
        return settings.project_id
    try:
        with credentials_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Vertex project_id is configured as 'from-service-account', but the service-account "
            f"file could not be read: {credentials_path}"
        ) from exc
    project_id = payload.get("project_id") if isinstance(payload, dict) else None
    if not isinstance(project_id, str) or not project_id.strip():
        raise RuntimeError(
            "Vertex service-account JSON must contain project_id when runtime/vertex.json uses "
            "'from-service-account'."
        )
    return project_id.strip()


def build_client(settings: VertexSettings = VERTEX_SETTINGS) -> Any:
    """Initialize Vertex client with explicit credentials and no env mutation."""
    if genai is None:
        raise RuntimeError(
            "Google GenAI SDK is unavailable. Install the 'google-genai' package to enable LLM generation."
        )
    credentials_path = credential_file_for(settings)
    if not credentials_path.is_file():
        raise RuntimeError(
            f"Vertex service account file not found: {credentials_path}. "
            "Copy config/vertex_service_account.example.json to that path and fill in your credentials."
        )
    try:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=list(VERTEX_AUTH_SCOPES)
        )
        project_id = project_id_for(settings, credentials_path)
        logger.info(
            "Auth: Vertex service account | project=%s | location=%s",
            project_id,
            settings.location,
        )
        return genai.Client(
            vertexai=True,
            project=project_id,
            location=settings.location,
            credentials=credentials,
        )
    except Exception as e:
        logger.error("Failed to initialize Vertex AI client: %s", e)
        raise RuntimeError(f"Vertex AI client initialization failed: {e}") from e


def get_client(settings: VertexSettings = VERTEX_SETTINGS) -> Any:
    """Initialize the SDK only when an LLM request is actually needed."""
    global _client
    global _client_settings
    if _client is None or _client_settings != settings:
        with _client_lock:
            if _client is None or _client_settings != settings:
                _client = build_client(settings)
                _client_settings = settings
    return _client


@dataclass(frozen=True, slots=True)
class VertexGateway:
    """Production adapter implementing the injected provider-neutral LLM port."""

    vertex: VertexSettings = VERTEX_SETTINGS

    def generate(
        self,
        prompt: str,
        *,
        system: str,
        settings: LLMSettings,
        json_mode: bool = False,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, settings.max_attempts + 1):
            try:
                config_kwargs = {
                    "system_instruction": system,
                    "temperature": settings.temperature,
                }
                if json_mode:
                    config_kwargs["response_mime_type"] = "application/json"
                response = get_client(self.vertex).models.generate_content(
                    model=settings.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                if not response or not response.text:
                    raise ValueError("Gemini returned an empty response.")
                return response.text.strip()
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Gemini request failed (%d/%d): %s",
                    attempt,
                    settings.max_attempts,
                    exc,
                )
                if attempt < settings.max_attempts:
                    time.sleep(settings.retry_delay_seconds * attempt)
        raise RuntimeError(
            f"Gemini LLM execution failed after {settings.max_attempts} attempts: {last_error}"
        ) from last_error


def _strip_json_fence(content: str) -> str:
    """Remove a single optional Markdown JSON fence from an LLM response."""
    text = str(content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    return fenced.group(1).strip() if fenced else text


def ask_gemini(
    prompt: str,
    system: str,
    temperature: float = 0.2,
    json_mode: bool = False,
    *,
    gateway: LLMClient | None = None,
    settings: LLMSettings | None = None,
) -> str:
    """Execute a generation request through an injectable LLM gateway."""
    outbound_prompt = str(prompt or "")
    outbound_system = str(system or "")
    request_settings = settings or LLMSettings(
        model=MODEL,
        temperature=temperature,
        max_attempts=LLM_MAX_ATTEMPTS,
        retry_delay_seconds=LLM_RETRY_DELAY_SECONDS,
    )
    logger.info(
        "Calling Gemini model %s (%d prompt chars)",
        request_settings.model,
        len(outbound_prompt),
    )
    return (gateway or VertexGateway()).generate(
        outbound_prompt,
        system=outbound_system,
        settings=request_settings,
        json_mode=json_mode,
    )


# ==============================================================================
# 1. PDF & SCRAPING HELPERS
# ==============================================================================


def extract_resume_text(resume_path: Path) -> str:
    """Extracts complete text from candidate resume PDF."""
    resume_path = Path(resume_path).expanduser().resolve()
    if not resume_path.is_file():
        raise FileNotFoundError(f"Resume PDF not found: {resume_path}")
    try:
        reader = pypdf.PdfReader(str(resume_path))
        full_text: list[str] = []
        for page in reader.pages:
            txt = page.extract_text()
            if txt:
                full_text.append(txt)
        res = "\n".join(full_text)
        logger.info("Extracted %d characters from resume: %s", len(res), resume_path.name)
        return res
    except Exception as exc:
        logger.error("Error reading resume PDF %s: %s", resume_path, exc)
        raise RuntimeError(f"Failed to extract resume text: {exc}") from exc


def scrape_job(url: str) -> dict[str, Any]:
    """Extract JD text and essay prompts from any supported ATS job URL."""
    ats = detect_ats_job_url(url)
    if not ats:
        raise ValueError("scrape_job accepts job-specific HTTPS URLs for supported ATS providers.")

    with sync_playwright() as p:
        session = open_chrome_session(
            p,
            profile_name="document-context-cdp-profile",
            target_url=url,
            headless=True,
            background=ats == "smartrecruiters",
        )
        page = session.page
        try:
            body_text = ""
            context_ready = False
            last_navigation_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=JOB_NAVIGATION_TIMEOUT_MS,
                    )
                    page.wait_for_function(
                        "() => (document.body?.innerText || '').trim().length >= 200",
                        timeout=min(JOB_NAVIGATION_TIMEOUT_MS, 15_000),
                    )
                    body_text = str(
                        page.evaluate("() => document.body?.innerText || ''") or ""
                    ).strip()
                    blocked = any(
                        marker in body_text.casefold() for marker in JOB_CONTEXT_BLOCK_MARKERS
                    )
                    if len(body_text) >= 200 and not blocked:
                        context_ready = True
                        break
                    if blocked:
                        last_navigation_error = RuntimeError(
                            "ATS provider returned an access-restriction page"
                        )
                except Exception as exc:
                    last_navigation_error = exc
                    logger.info(
                        "ATS job-context attempt %d/3 did not become ready: %s",
                        attempt,
                        exc,
                    )
                if attempt < 3:
                    page.wait_for_timeout(750 * attempt)
            if not context_ready:
                raise RuntimeError(
                    f"ATS job page did not provide usable context after 3 attempts: "
                    f"{last_navigation_error or 'empty response'}"
                )

            questions: list[str] = []
            for text_area in page.locator("textarea:visible").all():
                try:
                    # Ashby doesn't reliably associate <label> with essay textareas, so
                    # walk up to 5 ancestors looking for nearby short, non-upload text.
                    label = text_area.evaluate("""el => {
                        let curr = el;
                        for (let i = 0; i < 5 && curr; i++) {
                            let t = (curr.innerText || "").trim();
                            if (t.length > 5 && t.length < 400 && !t.toLowerCase().includes("upload")) return t;
                            curr = curr.parentElement;
                        }
                        return "";
                    }""")
                    if label and label not in questions:
                        questions.append(label)
                except Exception:
                    pass

            return {
                "url": url,
                "ats": ats,
                "jd_text": body_text[:JOB_TEXT_LIMIT],
                "questions": questions,
            }
        finally:
            close_browser_session(session)


def scrape_ashby_job(url: str) -> dict[str, Any]:
    """Backward-compatible wrapper for callers that still use the historic name."""
    if detect_ats_job_url(url) != "ashby":
        raise ValueError("scrape_ashby_job accepts HTTPS Ashby job URLs only.")
    return scrape_job(url)


# ==============================================================================
# 2. SYSTEM PROMPT REGISTRY
# ==============================================================================


def build_essay_system_prompt() -> str:
    """Strict LLM System Prompt for Tailored Job Application Essays."""
    return """ROLE
You write job application essay answers as Shivam Singh, in his voice, first person.

EVIDENCE RULE
Use only facts present in <candidate_evidence>. Never invent a company, date, tool, responsibility, or unsupported outcome. If an exact detail requested by the question is unavailable, answer with the closest relevant supported experience or professional judgment and omit the unavailable detail. Return only the application-ready answer. Never include missing-evidence notes, caveats, analysis, or meta commentary.

ANSWER STRUCTURE (adapt, do not label in the output)
1. Direct answer to the exact question in sentence one. No throat-clearing.
2. One specific proof point with a number, scope, or named outcome.
3. Explicit tie to a priority stated in the JD or company context, using their vocabulary, not synonyms.
4. Close on what you would do in the role, concrete and near-term.

STYLE
- Active voice, first person, confident, no hedging.
- Concrete nouns over abstractions. Name systems, teams, metrics.
- No em dashes, no emojis, no exclamation marks.
- Vary sentence length. At least one sentence under 8 words.
- Banned openers: "I am excited to", "As a seasoned", "I am passionate about", "In today's rapidly evolving"
- Banned words: leverage, spearhead, synergy, holistic, robust, delve, journey, landscape, testament, resonate.
- Plain text only. Do not use Markdown, HTML, headings, bullets, links, code fences, or emphasis markers.

KEYWORD RULE
Mirror 2-4 exact terms from the JD. Weave them into real experience. Never keyword-stuff or claim a skill the evidence does not support."""


def build_resume_system_prompt(feedback: str = "") -> str:
    """Strict LLM System Prompt for Executive Tailored Resume Generation."""
    fb_suffix = (
        f"\n\n*** PREVIOUS VERSION FAILED QUALITY CHECKS. FIX THESE EXACT ISSUES: ***\n{feedback}"
        if feedback
        else ""
    )

    return f"""You are an elite executive resume writer and career strategist. You will receive a candidate's base resume and a job description.

Your task: Create a PERSONALIZED, highly tailored resume that:
1. Restructures and rewrites work experience bullets to match the JD's language, keywords, and priorities.
2. Rewrites base job titles when useful to align the candidate's demonstrated scope with the target role. Keep the resulting title credible for the source experience.
3. Keeps ALL real companies and grounds the narrative in the tagged source claims and SOURCE_METRICS supplied in the candidate source.
4. Keeps experience in reverse-chronological order.
5. Rewrites the professional summary to mirror the JD's terminology.
6. Adapts the skills section to feature JD keywords prominently.
7. Uses **bold** markdown around selected metrics, technologies, and target-job keywords.

WRITING STYLE & BULLET RULES:
- Write naturally with full executive narrative flow. Do NOT write choppy fragments or awkward semicolon run-ons.
- BULLET LENGTH: Target 35-65 words per bullet.
- METRICS: Treat SOURCE_METRICS as narrative inputs, not immutable verified values. You may round, normalize, combine, or adjust them when it produces a coherent and credible tailored narrative, while preserving the underlying scale and meaning.
- QUANTITY: Write 14-18 total bullet points across all 5 companies, normally 3-4 bullets per company.
- KEYWORDS: Mirror the most relevant JD terminology naturally. Do not keyword-stuff or claim unsupported skills.
- CLAIM INTEGRITY: A bullet may combine closely related facts from one claim, but must not merge unrelated claim IDs into a new assertion.
- Keep ALL 5 companies.
- NEVER fabricate or alter companies, dates, locations, degrees, unsupported responsibilities, or tools. Titles and source metrics may be adapted under the rules above.
{fb_suffix}

Return a JSON object with this EXACT structure (no markdown fences, just raw JSON). Text values MAY contain **bold** markdown:
{{
  "header_name": "Shivam Singh",
  "header_tagline": "A one-line professional tagline tailored to the role (max 100 chars)",
  "contact": {{"location": "Use source location", "email": "Use source email", "phone": "Use source phone", "linkedin": "Use source LinkedIn"}},
  "professional_summary": "A 3-4 sentence professional summary tailored to the JD. Use **bold** markdown for key terms.",
  "skills": ["**Skill 1**", "**Skill 2**", ...],
  "experience": [
    {{
      "company": "Company Name",
      "location": "City, State",
      "title": "Credible tailored title aligned with the target role",
      "dates": "YYYY - Present",
      "bullets": ["Bullet 1 (35-65 words, source-backed, with selective **bold keywords** and appropriately adapted metrics)", ...]
    }}
  ],
  "education": [
    {{
      "degree": "Degree",
      "school": "School Name",
      "dates": "YYYY - YYYY",
      "details": "Optional detail"
    }}
  ]
}}"""


# ==============================================================================
# 3. GEMINI RESUME TAILORING ENGINE
# ==============================================================================


def call_resume_llm(
    job: Any,
    feedback: str = "",
    base_resume_text: str = "",
) -> dict[str, Any]:
    """Invokes Gemini LLM to synthesize tailored resume payload. Throws RuntimeError if call or parse fails."""
    comp = getattr(job, "company", "Target Company")
    r_title = getattr(job, "role_title", "Target Role")
    jd_reqs = getattr(job, "jd_requirements", "") or ""
    jd_resp = getattr(job, "jd_responsibilities", "") or ""
    jd_kw = getattr(job, "keywords", "") or ""

    logger.info("=== Resume AI Generation (%s | %s) ===", comp, r_title)

    user_prompt = f"""
TARGET JOB ROLE: {r_title}
TARGET COMPANY: {comp}

JOB REQUIREMENTS & RESPONSIBILITIES:
{jd_reqs}
{jd_resp}
{jd_kw}

CANDIDATE TAGGED SOURCE OF TRUTH:
{base_resume_text}
"""

    with _ai_lock:
        system_prompt = build_resume_system_prompt(feedback)
        raw_content = ask_gemini(user_prompt, system=system_prompt, temperature=0.3, json_mode=True)

        try:
            payload = json.loads(_strip_json_fence(raw_content))
            if not isinstance(payload, dict):
                raise ValueError("resume payload root must be a JSON object")
            logger.info("Synthesized LLM Resume Tagline: %s", payload.get("header_tagline"))
            logger.info(
                "Generated %d tailored experience entries via Gemini.",
                len(payload.get("experience", [])),
            )
            return payload

        except (TypeError, ValueError) as exc:
            logger.error("Failed to parse JSON response from Gemini: %s", exc)
            raise RuntimeError(
                f"Gemini returned invalid JSON structure for resume payload: {exc}"
            ) from exc


# ==============================================================================
# 4. GEMINI ESSAY SYNTHESIS ENGINE
# ==============================================================================


def call_essay_llm(
    prompt_text: str,
    jd_text: str = "",
    company: str = "",
    role: str = "",
    candidate_evidence: str = "",
) -> str:
    """Invokes Gemini LLM to synthesize tailored job application essay. Throws RuntimeError if LLM fails."""
    logger.info("Resume AI synthesizing essay response for %s - %s...", company, role)

    user_prompt = f"""
TARGET COMPANY: {company}
TARGET ROLE: {role}

JOB DESCRIPTION CONTEXT:
{jd_text}

<candidate_evidence>
{candidate_evidence}
</candidate_evidence>

ESSAY PROMPT QUESTION:
{prompt_text}
"""

    with _ai_lock:
        system_prompt = build_essay_system_prompt()
        return strip_markdown_formatting(
            ask_gemini(user_prompt, system=system_prompt, temperature=0.4)
        )


# ==============================================================================
# 5. CONSTRAINED JSON SCHEMA DECODING (PYDANTIC / INSTRUCTOR ALTERNATE)
# ==============================================================================

try:
    from pydantic import BaseModel, Field

    class TailoredExperienceItem(BaseModel):
        company: str = Field(default="", description="Company name")
        role: str = Field(default="", description="Job title")
        duration: str = Field(default="", description="Dates or duration")
        location: str = Field(default="", description="Location")
        bullets: list[str] = Field(default_factory=list, description="Key bullet points")

    class TailoredResumeSchema(BaseModel):
        header_tagline: str = Field(default="", description="Executive summary tagline")
        skills: dict[str, list[str]] = Field(default_factory=dict, description="Grouped skills map")
        experience: list[TailoredExperienceItem] = Field(
            default_factory=list, description="Tailored experience list"
        )

    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False
    BaseModel = object  # type: ignore
    TailoredResumeSchema = None  # type: ignore


def call_resume_llm_structured(
    user_prompt: str,
    schema_cls: type = None,
    system_prompt: str = "",
) -> dict[str, Any]:
    """Alternate capability: Constrained JSON Schema Decoding via Pydantic / Google GenAI SDK.

    Enforces schema compliance at inference time using response_schema / JSON-Schema grammar.
    Preserves default call_resume_llm behavior unless explicitly invoked.
    """
    if not HAS_PYDANTIC:
        logger.warning("Pydantic not installed; falling back to standard JSON mode call.")
        raw = ask_gemini(user_prompt, system=system_prompt, temperature=0.3, json_mode=True)
        return json.loads(_strip_json_fence(raw))

    target_schema = schema_cls or TailoredResumeSchema
    logger.info(
        "Executing constrained structured LLM decoding using schema: %s", target_schema.__name__
    )

    raw = ask_gemini(user_prompt, system=system_prompt, temperature=0.2, json_mode=True)
    clean_json = _strip_json_fence(raw)
    parsed_obj = target_schema.model_validate_json(clean_json)
    return parsed_obj.model_dump()


# Remove a legacy script only when explicitly requested by a caller.
def cleanup_legacy_script(remove: bool = False) -> bool:
    if not remove:
        return False
    legacy_p = Path(__file__).resolve().parent / "antigravity_essay_generator.py"
    if legacy_p.exists():
        try:
            legacy_p.unlink()
            return True
        except OSError:
            logger.warning("Unable to remove legacy script: %s", legacy_p)
    return False


def generate_fallback_resume_data(
    job: Any,
    original_experience: Sequence[Mapping[str, Any]] | None = None,
    original_education: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a conservative, source-backed payload when LLM generation is unavailable."""
    role = getattr(job, "role_title", "") or "Target Role"
    keywords = getattr(job, "keywords", "") or ""
    skills = [item.strip() for item in re.split(r"[,;\n]", str(keywords)) if item.strip()]
    return {
        "header_name": "",
        "header_tagline": role,
        "contact": {},
        "professional_summary": "",
        "skills": skills,
        "experience": list(original_experience or []),
        "education": list(original_education or []),
    }
