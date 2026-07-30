#!/usr/bin/env python3
"""
AI-Personalized Resume Generator v3.1 — Executive Hybrid (Natural Flow Edition)
================================================================================
Uses Google GenAI to tailor resume content to job descriptions,
renders a high-density executive PDF via ReportLab, and scores/retries using PyMuPDF.

Key Features:
  - Natural LLM Flow (No robotic sentence-stitching or semicolon merging)
  - Metric Density Scoring Engine (scans content lines for % / $ / numbers / scale)
  - Higher Content Capacity (0.38" L/R, 0.26" T/B margins; target 14-18 bullets)
  - AI-Native Text Bolding (LLM **bold** markdown merged with keyword extraction)

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. Headless Typst / LaTeX Binary Compilation Engine:
   - Replace complex procedural Python ReportLab drawing code (which requires manual spacing math and flowable table management) with Typst (`typst compile input.typ output.pdf`).
   - Typst is a state-of-the-art document compilation engine that compiles in <30ms, supports pixel-perfect CSS/grid styling, and naturally formats single-page executive resumes.
   - Benefit: 10x faster PDF compilation, superior typography layout engine, zero ReportLab canvas math bugs.

2. Web-Standard HTML/Tailwind to PDF rendering (WeasyPrint / Playwright PDF export):
   - Render tailored resumes as semantic HTML templates styled with Tailwind CSS, then export to PDF using headless Playwright (`page.pdf()`).
   - Benefit: Visual fidelity is identical to web view; easy live browser preview and web inspection.

3. Automated ATS Keyword Matcher & Single-Page Constraints Visual Feedback Loop:
   - Integrate an automated ATS parsing engine (e.g. PyResparser / Sovren / ResumeParser) locally to test parsed keyword match score before rendering PDF.
   - If page count exceeds 1.0 pages, employ a binary search optimizer on font size and line spacing parameters automatically.
==============================================================================
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from xml.sax.saxutils import escape as xml_escape

import fitz  # PyMuPDF remains a compatible module-level scorer patch point.

from ..core.paths import OUTPUT_DIR as PROJECT_OUTPUT_DIR
from ..core.paths import SRC_DIR
from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from .ai_client import call_resume_llm, generate_fallback_resume_data, scrape_job
from .cache import ResumeCache, cache_key
from .rendering import CallableResumeRenderer, ResumeRenderer, render_resume
from .scoring import policy_from_source, score_pdf
from .source import (
    ResumeSource,
    load_resume_source,
)
from .validation import (
    build_quality_feedback,
    company_matches,
    enforce_candidate_identity,
    enforce_source_invariants,
    ensure_minimum_bullets,
    normalize_experience,
    repair_missing_experience,
    restore_source_education,
    validate_resume_data,
)

# ---- Config ----
logger = logging.getLogger("ResumeGenerator")
WORKDIR = SRC_DIR
FONT_DIRS = [
    WORKDIR / "fonts",
    Path("/usr/share/fonts/truetype/english"),
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
]


def _find_font(*names: str) -> Optional[Path]:
    for directory in FONT_DIRS:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


FONT_REGULAR = _find_font("Carlito-Regular.ttf", "calibri.ttf")
FONT_BOLD = _find_font("Carlito-Bold.ttf", "calibrib.ttf")
FONT_ITALIC = _find_font("Carlito-Italic.ttf", "calibrii.ttf")
FONT_BI = _find_font("Carlito-BoldItalic.ttf", "calibriz.ttf")
OUTPUT_DIR = PROJECT_OUTPUT_DIR

# The ignored source material is deliberately loaded only when a caller starts
# a generation workflow. Importing this module must be safe on fresh clones
# and in tests that do not have candidate data.
BASE_RESUME_PATH = resolve_runtime_path(RUNTIME_CONFIG.application["resume_source_file"])
RESUME_CACHE_FILE = resolve_runtime_path(RUNTIME_CONFIG.resume["cache_file"])
BASE_RESUME_TEXT = ""

# ---- Original Resume Baseline Metrics ----
# A submitted one-page resume is compared with a candidate-specific baseline
# supplied through the runtime configuration, not the larger master experience
# bank stored in the resume source.
ORIG_CHAR_COUNT = int(RUNTIME_CONFIG.resume["original_character_count"])
ORIG_PAGE_HEIGHT = float(RUNTIME_CONFIG.resume["original_page_height"])

_resume_source: ResumeSource | None = None
ORIGINAL_EXPERIENCE: list[dict[str, Any]] = []
ORIGINAL_EDUCATION: list[dict[str, str]] = []
ORIGINAL_COMPANIES: list[str] = []
SOURCE_CANDIDATE: dict[str, str] = {}


def _ensure_resume_source() -> ResumeSource:
    """Load tagged candidate data lazily and retain legacy module globals."""
    global BASE_RESUME_TEXT
    global ORIGINAL_COMPANIES
    global ORIGINAL_EDUCATION
    global ORIGINAL_EXPERIENCE
    global SOURCE_CANDIDATE
    global _resume_source

    if _resume_source is None:
        _resume_source = load_resume_source(BASE_RESUME_PATH)
        BASE_RESUME_TEXT = _resume_source.text
        ORIGINAL_EXPERIENCE = [dict(entry) for entry in _resume_source.experience]
        ORIGINAL_EDUCATION = [dict(entry) for entry in _resume_source.education]
        ORIGINAL_COMPANIES = list(_resume_source.companies)
        SOURCE_CANDIDATE = dict(_resume_source.candidate)
    return _resume_source


@dataclass
class JobInfo:
    company: str
    role_title: str
    keywords: str
    jd_overview: str
    jd_responsibilities: str
    jd_requirements: str
    url: str = ""
    location: str = ""
    compensation: str = ""


# ---- Concurrency & Rate Limiting ----
_ai_lock = threading.Lock()
_llm_lock = threading.Lock()
_cache_lock = threading.Lock()
_last_llm_call = 0.0
LLM_MIN_INTERVAL = float(RUNTIME_CONFIG.resume["llm_min_interval_seconds"])
MAX_RETRIES = int(RUNTIME_CONFIG.resume["max_retries"])
MIN_SCORE = int(RUNTIME_CONFIG.resume["minimum_score"])
MIN_TOTAL_BULLETS = int(RUNTIME_CONFIG.resume["minimum_total_bullets"])
_persistent_cache_override = os.environ.get("RESUME_PERSIST_CACHE", "").strip()
PERSISTENT_CACHE_ENABLED = (
    _persistent_cache_override.lower() in {"1", "true", "yes"}
    if _persistent_cache_override
    else bool(RUNTIME_CONFIG.resume["persistent_cache_enabled"])
)


# ---- Caching ----
_llm_cache: Dict[str, dict] = {}
_resume_cache = ResumeCache(_llm_cache, lock=_cache_lock)


def _cache_key(job: JobInfo) -> str:
    """Backward-compatible facade for the reusable cache-key contract."""
    return cache_key(job)


def _get_cached(job: JobInfo) -> Optional[dict]:
    return _resume_cache.get(job)


def _set_cached(job: JobInfo, data: Mapping[str, Any]) -> None:
    _resume_cache.set(job, data)


def _load_disk_cache() -> None:
    if not PERSISTENT_CACHE_ENABLED:
        return
    cf = RESUME_CACHE_FILE
    if cf.exists():
        try:
            count = _resume_cache.load(cf)
            print(f"[CACHE] Loaded {count} cached resumes")
        except (OSError, ValueError) as exc:
            logger.warning("Could not load resume cache %s: %s", cf, exc)


def _save_disk_cache() -> None:
    if not PERSISTENT_CACHE_ENABLED:
        return
    cf = RESUME_CACHE_FILE
    try:
        _resume_cache.save(cf)
    except OSError as exc:
        logger.warning("Could not persist resume cache %s: %s", cf, exc)


def _generate_fallback_resume_data(job: JobInfo) -> Dict[str, Any]:
    _ensure_resume_source()
    return generate_fallback_resume_data(job, ORIGINAL_EXPERIENCE, ORIGINAL_EDUCATION)


def _call_llm(job: JobInfo, feedback: str = "") -> Optional[Dict[str, Any]]:
    """Delegate the LLM call to the shared resume AI utilities."""
    _ensure_resume_source()
    return call_resume_llm(
        job=job,
        feedback=feedback,
        base_resume_text=BASE_RESUME_TEXT,
    )


# ============================================================================
# STEP 2: DATA NORMALIZATION & REPAIR (No Semicolon Sentence Stitching)
# ============================================================================


def _company_match(llm_name: str, orig_name: str) -> bool:
    """Backward-compatible facade for source-company matching."""
    return company_matches(llm_name, orig_name)


def _normalize_experience(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compatible facade for pure LLM-payload normalization."""
    normalize_experience(resume_data)
    return resume_data


