"""Pure term parsing, normalization, alias expansion, and job matching.

Provider discovery and HTTP code deliberately stay out of this module so
matching behavior can be reused and tested without network dependencies.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from collections.abc import Sequence


def clean_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def split_terms(raw: str | None, defaults: Sequence[str]) -> list[str]:
    if raw is None:
        return list(defaults)
    return [part.strip() for part in raw.split(",") if part.strip()]


def split_repeated_terms(values: Sequence[str]) -> list[str]:
    """Split repeatable comma-separated CLI values and preserve their order."""
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        for term in split_terms(value, ()):
            key = term.casefold()
            if key not in seen:
                seen.add(key)
                terms.append(term)
    return terms


def quoted_search_term(value: str) -> str:
    """Render user-provided text as one quoted DDGS query term."""
    cleaned = clean_whitespace(value).replace('"', " ")
    return f'"{cleaned}"'


def term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term.strip())
    if re.fullmatch(r"[A-Za-z0-9]+", term.strip()):
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def normalize_match_text(value: Any) -> str:
    """Normalize punctuation and whitespace without broad substring matching."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\-/_]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return clean_whitespace(text)


def normalized_phrase_matches(text: str, term: str) -> bool:
    normalized_text = normalize_match_text(text)
    normalized_term = normalize_match_text(term)
    if not normalized_term:
        return False
    return f" {normalized_term} " in f" {normalized_text} "


def matching_terms(
    text: str,
    terms: Sequence[str],
    *,
    match_mode: str = "expanded",
) -> list[str]:
    if match_mode == "strict":
        return [term for term in terms if term_pattern(term).search(text)]
    return [term for term in terms if normalized_phrase_matches(text, term)]


def expand_aliases(
    terms: Sequence[str],
    aliases: dict[str, Sequence[str]],
    custom_aliases: Sequence[str] = (),
) -> list[str]:
    """Return user terms plus safe configured aliases, deduplicated in order."""
    expanded: list[str] = []
    seen: set[str] = set()
    for term in [*terms, *custom_aliases]:
        candidates = (term, *aliases.get(normalize_match_text(term), ()))
        for candidate in candidates:
            cleaned = clean_whitespace(candidate)
            key = normalize_match_text(cleaned)
            if cleaned and key and key not in seen:
                seen.add(key)
                expanded.append(cleaned)
    return expanded


def canonical_discovery_terms(
    terms: Sequence[str],
    aliases: dict[str, Sequence[str]],
    custom_aliases: Sequence[str] = (),
) -> list[str]:
    """Return one canonical discovery phrase per requested role family."""
    resolved: list[str] = []
    seen: set[str] = set()
    for term in [*terms, *custom_aliases]:
        cleaned = clean_whitespace(term)
        canonical = aliases.get(normalize_match_text(cleaned), (cleaned,))[0]
        canonical = clean_whitespace(canonical)
        key = normalize_match_text(canonical)
        if canonical and key and key not in seen:
            seen.add(key)
            resolved.append(canonical)
    return resolved


def location_matches(
    location: str,
    workplace_type: str,
    location_terms: Sequence[str],
    *,
    match_mode: str,
) -> list[str]:
    if not location_terms:
        return []
    haystack = clean_whitespace(f"{location} {workplace_type}")
    return matching_terms(haystack, location_terms, match_mode=match_mode)


def content_match_reason(
    *,
    title: str,
    description: str,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    match_mode: str,
) -> str | None:
    """Match title/content before location metadata is fully available."""
    title_text = clean_whitespace(title)
    full_text = clean_whitespace(f"{title_text} {description}")

    roles = matching_terms(title_text, role_terms, match_mode=match_mode)
    if not roles:
        return None
    ai_matches = matching_terms(full_text, ai_terms, match_mode=match_mode)
    if not ai_matches:
        return None
    excluded = matching_terms(full_text, exclude_terms, match_mode=match_mode)
    if excluded:
        return None
    return f"role={'; '.join(roles)} | AI={'; '.join(ai_matches)}"


def job_match_reason(
    *,
    title: str,
    description: str,
    location: str,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    location_terms: Sequence[str],
    workplace_type: str = "",
    match_mode: str = "expanded",
) -> str | None:
    """Return a formatted explanation when all final job filters pass."""
    content_reason = content_match_reason(
        title=title,
        description=description,
        role_terms=role_terms,
        ai_terms=ai_terms,
        exclude_terms=exclude_terms,
        match_mode=match_mode,
    )
    if content_reason is None:
        return None
    matched_locations = location_matches(
        location,
        workplace_type,
        location_terms,
        match_mode=match_mode,
    )
    if location_terms and not matched_locations:
        return None
    if matched_locations:
        return f"{content_reason} | location={'; '.join(matched_locations)}"
    return content_reason
