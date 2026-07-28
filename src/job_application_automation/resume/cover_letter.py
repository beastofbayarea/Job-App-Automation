#!/usr/bin/env python3
"""Standalone one-page, fact-grounded cover-letter generator (PRD F2)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..core.adapters import LLMClient
from ..core.artifacts import write_json
from ..core.engine_shared import load_json_config
from ..core.paths import CONFIG_DIR, OUTPUT_DIR
from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from .ai_client import scrape_ashby_job
from .career_narrative import CareerNarrative, load_career_narrative
from .cover_letter_ai import PROMPT_TEMPLATE_VERSION, call_cover_letter_llm
from .cover_letter_cache import CoverLetterCache, cover_letter_cache_key
from .cover_letter_claims import known_claim_ids, validate_claim_ids
from .cover_letter_models import CoverLetterJob
from .cover_letter_rendering import (
    DEFAULT_COVER_LETTER_RENDERER,
    CoverLetterRenderer,
    render_cover_letter,
)
from .cover_letter_validation import CoverLetterValidationPolicy, validate_cover_letter_pdf
from .source import ResumeSource, load_resume_source

DEFAULT_CONFIG_FILE = CONFIG_DIR / "candidate_profile_config.json"
COVER_LETTER_CACHE_FILE = resolve_runtime_path(RUNTIME_CONFIG.cover_letter["cache_file"])
MAX_RETRIES = int(RUNTIME_CONFIG.cover_letter["max_retries"])
MINIMUM_WORDS = int(RUNTIME_CONFIG.cover_letter["minimum_words"])
MAXIMUM_WORDS = int(RUNTIME_CONFIG.cover_letter["maximum_words"])


class JDContextUnavailable(RuntimeError):
    """Raised when no trustworthy job-description text is available."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _narrative_hash(narrative: CareerNarrative) -> str:
    payload = json.dumps(
        {
            "reason_for_change": narrative.reason_for_change,
            "next_role_priorities": list(narrative.next_role_priorities),
            "tone": narrative.tone,
            "default_salutation": narrative.default_salutation,
            "do_not_claim": list(narrative.do_not_claim),
        },
        sort_keys=True,
    )
    return _sha256(payload)


def _job_identity(job: CoverLetterJob) -> str:
    return job.url.strip() or f"company:{job.company}|role:{job.role}"


def cache_key_for(job: CoverLetterJob, narrative: CareerNarrative, source: ResumeSource) -> str:
    """Return the deterministic cache key for one job/narrative/source combination."""
    return cover_letter_cache_key(
        job_identity=_job_identity(job),
        jd_sha256=_sha256(job.jd_text),
        source_sha256=_sha256(source.text),
        narrative_sha256=_narrative_hash(narrative),
        template_version=PROMPT_TEMPLATE_VERSION,
    )


def _normalize_letter(payload: dict[str, Any]) -> dict[str, Any]:
    paragraphs = payload.get("paragraphs", [])
    return {
        "salutation": str(payload.get("salutation", "")).strip(),
        "paragraphs": [str(p).strip() for p in paragraphs if str(p).strip()]
        if isinstance(paragraphs, list)
        else [],
        "closing": str(payload.get("closing", "")).strip(),
        "signature": str(payload.get("signature", "")).strip(),
        "evidence_claim_ids": [
            str(cid).strip()
            for cid in (payload.get("evidence_claim_ids") or [])
            if str(cid).strip()
        ],
    }


def _structural_issues(letter: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not letter["salutation"]:
        issues.append("Missing required key: salutation")
    if not (3 <= len(letter["paragraphs"]) <= 4):
        issues.append(f"paragraphs must contain 3-4 entries; found {len(letter['paragraphs'])}")
    if not letter["closing"]:
        issues.append("Missing required key: closing")
    if not letter["signature"]:
        issues.append("Missing required key: signature")
    return issues


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def _audit_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".audit.json")