def _enforce_candidate_identity(
    resume_data: Dict[str, Any],
    email_override: str = "",
) -> Dict[str, Any]:
    _ensure_resume_source()
    enforce_candidate_identity(resume_data, SOURCE_CANDIDATE, email_override)
    return resume_data


def _repair_experience(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """If LLM dropped companies, add them back using full original entries."""
    _ensure_resume_source()
    _, missing_names = repair_missing_experience(resume_data, ORIGINAL_EXPERIENCE)
    if missing_names:
        print(
            f"  [REPAIR] LLM dropped companies: {list(missing_names)}. Restoring original full entries."
        )
    return resume_data


def _enforce_source_invariants(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalize immutable employment facts and restore chronological order."""
    _ensure_resume_source()
    enforce_source_invariants(resume_data, ORIGINAL_EXPERIENCE)
    return resume_data


def _repair_education(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """Always restore immutable education facts from the tagged source."""
    _ensure_resume_source()
    restore_source_education(resume_data, ORIGINAL_EDUCATION)
    return resume_data


def _ensure_min_bullets(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensures minimum capacity (14+ bullets) by appending COMPLETE natural bullets
    from original career data if an experience entry is under-populated.
    Does NOT cut or stitch sentences programmatically.
    """
    _ensure_resume_source()
    _, initial_total = ensure_minimum_bullets(
        resume_data,
        ORIGINAL_EXPERIENCE,
        minimum_total=MIN_TOTAL_BULLETS,
    )
    if initial_total < MIN_TOTAL_BULLETS:
        print(f"  [CAPACITY] {initial_total} bullets found. Adding full natural fallback bullets.")
    return resume_data


def _validate_llm_data(resume_data: Mapping[str, Any]) -> List[str]:
    """Validate structure prior to rendering."""
    _ensure_resume_source()
    return validate_resume_data(resume_data, ORIGINAL_COMPANIES)


# ============================================================================
# STEP 2.5: KEYWORD & AI-NATIVE BOLDING PROCESSOR
# ============================================================================


def _build_keyword_set(job: JobInfo) -> Set[str]:
    """Build a keyword set from JD for programmatic highlighting."""
    keywords = set()
    if job.keywords:
        for kw in re.split(r"[,;|/]", job.keywords):
            kw = kw.strip().strip("'\"")
            if 1 < len(kw) < 60:
                keywords.add(kw)

    jd = f"{job.jd_overview} {job.jd_responsibilities} {job.jd_requirements}"

    for m in re.finditer(r'"([^"]{3,50})"', jd):
        keywords.add(m.group(1))

    for m in re.finditer(r"\b([A-Z]{2,6})\b", jd):
        keywords.add(m.group(1))

    for m in re.finditer(r"(?<!\w)([a-zA-Z]+[-/][a-zA-Z][\w-]{2,})(?!\w)", jd):
        t = m.group(1)
        if 4 < len(t) < 50:
            keywords.add(t)

    return keywords


def _bold_keywords_in_text(
    text: str,
    keywords: Optional[Set[str]],
    bold_metrics: bool = True,
) -> str:
    """
    Applies bolding while fully preserving LLM markdown/HTML <b> tags.
    Does not distort natural LLM sentence structure or HTML tags.
    """
    if not text:
        return ""

    # Escape untrusted text before adding the only ReportLab markup we allow.
    text = xml_escape(str(text))
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Split out already-bolded spans so keyword/metric patterns below only scan
    # plain text and never re-match (or nest tags) inside an existing <b>...</b>.
    tokens = re.split(r"(<b>.*?</b>)", text, flags=re.IGNORECASE | re.DOTALL)
    processed_tokens: List[str] = []

    patterns: List[re.Pattern[str]] = []
    if keywords:
        sorted_kws = sorted(keywords, key=len, reverse=True)
        for kw in sorted_kws:
            if len(kw) < 2:
                continue
            try:
                escaped = re.escape(kw)
                if " " not in kw and "/" not in kw and "-" not in kw:
                    patterns.append(re.compile(r"\b" + escaped + r"\b", re.IGNORECASE))
                else:
                    patterns.append(re.compile(escaped, re.IGNORECASE))
            except re.error:
                pass

    if bold_metrics:
        patterns.append(re.compile(r"\$[\d,.]+[KMBT]?\b"))
        patterns.append(re.compile(r"[+-]?\d+(?:\.\d+)?%"))
        patterns.append(re.compile(r"\b\d+\s*[\u2192\-]\s*\d+"))
        patterns.append(re.compile(r"\b\d+[KMB]\b"))

    for token in tokens:
        if token.lower().startswith("<b>") and token.lower().endswith("</b>"):
            processed_tokens.append(token)
        else:
            safe_token = token
            if not patterns:
                processed_tokens.append(safe_token)
                continue

            matches = []
            for pat in patterns:
                for m in pat.finditer(safe_token):
                    matches.append((m.start(), m.end()))

            if not matches:
                processed_tokens.append(safe_token)
                continue

            matches.sort()
            merged = []
            for start, end in matches:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))

            res = []
            last = 0
            for start, end in merged:
                if start > last:
                    res.append(safe_token[last:start])
                res.append(f"<b>{safe_token[start:end]}</b>")
                last = end
            if last < len(safe_token):
                res.append(safe_token[last:])
            processed_tokens.append("".join(res))

    return "".join(processed_tokens)


