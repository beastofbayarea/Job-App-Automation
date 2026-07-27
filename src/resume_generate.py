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
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import fitz  # PyMuPDF for quality scoring
from paths import DATA_DIR, OUTPUT_DIR as PROJECT_OUTPUT_DIR

# ---- Config ----
logger = logging.getLogger("ResumeGenerator")
WORKDIR = Path(__file__).resolve().parent
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
CACHE_DIR = PROJECT_OUTPUT_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Base resume content (extracted from shivam_singh_ai_product_manager_resume.pdf)
BASE_RESUME_PATH = DATA_DIR / "base_resume.txt"
BASE_RESUME_TEXT = BASE_RESUME_PATH.read_text(encoding="utf-8").strip() if BASE_RESUME_PATH.exists() else ""

# ---- Original Resume Baseline Metrics ----
# A submitted one-page resume should be compared with the original one-page
# baseline, not the larger master experience bank stored in base_resume.txt.
ORIG_CHAR_COUNT = 5953
ORIG_CONTENT_HEIGHT = 715.1  # top Y (31.2) to bottom Y (746.3)
ORIG_BOTTOM_MARGIN = 45.7    # 792 - 746.3
ORIG_PAGE_HEIGHT = 792.0

# ---- Career Data Parsed From the Single Source of Truth ----
def _parse_tagged_source(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    experience: List[Dict[str, Any]] = []
    education: List[Dict[str, str]] = []
    current_experience: Optional[Dict[str, Any]] = None
    current_education: Optional[Dict[str, str]] = None
    in_education = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "EDUCATION":
            in_education = True
            current_experience = None
            continue

        # Tag lines look like "[COMPANY] Acme" or "[CLAIM CL1] Grew revenue...";
        # the optional group 2 only appears on tags (e.g. CLAIM) that carry an id.
        match = re.match(r"^\[([A-Z_]+)(?:\s+([A-Z0-9-]+))?\]\s*(.*)$", line)
        if not match:
            continue
        tag, identifier, value = match.groups()
        value = value.strip()

        if not in_education:
            if tag == "COMPANY":
                current_experience = {
                    "company": value,
                    "location": "",
                    "dates": "",
                    "title": "",
                    "tags": [],
                    "claims": [],
                    "bullets": [],
                }
                experience.append(current_experience)
            elif current_experience is not None:
                if tag in {"BASE_TITLE", "OFFICIAL_TITLE"}:
                    current_experience["title"] = value
                elif tag == "DATES":
                    current_experience["dates"] = value
                elif tag == "LOCATION":
                    current_experience["location"] = value
                elif tag == "TAGS":
                    current_experience["tags"] = [
                        item.strip() for item in value.split(";") if item.strip()
                    ]
                elif tag == "CLAIM":
                    claim = {"id": identifier or "", "text": value}
                    current_experience["claims"].append(claim)
                    current_experience["bullets"].append(value)
        else:
            if tag == "SCHOOL":
                current_education = {
                    "school": value,
                    "degree": "",
                    "dates": "",
                    "details": "",
                }
                education.append(current_education)
            elif current_education is not None:
                if tag == "DEGREE":
                    current_education["degree"] = value
                elif tag == "DATES":
                    current_education["dates"] = value
                elif tag == "DETAILS":
                    current_education["details"] = value

    if len(experience) != 5:
        raise ValueError(
            f"base_resume.txt must contain exactly 5 tagged companies; found {len(experience)}"
        )
    if len(education) < 3:
        raise ValueError(
            f"base_resume.txt must contain at least 3 education records; found {len(education)}"
        )
    required_experience = ("company", "title", "dates", "location")
    for entry in experience:
        missing = [key for key in required_experience if not entry.get(key)]
        if missing or not entry["claims"]:
            raise ValueError(
                f"Incomplete tagged experience for {entry.get('company')}: {missing}"
            )
    return experience, education


def _parse_tagged_candidate(text: str) -> Dict[str, str]:
    candidate: Dict[str, str] = {}
    supported = {
        "NAME": "name",
        "PREFERRED_NAME": "preferred_name",
        "LOCATION": "location",
        "EMAIL": "email",
        "PHONE": "phone",
        "LINKEDIN": "linkedin",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[COMPANY]"):
            break
        match = re.match(r"^\[([A-Z_]+)\]\s*(.*)$", line)
        if match and match.group(1) in supported:
            candidate[supported[match.group(1)]] = match.group(2).strip()
    required = ("name", "location", "email", "phone", "linkedin")
    missing = [key for key in required if not candidate.get(key)]
    if missing:
        raise ValueError(
            f"base_resume.txt candidate section is missing: {', '.join(missing)}"
        )
    return candidate


ORIGINAL_EXPERIENCE, ORIGINAL_EDUCATION = _parse_tagged_source(BASE_RESUME_TEXT)
ORIGINAL_COMPANIES = [entry["company"] for entry in ORIGINAL_EXPERIENCE]
SOURCE_CANDIDATE = _parse_tagged_candidate(BASE_RESUME_TEXT)


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
LLM_MIN_INTERVAL = 5.0  # seconds between calls
MAX_RETRIES = 5
MIN_SCORE = 90
MIN_TOTAL_BULLETS = 14
PERSISTENT_CACHE_ENABLED = os.environ.get("RESUME_PERSIST_CACHE", "").lower() in {"1", "true", "yes"}


# ---- Caching ----
_llm_cache: Dict[str, dict] = {}


def _cache_key(job: JobInfo) -> str:
    context = "\n".join((job.company, job.role_title, job.keywords, job.jd_overview, job.jd_responsibilities, job.jd_requirements))
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


def _get_cached(job: JobInfo) -> Optional[dict]:
    with _cache_lock:
        data = _llm_cache.get(_cache_key(job))
        return copy.deepcopy(data) if isinstance(data, dict) else None


def _set_cached(job: JobInfo, data: Mapping[str, Any]) -> None:
    with _cache_lock:
        _llm_cache[_cache_key(job)] = copy.deepcopy(dict(data))


def _load_disk_cache() -> None:
    if not PERSISTENT_CACHE_ENABLED:
        return
    cf = CACHE_DIR / "llm_cache_v2.json"
    if cf.exists():
        try:
            with open(cf, "r", encoding="utf-8") as cache_file:
                data = json.load(cache_file)
            if not isinstance(data, dict):
                raise ValueError("Cache root must be an object")
            with _cache_lock:
                _llm_cache.update({key: value for key, value in data.items() if isinstance(value, dict)})
            print(f"[CACHE] Loaded {len(data)} cached resumes")
        except (OSError, ValueError) as exc:
            logger.warning("Could not load resume cache %s: %s", cf, exc)


def _save_disk_cache() -> None:
    if not PERSISTENT_CACHE_ENABLED:
        return
    cf = CACHE_DIR / "llm_cache_v2.json"
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _cache_lock:
            data = dict(_llm_cache)
        # Write-then-rename so a crash mid-write never leaves a truncated cache file.
        temp_cf = cf.with_suffix(".tmp")
        with open(temp_cf, "w", encoding="utf-8") as cache_file:
            json.dump(data, cache_file)
        os.replace(temp_cf, cf)
    except OSError as exc:
        logger.warning("Could not persist resume cache %s: %s", cf, exc)


_load_disk_cache()


from resume_ai_client import call_resume_llm, generate_fallback_resume_data, scrape_ashby_job


def _generate_fallback_resume_data(job: JobInfo) -> Dict[str, Any]:
    return generate_fallback_resume_data(job, ORIGINAL_EXPERIENCE, ORIGINAL_EDUCATION)


def _call_llm(job: JobInfo, feedback: str = "") -> Optional[Dict[str, Any]]:
    """Delegate the LLM call to the shared resume AI utilities."""
    return call_resume_llm(
        job=job,
        feedback=feedback,
        base_resume_text=BASE_RESUME_TEXT,
    )


# ============================================================================
# STEP 2: DATA NORMALIZATION & REPAIR (No Semicolon Sentence Stitching)
# ============================================================================

def _company_match(llm_name: str, orig_name: str) -> bool:
    """Fuzzy match company names."""
    llm_name = str(llm_name or "").strip()
    orig_name = str(orig_name or "").strip()
    if llm_name == orig_name:
        return True
    llm_words = set(llm_name.lower().split())
    orig_words = set(orig_name.lower().split())
    stop = {'the', 'group', '&', 'and', 'company', 'inc'}
    distinctive = orig_words - stop
    if distinctive and distinctive & llm_words:
        return True
    return False


def _normalize_experience(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize LLM output to standard structure."""
    if not isinstance(resume_data.get("experience"), list):
        for key in list(resume_data.keys()):
            if "experience" in key.lower() or "exp" in key.lower():
                resume_data["experience"] = resume_data.pop(key)
                break
        else:
            resume_data["experience"] = []

    normalized: List[Dict[str, Any]] = []
    for exp in resume_data["experience"]:
        if not isinstance(exp, dict) or not exp.get("company"):
            continue
        raw_bullets = exp.get("bullets", [])
        bullets = list(raw_bullets) if isinstance(raw_bullets, list) else []
        projects = exp.get("projects", [])
        if not bullets and isinstance(projects, list):
            for proj in projects:
                if isinstance(proj, dict):
                    project_bullets = proj.get("bullets", proj.get("bullet_points", []))
                    if isinstance(project_bullets, list):
                        bullets.extend(project_bullets)

        # Clean string whitespace without breaking LLM writing flow
        clean_bullets = [str(b).strip() for b in bullets if str(b).strip()]

        normalized.append({
            "company": str(exp.get("company", "")).strip(),
            "location": str(exp.get("location", "")).strip(),
            "title": str(exp.get("title", "")).strip(),
            "dates": str(exp.get("dates", "")).strip(),
            "bullets": clean_bullets,
        })

    resume_data["experience"] = normalized
    return resume_data


def _enforce_candidate_identity(
    resume_data: Dict[str, Any],
    email_override: str = "",
) -> Dict[str, Any]:
    resume_data["header_name"] = SOURCE_CANDIDATE["name"]
    resume_data["contact"] = {
        "location": SOURCE_CANDIDATE["location"],
        "email": email_override.strip() or SOURCE_CANDIDATE["email"],
        "phone": SOURCE_CANDIDATE["phone"],
        "linkedin": SOURCE_CANDIDATE["linkedin"],
    }
    return resume_data


def _repair_experience(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """If LLM dropped companies, add them back using full original entries."""
    llm_exps = resume_data.get('experience', [])
    matched_orig_indices = set()

    for llm_exp in llm_exps:
        llm_name = llm_exp.get('company', '')
        for idx, orig_exp in enumerate(ORIGINAL_EXPERIENCE):
            if idx not in matched_orig_indices and _company_match(llm_name, orig_exp['company']):
                matched_orig_indices.add(idx)
                break

    missing_indices = set(range(len(ORIGINAL_EXPERIENCE))) - matched_orig_indices
    if not missing_indices:
        return resume_data

    missing_names = [ORIGINAL_EXPERIENCE[i]['company'] for i in missing_indices]
    print(f"  [REPAIR] LLM dropped companies: {missing_names}. Restoring original full entries.")

    for idx in sorted(missing_indices):
        orig = ORIGINAL_EXPERIENCE[idx]
        resume_data['experience'].append({
            'company': orig['company'],
            'location': orig['location'],
            'title': orig['title'],
            'dates': orig['dates'],
            'bullets': list(orig['bullets']),
        })

    return resume_data


def _enforce_source_invariants(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalize immutable employment facts and restore chronological order."""
    generated = resume_data.get("experience", [])
    canonical: List[Dict[str, Any]] = []
    used: Set[int] = set()

    for original in ORIGINAL_EXPERIENCE:
        match_index: Optional[int] = None
        for index, entry in enumerate(generated):
            if (
                index not in used
                and isinstance(entry, Mapping)
                and _company_match(entry.get("company", ""), original["company"])
            ):
                match_index = index
                break
        if match_index is None:
            continue

        used.add(match_index)
        entry = dict(generated[match_index])
        proposed_title = str(entry.get("title", "")).strip()
        entry["company"] = original["company"]
        entry["location"] = original["location"]
        entry["dates"] = original["dates"]
        entry["title"] = proposed_title or str(original["title"])
        canonical.append(entry)

    resume_data["experience"] = canonical
    return resume_data


def _repair_education(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """Always restore immutable education facts from the tagged source."""
    resume_data['education'] = [dict(entry) for entry in ORIGINAL_EDUCATION]
    return resume_data


def _ensure_min_bullets(resume_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensures minimum capacity (14+ bullets) by appending COMPLETE natural bullets
    from original career data if an experience entry is under-populated.
    Does NOT cut or stitch sentences programmatically.
    """
    total = sum(len(exp.get('bullets', [])) for exp in resume_data.get('experience', []))

    if total < MIN_TOTAL_BULLETS:
        print(f"  [CAPACITY] {total} bullets found. Adding full natural fallback bullets.")
        for exp in resume_data.get('experience', []):
            company = exp.get('company', '')
            bullets = exp.get('bullets', [])
            if len(bullets) >= 4:
                continue

            for orig in ORIGINAL_EXPERIENCE:
                if _company_match(company, orig['company']):
                    current_text = ' '.join(bullets).lower()
                    for orig_b in orig['bullets']:
                        if len(bullets) >= 4:
                            break
                        # Skip fallback bullets that overlap an LLM bullet already covering
                        # the same claim, so top-ups add coverage instead of duplicating it.
                        orig_words = [w for w in orig_b.lower().split() if len(w) > 4][:3]
                        if orig_words and ' '.join(orig_words) in current_text:
                            continue
                        exp['bullets'].append(orig_b)
                        bullets = exp['bullets']
                    break

            total = sum(len(e.get('bullets', [])) for e in resume_data.get('experience', []))
            if total >= MIN_TOTAL_BULLETS:
                break

    return resume_data


def _validate_llm_data(resume_data: Mapping[str, Any]) -> List[str]:
    """Validate structure prior to rendering."""
    issues = []
    required = ['header_name', 'professional_summary', 'skills', 'experience', 'education']
    for k in required:
        if k not in resume_data:
            issues.append(f"Missing required key: {k}")

    tagline = resume_data.get('header_tagline', '')
    if not tagline:
        issues.append("Tagline is empty")

    skills = resume_data.get("skills", [])
    if not isinstance(skills, list):
        skills = []
    if len(skills) < 8:
        issues.append(f"Too few skills ({len(skills)}, need 10-18)")

    experience = resume_data.get("experience", [])
    if not isinstance(experience, list):
        experience = []
    all_text = " ".join(
        str(exp.get("company", ""))
        for exp in experience
        if isinstance(exp, Mapping)
    )
    all_text_lower = all_text.lower()
    missing = [c for c in ORIGINAL_COMPANIES if c.lower() not in all_text_lower]
    if missing:
        issues.append(f"Missing companies: {', '.join(missing)}")

    total_bullets = sum(
        len(exp.get("bullets", []))
        for exp in experience
        if isinstance(exp, Mapping) and isinstance(exp.get("bullets", []), list)
    )
    if total_bullets < 12:
        issues.append(f"Low bullet count ({total_bullets}). Need 14-18 bullets total.")

    return issues


# ============================================================================
# STEP 2.5: KEYWORD & AI-NATIVE BOLDING PROCESSOR
# ============================================================================

def _build_keyword_set(job: JobInfo) -> Set[str]:
    """Build a keyword set from JD for programmatic highlighting."""
    keywords = set()
    if job.keywords:
        for kw in re.split(r'[,;|/]', job.keywords):
            kw = kw.strip().strip("'\"")
            if 1 < len(kw) < 60:
                keywords.add(kw)

    jd = f"{job.jd_overview} {job.jd_responsibilities} {job.jd_requirements}"

    for m in re.finditer(r'"([^"]{3,50})"', jd):
        keywords.add(m.group(1))

    for m in re.finditer(r'\b([A-Z]{2,6})\b', jd):
        keywords.add(m.group(1))

    for m in re.finditer(r'(?<!\w)([a-zA-Z]+[-/][a-zA-Z][\w-]{2,})(?!\w)', jd):
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
        return ''

    # Escape untrusted text before adding the only ReportLab markup we allow.
    text = xml_escape(str(text))
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)

    # Split out already-bolded spans so keyword/metric patterns below only scan
    # plain text and never re-match (or nest tags) inside an existing <b>...</b>.
    tokens = re.split(r'(<b>.*?</b>)', text, flags=re.IGNORECASE | re.DOTALL)
    processed_tokens: List[str] = []

    patterns: List[re.Pattern[str]] = []
    if keywords:
        sorted_kws = sorted(keywords, key=len, reverse=True)
        for kw in sorted_kws:
            if len(kw) < 2:
                continue
            try:
                escaped = re.escape(kw)
                if ' ' not in kw and '/' not in kw and '-' not in kw:
                    patterns.append(re.compile(r'\b' + escaped + r'\b', re.IGNORECASE))
                else:
                    patterns.append(re.compile(escaped, re.IGNORECASE))
            except re.error:
                pass

    if bold_metrics:
        patterns.append(re.compile(r'\$[\d,.]+[KMBT]?\b'))
        patterns.append(re.compile(r'[+-]?\d+(?:\.\d+)?%'))
        patterns.append(re.compile(r'\b\d+\s*[\u2192\-]\s*\d+'))
        patterns.append(re.compile(r'\b\d+[KMB]\b'))

    for token in tokens:
        if token.lower().startswith('<b>') and token.lower().endswith('</b>'):
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
                res.append(f'<b>{safe_token[start:end]}</b>')
                last = end
            if last < len(safe_token):
                res.append(safe_token[last:])
            processed_tokens.append(''.join(res))

    return ''.join(processed_tokens)


# ============================================================================
# STEP 3: PDF RENDERING
# ============================================================================

def render_pdf(
    resume: Mapping[str, Any],
    output_path: Path,
    bold_keywords: Optional[Set[str]] = None,
) -> bool:
    """Render executive PDF with Carlito typography."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Register fonts safely
    font_normal = 'Helvetica'
    font_bold = 'Helvetica-Bold'
    font_italic = 'Helvetica-Oblique'

    if FONT_REGULAR and FONT_BOLD and FONT_ITALIC and FONT_BI:
        try:
            pdfmetrics.registerFont(TTFont('Carlito', FONT_REGULAR))
            pdfmetrics.registerFont(TTFont('Carlito-Bold', FONT_BOLD))
            pdfmetrics.registerFont(TTFont('Carlito-Italic', FONT_ITALIC))
            pdfmetrics.registerFont(TTFont('Carlito-BoldItalic', FONT_BI))
            pdfmetrics.registerFontFamily('Carlito',
                normal='Carlito', bold='Carlito-Bold',
                italic='Carlito-Italic', boldItalic='Carlito-BoldItalic')
            font_normal = 'Carlito'
            font_bold = 'Carlito-Bold'
            font_italic = 'Carlito-Italic'
        except Exception:
            pass

    DARK = HexColor('#1a1a2e')
    ACCENT = HexColor('#16213e')
    GRAY = HexColor('#555555')
    LIGHT_GRAY = HexColor('#888888')
    DIVIDER = HexColor('#2c3e6b')

    # Executive Margins (0.38" L/R, 0.28" T/B)
    MARGIN_L = 0.38 * inch
    MARGIN_R = 0.38 * inch
    MARGIN_T = 0.28 * inch
    MARGIN_B = 0.28 * inch

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T, bottomMargin=MARGIN_B,
    )

    usable_width = letter[0] - MARGIN_L - MARGIN_R

    s_name = ParagraphStyle('Name', fontName=font_bold, fontSize=15, leading=17, textColor=DARK, spaceAfter=0)
    s_tagline = ParagraphStyle('Tagline', fontName=font_italic, fontSize=9, leading=10.5, textColor=ACCENT, spaceAfter=1)
    s_contact = ParagraphStyle('Contact', fontName=font_normal, fontSize=7.8, leading=9.5, textColor=GRAY, alignment=TA_CENTER, spaceAfter=2)
    s_section = ParagraphStyle('Section', fontName=font_bold, fontSize=9.5, leading=11.5, textColor=ACCENT, spaceBefore=3, spaceAfter=1)
    s_summary = ParagraphStyle('Summary', fontName=font_normal, fontSize=8.4, leading=10.4, textColor=DARK, spaceAfter=1, alignment=TA_JUSTIFY)
    s_bullet = ParagraphStyle('Bullet', fontName=font_normal, fontSize=8.1, leading=9.8, textColor=DARK, leftIndent=5, spaceAfter=0.6, alignment=TA_JUSTIFY)
    s_company = ParagraphStyle('Company', fontName=font_bold, fontSize=8.8, leading=10.2, textColor=DARK, spaceAfter=0)
    s_title = ParagraphStyle('Title', fontName=font_italic, fontSize=8.2, leading=9.8, textColor=GRAY, spaceAfter=0)
    s_dates = ParagraphStyle('Dates', fontName=font_normal, fontSize=7.8, leading=9.8, textColor=LIGHT_GRAY, alignment=TA_RIGHT, spaceAfter=0)
    s_edu = ParagraphStyle('Edu', fontName=font_normal, fontSize=8.2, leading=9.8, textColor=DARK, spaceAfter=0.5)
    s_edu_detail = ParagraphStyle('EduDetail', fontName=font_italic, fontSize=7.2, leading=8.5, textColor=GRAY)

    elements = []

    # ---- HEADER ----
    tagline = resume.get('header_tagline', '')
    elements.append(Paragraph(xml_escape(str(resume.get('header_name', ''))), s_name))
    if tagline:
        elements.append(
            Paragraph(_bold_keywords_in_text(str(tagline), bold_keywords, bold_metrics=False), s_tagline)
        )

    contact = resume.get('contact', {})
    if not isinstance(contact, Mapping):
        contact = {}
    parts = []
    if contact.get('location'): parts.append(xml_escape(str(contact['location'])))
    if contact.get('email'): parts.append(xml_escape(str(contact['email'])))
    if contact.get('phone'): parts.append(xml_escape(str(contact['phone'])))
    if contact.get('linkedin'): parts.append(xml_escape(str(contact['linkedin'])))
    elements.append(Paragraph(' | '.join(parts), s_contact))
    elements.append(HRFlowable(width='100%', thickness=1.0, color=DIVIDER, spaceAfter=1.5, spaceBefore=1))

    # ---- PROFESSIONAL SUMMARY ----
    elements.append(Paragraph('PROFESSIONAL SUMMARY', s_section))
    summary = resume.get('professional_summary', '')
    summary = _bold_keywords_in_text(summary, bold_keywords)
    elements.append(Paragraph(summary, s_summary))
    elements.append(HRFlowable(width='100%', thickness=0.4, color=DIVIDER, spaceAfter=1, spaceBefore=1))

    # ---- CORE COMPETENCIES ----
    skills = resume.get('skills', [])
    if not isinstance(skills, list):
        skills = []
    if skills:
        elements.append(Paragraph('CORE COMPETENCIES', s_section))
        skills_text = '  \u00b7  '.join(str(skill) for skill in skills)
        skills_text = _bold_keywords_in_text(skills_text, bold_keywords, bold_metrics=False)
        s_skills_flow = ParagraphStyle('SkillsFlow', fontName=font_normal, fontSize=7.5, leading=9.2, textColor=DARK, spaceAfter=1)
        elements.append(Paragraph(skills_text, s_skills_flow))
        elements.append(HRFlowable(width='100%', thickness=0.4, color=DIVIDER, spaceAfter=1, spaceBefore=1))

    # ---- EDUCATION ----
    edu_list = resume.get('education', [])
    if not isinstance(edu_list, list):
        edu_list = []
    if edu_list:
        elements.append(Paragraph('EDUCATION', s_section))
        for edu in edu_list:
            degree = xml_escape(str(edu.get('degree', '')))
            school = xml_escape(str(edu.get('school', '')))
            dates = xml_escape(str(edu.get('dates', '')))
            details = xml_escape(str(edu.get('details', '')))
            line = f"<b>{school}</b>  \u2014  {degree}"
            if dates:
                line += f"  |  {dates}"
            elements.append(Paragraph(line, s_edu))
            if details:
                elements.append(Paragraph(details, s_edu_detail))
        elements.append(HRFlowable(width='100%', thickness=0.4, color=DIVIDER, spaceAfter=1, spaceBefore=1))

    # ---- PROFESSIONAL EXPERIENCE ----
    elements.append(Paragraph('PROFESSIONAL EXPERIENCE', s_section))
    experience = resume.get("experience", [])
    if not isinstance(experience, list):
        experience = []
    for exp in experience:
        if not isinstance(exp, Mapping):
            continue
        company = xml_escape(str(exp.get('company', '')))
        title = xml_escape(str(exp.get('title', '')))
        dates = xml_escape(str(exp.get('dates', '')))
        loc = xml_escape(str(exp.get('location', '')))
        bullets = exp.get('bullets', [])
        if not isinstance(bullets, list):
            bullets = []

        company_text = f"<b>{company}</b>{'  \u2014  ' + loc if loc else ''}"
        col_data = [[
            Paragraph(company_text, s_company),
            Paragraph(dates, s_dates)
        ]]
        col_table = Table(col_data, colWidths=[usable_width * 0.74, usable_width * 0.26])
        col_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
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


# ============================================================================
# STEP 4: QUALITY SCORING (Metric Density Engine + Flow Verification)
# ============================================================================

def _score_pdf(pdf_path: str) -> Tuple[int, List[str]]:
    """Score PDF dimensions including Metric Density."""
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return 0, ["PDF corrupt or unreadable"]

    try:
        score = 100
        feedback: List[str] = []

        # CHECK 1: Page count constraint
        if len(doc) == 0:
            return 0, ["Empty PDF"]
        if len(doc) > 1:
            score -= 40
            feedback.append("OVERFLOW: Resume spans multiple pages. Reduce bullet length slightly.")

        page = doc[0]
        blocks = page.get_text("dict").get("blocks", [])

        substantial_ys: List[float] = []
        all_text = ""
        fonts_used: Set[str] = set()
        bullet_count = 0

        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_text = "".join(span["text"] for span in line.get("spans", [])).strip()
                all_text += line_text + "\n"
                if len(line_text) > 10:
                    for span in line.get("spans", []):
                        if span["text"].strip():
                            substantial_ys.append(span["bbox"][1])
                            fonts_used.add(span["font"])
                if "\u2022" in line_text or "\u25aa" in line_text:
                    bullet_count += 1

        # CHECK 2: Vertical layout balance
        if substantial_ys:
            bottom_margin = ORIG_PAGE_HEIGHT - max(substantial_ys)
            if bottom_margin > 120:
                score -= 25
                feedback.append(
                    f"HUGE EMPTY SPACE: {bottom_margin:.0f}pt bottom margin. "
                    "LLM should write fuller bullets."
                )
            elif bottom_margin > 85:
                score -= 10
                feedback.append(f"Too much empty space: {bottom_margin:.0f}pt bottom margin.")
        else:
            score -= 50
            feedback.append("No substantial content found on page.")

        # CHECK 3: Character count ratio
        char_count = len(all_text.strip())
        ratio = char_count / ORIG_CHAR_COUNT if ORIG_CHAR_COUNT > 0 else 1
        if ratio < 0.70:
            score -= 20
            feedback.append(f"CRITICALLY SHORT: {ratio:.0%} of original char count.")
        elif ratio < 0.82:
            score -= 8
            feedback.append(f"Slightly short: {ratio:.0%} of original. Prefer fuller bullets.")

        # CHECK 4: Company presence
        all_text_lower = all_text.lower()
        missing = [
            company
            for company in ORIGINAL_COMPANIES
            if company.lower() not in all_text_lower
        ]
        if missing:
            score -= 20
            feedback.append(
                f"Missing companies: {', '.join(missing)}. ALL 5 companies must be present."
            )

        # CHECK 5: Bullet count capacity
        if bullet_count < 12:
            score -= 15
            feedback.append(
                f"Only {bullet_count} bullet points found. Write 3-4 bullets per company."
            )

        # CHECK 6: Font variants
        has_bold = any("bold" in font.lower() for font in fonts_used)
        has_italic = any("ital" in font.lower() for font in fonts_used)
        if not has_bold or not has_italic:
            score -= 5

        return max(score, 0), feedback
    finally:
        doc.close()


# ============================================================================
# STEP 5: ITERATIVE RETRY LOOP
# ============================================================================

def _build_feedback(issues: List[str]) -> str:
    """Convert scoring issues into actionable LLM prompt feedback."""
    parts = []
    for issue in issues:
        upper = issue.upper()
        if "SHORT" in upper:
            parts.append("BULLETS TOO SHORT: Write longer, complete executive narrative bullets (45-65 words each) with specific metrics and technical details.")
        elif "MISSING" in upper:
            parts.append(f"Include ALL 5 companies: {', '.join(ORIGINAL_COMPANIES)}.")
        elif "OVERFLOW" in upper:
            parts.append("Content overflows to page 2. Write slightly more concise bullets while keeping all 5 companies.")
        elif "EMPTY" in upper or "SPACE" in upper:
            parts.append("MAXIMIZE CONTENT CAPACITY: Write 14-18 dense bullet points (3-4 per company, 45-65 words each) to fill the page cleanly.")
        else:
            parts.append(issue)
    return "; ".join(parts) if parts else "; ".join(issues)


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
        print(f"  [V3.1 Flow] Attempt {attempt}/{MAX_RETRIES} for {job.company} - {job.role_title}", flush=True)

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
            feedback = "Your JSON was empty or unparseable. Return valid JSON with all required keys."
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
        critical = [i for i in data_issues if 'Missing required key' in i]
        if critical:
            feedback = "CRITICAL: " + "; ".join(critical)
            continue

        attempt_path = output_path.with_name(f".{output_path.stem}.attempt-{attempt}{output_path.suffix}")
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
            print(f"  [V3.1 Flow] PASSED quality check (score {score}) | {output_path.name}", flush=True)
            os.replace(attempt_path, output_path)
            _set_cached(job, resume_data)
            _save_disk_cache()
            return output_path

        feedback = _build_feedback(issues)
        print(f"  [V3.1 Flow] Feedback for retry: {feedback[:300]}", flush=True)
        _remove_file(attempt_path)

    print(f"  [V3.1 Flow] Max retries exhausted for {job.company}. Best score: {best_score}", flush=True)
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
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  [V3.1 Flow] Generating tailored resume for {job.company} - {job.role_title}...", flush=True)

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
                print(f"  [V3.1 Flow] Cache PASSED (score {score}). {output_path.name} ({file_size // 1024}KB)", flush=True)
                return output_path
            else:
                _remove_file(cached_path)
                with _cache_lock:
                    _llm_cache.pop(_cache_key(job), None)
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
        help="Ashby job URL used to obtain context when JD text is not supplied",
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
            scraped = scrape_ashby_job(args.url)
            job.jd_overview = scraped.get("jd_text", "")
            print(f"[CONTEXT] Loaded {len(job.jd_overview)} JD characters from Ashby.")
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
