"""Pure normalization and source-invariant checks for resume payloads.

These functions deliberately do not read candidate files, call an LLM, or
render a document.  The CLI/orchestrator can therefore keep its established
workflow while tests exercise its data rules using small in-memory fixtures.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Mapping, Sequence


def company_matches(llm_name: str, source_name: str) -> bool:
    """Return the historical tolerant match used for source companies."""
    llm_name = str(llm_name or "").strip()
    source_name = str(source_name or "").strip()
    if llm_name == source_name:
        return True
    llm_words = set(llm_name.lower().split())
    source_words = set(source_name.lower().split())
    stop_words = {"the", "group", "&", "and", "company", "inc"}
    distinctive = source_words - stop_words
    return bool(distinctive and distinctive & llm_words)


def normalize_experience(resume_data: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Normalize flexible LLM experience fields into renderer-ready entries."""
    if not isinstance(resume_data.get("experience"), list):
        for key in list(resume_data.keys()):
            if "experience" in str(key).lower() or "exp" in str(key).lower():
                resume_data["experience"] = resume_data.pop(key)
                break
        else:
            resume_data["experience"] = []

    normalized: list[dict[str, Any]] = []
    for experience in resume_data["experience"]:
        if not isinstance(experience, Mapping) or not experience.get("company"):
            continue
        raw_bullets = experience.get("bullets", [])
        bullets = list(raw_bullets) if isinstance(raw_bullets, list) else []
        projects = experience.get("projects", [])
        if not bullets and isinstance(projects, list):
            for project in projects:
                if isinstance(project, Mapping):
                    project_bullets = project.get("bullets", project.get("bullet_points", []))
                    if isinstance(project_bullets, list):
                        bullets.extend(project_bullets)
        normalized.append(
            {
                "company": str(experience.get("company", "")).strip(),
                "location": str(experience.get("location", "")).strip(),
                "title": str(experience.get("title", "")).strip(),
                "dates": str(experience.get("dates", "")).strip(),
                "bullets": [str(bullet).strip() for bullet in bullets if str(bullet).strip()],
            }
        )
    resume_data["experience"] = normalized
    return resume_data


def enforce_candidate_identity(
    resume_data: MutableMapping[str, Any],
    candidate: Mapping[str, str],
    email_override: str = "",
) -> MutableMapping[str, Any]:
    """Replace generated identity data with the tagged source of truth."""
    resume_data["header_name"] = candidate["name"]
    resume_data["contact"] = {
        "location": candidate["location"],
        "email": email_override.strip() or candidate["email"],
        "phone": candidate["phone"],
        "linkedin": candidate["linkedin"],
    }
    return resume_data


def repair_missing_experience(
    resume_data: MutableMapping[str, Any],
    source_experience: Sequence[Mapping[str, Any]],
) -> tuple[MutableMapping[str, Any], tuple[str, ...]]:
    """Restore source entries omitted by an LLM and report restored companies."""
    generated = resume_data.get("experience", [])
    if not isinstance(generated, list):
        generated = []
        resume_data["experience"] = generated
    matched_source_indices: set[int] = set()
    for generated_entry in generated:
        if not isinstance(generated_entry, Mapping):
            continue
        generated_name = generated_entry.get("company", "")
        for index, source_entry in enumerate(source_experience):
            if index not in matched_source_indices and company_matches(
                generated_name, str(source_entry.get("company", ""))
            ):
                matched_source_indices.add(index)
                break

    missing_indices = set(range(len(source_experience))) - matched_source_indices
    missing_names = tuple(str(source_experience[index]["company"]) for index in missing_indices)
    for index in sorted(missing_indices):
        source = source_experience[index]
        generated.append(
            {
                "company": source["company"],
                "location": source["location"],
                "title": source["title"],
                "dates": source["dates"],
                "bullets": list(source["bullets"]),
            }
        )
    return resume_data, missing_names


def enforce_source_invariants(
    resume_data: MutableMapping[str, Any],
    source_experience: Sequence[Mapping[str, Any]],
) -> MutableMapping[str, Any]:
    """Canonicalize immutable employment facts and source ordering."""
    generated = resume_data.get("experience", [])
    if not isinstance(generated, list):
        generated = []
    canonical: list[dict[str, Any]] = []
    used: set[int] = set()
    for source in source_experience:
        matching_index: int | None = None
        for index, entry in enumerate(generated):
            if (
                index not in used
                and isinstance(entry, Mapping)
                and company_matches(str(entry.get("company", "")), str(source["company"]))
            ):
                matching_index = index
                break
        if matching_index is None:
            continue
        used.add(matching_index)
        entry = dict(generated[matching_index])
        proposed_title = str(entry.get("title", "")).strip()
        entry["company"] = source["company"]
        entry["location"] = source["location"]
        entry["dates"] = source["dates"]
        entry["title"] = proposed_title or str(source["title"])
        canonical.append(entry)
    resume_data["experience"] = canonical
    return resume_data