# ============================================================================
# STEP 3: PDF RENDERING
# ============================================================================


def _render_pdf_with_reportlab(
    resume: Mapping[str, Any],
    output_path: Path,
    bold_keywords: Optional[Set[str]] = None,
) -> bool:
    """Render executive PDF with Carlito typography."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Register fonts safely
    font_normal = "Helvetica"
    font_bold = "Helvetica-Bold"
    font_italic = "Helvetica-Oblique"

    if FONT_REGULAR and FONT_BOLD and FONT_ITALIC and FONT_BI:
        try:
            pdfmetrics.registerFont(TTFont("Carlito", FONT_REGULAR))
            pdfmetrics.registerFont(TTFont("Carlito-Bold", FONT_BOLD))
            pdfmetrics.registerFont(TTFont("Carlito-Italic", FONT_ITALIC))
            pdfmetrics.registerFont(TTFont("Carlito-BoldItalic", FONT_BI))
            pdfmetrics.registerFontFamily(
                "Carlito",
                normal="Carlito",
                bold="Carlito-Bold",
                italic="Carlito-Italic",
                boldItalic="Carlito-BoldItalic",
            )
            font_normal = "Carlito"
            font_bold = "Carlito-Bold"
            font_italic = "Carlito-Italic"
        except Exception:
            pass

    DARK = HexColor("#1a1a2e")
    ACCENT = HexColor("#16213e")
    GRAY = HexColor("#555555")
    LIGHT_GRAY = HexColor("#888888")
    DIVIDER = HexColor("#2c3e6b")

    # Executive Margins (0.38" L/R, 0.28" T/B)
    MARGIN_L = 0.38 * inch
    MARGIN_R = 0.38 * inch
    MARGIN_T = 0.28 * inch
    MARGIN_B = 0.28 * inch

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=MARGIN_L,
        rightMargin=MARGIN_R,
        topMargin=MARGIN_T,
        bottomMargin=MARGIN_B,
    )

    usable_width = letter[0] - MARGIN_L - MARGIN_R

    s_name = ParagraphStyle(
        "Name", fontName=font_bold, fontSize=15, leading=17, textColor=DARK, spaceAfter=0
    )
    s_tagline = ParagraphStyle(
        "Tagline", fontName=font_italic, fontSize=9, leading=10.5, textColor=ACCENT, spaceAfter=1
    )
    s_contact = ParagraphStyle(
        "Contact",
        fontName=font_normal,
        fontSize=7.8,
        leading=9.5,
        textColor=GRAY,
        alignment=TA_CENTER,
        spaceAfter=2,
    )
    s_section = ParagraphStyle(
        "Section",
        fontName=font_bold,
        fontSize=9.5,
        leading=11.5,
        textColor=ACCENT,
        spaceBefore=3,
        spaceAfter=1,
    )
    s_summary = ParagraphStyle(
        "Summary",
        fontName=font_normal,
        fontSize=8.4,
        leading=10.4,
        textColor=DARK,
        spaceAfter=1,
        alignment=TA_JUSTIFY,
    )
    s_bullet = ParagraphStyle(
        "Bullet",
        fontName=font_normal,
        fontSize=8.1,
        leading=9.8,
        textColor=DARK,
        leftIndent=5,
        spaceAfter=0.6,
        alignment=TA_JUSTIFY,
    )
    s_company = ParagraphStyle(
        "Company", fontName=font_bold, fontSize=8.8, leading=10.2, textColor=DARK, spaceAfter=0
    )
    s_title = ParagraphStyle(
        "Title", fontName=font_italic, fontSize=8.2, leading=9.8, textColor=GRAY, spaceAfter=0
    )
    s_dates = ParagraphStyle(
        "Dates",
        fontName=font_normal,
        fontSize=7.8,
        leading=9.8,
        textColor=LIGHT_GRAY,
        alignment=TA_RIGHT,
        spaceAfter=0,
    )
    s_edu = ParagraphStyle(
        "Edu", fontName=font_normal, fontSize=8.2, leading=9.8, textColor=DARK, spaceAfter=0.5
    )
    s_edu_detail = ParagraphStyle(
        "EduDetail", fontName=font_italic, fontSize=7.2, leading=8.5, textColor=GRAY
    )

    elements = []

    # ---- HEADER ----
    tagline = resume.get("header_tagline", "")
    elements.append(Paragraph(xml_escape(str(resume.get("header_name", ""))), s_name))
    if tagline:
        elements.append(
            Paragraph(
                _bold_keywords_in_text(str(tagline), bold_keywords, bold_metrics=False), s_tagline
            )
        )

    contact = resume.get("contact", {})
    if not isinstance(contact, Mapping):
        contact = {}
    parts = []
    if contact.get("location"):
        parts.append(xml_escape(str(contact["location"])))
    if contact.get("email"):
        parts.append(xml_escape(str(contact["email"])))
    if contact.get("phone"):
        parts.append(xml_escape(str(contact["phone"])))
    if contact.get("linkedin"):
        parts.append(xml_escape(str(contact["linkedin"])))
    elements.append(Paragraph(" | ".join(parts), s_contact))
    elements.append(
        HRFlowable(width="100%", thickness=1.0, color=DIVIDER, spaceAfter=1.5, spaceBefore=1)
    )

    # ---- PROFESSIONAL SUMMARY ----
    elements.append(Paragraph("PROFESSIONAL SUMMARY", s_section))
    summary = resume.get("professional_summary", "")
    summary = _bold_keywords_in_text(summary, bold_keywords)
    elements.append(Paragraph(summary, s_summary))
    elements.append(
        HRFlowable(width="100%", thickness=0.4, color=DIVIDER, spaceAfter=1, spaceBefore=1)
    )

    # ---- CORE COMPETENCIES ----
    skills = resume.get("skills", [])
    if not isinstance(skills, list):
        skills = []
    if skills:
        elements.append(Paragraph("CORE COMPETENCIES", s_section))
        skills_text = "  \u00b7  ".join(str(skill) for skill in skills)
        skills_text = _bold_keywords_in_text(skills_text, bold_keywords, bold_metrics=False)
        s_skills_flow = ParagraphStyle(
            "SkillsFlow",
            fontName=font_normal,
            fontSize=7.5,
            leading=9.2,
            textColor=DARK,
            spaceAfter=1,
        )
        elements.append(Paragraph(skills_text, s_skills_flow))
        elements.append(
            HRFlowable(width="100%", thickness=0.4, color=DIVIDER, spaceAfter=1, spaceBefore=1)
        )

    # ---- EDUCATION ----
    edu_list = resume.get("education", [])
    if not isinstance(edu_list, list):
        edu_list = []
    if edu_list:
        elements.append(Paragraph("EDUCATION", s_section))
        for edu in edu_list:
            degree = xml_escape(str(edu.get("degree", "")))
            school = xml_escape(str(edu.get("school", "")))
            dates = xml_escape(str(edu.get("dates", "")))
            details = xml_escape(str(edu.get("details", "")))
            line = f"<b>{school}</b>  \u2014  {degree}"
            if dates:
                line += f"  |  {dates}"
            elements.append(Paragraph(line, s_edu))
            if details:
                elements.append(Paragraph(details, s_edu_detail))
        elements.append(
            HRFlowable(width="100%", thickness=0.4, color=DIVIDER, spaceAfter=1, spaceBefore=1)
        )

    # ---- PROFESSIONAL EXPERIENCE ----
    elements.append(Paragraph("PROFESSIONAL EXPERIENCE", s_section))
    experience = resume.get("experience", [])
    if not isinstance(experience, list):
        experience = []
    for exp in experience:
        if not isinstance(exp, Mapping):
            continue
        company = xml_escape(str(exp.get("company", "")))
        title = xml_escape(str(exp.get("title", "")))
        dates = xml_escape(str(exp.get("dates", "")))
        loc = xml_escape(str(exp.get("location", "")))
        bullets = exp.get("bullets", [])
        if not isinstance(bullets, list):
            bullets = []

        company_location = f"  —  {loc}" if loc else ""
        company_text = f"<b>{company}</b>{company_location}"
        col_data = [[Paragraph(company_text, s_company), Paragraph(dates, s_dates)]]
        col_table = Table(col_data, colWidths=[usable_width * 0.74, usable_width * 0.26])
        col_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        elements.append(col_table)
        if title:
            elements.append(Paragraph(f"<i>{title}</i>", s_title))

        for b in bullets:
            final_b = _bold_keywords_in_text(str(b), bold_keywords)
            elements.append(Paragraph(f"\u2022  {final_b}", s_bullet))

        elements.append(Spacer(1, 1.5))

    try:
        doc.build(elements)
        return True
    except Exception as e:
        print(f"  [PDF] Render error: {e}", flush=True)
        traceback.print_exc()
        return False


_default_pdf_renderer = CallableResumeRenderer(_render_pdf_with_reportlab)


def render_pdf(
    resume: Mapping[str, Any],
    output_path: Path,
    bold_keywords: Optional[Set[str]] = None,
    *,
    renderer: ResumeRenderer | None = None,
) -> bool:
    """Render a resume through an injectable renderer with legacy defaults."""
    return render_resume(
        renderer or _default_pdf_renderer,
        resume,
        Path(output_path),
        bold_keywords,
    )


# ============================================================================
# STEP 4: QUALITY SCORING (Metric Density Engine + Flow Verification)
# ============================================================================


def _score_pdf(pdf_path: str) -> Tuple[int, List[str]]:
    """Backward-compatible facade for the reusable PDF scoring policy."""
    _ensure_resume_source()
    return score_pdf(
        pdf_path,
        policy_from_source(
            original_character_count=ORIG_CHAR_COUNT,
            page_height=ORIG_PAGE_HEIGHT,
            source_companies=ORIGINAL_COMPANIES,
        ),
        fitz_module=fitz,
    )


# ============================================================================
# STEP 5: ITERATIVE RETRY LOOP
# ============================================================================


def _build_feedback(issues: List[str]) -> str:
    """Convert scoring issues into actionable LLM prompt feedback."""
    _ensure_resume_source()
    return build_quality_feedback(issues, ORIGINAL_COMPANIES)


def _remove_file(path: Path) -> None:
    """Best-effort cleanup for an artifact owned by this generation attempt."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove temporary resume artifact %s: %s", path, exc)


