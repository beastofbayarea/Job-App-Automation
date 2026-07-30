"""Structured LLM call for one-page, claim-evidenced cover letters.

Reuses the existing injectable Gemini gateway (``resume/ai_client.ask_gemini``)
so this module never talks to a real LLM SDK in tests.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.adapters import LLMClient
from .ai_client import _strip_json_fence, ask_gemini
from .career_narrative import CareerNarrative
from .cover_letter_models import CoverLetterJob

PROMPT_TEMPLATE_VERSION = "cover-letter-v1"
TARGET_BODY_MINIMUM_WORDS = 180
TARGET_BODY_MAXIMUM_WORDS = 260


def build_cover_letter_system_prompt(narrative: CareerNarrative, feedback: str = "") -> str:
    """Strict LLM system prompt for a one-page, evidence-grounded cover letter."""
    fb_suffix = (
        f"\n\n*** PREVIOUS ATTEMPT FAILED. FIX THESE EXACT ISSUES: ***\n{feedback}"
        if feedback
        else ""
    )
    narrative_lines: list[str] = []
    if narrative.reason_for_change:
        narrative_lines.append(f"Reason for seeking a new role: {narrative.reason_for_change}")
    if narrative.next_role_priorities:
        narrative_lines.append(
            "Priorities for the next role: " + ", ".join(narrative.next_role_priorities)
        )
    if narrative.tone:
        narrative_lines.append(f"Requested tone: {narrative.tone}")
    if narrative.do_not_claim:
        narrative_lines.append(
            "Never claim any of the following: " + ", ".join(narrative.do_not_claim)
        )
    narrative_block = "\n".join(narrative_lines) if narrative_lines else "(none supplied)"

    return f"""ROLE
You write a one-page cover letter as the candidate, in first person, addressed
to "{narrative.default_salutation}" unless the job context names a specific
hiring contact.

EVIDENCE RULE
Use only facts present in the supplied CANDIDATE TAGGED SOURCE. Every fact you
reference must come from a "[CLAIM <id>]" line. Never invent a company, date,
metric, tool, or motivation. Do not fabricate personal enthusiasm not grounded
in the supplied narrative.

LENGTH RULE
The combined text of every entry in "paragraphs" must be between
{TARGET_BODY_MINIMUM_WORDS} and {TARGET_BODY_MAXIMUM_WORDS} words. This range
is strict so the rendered business letter remains one page. Prefer three
focused paragraphs with concise sentences; never exceed the upper bound.

CANDIDATE-APPROVED NARRATIVE
{narrative_block}
{fb_suffix}

Return a JSON object with this EXACT structure (no markdown fences, plain text
values only, three to four entries in "paragraphs"):
{{
  "salutation": "e.g. Dear Hiring Team,",
  "paragraphs": ["paragraph 1", "paragraph 2", "paragraph 3"],
  "closing": "e.g. Sincerely,",
  "signature": "Candidate's name exactly as it appears in the tagged source",
  "evidence_claim_ids": ["ID-1", "ID-2"]
}}

"evidence_claim_ids" must list every "[CLAIM <id>]" identifier your paragraphs
draw on, and nothing else."""


def call_cover_letter_llm(
    job: CoverLetterJob,
    narrative: CareerNarrative,
    source_text: str,
    feedback: str = "",
    *,
    gateway: LLMClient | None = None,
) -> dict[str, Any]:
    """Invoke the LLM gateway and return the parsed structured payload."""
    user_prompt = f"""
TARGET COMPANY: {job.company}
TARGET ROLE: {job.role}

JOB DESCRIPTION CONTEXT:
{job.jd_text}

CANDIDATE TAGGED SOURCE OF TRUTH:
{source_text}
"""
    system_prompt = build_cover_letter_system_prompt(narrative, feedback)
    raw_content = ask_gemini(
        user_prompt, system=system_prompt, temperature=0.3, json_mode=True, gateway=gateway
    )
    payload = json.loads(_strip_json_fence(raw_content))
    if not isinstance(payload, dict):
        raise ValueError("cover letter payload root must be a JSON object")
    return payload
