#!/usr/bin/env python3
"""
Resume AI Utilities (Google GenAI Edition)
================================================================================
Universal LLM and document generation utilities for:
1. Resume Content Tailoring via Google GenAI SDK
2. Zero-Template Application Essay Synthesis via Google GenAI SDK
3. Resume Text Extraction & Ashby Job Scraping
"""

from __future__ import annotations

import json
import html
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pypdf
from playwright.sync_api import sync_playwright

from paths import CONFIG_DIR
from engine_shared import validate_ats_url

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

# =============================================================================
# LOGGING & HARDCODED CONFIGURATION
# =============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ResumeAIUtilities")

PROJECT_ID = "cent-capital-472820"
LOCATION = "global"
MODEL = "gemini-flash-latest"
SERVICE_ACCOUNT_FILE = CONFIG_DIR / "vertex_service_account.json"

# Serializes LLM calls; the orchestrator can run multiple ATS engines concurrently
# but Vertex AI quota and the shared client are not safe for parallel requests.
_ai_lock = threading.Lock()
_client_lock = threading.Lock()
_client = None
LLM_MAX_ATTEMPTS = 3
LLM_RETRY_DELAY_SECONDS = 2.0
CDP_ENDPOINT = "http://localhost:9222"
JOB_TEXT_LIMIT = 6_000
JOB_NAVIGATION_TIMEOUT_MS = 30_000


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
    # LLMs occasionally emit simple HTML formatting instead of Markdown.
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|p|br)\b[^>]*>", " ", text, flags=re.I)
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# =============================================================================
# GEMINI CLIENT BUILDER
# =============================================================================
def build_client() -> Any:
    """Initializes Vertex AI client using a local service account credentials file."""
    if genai is None:
        raise RuntimeError("Google GenAI SDK is unavailable. Install the 'google-genai' package to enable LLM generation.")
    if not SERVICE_ACCOUNT_FILE.is_file():
        raise RuntimeError(
            f"Vertex service account file not found: {SERVICE_ACCOUNT_FILE}. "
            "Copy config/vertex_service_account.example.json to that path and fill in your credentials."
        )
    try:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(SERVICE_ACCOUNT_FILE)
        logger.info("Auth: Vertex AI service account | project=%s | location=%s", PROJECT_ID, LOCATION)
        return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
    except Exception as e:
        logger.error("Failed to initialize Vertex AI client: %s", e)
        raise RuntimeError(f"Vertex AI client initialization failed: {e}") from e


def get_client() -> Any:
    """Initialize the SDK only when an LLM request is actually needed."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_client()
    return _client


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
) -> str:
    """Executes a text generation request using the Google GenAI SDK."""
    outbound_prompt = str(prompt or "")
    outbound_system = str(system or "")
    logger.info("Calling Gemini model %s (%d prompt chars)", MODEL, len(outbound_prompt))
    last_error: Optional[Exception] = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            config_kwargs = {
                "system_instruction": outbound_system,
                "temperature": temperature,
            }
            if json_mode:
                config_kwargs["response_mime_type"] = "application/json"

            resp = get_client().models.generate_content(
                model=MODEL,
                contents=outbound_prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            if not resp or not resp.text:
                raise ValueError("Gemini returned an empty response.")
            return resp.text.strip()
        except Exception as exc:
            last_error = exc
            logger.warning("Gemini request failed (%d/%d): %s", attempt, LLM_MAX_ATTEMPTS, exc)
            if attempt < LLM_MAX_ATTEMPTS:
                time.sleep(LLM_RETRY_DELAY_SECONDS * attempt)
    raise RuntimeError(f"Gemini LLM execution failed after {LLM_MAX_ATTEMPTS} attempts: {last_error}") from last_error


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
        full_text: List[str] = []
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


def scrape_ashby_job(url: str) -> Dict[str, Any]:
    """Extracts JD text and essay prompts directly from an Ashby application URL."""
    if not validate_ats_url(url, "ashby"):
        raise ValueError("scrape_ashby_job accepts HTTPS Ashby URLs only.")

    with sync_playwright() as p:
        owns_browser = False
        owns_context = False
        try:
            browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = browser.new_context()
                owns_context = True
        except Exception:
            logger.info("CDP session unavailable, launching headless Chromium instance...")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            owns_browser = True

        page = context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=JOB_NAVIGATION_TIMEOUT_MS)
            body_text = page.evaluate("() => document.body.innerText || ''")

            questions: List[str] = []
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
                "jd_text": body_text[:JOB_TEXT_LIMIT],
                "questions": questions,
            }
        finally:
            try:
                page.close()
            finally:
                # Do not close a browser owned by an existing CDP session.
                if owns_browser:
                    browser.close()
                elif owns_context:
                    context.close()


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
    fb_suffix = f"\n\n*** PREVIOUS VERSION FAILED QUALITY CHECKS. FIX THESE EXACT ISSUES: ***\n{feedback}" if feedback else ""

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
) -> Dict[str, Any]:
    """Invokes Gemini LLM to synthesize tailored resume payload. Throws RuntimeError if call or parse fails."""
    comp = getattr(job, 'company', 'Target Company')
    r_title = getattr(job, 'role_title', 'Target Role')
    jd_reqs = getattr(job, 'jd_requirements', '') or ''
    jd_resp = getattr(job, 'jd_responsibilities', '') or ''
    jd_kw = getattr(job, 'keywords', '') or ''

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
    original_experience: Optional[Sequence[Mapping[str, Any]]] = None,
    original_education: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
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