def _generate_with_retries(
    job: JobInfo,
    output_path: Path,
    email_override: str = "",
) -> Optional[Path]:
    """Generate resume with iterative prompt quality loop."""
    feedback = ""
    best_score = 0
    best_data = None

    for attempt in range(1, MAX_RETRIES + 1):
        print(
            f"  [V3.1 Flow] Attempt {attempt}/{MAX_RETRIES} for {job.company} - {job.role_title}",
            flush=True,
        )

        global _last_llm_call
        with _llm_lock:
            elapsed = time.time() - _last_llm_call
            if elapsed < LLM_MIN_INTERVAL:
                time.sleep(LLM_MIN_INTERVAL - elapsed)
            _last_llm_call = time.time()

        try:
            resume_data = _call_llm(job, feedback)
        except Exception as exc:
            feedback = f"LLM request failed: {type(exc).__name__}. Return valid JSON using only supplied evidence."
            print(f"  [V3.1 Flow] LLM request failed: {exc}", flush=True)
            continue
        if not resume_data:
            feedback = (
                "Your JSON was empty or unparseable. Return valid JSON with all required keys."
            )
            continue

        resume_data = _enforce_candidate_identity(resume_data, email_override)
        resume_data = _normalize_experience(resume_data)
        resume_data = _repair_experience(resume_data)
        resume_data = _enforce_source_invariants(resume_data)
        resume_data = _repair_education(resume_data)
        resume_data = _ensure_min_bullets(resume_data)

        # Only a missing structural key aborts before rendering; content-quality
        # issues (thin skills, missing companies, low bullet count) are left for
        # _score_pdf below, which judges the actual rendered layout instead.
        data_issues = _validate_llm_data(resume_data)
        critical = [i for i in data_issues if "Missing required key" in i]
        if critical:
            feedback = "CRITICAL: " + "; ".join(critical)
            continue

        attempt_path = output_path.with_name(
            f".{output_path.stem}.attempt-{attempt}{output_path.suffix}"
        )
        _remove_file(attempt_path)
        ok = render_pdf(resume_data, attempt_path, _build_keyword_set(job))
        if not ok:
            _remove_file(attempt_path)
            feedback = "PDF render failed. Check your JSON structure."
            continue

        score, issues = _score_pdf(str(attempt_path))
        print(f"  [V3.1 Flow] Score: {score}/100 | Issues: {len(issues)}", flush=True)

        if score > best_score:
            best_score = score
            best_data = resume_data

        if score >= MIN_SCORE:
            print(
                f"  [V3.1 Flow] PASSED quality check (score {score}) | {output_path.name}",
                flush=True,
            )
            os.replace(attempt_path, output_path)
            _set_cached(job, resume_data)
            _save_disk_cache()
            return output_path

        feedback = _build_feedback(issues)
        print(f"  [V3.1 Flow] Feedback for retry: {feedback[:300]}", flush=True)
        _remove_file(attempt_path)

    print(
        f"  [V3.1 Flow] Max retries exhausted for {job.company}. Best score: {best_score}",
        flush=True,
    )
    if not best_data:
        print(
            f"  [V3.1 Flow] AI unavailable after {MAX_RETRIES} attempts. "
            "Using rule-based fallback.",
            flush=True,
        )
        fallback_data = _generate_fallback_resume_data(job)
        fallback_data = _enforce_candidate_identity(fallback_data, email_override)
        fallback_data = _normalize_experience(fallback_data)
        fallback_data = _repair_experience(fallback_data)
        fallback_data = _enforce_source_invariants(fallback_data)
        fallback_data = _repair_education(fallback_data)
        fallback_data = _ensure_min_bullets(fallback_data)
        best_data = fallback_data

    if best_data:
        best_path = output_path.with_name(f".{output_path.stem}.best{output_path.suffix}")
        _remove_file(best_path)
        if not render_pdf(best_data, best_path, _build_keyword_set(job)):
            _remove_file(best_path)
            return None
        os.replace(best_path, output_path)
        _set_cached(job, best_data)
        _save_disk_cache()
        return output_path

    return None


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================