def restore_source_education(
    resume_data: MutableMapping[str, Any], source_education: Sequence[Mapping[str, str]]
) -> MutableMapping[str, Any]:
    """Restore immutable education facts from the tagged candidate source."""
    resume_data["education"] = [dict(entry) for entry in source_education]
    return resume_data


def ensure_minimum_bullets(
    resume_data: MutableMapping[str, Any],
    source_experience: Sequence[Mapping[str, Any]],
    *,
    minimum_total: int,
) -> tuple[MutableMapping[str, Any], int]:
    """Top up sparse entries with complete source bullets, without stitching text."""
    experience = resume_data.get("experience", [])
    if not isinstance(experience, list):
        experience = []
        resume_data["experience"] = experience
    initial_total = sum(
        len(entry.get("bullets", []))
        for entry in experience
        if isinstance(entry, Mapping) and isinstance(entry.get("bullets", []), list)
    )
    total = initial_total
    if total >= minimum_total:
        return resume_data, initial_total

    for entry in experience:
        if not isinstance(entry, MutableMapping):
            continue
        company = entry.get("company", "")
        bullets = entry.get("bullets", [])
        if not isinstance(bullets, list):
            bullets = []
            entry["bullets"] = bullets
        if len(bullets) >= 4:
            continue
        for source in source_experience:
            if not company_matches(str(company), str(source.get("company", ""))):
                continue
            current_text = " ".join(str(item) for item in bullets).lower()
            for source_bullet in source.get("bullets", []):
                if len(bullets) >= 4:
                    break
                first_words = [
                    word for word in str(source_bullet).lower().split() if len(word) > 4
                ][:3]
                if first_words and " ".join(first_words) in current_text:
                    continue
                bullets.append(source_bullet)
            break
        total = sum(
            len(item.get("bullets", []))
            for item in experience
            if isinstance(item, Mapping) and isinstance(item.get("bullets", []), list)
        )
        if total >= minimum_total:
            break
    return resume_data, initial_total


def validate_resume_data(
    resume_data: Mapping[str, Any], source_companies: Sequence[str]
) -> list[str]:
    """Validate renderer prerequisites and quality signals without side effects."""
    issues: list[str] = []
    required = ["header_name", "professional_summary", "skills", "experience", "education"]
    for key in required:
        if key not in resume_data:
            issues.append(f"Missing required key: {key}")
    if not resume_data.get("header_tagline", ""):
        issues.append("Tagline is empty")

    skills = resume_data.get("skills", [])
    skill_count = len(skills) if isinstance(skills, list) else 0
    if skill_count < 8:
        issues.append(f"Too few skills ({skill_count}, need 10-18)")

    experience = resume_data.get("experience", [])
    normalized_experience = experience if isinstance(experience, list) else []
    all_text = " ".join(
        str(entry.get("company", ""))
        for entry in normalized_experience
        if isinstance(entry, Mapping)
    ).lower()
    missing = [company for company in source_companies if company.lower() not in all_text]
    if missing:
        issues.append(f"Missing companies: {', '.join(missing)}")
    total_bullets = sum(
        len(entry.get("bullets", []))
        for entry in normalized_experience
        if isinstance(entry, Mapping) and isinstance(entry.get("bullets", []), list)
    )
    if total_bullets < 12:
        issues.append(f"Low bullet count ({total_bullets}). Need 14-18 bullets total.")
    return issues


def build_quality_feedback(issues: Sequence[str], source_companies: Sequence[str]) -> str:
    """Convert scoring issues into the established retry-prompt language."""
    parts: list[str] = []
    for issue in issues:
        upper = issue.upper()
        if "SHORT" in upper:
            parts.append(
                "BULLETS TOO SHORT: Write longer, complete executive narrative bullets "
                "(45-65 words each) with specific metrics and technical details."
            )
        elif "MISSING" in upper:
            parts.append(f"Include ALL 5 companies: {', '.join(source_companies)}.")
        elif "OVERFLOW" in upper:
            parts.append(
                "Content overflows to page 2. Write slightly more concise bullets while keeping "
                "all 5 companies."
            )
        elif "EMPTY" in upper or "SPACE" in upper:
            parts.append(
                "MAXIMIZE CONTENT CAPACITY: Write 14-18 dense bullet points (3-4 per company, "
                "45-65 words each) to fill the page cleanly."
            )
        else:
            parts.append(issue)
    return "; ".join(parts) if parts else "; ".join(issues)