def generate_cover_letter(
    job: CoverLetterJob,
    narrative: CareerNarrative,
    source: ResumeSource,
    output_path: Path,
    *,
    gateway: LLMClient | None = None,
    renderer: CoverLetterRenderer | None = None,
    fitz_module: Any | None = None,
    cache: CoverLetterCache | None = None,
    clock: Callable[[], datetime] | None = None,
    max_retries: int | None = None,
    minimum_words: int | None = None,
    maximum_words: int | None = None,
) -> Optional[Path]:
    """Generate one validated, one-page cover letter PDF plus its audit sidecar.

    Returns the output path on success or ``None`` after every attempt fails.
    A two-page or otherwise invalid render is never promoted to ``output_path``.
    """
    if not job.jd_text.strip():
        raise JDContextUnavailable(
            f"No job-description context available for {job.company} - {job.role}."
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    active_renderer = renderer or DEFAULT_COVER_LETTER_RENDERER
    active_clock = clock or (lambda: datetime.now(timezone.utc))
    attempts = max_retries if max_retries is not None else MAX_RETRIES
    policy = CoverLetterValidationPolicy(
        minimum_words=minimum_words if minimum_words is not None else MINIMUM_WORDS,
        maximum_words=maximum_words if maximum_words is not None else MAXIMUM_WORDS,
        required_signature=source.candidate.get("name", ""),
    )
    known_ids = known_claim_ids(source.experience)
    key = cache_key_for(job, narrative, source)

    def _finish(letter: dict[str, Any]) -> Optional[Path]:
        attempt_path = output_path.with_name(f".{output_path.stem}.attempt{output_path.suffix}")
        _remove_file(attempt_path)
        if not render_cover_letter(active_renderer, letter, dict(source.candidate), attempt_path):
            _remove_file(attempt_path)
            return None
        valid, _issues = validate_cover_letter_pdf(attempt_path, policy, fitz_module=fitz_module)
        if not valid:
            _remove_file(attempt_path)
            return None
        os.replace(attempt_path, output_path)
        write_json(
            _audit_path_for(output_path),
            {
                "schema_version": 1,
                "evidence_claim_ids": letter["evidence_claim_ids"],
                "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                "jd_sha256": _sha256(job.jd_text),
                "source_sha256": _sha256(source.text),
                "narrative_sha256": _narrative_hash(narrative),
                "generated_at": active_clock().isoformat(),
            },
        )
        if cache is not None:
            cache.set(key, letter)
        return output_path

    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            result = _finish(cached)
            if result is not None:
                return result
            cache.discard(key)

    feedback = ""
    for _attempt in range(1, attempts + 1):
        try:
            payload = call_cover_letter_llm(job, narrative, source.text, feedback, gateway=gateway)
        except Exception as exc:  # noqa: BLE001 - feed the error back as retry guidance
            feedback = f"LLM request failed: {type(exc).__name__}. Return valid JSON."
            continue

        letter = _normalize_letter(payload)
        issues = _structural_issues(letter)
        if issues:
            feedback = "CRITICAL: " + "; ".join(issues)
            continue

        invalid_claims = validate_claim_ids(letter["evidence_claim_ids"], known_ids)
        if invalid_claims:
            feedback = (
                "These evidence_claim_ids do not exist in the tagged source and must be "
                "removed or replaced with real claim IDs: " + ", ".join(invalid_claims)
            )
            continue

        result = _finish(letter)
        if result is not None:
            return result
        feedback = "The rendered PDF failed one-page or word-budget validation. Write fewer words."

    return None


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone one-page cover-letter generator")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--url", default="", help="Job URL; also used for an Ashby JD fallback")
    parser.add_argument("--jd-overview", default="")
    parser.add_argument("--jd-resp", default="")
    parser.add_argument("--jd-req", default="")
    parser.add_argument("--jd-file", default="", help="Path to a file containing JD text")
    parser.add_argument("--profile", default=str(DEFAULT_CONFIG_FILE))
    parser.add_argument("--output", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argument_parser().parse_args(argv)

    jd_text = "\n".join(part for part in (args.jd_overview, args.jd_resp, args.jd_req) if part)
    if args.jd_file:
        jd_text = Path(args.jd_file).read_text(encoding="utf-8")
    if not jd_text.strip() and args.url:
        try:
            scraped = scrape_ashby_job(args.url)
            jd_text = scraped.get("jd_text", "")
        except Exception as exc:
            print(f"[CONTEXT] Could not load job context: {exc}", file=sys.stderr)

    job = CoverLetterJob(company=args.company, role=args.role, jd_text=jd_text, url=args.url)

    try:
        profile = load_json_config(Path(args.profile))
    except (OSError, ValueError) as exc:
        print(f"Could not load candidate profile: {exc}", file=sys.stderr)
        return 2
    narrative = load_career_narrative(profile)

    try:
        source = load_resume_source(
            resolve_runtime_path(RUNTIME_CONFIG.application["resume_source_file"])
        )
    except (OSError, ValueError) as exc:
        print(f"Could not load tagged resume source: {exc}", file=sys.stderr)
        return 2

    slug = f"{args.company}_{args.role}".replace(" ", "_")
    output = Path(args.output or str(OUTPUT_DIR / f"{slug}_Cover_Letter.pdf"))

    cache = CoverLetterCache()
    if COVER_LETTER_CACHE_FILE.exists():
        try:
            cache.load(COVER_LETTER_CACHE_FILE)
        except (OSError, ValueError):
            pass

    try:
        result = generate_cover_letter(job, narrative, source, output, cache=cache)
    except JDContextUnavailable as exc:
        print(f"JD_CONTEXT_UNAVAILABLE: {exc}", file=sys.stderr)
        return 2

    if result:
        cache.save(COVER_LETTER_CACHE_FILE)
        print(f"\nSUCCESS: {result}")
        return 0

    print(f"\nFAILED to generate a valid one-page cover letter for {args.company} - {args.role}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