def generate_personalized_resume(
    job: JobInfo,
    output_path: Path,
    email_override: str = "",
) -> Optional[Path]:
    """Full pipeline: AI -> normalize -> repair -> render -> score -> retry."""
    _ensure_resume_source()
    _load_disk_cache()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"  [V3.1 Flow] Generating tailored resume for {job.company} - {job.role_title}...",
        flush=True,
    )

    cached = _get_cached(job)
    if cached:
        print(f"  [V3.1 Flow] Cache hit for {job.company} - {job.role_title}", flush=True)
        cached = _enforce_candidate_identity(cached, email_override)
        cached_path = output_path.with_name(f".{output_path.stem}.cache{output_path.suffix}")
        _remove_file(cached_path)
        ok = render_pdf(cached, cached_path, _build_keyword_set(job))
        if ok:
            score, _ = _score_pdf(str(cached_path))
            if score >= MIN_SCORE:
                os.replace(cached_path, output_path)
                file_size = output_path.stat().st_size
                print(
                    f"  [V3.1 Flow] Cache PASSED (score {score}). {output_path.name} ({file_size // 1024}KB)",
                    flush=True,
                )
                return output_path
            else:
                _remove_file(cached_path)
                _resume_cache.discard(job)
        else:
            _remove_file(cached_path)

    return _generate_with_retries(job, output_path, email_override)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Executive Hybrid Resume Generator v3.1 (Natural Flow)"
    )
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--keywords", default="")
    parser.add_argument("--jd-overview", default="")
    parser.add_argument("--jd-resp", default="")
    parser.add_argument("--jd-req", default="")
    parser.add_argument(
        "--url",
        default="",
        help="Supported ATS job URL used to obtain context when JD text is not supplied",
    )
    parser.add_argument("--location", default="")
    parser.add_argument(
        "--email",
        default="",
        help="Override the source email in the generated resume for this run",
    )
    parser.add_argument("--output", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    if args.email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", args.email.strip()):
        print("Invalid --email value.", file=sys.stderr)
        return 2
    job = JobInfo(
        company=args.company,
        role_title=args.role,
        keywords=args.keywords,
        jd_overview=args.jd_overview,
        jd_responsibilities=args.jd_resp,
        jd_requirements=args.jd_req,
        url=args.url,
        location=args.location,
    )
    if args.url and not any((args.jd_overview, args.jd_resp, args.jd_req)):
        try:
            scraped = scrape_job(args.url)
            job.jd_overview = scraped.get("jd_text", "")
            print(
                f"[CONTEXT] Loaded {len(job.jd_overview)} JD characters "
                f"from {scraped.get('ats', 'ATS')}."
            )
        except Exception as exc:
            print(f"[CONTEXT] Could not load job context: {exc}", file=sys.stderr)

    output = args.output or str(OUTPUT_DIR / f"{args.company.replace(' ', '_')}_Resume.pdf")
    result = generate_personalized_resume(job, Path(output), args.email)
    if result:
        score, issues = _score_pdf(str(result))
        print(f"\nSUCCESS: {result}")
        print(f"Final score: {score}/100")
        if issues:
            print(f"Remaining issues: {issues}")
        return 0

    print(f"\nFAILED for {args.company}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
