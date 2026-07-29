#!/usr/bin/env python3
"""
Account-free, high-coverage search for open AI roles on public ATS boards.

How it works:
1. Uses phased DDGS discovery to find public ATS job-board and job-page URLs.
2. Extracts each company's public board identifier.
3. Pulls currently published jobs from the ATS's public, unauthenticated feed.
4. Filters by the requested role type, ATS platform, location, AI relevance,
   and posting date.
5. Parses discovered JobPosting JSON-LD pages additively, including common ATSs
   that do not expose a stable public board API.
6. Caches boards and candidates, reports search coverage, and can verify that
   final roles are still live immediately before output.

No account or API key is required.

==============================================================================
OUT-OF-THE-BOX ALTERNATE APPROACHES / ARCHITECTURAL OPTIONS:
1. Multi-Engine Search Crawler Aggregator (SerpAPI / Google CSE / Bing Search / CommonCrawl Index):
   - Rather than relying solely on `ddgs` (DuckDuckGo Search), implement a multi-source web search aggregator.
   - Query Google Custom Search, Bing Web Search API, and index-matched CommonCrawl dumps in parallel.
   - Benefit: Immunizes job discovery against DDGS rate limiting and anti-scraping blocks, expanding search coverage by 3x.

2. Asynchronous Asyncio HTTP Engine with HTTP/2 Multiplexing (`httpx` / `aiohttp`):
   - Replace standard synchronous Python `requests` calls with `asyncio` and `httpx`.
   - Concurrently poll thousands of public ATS endpoints (Ashby, Greenhouse, Lever) in parallel with shared connection pools.
   - Benefit: Reduces search duration from ~3 minutes to under 5 seconds.

3. Graph-Based Job & Skill Knowledge Network (Neo4j / NetworkX):
   - Ingest discovered job postings into a property graph mapping required skills, compensation ranges, growth stage, and tech stack tags.
   - Benefit: Enables advanced semantic graph queries like "find AI roles requiring PyTorch at Series-B remote companies with engineering team > 50".
==============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import cache as _search_cache
from . import discovery as _search_discovery
from . import jsonld as _search_jsonld
from . import liveness as _search_liveness
from . import models as _search_models
from . import serialization as _search_serialization
from . import terms as _search_terms
from ..core.artifacts import atomic_write_text, write_json as atomic_write_json
from ..core.paths import OUTPUT_DIR

try:
    from ddgs import DDGS

    try:
        from ddgs.exceptions import DDGSException, RatelimitException  # type: ignore
    except ImportError:
        DDGSException = Exception  # type: ignore[assignment,misc]
        RatelimitException = Exception  # type: ignore[assignment,misc]
except ImportError:
    try:
        # Compatibility with the package's former name.
        from duckduckgo_search import DDGS  # type: ignore

        try:
            from duckduckgo_search.exceptions import (  # type: ignore
                DuckDuckGoSearchException as DDGSException,
                RatelimitException,
            )
        except ImportError:
            DDGSException = Exception  # type: ignore[assignment,misc]
            RatelimitException = Exception  # type: ignore[assignment,misc]
    except ImportError:
        DDGS = None  # type: ignore[assignment,misc]
        DDGSException = Exception  # type: ignore[assignment,misc]
        RatelimitException = Exception  # type: ignore[assignment,misc]


LOGGER = logging.getLogger("search_job_boards")
UTC = timezone.utc

SEARCH_PHRASE_TEMPLATES = (
    '"AI" {role} jobs',
    '"artificial intelligence" {role} jobs',
    '"generative AI" {role} jobs',
)

DEFAULT_AI_TERMS = (
    "AI",
    "artificial intelligence",
    "generative AI",
    "GenAI",
    "machine learning",
    "ML",
    "large language model",
    "large language models",
    "LLM",
    "LLMs",
    "A.I.",
    "Gen AI",
    "AI/ML",
    "machine-learning",
    "foundation model",
    "foundation models",
    "natural language processing",
    "NLP",
)

# Used only when the caller does not supply any ``--location`` parameters.
# Keep region-specific remote preferences separate from generic ``Remote`` so
# a default search does not silently include roles that can be performed from
# any country.
DEFAULT_LOCATION_TERMS = (
    "US Remote",
    "UK",
    "Ireland",
    "India Remote",
    "Delhi",
    "Noida",
    "France",
    "Europe Remote",
    "UAE",
    "Saudi Arabia",
    "Singapore",
    "Australia",
    "New Zealand",
    "Hong Kong",
)

# Queries deliberately use a smaller, diverse vocabulary than final matching to
# avoid spending most of a discovery run on near-identical search-engine queries.
AI_DISCOVERY_TERMS = (
    "AI",
    "artificial intelligence",
    "generative AI",
    "GenAI",
    "AI/ML",
    "machine learning",
    "LLM",
    "foundation models",
)

# Curated role families let users use the names they commonly see in job
# searches while still requiring a title-level match.  Keep adjacent but
# materially different functions out of these groups (for example, Business
# Development is not Corporate Development, and Revenue Operations is not
# Marketing Operations) to avoid silently broadening the final results.
ROLE_FAMILY_VARIANTS: dict[str, tuple[str, ...]] = {
    "growth_marketing": (
        "Growth Marketing",
        "Growth Marketer",
        "Growth Mkt",
    ),
    "performance_marketing": (
        "Performance Marketing",
        "Performance Marketer",
        "Performance Mkt",
    ),
    "paid_media": (
        "Paid Media",
        "Paid Social",
        "Paid Search",
        "Search Engine Marketing",
        "SEM",
        "PPC",
        "Media Buyer",
    ),
    "marketing_operations": (
        "Marketing Operations",
        "Marketing Ops",
        "Marketing Automation",
    ),
    "management_consulting": (
        "Management Consulting",
        "Management Consultant",
        "Strategy Consulting",
        "Strategy Consultant",
    ),
    "corporate_development": (
        "Corporate Development",
        "Corp Dev",
        "Corporate Strategy & Development",
        "Mergers and Acquisitions",
        "Mergers & Acquisitions",
        "M&A",
    ),
    "venture_capital": (
        "Venture Capital",
        "Venture Capitalist",
        "Venture Investing",
        "Venture Investor",
        "VC Associate",
        "VC Analyst",
        "VC Principal",
        "VC Investment",
        "Venture Partner",
    ),
}

# Keys are deliberately normalized so they can be looked up by
# ``normalize_match_text`` without separately maintaining a parallel map.
ROLE_FAMILY_INPUT_ALIASES: dict[str, tuple[str, ...]] = {
    "growth_marketing": (
        "growth marketing",
        "growth marketer",
        "growth mkt",
        "grwoth marketing",
        "grwoth mkt",
    ),
    "performance_marketing": (
        "performance marketing",
        "performance marketer",
        "performance mkt",
        "performace marketing",
        "performace mkt",
    ),
    "paid_media": (
        "paid media",
        "paid social",
        "paid search",
        "search engine marketing",
        "sem",
        "ppc",
        "media buyer",
    ),
    "marketing_operations": (
        "marketing operations",
        "marketing ops",
        "marketing automation",
    ),
    "management_consulting": (
        "management consulting",
        "management consultant",
        "strategy consulting",
        "strategy consultant",
        "management consukting",
    ),
    "corporate_development": (
        "corporate development",
        "corp dev",
        "corporate strategy development",
        "mergers and acquisitions",
        "mergers acquisitions",
        "m a",
    ),
    "venture_capital": (
        "venture capital",
        "venture capitalist",
        "venture investing",
        "venture investor",
        "vc",
        "vc associate",
        "vc analyst",
        "vc principal",
        "venture partner",
    ),
}


def build_role_alias_map() -> dict[str, tuple[str, ...]]:
    """Build role aliases from one family declaration to prevent drift."""
    aliases = {
        "program": ("Program", "Programme"),
        "programme": ("Program", "Programme"),
    }
    for family_name, input_aliases in ROLE_FAMILY_INPUT_ALIASES.items():
        aliases.update(
            {input_alias: ROLE_FAMILY_VARIANTS[family_name] for input_alias in input_aliases}
        )
    return aliases


ROLE_ALIAS_MAP = build_role_alias_map()

LOCATION_ALIAS_MAP = {
    "new york": ("New York", "New York City", "NYC"),
    "new york city": ("New York", "New York City", "NYC"),
    "nyc": ("New York", "New York City", "NYC"),
    "san francisco": ("San Francisco", "Bay Area", "SF"),
    "bay area": ("San Francisco", "Bay Area", "SF"),
    "remote": ("Remote", "Anywhere", "Distributed", "Work from home"),
    "united states": ("United States", "USA", "US"),
    "usa": ("United States", "USA", "US"),
}

GENERIC_ATS_HOST_SUFFIXES = (
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "icims.com",
    "workable.com",
    "jobvite.com",
    "teamtailor.com",
    "recruitee.com",
    "personio.com",
    "bamboohr.com",
    "pinpoint.com",
)
ATS_SEARCH_HOSTS = {
    "greenhouse": (
        "site:boards.greenhouse.io",
        "site:job-boards.greenhouse.io",
        "site:job-boards.eu.greenhouse.io",
    ),
    "lever": ("site:jobs.lever.co", "site:jobs.eu.lever.co"),
    "ashby": ("site:jobs.ashbyhq.com",),
    # Public pages for these providers are parsed through JobPosting JSON-LD.
    "web": tuple(f"site:{suffix}" for suffix in GENERIC_ATS_HOST_SUFFIXES),
}
SUPPORTED_ATS_PLATFORMS = tuple(ATS_SEARCH_HOSTS)
ALL_DDGS_BACKENDS = ("auto", "duckduckgo", "bing", "brave", "google", "yahoo", "mojeek")
DEFAULT_COVERAGE_REPORT = OUTPUT_DIR / "job_search_coverage.json"

DEAD_ROLE_MARKERS = (
    "job is no longer available",
    "this job is no longer available",
    "job posting is no longer available",
    "position has been filled",
    "position is no longer available",
    "job has expired",
    "this role has been filled",
    "job not found",
)

CSV_FIELDS = _search_models.CSV_FIELDS
Board = _search_models.Board
SearchCandidate = _search_models.SearchCandidate
DiscoveryQuery = _search_models.DiscoveryQuery
DiscoveryCache = _search_models.DiscoveryCache
DiscoveryStats = _search_models.DiscoveryStats
Job = _search_models.Job


# Criteria intentionally remains at the facade boundary because its methods
# dispatch to the historic ``job_match_reason`` and ``is_recent`` patch seams.
@dataclass(frozen=True)
class SearchCriteria:
    """The final filters shared by every ATS adapter and JSON-LD fallback."""

    role_terms: tuple[str, ...]
    ai_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    location_terms: tuple[str, ...]
    days: int
    include_unknown_dates: bool
    posted_since: date | None = None
    posted_until: date | None = None
    match_mode: str = "expanded"

    def matches_job(
        self,
        *,
        title: str,
        description: str,
        location: str,
        workplace_type: str = "",
    ) -> str | None:
        return job_match_reason(
            title=title,
            description=description,
            location=location,
            role_terms=self.role_terms,
            ai_terms=self.ai_terms,
            exclude_terms=self.exclude_terms,
            location_terms=self.location_terms,
            workplace_type=workplace_type,
            match_mode=self.match_mode,
        )

    def includes_posted_at(self, posted_at: datetime | None, *, now: datetime) -> bool:
        return is_recent(
            posted_at,
            days=self.days,
            now=now,
            include_unknown_dates=self.include_unknown_dates,
            posted_since=self.posted_since,
            posted_until=self.posted_until,
        )


@dataclass(frozen=True)
class FetchContext:
    """Shared transport and filtering inputs for typed provider adapters."""

    criteria: SearchCriteria
    now: datetime
    timeout: float
    delay: float
    max_lever_pages: int


TextExtractor = _search_jsonld.TextExtractor
JsonLdExtractor = _search_jsonld.JsonLdExtractor
LinkExtractor = _search_discovery.LinkExtractor


def strip_html(value: Any) -> str:
    """Compatibility wrapper for the reusable JSON-LD/HTML text extractor."""
    return _search_jsonld.strip_html(value)


def _legacy_clean_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def mapping_text(value: Any, key: str = "name") -> str:
    """Safely read a text field from optional ATS response objects."""
    return clean_whitespace(value.get(key)) if isinstance(value, dict) else ""


def mapping_items(value: Any) -> list[dict[str, Any]]:
    """Return only mappings from an optional list-like API field."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def prettify_slug(slug: str) -> str:
    value = unquote(slug).replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in value.split()) or slug


def make_session(user_agent: str) -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        }
    )
    return session


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float,
) -> Any:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        content_type = response.headers.get("Content-Type", "unknown")
        raise RuntimeError(f"Expected JSON from {response.url}, received {content_type}") from exc


def parse_datetime(value: Any) -> datetime | None:
    """Parse an epoch number, ISO 8601, or RFC 2822 timestamp into a UTC datetime."""
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        number = float(value)
        # Lever's createdAt is usually epoch milliseconds.
        if abs(number) > 10_000_000_000:
            number /= 1000.0
        try:
            dt = datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            try:
                return parse_datetime(float(raw))
            except ValueError:
                return None

        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_expiry_datetime(value: Any) -> datetime | None:
    """Parse an expiration/deadline value, treating a date as inclusive.

    Schema.org ``validThrough`` values and some Greenhouse deadlines are plain
    calendar dates. A role should remain eligible for the whole stated date,
    rather than expiring at its opening midnight.
    """
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def iso_or_blank(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def days_old(dt: datetime | None, now: datetime) -> int | str:
    if dt is None:
        return ""
    seconds = max(0.0, (now - dt).total_seconds())
    return int(seconds // 86400)


def is_recent(
    dt: datetime | None,
    *,
    days: int,
    now: datetime,
    include_unknown_dates: bool,
    posted_since: date | None = None,
    posted_until: date | None = None,
) -> bool:
    if dt is None:
        return include_unknown_dates

    posted_date = dt.astimezone(UTC).date()
    # An explicit calendar-date range supersedes the rolling --days filter.
    # This lets a user search an older period without also having to remember
    # to pass --days 0.
    if posted_since is not None or posted_until is not None:
        if posted_since is not None and posted_date < posted_since:
            return False
        if posted_until is not None and posted_date > posted_until:
            return False
        return True

    if days <= 0:
        return True
    cutoff = now - timedelta(days=days)
    # Allow a small future-clock skew.
    return cutoff <= dt <= now + timedelta(days=1)


def parse_calendar_date(value: str) -> date:
    """Parse a YYYY-MM-DD command-line date with an actionable error message."""
    raw = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a valid YYYY-MM-DD date") from exc


def discovery_timelimit(
    *,
    days: int,
    now: datetime,
    posted_since: date | None,
    posted_until: date | None,
) -> str | None:
    """Choose DDGS's coarse time range without excluding an explicit older window."""
    if posted_since is not None:
        age_days = max(0, (now.astimezone(UTC).date() - posted_since).days)
    elif posted_until is not None:
        # DDGS cannot express an upper-bound-only historical date range.
        return None
    else:
        if days <= 0:
            return None
        age_days = days

    if age_days <= 1:
        return "d"
    if age_days <= 31:
        return "m"
    if age_days <= 366:
        return "y"
    return None


def _legacy_split_terms(raw: str | None, defaults: Sequence[str]) -> list[str]:
    if raw is None:
        return list(defaults)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _legacy_split_repeated_terms(values: Sequence[str]) -> list[str]:
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


def _legacy_quoted_search_term(value: str) -> str:
    """Render user-provided text as one quoted DDGS query term."""
    cleaned = clean_whitespace(value).replace('"', " ")
    return f'"{cleaned}"'


def build_discovery_queries(
    *,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    location_terms: Sequence[str],
    extra_phrases: Sequence[str],
    mode: str,
) -> list[DiscoveryQuery]:
    """Plan broad discovery while retaining strict filtering after ATS retrieval.

    Search engines often index an ATS board root without the job's exact AI or
    location text. The query families deliberately widen board discovery in
    stages; every returned job is still validated against all user filters.
    """
    queries: list[DiscoveryQuery] = []
    seen: set[str] = set()

    def add(text: str, family: str) -> None:
        cleaned = clean_whitespace(text)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            queries.append(DiscoveryQuery(cleaned, family))

    normalized_ai = {normalize_match_text(term) for term in ai_terms}
    discovery_ai = [
        term for term in AI_DISCOVERY_TERMS if normalize_match_text(term) in normalized_ai
    ]
    if not discovery_ai:
        discovery_ai = list(ai_terms[:3])
    precise_ai = discovery_ai[:3]

    # Build the plan in waves across roles instead of exhausting every query
    # variant for the first role. This gives each requested role family useful
    # coverage even when --max-discovery-queries imposes a budget.
    for ai_term in precise_ai:
        for location_term in location_terms:
            location = quoted_search_term(location_term)
            for role_term in role_terms:
                role = quoted_search_term(role_term)
                add(
                    f"{quoted_search_term(ai_term)} {role} jobs {location}",
                    "role_ai_location",
                )

    for extra_phrase in extra_phrases:
        for role_term in role_terms:
            add(f"{extra_phrase} {quoted_search_term(role_term)} jobs", "custom")

    if mode in {"expanded", "exhaustive"}:
        for location_term in location_terms:
            location = quoted_search_term(location_term)
            for role_term in role_terms:
                add(f"{quoted_search_term(role_term)} jobs {location}", "role_location")

    if mode == "exhaustive":
        # Broad role and career queries find boards whose indexed root omits AI
        # or location language. Run them before the remaining AI variants so a
        # finite budget still reaches every requested role family.
        for role_term in role_terms:
            role = quoted_search_term(role_term)
            add(f"{role} jobs", "role_only")
            add(f"careers {role}", "careers_role")

    if mode in {"expanded", "exhaustive"}:
        for ai_term in discovery_ai:
            for role_term in role_terms:
                add(
                    f"{quoted_search_term(ai_term)} {quoted_search_term(role_term)} jobs",
                    "role_ai",
                )

    return queries


def build_search_phrases(
    role_types: Sequence[str],
    extra_phrases: Sequence[str],
) -> list[str]:
    """Compatibility helper retained for callers of the original API."""
    phrases: list[str] = []
    for role_type in role_types:
        role = quoted_search_term(role_type)
        phrases.extend(template.format(role=role) for template in SEARCH_PHRASE_TEMPLATES)
        phrases.extend(f"{phrase} {role} jobs" for phrase in extra_phrases)
    return list(dict.fromkeys(clean_whitespace(phrase) for phrase in phrases if phrase))


def _legacy_term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term.strip())
    if re.fullmatch(r"[A-Za-z0-9]+", term.strip()):
        # Single alphanumeric terms (e.g. "AI", "ML") need boundaries so they don't
        # match inside unrelated words like "said" or "main"; multi-word/symbol
        # terms (e.g. "generative AI") are matched as a plain substring instead.
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def _legacy_normalize_match_text(value: Any) -> str:
    """Normalize punctuation and whitespace without turning short terms into substrings."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Hyphens, slashes, and underscores commonly vary between ATSs (e.g.
    # Product-Manager, AI/ML, machine_learning).
    text = re.sub(r"[\-/_]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return clean_whitespace(text)


def _legacy_normalized_phrase_matches(text: str, term: str) -> bool:
    normalized_text = normalize_match_text(text)
    normalized_term = normalize_match_text(term)
    if not normalized_term:
        return False
    return f" {normalized_term} " in f" {normalized_text} "


def _legacy_matching_terms(
    text: str,
    terms: Sequence[str],
    *,
    match_mode: str = "expanded",
) -> list[str]:
    if match_mode == "strict":
        return [term for term in terms if term_pattern(term).search(text)]
    return [term for term in terms if normalized_phrase_matches(text, term)]


def _legacy_expand_aliases(
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


def _legacy_canonical_discovery_terms(
    terms: Sequence[str],
    aliases: dict[str, Sequence[str]],
    custom_aliases: Sequence[str] = (),
) -> list[str]:
    """Return one canonical discovery phrase per requested role family.

    Final matching still uses every safe equivalent title. Discovery only needs
    a concise, representative phrase to find a board, where the provider feed
    can then be filtered with the complete role family. This prevents typo and
    alias expansion from starving later requested roles under a query cap.
    """
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


def _legacy_location_matches(
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


def _legacy_content_match_reason(
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


def _legacy_job_match_reason(
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
    """Return a formatted match reason if the job passes the role/AI/exclude/location
    filters, otherwise None."""
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


# Keep these names on the historic module for direct-import compatibility while
# routing all active matching through the reusable, dependency-free module.
clean_whitespace = _search_terms.clean_whitespace
split_terms = _search_terms.split_terms
split_repeated_terms = _search_terms.split_repeated_terms
quoted_search_term = _search_terms.quoted_search_term
term_pattern = _search_terms.term_pattern
normalize_match_text = _search_terms.normalize_match_text
normalized_phrase_matches = _search_terms.normalized_phrase_matches
matching_terms = _search_terms.matching_terms
expand_aliases = _search_terms.expand_aliases
canonical_discovery_terms = _search_terms.canonical_discovery_terms
location_matches = _search_terms.location_matches
content_match_reason = _search_terms.content_match_reason
job_match_reason = _search_terms.job_match_reason


def unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("uddg", "u", "url", "target"):
        values = query.get(key)
        if values:
            candidate = unquote(values[0])
            if candidate.startswith(("http://", "https://")):
                return candidate
    return url


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    # Strip a trailing application path so a job page and its application form
    # canonicalize to the same key for final-job deduplication.
    path = re.sub(r"/(?:apply|application)/?$", "", parsed.path, flags=re.IGNORECASE)
    path = path.rstrip("/") or "/"
    # Preserve query only for Greenhouse embed URLs where it can identify the job.
    query = parsed.query if "greenhouse.io/embed/" in url.lower() else ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def discovery_url_key(url: str) -> str:
    """Deduplicate search results without collapsing a job page into /apply.

    The distinct URLs can carry different structured data or application links;
    canonical_url() remains the final result-level dedupe key.
    """
    parsed = urlparse(unwrap_search_url(url))
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def board_from_url(raw_url: str) -> Board | None:
    """Identify the ATS platform, board token, and region encoded in a URL, if any."""
    url = unwrap_search_url(raw_url)
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    if host == "jobs.ashbyhq.com" or host.endswith(".jobs.ashbyhq.com"):
        if parts:
            return Board("ashby", parts[0], "global")
        return None

    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        if parts:
            region = "eu" if host == "jobs.eu.lever.co" else "global"
            return Board("lever", parts[0], region)
        return None

    greenhouse_hosts = {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
    if host in greenhouse_hosts:
        if parts and parts[0].lower() != "embed":
            region = "eu" if ".eu." in host else "global"
            return Board("greenhouse", parts[0], region)
        # Embed widget URLs carry the board token as a query param instead of a path segment.
        for key in ("for", "board", "board_token"):
            values = query.get(key)
            if values and values[0]:
                region = "eu" if ".eu." in host else "global"
                return Board("greenhouse", unquote(values[0]), region)

    if any(host == suffix or host.endswith(f".{suffix}") for suffix in GENERIC_ATS_HOST_SUFFIXES):
        # Generic public ATS pages are scraped through JSON-LD rather than a
        # provider-specific board feed. Keep the host as a stable grouping key.
        return Board("web", host, "global")
    return None


def looks_like_job_url(url: str) -> bool:
    """Heuristically detect whether a URL points at a specific job posting rather than a board root."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    if host == "jobs.ashbyhq.com":
        return len(parts) >= 2
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        # /apply is the application form for a job, not the job posting itself.
        return len(parts) >= 2 and parts[-1].lower() != "apply"
    if host in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        return "jobs" in [part.lower() for part in parts] or "gh_jid" in query or "token" in query
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in GENERIC_ATS_HOST_SUFFIXES):
        return len(parts) >= 2
    return False


def board_from_cache_value(value: Any) -> Board | None:
    if not isinstance(value, dict) or not value.get("platform") or not value.get("token"):
        return None
    return Board(
        platform=str(value["platform"]),
        token=str(value["token"]),
        region=str(value.get("region", "global")),
    )


def add_candidate(
    candidates_by_board: dict[str, list[SearchCandidate]],
    candidate: SearchCandidate,
) -> bool:
    """Merge a candidate by its discovery URL and retain all discovery provenance."""
    if candidate.board is None:
        return False
    bucket = candidates_by_board.setdefault(candidate.board.key, [])
    for existing in bucket:
        if existing.cache_key != candidate.cache_key:
            continue
        if not existing.title and candidate.title:
            existing.title = candidate.title
        if not existing.snippet and candidate.snippet:
            existing.snippet = candidate.snippet
        existing.provenance = list(dict.fromkeys([*existing.provenance, *candidate.provenance]))
        if not existing.first_seen_at:
            existing.first_seen_at = candidate.first_seen_at
        if candidate.last_seen_at:
            existing.last_seen_at = candidate.last_seen_at
        return False
    bucket.append(candidate)
    return True


def merge_candidates(
    target: dict[str, list[SearchCandidate]],
    source: dict[str, list[SearchCandidate]],
) -> int:
    added = 0
    for candidates in source.values():
        for candidate in candidates:
            added += int(add_candidate(target, candidate))
    return added


def load_discovery_cache(path: Path) -> DiscoveryCache:
    """Load legacy/current cache data through the reusable cache boundary."""
    return _search_cache.load_discovery_cache(
        path,
        make_cache=DiscoveryCache,
        decode=lambda payload: _search_cache.decode_discovery_cache(
            payload,
            make_cache=DiscoveryCache,
            board_from_cache_value=board_from_cache_value,
            make_candidate=SearchCandidate,
            add_candidate=add_candidate,
            clean_text=clean_whitespace,
        ),
        on_error=lambda exc: LOGGER.warning("Could not read discovery cache %s: %s", path, exc),
    )


def save_discovery_cache(path: Path, cache: DiscoveryCache) -> None:
    """Persist the versioned cache atomically without changing its schema."""
    _search_cache.save_discovery_cache(
        path,
        cache,
        updated_at=iso_or_blank(datetime.now(UTC)),
        write_json=atomic_write_json,
    )


def load_board_cache(path: Path) -> set[Board]:
    """Compatibility wrapper for callers that only need cached boards."""
    return load_discovery_cache(path).boards


def save_board_cache(path: Path, boards: Iterable[Board]) -> None:
    """Compatibility wrapper that preserves the versioned discovery cache format."""
    save_discovery_cache(path, DiscoveryCache(boards=set(boards)))


def ddgs_text_search(
    query: str,
    *,
    region: str,
    timelimit: str | None,
    max_results: int,
    backend: str,
    timeout: float,
    retries: int = 1,
    retry_delay: float = 1.0,
) -> list[dict[str, Any]]:
    if DDGS is None:
        raise RuntimeError("DDGS is not installed. Run: python -m pip install -U requests ddgs")
    client = DDGS(timeout=max(5, int(timeout)))
    kwargs: dict[str, Any] = {
        "region": region,
        "safesearch": "moderate",
        "max_results": max_results,
        "backend": backend,
    }
    if timelimit:
        kwargs["timelimit"] = timelimit

    last_error: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            results = client.text(query, **kwargs)
            return list(results or [])
        except TypeError:
            # Older duckduckgo_search versions do not accept every current option.
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("backend", None)
            try:
                results = client.text(query, **fallback_kwargs)
                return list(results or [])
            except Exception as exc:  # pragma: no cover - depends on DDGS version
                last_error = exc
        except Exception as exc:  # pragma: no cover - provider/network dependent
            last_error = exc
        if attempt < retries and retry_delay > 0:
            time.sleep(retry_delay * (attempt + 1))
    if last_error is not None:
        raise last_error
    return []


def iter_discovery_requests(
    queries: Sequence[DiscoveryQuery],
    site_hosts: Sequence[str],
    regions: Sequence[str],
    backends: Sequence[str],
) -> Iterator[tuple[DiscoveryQuery, str, str, str]]:
    """Expand a fair discovery request plan through the reusable service."""
    yield from _search_discovery.iter_discovery_requests(queries, site_hosts, regions, backends)


def discover_boards(
    *,
    queries: Sequence[DiscoveryQuery],
    site_hosts: Sequence[str],
    allowed_platforms: set[str],
    regions: Sequence[str],
    timelimit: str | None,
    results_per_query: int,
    backends: Sequence[str],
    timeout: float,
    delay: float,
    max_queries: int,
    search_retries: int,
    stats: DiscoveryStats | None = None,
    on_progress: Callable[[set[Board], dict[str, list[SearchCandidate]], DiscoveryStats], None]
    | None = None,
) -> tuple[set[Board], dict[str, list[SearchCandidate]], DiscoveryStats]:
    """Discover boards while preserving public helpers as injectable seams."""
    return _search_discovery.discover_boards(
        queries=queries,
        site_hosts=site_hosts,
        allowed_platforms=allowed_platforms,
        regions=regions,
        timelimit=timelimit,
        results_per_query=results_per_query,
        backends=backends,
        timeout=timeout,
        delay=delay,
        max_queries=max_queries,
        search_retries=search_retries,
        stats=stats or DiscoveryStats(),
        now_text=iso_or_blank(datetime.now(UTC)),
        search_text=ddgs_text_search,
        unwrap_url=unwrap_search_url,
        board_from_url=board_from_url,
        looks_like_job_url=looks_like_job_url,
        make_candidate=SearchCandidate,
        add_candidate=add_candidate,
        clean_text=clean_whitespace,
        sleep=time.sleep,
        logger=LOGGER,
        on_progress=on_progress,
    )


def load_url_file(path: Path) -> list[str]:
    """Read one URL per line, accepting a JSON list as a convenience."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Could not read URL file %s: %s", path, exc)
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        urls: list[str] = []
        for value in parsed:
            candidate = value.get("url", "") if isinstance(value, dict) else value
            cleaned = clean_whitespace(candidate)
            if cleaned:
                urls.append(cleaned)
        return urls
    return [
        clean_whitespace(line)
        for line in raw.splitlines()
        if clean_whitespace(line) and not clean_whitespace(line).startswith("#")
    ]


def extract_ats_urls_from_html(html_text: str, *, base_url: str = "") -> list[str]:
    """Extract absolute, protocol-relative, and relative ATS links from one page."""
    return _search_discovery.extract_ats_urls_from_html(
        html_text,
        base_url=base_url,
        discovery_url_key=discovery_url_key,
    )


def discover_boards_from_career_pages(
    session: requests.Session,
    page_urls: Sequence[str],
    *,
    allowed_platforms: set[str],
    timeout: float,
    delay: float,
    max_pages: int,
) -> tuple[set[Board], dict[str, list[SearchCandidate]]]:
    """Discover one-hop ATS links through the reusable discovery service."""
    return _search_discovery.discover_boards_from_career_pages(
        session,
        page_urls,
        allowed_platforms=allowed_platforms,
        timeout=timeout,
        delay=delay,
        max_pages=max_pages,
        now_text=iso_or_blank(datetime.now(UTC)),
        extract_urls=extract_ats_urls_from_html,
        board_from_url=board_from_url,
        looks_like_job_url=looks_like_job_url,
        make_candidate=SearchCandidate,
        add_candidate=add_candidate,
        sleep=time.sleep,
        logger=LOGGER,
    )


def ensure_not_expired(value: Any, now: datetime) -> bool:
    valid_through = parse_expiry_datetime(value)
    return valid_through is None or valid_through >= now


def extract_jsonld_objects(html_text: str) -> Iterator[dict[str, Any]]:
    """Yield JSON-LD objects via the dependency-free Schema.org parser."""
    yield from _search_jsonld.extract_jsonld_objects(html_text)


def is_jobposting_object(value: dict[str, Any]) -> bool:
    return _search_jsonld.is_jobposting_object(value)


def jsonld_location(value: dict[str, Any]) -> str:
    return _search_jsonld.jsonld_location(value, clean_text=clean_whitespace)


def jsonld_salary(value: Any) -> str:
    return _search_jsonld.jsonld_salary(value, clean_text=clean_whitespace)


def platform_from_url(url: str) -> str:
    board = board_from_url(url)
    return board.platform if board else "web"


def scrape_jsonld_jobs(
    session: requests.Session,
    candidate: SearchCandidate,
    *,
    timeout: float,
    now: datetime,
    criteria: SearchCriteria,
) -> list[Job]:
    """Parse candidate JSON-LD through an injectable, network-free adapter."""
    return _search_jsonld.scrape_jsonld_jobs(
        session,
        candidate,
        timeout=timeout,
        now=now,
        criteria=criteria,
        extract_objects=extract_jsonld_objects,
        is_jobposting=is_jobposting_object,
        is_not_expired=ensure_not_expired,
        clean_text=clean_whitespace,
        strip_html_text=strip_html,
        location_from_jsonld=jsonld_location,
        board_from_url=board_from_url,
        prettify_board=prettify_slug,
        parse_datetime=parse_datetime,
        format_datetime=iso_or_blank,
        age_in_days=days_old,
        salary_from_jsonld=jsonld_salary,
        canonical_url=canonical_url,
        normalize_text=normalize_match_text,
        make_job=Job,
    )


def scrape_jsonld_job(
    session: requests.Session,
    candidate: SearchCandidate,
    *,
    timeout: float,
    now: datetime,
    days: int,
    include_unknown_dates: bool,
    posted_since: date | None = None,
    posted_until: date | None = None,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    location_terms: Sequence[str],
    match_mode: str = "expanded",
) -> Job | None:
    """Compatibility wrapper returning the first matching JSON-LD job."""
    criteria = SearchCriteria(
        role_terms=tuple(role_terms),
        ai_terms=tuple(ai_terms),
        exclude_terms=tuple(exclude_terms),
        location_terms=tuple(location_terms),
        days=days,
        include_unknown_dates=include_unknown_dates,
        posted_since=posted_since,
        posted_until=posted_until,
        match_mode=match_mode,
    )
    jobs = scrape_jsonld_jobs(session, candidate, timeout=timeout, now=now, criteria=criteria)
    return jobs[0] if jobs else None


def greenhouse_base_url(board: Board) -> str:
    # Greenhouse's documented public Job Board API uses this base URL. The board
    # token remains the same even when the hosted board URL is regional.
    return f"https://boards-api.greenhouse.io/v1/boards/{quote(board.token, safe='')}"


def fetch_greenhouse_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
) -> list[Job]:
    criteria = context.criteria
    now = context.now
    timeout = context.timeout
    delay = context.delay
    base = greenhouse_base_url(board)
    payload = get_json(session, f"{base}/jobs", params={"content": "true"}, timeout=timeout)
    jobs_raw = payload.get("jobs", []) if isinstance(payload, dict) else []
    normalized: list[Job] = []

    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        title = clean_whitespace(item.get("title"))
        description = strip_html(item.get("content"))
        location = mapping_text(item.get("location"))
        # Fetch detail before applying location/AI filters. Greenhouse can put
        # office metadata or richer content only on the detail endpoint.
        if not matching_terms(title, criteria.role_terms, match_mode=criteria.match_mode):
            continue
        if matching_terms(
            clean_whitespace(f"{title} {description}"),
            criteria.exclude_terms,
            match_mode=criteria.match_mode,
        ):
            continue

        detail = item
        date_source = "updated_at_fallback"
        job_id = item.get("id")
        if job_id is not None:
            # The list endpoint doesn't expose first_published; fetch each job's
            # detail record to get an accurate posting date when possible.
            try:
                detail_payload = get_json(session, f"{base}/jobs/{job_id}", timeout=timeout)
                if isinstance(detail_payload, dict):
                    detail = {**item, **detail_payload}
                    date_source = "first_published"
            except Exception as exc:
                LOGGER.warning(
                    "Greenhouse detail failed for %s job %s: %s", board.token, job_id, exc
                )
            if delay > 0:
                time.sleep(delay)

        description = strip_html(detail.get("content") or item.get("content"))

        deadline = parse_expiry_datetime(detail.get("application_deadline"))
        if deadline is not None and deadline <= now:
            continue

        posted_dt = parse_datetime(detail.get("first_published"))
        if posted_dt is None:
            posted_dt = parse_datetime(detail.get("updated_at"))
            date_source = "updated_at_fallback"
        if not criteria.includes_posted_at(posted_dt, now=now):
            continue

        departments = " | ".join(
            clean_whitespace(dep.get("name"))
            for dep in mapping_items(detail.get("departments"))
            if dep.get("name")
        )
        offices = " | ".join(
            clean_whitespace(office.get("name"))
            for office in mapping_items(detail.get("offices"))
            if office.get("name")
        )
        detail_location = mapping_text(detail.get("location"))
        location_full = " | ".join(
            dict.fromkeys(part for part in (location, detail_location, offices) if part)
        )

        reason = criteria.matches_job(
            title=title,
            description=description,
            location=location_full,
        )
        if reason is None:
            continue

        company = clean_whitespace(detail.get("company_name")) or prettify_slug(board.token)
        job_url = clean_whitespace(detail.get("absolute_url") or item.get("absolute_url"))
        unique = (
            f"greenhouse:{board.region}:{board.token}:{job_id}"
            if job_id is not None
            else canonical_url(job_url)
        )

        normalized.append(
            Job(
                platform="greenhouse",
                company=company,
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location_full,
                workplace_type="",
                employment_type="",
                department=departments,
                team="",
                salary="",
                job_url=job_url,
                apply_url=job_url,
                board_token=board.token,
                date_source=date_source,
                match_reason=reason,
                description=description,
                platform_job_id=clean_whitespace(job_id),
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="greenhouse_public_feed",
                live_check_reason="job_present_in_current_board_feed",
                unique_id=unique,
            )
        )

    return normalized


def lever_api_base(board: Board) -> str:
    host = "api.eu.lever.co" if board.region == "eu" else "api.lever.co"
    return f"https://{host}/v0/postings/{quote(board.token, safe='')}"


def format_lever_salary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    currency = clean_whitespace(value.get("currency", ""))
    minimum = value.get("min")
    maximum = value.get("max")
    interval = clean_whitespace(value.get("interval", ""))
    if minimum is not None and maximum is not None:
        amount = f"{minimum} - {maximum}"
    else:
        amount = str(minimum if minimum is not None else maximum or "")
    return clean_whitespace(" ".join(part for part in (currency, amount, interval) if part))


def fetch_lever_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
) -> list[Job]:
    criteria = context.criteria
    now = context.now
    timeout = context.timeout
    delay = context.delay
    max_pages = context.max_lever_pages
    base = lever_api_base(board)
    page_size = 100
    skip = 0
    all_items: list[dict[str, Any]] = []

    page_number = 0
    while max_pages <= 0 or page_number < max_pages:
        payload = get_json(
            session,
            base,
            params={"mode": "json", "skip": skip, "limit": page_size},
            timeout=timeout,
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected Lever response for {board.token}")
        page = [item for item in payload if isinstance(item, dict)]
        all_items.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
        page_number += 1
        if delay > 0:
            time.sleep(delay)

    normalized: list[Job] = []
    for item in all_items:
        title = clean_whitespace(item.get("text"))
        lists_text = " ".join(
            f"{clean_whitespace(section.get('text'))} {strip_html(section.get('content'))}"
            for section in mapping_items(item.get("lists"))
        )
        description = clean_whitespace(
            " ".join(
                part
                for part in (
                    item.get("descriptionPlain"),
                    item.get("openingPlain"),
                    item.get("descriptionBodyPlain"),
                    item.get("additionalPlain"),
                    lists_text,
                )
                if part
            )
        )
        categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
        locations = categories.get("allLocations") or []
        if not isinstance(locations, list):
            locations = [locations]
        location_parts = [clean_whitespace(part) for part in locations if part]
        primary_location = clean_whitespace(categories.get("location"))
        if primary_location and primary_location not in location_parts:
            location_parts.insert(0, primary_location)
        location = " | ".join(dict.fromkeys(location_parts))
        workplace_type = clean_whitespace(item.get("workplaceType"))

        reason = criteria.matches_job(
            title=title,
            description=description,
            location=location,
            workplace_type=workplace_type,
        )
        if reason is None:
            continue

        posted_dt = parse_datetime(item.get("createdAt"))
        date_source = "createdAt"
        if posted_dt is None:
            hosted_url = clean_whitespace(item.get("hostedUrl"))
            if hosted_url:
                candidate = SearchCandidate(url=hosted_url, board=board)
                try:
                    fallback_jobs = scrape_jsonld_jobs(
                        session,
                        candidate,
                        timeout=timeout,
                        now=now,
                        criteria=criteria,
                    )
                except Exception as exc:
                    LOGGER.warning("Lever date fallback failed for %s: %s", hosted_url, exc)
                    fallback_jobs = []
                fallback = fallback_jobs[0] if fallback_jobs else None
                if fallback is not None:
                    posted_dt = parse_datetime(fallback.posted_at)
                    date_source = "jsonld.datePosted"

        if not criteria.includes_posted_at(posted_dt, now=now):
            continue

        job_id = clean_whitespace(item.get("id"))
        job_url = clean_whitespace(item.get("hostedUrl"))
        apply_url = clean_whitespace(item.get("applyUrl"))
        unique = (
            f"lever:{board.region}:{board.token}:{job_id}" if job_id else canonical_url(job_url)
        )

        normalized.append(
            Job(
                platform="lever",
                company=prettify_slug(board.token),
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location,
                workplace_type=workplace_type,
                employment_type=clean_whitespace(categories.get("commitment")),
                department=clean_whitespace(categories.get("department")),
                team=clean_whitespace(categories.get("team")),
                salary=format_lever_salary(item.get("salaryRange"))
                or strip_html(item.get("salaryDescriptionPlain") or item.get("salaryDescription")),
                job_url=job_url,
                apply_url=apply_url,
                board_token=board.token,
                date_source=date_source,
                match_reason=reason,
                description=description,
                platform_job_id=job_id,
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="lever_public_feed",
                live_check_reason="posting_present_in_current_feed",
                unique_id=unique,
            )
        )

    return normalized


def ashby_salary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return clean_whitespace(
        value.get("scrapeableCompensationSalarySummary")
        or value.get("compensationTierSummary")
        or ""
    )


def fetch_ashby_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
) -> list[Job]:
    criteria = context.criteria
    now = context.now
    timeout = context.timeout
    url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board.token, safe='')}"
    payload = get_json(
        session,
        url,
        params={"includeCompensation": "true"},
        timeout=timeout,
    )
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    normalized: list[Job] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("isListed") is False:
            continue

        title = clean_whitespace(item.get("title"))
        description = clean_whitespace(
            item.get("descriptionPlain") or strip_html(item.get("descriptionHtml"))
        )
        primary_location = clean_whitespace(item.get("location"))
        secondary = [
            clean_whitespace(location.get("location"))
            for location in mapping_items(item.get("secondaryLocations"))
            if location.get("location")
        ]
        location = " | ".join(
            dict.fromkeys(part for part in [primary_location, *secondary] if part)
        )
        workplace_type = clean_whitespace(item.get("workplaceType"))

        reason = criteria.matches_job(
            title=title,
            description=description,
            location=location,
            workplace_type=workplace_type,
        )
        if reason is None:
            continue

        posted_dt = parse_datetime(item.get("publishedAt"))
        if not criteria.includes_posted_at(posted_dt, now=now):
            continue

        job_url = clean_whitespace(item.get("jobUrl"))
        apply_url = clean_whitespace(item.get("applyUrl"))
        item_id = clean_whitespace(item.get("id"))
        unique = f"ashby:{board.token}:{item_id}" if item_id else canonical_url(job_url)

        normalized.append(
            Job(
                platform="ashby",
                company=prettify_slug(board.token),
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location,
                workplace_type=workplace_type,
                employment_type=clean_whitespace(item.get("employmentType")),
                department=clean_whitespace(item.get("department")),
                team=clean_whitespace(item.get("team")),
                salary=ashby_salary(item.get("compensation")),
                job_url=job_url,
                apply_url=apply_url,
                board_token=board.token,
                date_source="publishedAt",
                match_reason=reason,
                description=description,
                platform_job_id=item_id,
                board_region=board.region,
                provider_id_trusted=True,
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="ashby_public_board",
                live_check_reason="job_present_in_current_board_response",
                unique_id=unique,
            )
        )

    return normalized


BOARD_FETCHERS: dict[str, Callable[[requests.Session, Board, FetchContext], list[Job]]] = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
}


def fetch_board_jobs(
    session: requests.Session,
    board: Board,
    context: FetchContext,
) -> list[Job]:
    """Dispatch typed adapter inputs without fragile provider-specific kwargs."""
    fetcher = BOARD_FETCHERS.get(board.platform)
    if fetcher is not None:
        return fetcher(session, board, context)
    if board.platform == "web":
        # Generic ATS candidates are handled through the additive JSON-LD pass.
        return []
    raise ValueError(f"Unsupported platform: {board.platform}")


def set_live_status(
    job: Job,
    *,
    status: str,
    source: str,
    now: datetime,
    reason: str,
    http_status: int | str = "",
    final_url: str = "",
) -> None:
    job.live_status = status
    job.live_checked_at = iso_or_blank(now)
    job.live_check_source = source
    job.live_check_reason = reason
    job.live_check_http_status = http_status
    job.live_check_final_url = final_url


def response_or_none(
    session: requests.Session,
    url: str,
    *,
    timeout: float,
    accept: str = "application/json,text/html;q=0.9,*/*;q=0.8",
) -> requests.Response | None:
    try:
        return session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"Accept": accept},
        )
    except requests.RequestException:
        return None


def verify_greenhouse_job_live(
    session: requests.Session,
    job: Job,
    *,
    timeout: float,
    now: datetime,
) -> None:
    board = Board("greenhouse", job.board_token, job.board_region)
    if not job.provider_id_trusted or not job.platform_job_id or not job.board_token:
        set_live_status(
            job,
            status="unknown",
            source="greenhouse_job_api",
            now=now,
            reason="untrusted_or_missing_board_or_job_id",
        )
        return
    url = f"{greenhouse_base_url(board)}/jobs/{quote(job.platform_job_id, safe='')}"
    response = response_or_none(session, url, timeout=timeout)
    if response is None:
        set_live_status(
            job, status="unknown", source="greenhouse_job_api", now=now, reason="request_failed"
        )
        return
    if response.status_code in {404, 410}:
        set_live_status(
            job,
            status="closed",
            source="greenhouse_job_api",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    if response.status_code >= 400:
        set_live_status(
            job,
            status="unknown",
            source="greenhouse_job_api",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or str(payload.get("id", "")) != str(job.platform_job_id):
        set_live_status(
            job,
            status="unknown",
            source="greenhouse_job_api",
            now=now,
            reason="unexpected_job_response",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    deadline = parse_expiry_datetime(payload.get("application_deadline"))
    if deadline is not None and deadline <= now:
        set_live_status(
            job,
            status="closed",
            source="greenhouse_job_api",
            now=now,
            reason="application_deadline_elapsed",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    set_live_status(
        job,
        status="live",
        source="greenhouse_job_api",
        now=now,
        reason="job_present_and_deadline_open",
        http_status=response.status_code,
        final_url=response.url,
    )


def verify_lever_job_live(
    session: requests.Session,
    job: Job,
    *,
    timeout: float,
    now: datetime,
) -> None:
    board = Board("lever", job.board_token, job.board_region)
    if not job.provider_id_trusted or not job.platform_job_id or not job.board_token:
        set_live_status(
            job,
            status="unknown",
            source="lever_posting_api",
            now=now,
            reason="untrusted_or_missing_board_or_job_id",
        )
        return
    url = f"{lever_api_base(board)}/{quote(job.platform_job_id, safe='')}?mode=json"
    response = response_or_none(session, url, timeout=timeout)
    if response is None:
        set_live_status(
            job, status="unknown", source="lever_posting_api", now=now, reason="request_failed"
        )
        return
    if response.status_code in {404, 410}:
        set_live_status(
            job,
            status="closed",
            source="lever_posting_api",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    if response.status_code >= 400:
        set_live_status(
            job,
            status="unknown",
            source="lever_posting_api",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or str(payload.get("id", "")) != str(job.platform_job_id):
        set_live_status(
            job,
            status="unknown",
            source="lever_posting_api",
            now=now,
            reason="unexpected_posting_response",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    set_live_status(
        job,
        status="live",
        source="lever_posting_api",
        now=now,
        reason="posting_present",
        http_status=response.status_code,
        final_url=response.url,
    )


def verify_ashby_jobs_live(
    session: requests.Session,
    jobs: Sequence[Job],
    *,
    timeout: float,
    now: datetime,
) -> None:
    """Verify Ashby roles per board to avoid one request per final job."""
    by_board: dict[tuple[str, str], list[Job]] = {}
    for job in jobs:
        if not job.provider_id_trusted or not job.platform_job_id:
            set_live_status(
                job,
                status="unknown",
                source="ashby_board_api",
                now=now,
                reason="untrusted_or_missing_job_id",
            )
            continue
        by_board.setdefault((job.board_region, job.board_token), []).append(job)
    for (_board_region, board_token), board_jobs in by_board.items():
        if not board_token:
            for job in board_jobs:
                set_live_status(
                    job, status="unknown", source="ashby_board_api", now=now, reason="missing_board"
                )
            continue
        url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board_token, safe='')}"
        response = response_or_none(session, url, timeout=timeout)
        if response is None:
            for job in board_jobs:
                set_live_status(
                    job,
                    status="unknown",
                    source="ashby_board_api",
                    now=now,
                    reason="request_failed",
                )
            continue
        if response.status_code in {404, 410}:
            for job in board_jobs:
                set_live_status(
                    job,
                    status="closed",
                    source="ashby_board_api",
                    now=now,
                    reason=f"http_{response.status_code}",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            continue
        if response.status_code >= 400:
            for job in board_jobs:
                set_live_status(
                    job,
                    status="unknown",
                    source="ashby_board_api",
                    now=now,
                    reason=f"http_{response.status_code}",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = None
        jobs_payload = payload.get("jobs", []) if isinstance(payload, dict) else []
        active_ids = {
            clean_whitespace(item.get("id"))
            for item in jobs_payload
            if isinstance(item, dict) and item.get("id") and item.get("isListed") is not False
        }
        for job in board_jobs:
            if job.platform_job_id and job.platform_job_id in active_ids:
                set_live_status(
                    job,
                    status="live",
                    source="ashby_board_api",
                    now=now,
                    reason="job_present_in_current_board_response",
                    http_status=response.status_code,
                    final_url=response.url,
                )
            else:
                set_live_status(
                    job,
                    status="closed",
                    source="ashby_board_api",
                    now=now,
                    reason="job_missing_from_current_board_response",
                    http_status=response.status_code,
                    final_url=response.url,
                )


def preserve_listing_status_on_page_uncertainty(
    job: Job,
    *,
    now: datetime,
    reason: str,
    http_status: int | str = "",
    final_url: str = "",
) -> bool:
    """Do not discard an authoritative listing confirmation because a page blocks bots."""
    if job.live_status not in {"listed", "live"}:
        return False
    job.live_checked_at = iso_or_blank(now)
    job.live_check_source = " ; ".join(
        value for value in (job.live_check_source, "job_page") if value
    )
    job.live_check_reason = " ; ".join(value for value in (job.live_check_reason, reason) if value)
    if http_status:
        job.live_check_http_status = http_status
    if final_url:
        job.live_check_final_url = final_url
    return True


def set_page_jsonld_live_status(
    job: Job,
    value: dict[str, Any],
    *,
    now: datetime,
    response: requests.Response,
) -> None:
    """Record a page-level status after the record has passed identity checks."""
    _apply_page_liveness_decision(
        job,
        _search_liveness.page_jsonld_decision(
            value,
            now=now,
            is_not_expired=ensure_not_expired,
            http_status=response.status_code,
            final_url=response.url,
        ),
        now=now,
    )


def _apply_page_liveness_decision(
    job: Job,
    decision: _search_liveness.LivenessDecision,
    *,
    now: datetime,
) -> None:
    """Apply a pure page decision while retaining the public mutation seam."""
    if decision.preserve_listing and preserve_listing_status_on_page_uncertainty(
        job,
        now=now,
        reason=decision.reason,
        http_status=decision.http_status,
        final_url=decision.final_url,
    ):
        return
    set_live_status(
        job,
        status=decision.status or "unknown",
        source=decision.source,
        now=now,
        reason=decision.reason,
        http_status=decision.http_status,
        final_url=decision.final_url,
    )


def verify_page_job_live(
    session: requests.Session,
    job: Job,
    *,
    timeout: float,
    now: datetime,
) -> None:
    url = job.apply_url or job.job_url
    if not url:
        _apply_page_liveness_decision(
            job,
            _search_liveness.page_uncertainty(
                existing_status=job.live_status,
                reason="missing_job_url",
            ),
            now=now,
        )
        return
    response = response_or_none(session, url, timeout=timeout, accept="text/html,*/*;q=0.8")
    if response is None:
        _apply_page_liveness_decision(
            job,
            _search_liveness.page_uncertainty(
                existing_status=job.live_status,
                reason="request_failed",
            ),
            now=now,
        )
        return
    _apply_page_liveness_decision(
        job,
        _search_liveness.page_response_decision(
            status_code=response.status_code,
            response_url=response.url,
            html_text=response.text,
            job_title=job.title,
            job_urls=(job.job_url, job.apply_url),
            existing_status=job.live_status,
            dead_role_markers=DEAD_ROLE_MARKERS,
            canonical_url=canonical_url,
            clean_text=clean_whitespace,
            normalize_text=normalize_match_text,
            extract_jsonld_objects=extract_jsonld_objects,
            is_jobposting_object=is_jobposting_object,
            is_not_expired=ensure_not_expired,
            now=now,
        ),
        now=now,
    )


def verify_live_jobs(
    session: requests.Session,
    jobs: Sequence[Job],
    *,
    timeout: float,
    delay: float,
    now: datetime,
    target: str,
) -> None:
    """Perform a final tri-state liveness check after result deduplication."""
    provider_jobs = [job for job in jobs if job.provider_id_trusted]
    ashby_jobs = [job for job in provider_jobs if job.platform == "ashby"]
    if target in {"listing", "both"}:
        for job in provider_jobs:
            if job.platform == "greenhouse":
                verify_greenhouse_job_live(session, job, timeout=timeout, now=now)
            elif job.platform == "lever":
                verify_lever_job_live(session, job, timeout=timeout, now=now)
            if job.platform in {"greenhouse", "lever"} and delay > 0:
                time.sleep(delay)
        if ashby_jobs:
            verify_ashby_jobs_live(session, ashby_jobs, timeout=timeout, now=now)
            if delay > 0:
                time.sleep(delay)

    # A JSON-LD record has no provider-trusted listing identifier. Its page is
    # therefore the only evidence available, including for --target listing.
    page_jobs = _search_liveness.page_jobs_for_target(jobs, target)
    if page_jobs:
        for job in page_jobs:
            verify_page_job_live(session, job, timeout=timeout, now=now)
            if delay > 0:
                time.sleep(delay)


def job_identity_keys(job: Job) -> set[str]:
    """Return stable, scoped identities for merging API and JSON-LD versions."""
    keys: set[str] = set()
    if job.url_is_record_specific or job.provider_id_trusted:
        for value in (job.job_url, job.apply_url):
            canonical = canonical_url(value) if value else ""
            if canonical:
                keys.add(f"url:{canonical}")
    if job.source_identity:
        keys.add(f"source:{job.source_identity}")
    if (
        job.provider_id_trusted
        and job.platform in {"greenhouse", "lever", "ashby"}
        and job.board_token
        and job.platform_job_id
    ):
        keys.add(
            f"provider:{job.platform}:{job.board_region}:{job.board_token}:{job.platform_job_id}"
        )
    if job.provider_id_trusted and job.unique_id and ":" in job.unique_id:
        keys.add(f"unique:{job.unique_id}")
    return keys


def job_quality_score(job: Job) -> tuple[int, int, int]:
    live_priority = {"live": 4, "listed": 3, "not_checked": 2, "unknown": 1, "closed": 0}
    date_priority = {
        "first_published": 3,
        "createdAt": 3,
        "publishedAt": 3,
        "jsonld.datePosted": 2,
        "updated_at_fallback": 1,
    }
    metadata_score = sum(
        bool(value)
        for value in (
            job.posted_at,
            job.company,
            job.location,
            job.workplace_type,
            job.department,
            job.team,
            job.salary,
            job.apply_url,
        )
    )
    return (
        live_priority.get(job.live_status, 0),
        date_priority.get(job.date_source, 0),
        metadata_score,
    )


def merge_job_records(first: Job, second: Job) -> Job:
    """Keep the stronger row and fill any missing metadata from its duplicate."""
    preferred, supplemental = (
        (second, first) if job_quality_score(second) > job_quality_score(first) else (first, second)
    )
    if supplemental.provider_id_trusted and not preferred.provider_id_trusted:
        # A provider adapter's scoped ID is safer than an arbitrary JSON-LD
        # identifier, even when the JSON-LD row carries richer text metadata.
        for field_name in (
            "platform",
            "platform_job_id",
            "board_token",
            "board_region",
            "job_url",
            "apply_url",
            "unique_id",
        ):
            value = getattr(supplemental, field_name)
            if value:
                setattr(preferred, field_name, value)
        preferred.provider_id_trusted = True

    def is_missing(value: Any) -> bool:
        return value is None or value == ""

    for field_name in (
        "company",
        "title",
        "posted_at",
        "days_old",
        "location",
        "workplace_type",
        "employment_type",
        "department",
        "team",
        "salary",
        "job_url",
        "apply_url",
        "board_token",
        "board_region",
        "date_source",
        "match_reason",
        "platform_job_id",
        "live_checked_at",
        "live_check_source",
        "live_check_http_status",
        "live_check_final_url",
        "live_check_reason",
        "source_identity",
        "unique_id",
    ):
        if is_missing(getattr(preferred, field_name)) and not is_missing(
            getattr(supplemental, field_name)
        ):
            setattr(preferred, field_name, getattr(supplemental, field_name))
    preferred.provider_id_trusted = (
        preferred.provider_id_trusted or supplemental.provider_id_trusted
    )
    preferred.url_is_record_specific = (
        preferred.url_is_record_specific or supplemental.url_is_record_specific
    )
    return preferred


def deduplicate_jobs(jobs: Iterable[Job]) -> list[Job]:
    result: list[Job | None] = []
    identity_index: dict[str, int] = {}
    for job in jobs:
        identities = job_identity_keys(job)
        matching_indices = sorted(
            {identity_index[key] for key in identities if key in identity_index}
        )
        if not matching_indices:
            result.append(job)
            job_index = len(result) - 1
        else:
            job_index = matching_indices[0]
            existing = result[job_index]
            assert existing is not None
            merged = merge_job_records(existing, job)
            result[job_index] = merged
            # Merge any previously separate records connected by this record's
            # URL/provider identities.
            for duplicate_index in reversed(matching_indices[1:]):
                duplicate = result[duplicate_index]
                if duplicate is None:
                    continue
                merged = merge_job_records(merged, duplicate)
                result[job_index] = merged
                result[duplicate_index] = None
                for key, index in list(identity_index.items()):
                    if index == duplicate_index:
                        identity_index[key] = job_index
        resolved_job = result[job_index]
        assert resolved_job is not None
        for identity in [*identities, *job_identity_keys(resolved_job)]:
            identity_index[identity] = job_index
    return [job for job in result if job is not None]


def sort_jobs(jobs: Iterable[Job]) -> list[Job]:
    def key(job: Job) -> tuple[float, str, str]:
        dt = parse_datetime(job.posted_at)
        timestamp = dt.timestamp() if dt else float("-inf")
        return (-timestamp, job.company.lower(), job.title.lower())

    return sorted(jobs, key=key)


def write_csv(path: Path, jobs: Sequence[Job]) -> None:
    rendered = _search_serialization.render_csv(
        _search_serialization.job_rows(jobs),
        fieldnames=CSV_FIELDS,
    )
    # Existing consumers expect an Excel-friendly UTF-8 BOM in search CSVs.
    atomic_write_text(path, rendered, encoding="utf-8-sig")


def write_json(path: Path, jobs: Sequence[Job]) -> None:
    write_json_file(path, _search_serialization.job_rows(jobs))


def write_private_generation_json(path: Path, jobs: Sequence[Job]) -> None:
    """Write full job descriptions for local document generation only."""
    write_json_file(path, [job.to_private_dict() for job in jobs])


def write_coverage_report(path: Path, report: dict[str, Any]) -> None:
    write_json_file(path, report)


def write_json_file(path: Path, payload: Any) -> None:
    """Write a UTF-8 JSON artifact shared by result and coverage outputs."""
    atomic_write_text(path, _search_serialization.render_json(payload), encoding="utf-8")


def print_summary(jobs: Sequence[Job], output: Path, limit: int) -> None:
    status_counts: dict[str, int] = {}
    for job in jobs:
        status_counts[job.live_status] = status_counts.get(job.live_status, 0) + 1
    status_summary = (
        ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items())) or "none"
    )
    print(f"\nFound {len(jobs)} matching roles. Live status: {status_summary}.")
    print(f"CSV: {output.resolve()}")
    if not jobs:
        return
    print("\nNewest matches:")
    for job in jobs[:limit]:
        posted = job.posted_at[:10] if job.posted_at else "date unknown"
        print(
            f"- [{job.platform}] {posted} | {job.company} | {job.title} | "
            f"{job.location or 'location unspecified'}\n  {job.job_url}"
        )


def iter_candidates_round_robin(
    candidates_by_board: dict[str, list[SearchCandidate]],
    board_keys: Sequence[str],
) -> Iterator[SearchCandidate]:
    """Yield candidate pages fairly so one large board cannot consume a global cap."""
    queues = [
        deque(candidates_by_board.get(board_key, []))
        for board_key in sorted(dict.fromkeys(board_keys))
    ]
    while True:
        yielded = False
        for queue in queues:
            if not queue:
                continue
            yielded = True
            yield queue.popleft()
        if not yielded:
            return


def fallback_candidate_board_keys(
    candidates_by_board: dict[str, list[SearchCandidate]],
    failed_boards: set[str],
    mode: str,
) -> list[str]:
    """Choose JSON-LD fallback pages without excluding generic web candidates."""
    if mode == "all":
        return sorted(candidates_by_board)
    if mode != "failed-feed":
        return []
    generic_web_boards = {
        board_key
        for board_key, candidates in candidates_by_board.items()
        if any(
            candidate.board is not None and candidate.board.platform == "web"
            for candidate in candidates
        )
    }
    return sorted(failed_boards | generic_web_boards)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover and retrieve open AI jobs from the requested public ATS "
            "platforms, role types, and locations without API keys."
        ),
        epilog=(
            "Example: python src/job_automation.py search --role-type product "
            '--ats-platform greenhouse --location "New York"'
        ),
    )
    parser.add_argument(
        "--role-type",
        "--role-terms",
        dest="role_types",
        action="append",
        required=True,
        metavar="ROLE",
        help=(
            "Job-title term to search for and require in results; repeat or use "
            "comma-separated values for OR matching (required). Expanded matching "
            "includes curated role families such as Growth Marketing, Performance "
            "Marketing, Paid Media, Marketing Operations, Management Consulting, "
            "Corporate Development, and Venture Capital. --role-terms is a "
            "compatibility alias."
        ),
    )
    parser.add_argument(
        "--ats-platform",
        "--ats",
        "--platform",
        dest="ats_platforms",
        action="append",
        required=True,
        type=lambda value: value.strip().lower(),
        choices=SUPPORTED_ATS_PLATFORMS,
        metavar="ATS",
        help=(
            "ATS platform to search; repeat to include more than one. Supported: "
            f"{', '.join(SUPPORTED_ATS_PLATFORMS)} (required)."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help=(
            "Keep jobs posted within this many days; 0 disables rolling date "
            "filtering. Ignored when an explicit posted-date filter is supplied "
            "(default: 0 for maximum coverage)."
        ),
    )
    parser.add_argument(
        "--posted-on",
        "--date-posted",
        "--posted-date",
        dest="posted_on",
        type=parse_calendar_date,
        metavar="YYYY-MM-DD",
        help="Keep jobs posted on this exact calendar date.",
    )
    parser.add_argument(
        "--posted-since",
        "--posted-after",
        dest="posted_since",
        type=parse_calendar_date,
        metavar="YYYY-MM-DD",
        help="Keep jobs posted on or after this calendar date.",
    )
    parser.add_argument(
        "--posted-until",
        "--posted-before",
        dest="posted_until",
        type=parse_calendar_date,
        metavar="YYYY-MM-DD",
        help="Keep jobs posted on or before this calendar date.",
    )
    parser.add_argument(
        "--search",
        action="append",
        dest="search_phrases",
        help=(
            "Additional web-search phrase combined with each required role type. "
            "It broadens board discovery; final location filters still apply. "
            "Repeat for multiple phrases."
        ),
    )
    parser.add_argument(
        "--discovery-mode",
        choices=("focused", "expanded", "exhaustive"),
        default="exhaustive",
        help=(
            "Breadth of board discovery. Exhaustive adds role-only and careers "
            "queries, while final results still use all required filters "
            "(default: exhaustive)."
        ),
    )
    parser.add_argument(
        "--exhaustive",
        action="store_true",
        help="Compatibility shortcut for --discovery-mode exhaustive.",
    )
    parser.add_argument(
        "--max-discovery-queries",
        type=int,
        default=400,
        help=(
            "Maximum backend/region/site discovery queries; 0 means unlimited. "
            "The coverage report shows planned versus attempted queries (default: 400)."
        ),
    )
    parser.add_argument(
        "--discovery-timelimit",
        choices=("auto", "none"),
        default="none",
        help=(
            "DDGS date limit for board discovery. Use none for maximum board "
            "coverage; ATS feeds enforce the final posted-date filter (default: none)."
        ),
    )
    parser.add_argument(
        "--role-alias",
        action="append",
        default=[],
        help="Additional equivalent role-title term for expanded matching; repeatable.",
    )
    parser.add_argument(
        "--location-alias",
        action="append",
        default=[],
        help="Additional equivalent location term for matching and discovery; repeatable.",
    )
    parser.add_argument(
        "--match-mode",
        choices=("strict", "expanded"),
        default="expanded",
        help="Use punctuation/alias-aware matching or literal matching (default: expanded).",
    )
    parser.add_argument(
        "--ai-terms",
        help="Comma-separated AI terms that may appear in the title or description.",
    )
    parser.add_argument(
        "--exclude-terms",
        default="",
        help="Comma-separated title/description terms to exclude, such as intern,contract.",
    )
    parser.add_argument(
        "--location",
        action="append",
        metavar="LOCATION",
        help=(
            "Keep jobs whose location contains this term and include it in web "
            "discovery; repeat for OR matching. When omitted, searches the default "
            "locations: US Remote, UK, Ireland, India Remote, Delhi, Noida, France, "
            "Europe Remote, UAE, Saudi Arabia, Singapore, Australia, New Zealand, "
            "and Hong Kong."
        ),
    )
    parser.add_argument(
        "--board-url",
        action="append",
        default=[],
        help="Seed a known ATS board or job URL; repeat for multiple boards.",
    )
    parser.add_argument(
        "--boards-file",
        action="append",
        type=Path,
        default=[],
        help="Text or JSON file containing one ATS board/job URL per entry; repeatable.",
    )
    parser.add_argument(
        "--career-page",
        action="append",
        default=[],
        help="Company career page to scan once for embedded ATS links; repeatable.",
    )
    parser.add_argument(
        "--career-pages-file",
        "--companies-file",
        action="append",
        type=Path,
        default=[],
        help="Text or JSON file of company career-page URLs to scan once; repeatable.",
    )
    parser.add_argument(
        "--max-career-pages",
        type=int,
        default=25,
        help="Maximum explicitly supplied career pages to scan; 0 means unlimited (default: 25).",
    )
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Skip DuckDuckGo discovery and use only cached/seeded boards.",
    )
    parser.add_argument(
        "--include-unknown-dates",
        action="store_true",
        help=(
            "Keep matching jobs when the platform exposes no reliable posting date. "
            "Exhaustive mode does this automatically only for the unbounded --days 0 "
            "search unless an explicit date range is used."
        ),
    )
    parser.add_argument(
        "--scrape-discovered-pages",
        choices=("none", "failed-feed", "all"),
        default="all",
        help=(
            "Parse discovered job pages as an additive JSON-LD source. failed-feed "
            "also includes generic web ATS pages; all gives the highest coverage "
            "(default: all)."
        ),
    )
    parser.add_argument(
        "--verify-live",
        action="store_true",
        help="Re-check final roles against the public ATS endpoint or job page.",
    )
    parser.add_argument(
        "--require-live",
        action="store_true",
        help="Keep only roles confirmed live by --verify-live; implies --verify-live.",
    )
    parser.add_argument(
        "--live-check-target",
        choices=("listing", "application", "both"),
        default="both",
        help=(
            "Verify the provider listing, job/application page, or both. Generic "
            "JSON-LD roles use their page when no provider listing ID is trusted "
            "(default: both)."
        ),
    )
    parser.add_argument(
        "--live-check-timeout",
        type=float,
        help="Per-role live-check timeout in seconds (default: --timeout).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_DIR / "ai_jobs.csv",
        help="CSV output path (default: output/ai_jobs.csv).",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional JSON output path.",
    )
    parser.add_argument(
        "--private-generation-output",
        type=Path,
        help=(
            "Optional private JSON containing full descriptions for document generation; "
            "never publish this file through the VPS sync branch."
        ),
    )
    parser.add_argument(
        "--coverage-report",
        type=Path,
        default=DEFAULT_COVERAGE_REPORT,
        help=(
            "JSON coverage report path; records discovery, feed, fallback, and "
            f"live-check outcomes (default: {DEFAULT_COVERAGE_REPORT})."
        ),
    )
    parser.add_argument(
        "--no-coverage-report",
        action="store_true",
        help="Do not write the coverage report.",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=OUTPUT_DIR / "ats_boards_cache.json",
        help="Discovered-board cache path (default: output/ats_boards_cache.json).",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Ignore and overwrite the existing board cache.",
    )
    parser.add_argument(
        "--region",
        "--discovery-region",
        dest="discovery_regions",
        action="append",
        default=[],
        help="DDGS search region, such as wt-wt, us-en, or in-en; repeatable (default: wt-wt).",
    )
    parser.add_argument(
        "--search-backend",
        action="append",
        default=[],
        help=(
            "DDGS text-search backend; repeat to merge providers, or pass all to "
            "try supported backends (default: auto)."
        ),
    )
    parser.add_argument(
        "--search-retries",
        type=int,
        default=1,
        help="Retries for each failed DDGS query (default: 1).",
    )
    parser.add_argument(
        "--results-per-query",
        type=int,
        default=50,
        help="Maximum web results per discovery query (default: 50).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP/search timeout in seconds (default: 20).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Polite delay between searches/API detail calls (default: 0.25).",
    )
    parser.add_argument(
        "--max-lever-pages",
        type=int,
        default=0,
        help="Maximum 100-job Lever pages per company; 0 means unlimited (default: 0).",
    )
    parser.add_argument(
        "--max-fallback-pages",
        type=int,
        default=0,
        help="Maximum discovered job pages to parse; 0 means unlimited (default: 0).",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=20,
        help="Number of matches to print in the terminal (default: 20).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress logs.",
    )
    parser.add_argument(
        "--async-http",
        action="store_true",
        help="Use Asyncio HTTP/2 multiplexed search engine for parallel feed fetching.",
    )
    return parser


async def search_job_boards_async(urls: Sequence[str], timeout: float = 10.0) -> list[dict[str, Any]]:
    """Alternate capability: Asyncio HTTP/2 multiplexed search feed crawler.

    Concurrently fetches public ATS board feeds in parallel using asyncio and httpx/aiohttp.
    Preserves default synchronous search workflow unless explicitly requested.
    """
    import asyncio
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            tasks = [client.get(u) for u in urls]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            results = []
            for u, resp in zip(urls, responses):
                if not isinstance(resp, Exception) and resp.status_code == 200:
                    results.append({"url": u, "status": resp.status_code, "text": resp.text[:500]})
                else:
                    results.append({"url": u, "status": 0, "error": str(resp)})
            return results
    except ImportError:
        LOGGER.warning("httpx not installed; running fallback async threadpool crawler")
        loop = asyncio.get_running_loop()
        def _fetch_sync(u: str) -> dict[str, Any]:
            import urllib.request
            try:
                with urllib.request.urlopen(u, timeout=timeout) as r:
                    return {"url": u, "status": r.status, "text": r.read().decode("utf-8", "ignore")[:500]}
            except Exception as e:
                return {"url": u, "status": 0, "error": str(e)}
        tasks = [loop.run_in_executor(None, _fetch_sync, u) for u in urls]
        return await asyncio.gather(*tasks)



def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.days < 0:
        raise SystemExit("--days must be 0 or greater")
    if args.results_per_query < 1:
        raise SystemExit("--results-per-query must be at least 1")
    if args.max_discovery_queries < 0:
        raise SystemExit("--max-discovery-queries must be 0 or greater")
    if args.max_career_pages < 0:
        raise SystemExit("--max-career-pages must be 0 or greater")
    if args.max_lever_pages < 0:
        raise SystemExit("--max-lever-pages must be 0 or greater")
    if args.max_fallback_pages < 0:
        raise SystemExit("--max-fallback-pages must be 0 or greater")
    if args.search_retries < 0:
        raise SystemExit("--search-retries must be 0 or greater")
    if args.live_check_timeout is not None and args.live_check_timeout <= 0:
        raise SystemExit("--live-check-timeout must be greater than 0")
    if args.posted_on is not None and (
        args.posted_since is not None or args.posted_until is not None
    ):
        raise SystemExit("--posted-on cannot be combined with --posted-since or --posted-until")

    posted_since = args.posted_on or args.posted_since
    posted_until = args.posted_on or args.posted_until
    if posted_since is not None and posted_until is not None and posted_since > posted_until:
        raise SystemExit("--posted-since must not be later than --posted-until")

    base_role_terms = split_repeated_terms(args.role_types)
    role_aliases = split_repeated_terms(args.role_alias)
    ai_terms = split_terms(args.ai_terms, DEFAULT_AI_TERMS)
    exclude_terms = split_terms(args.exclude_terms, ())
    raw_location_values = args.location if args.location is not None else DEFAULT_LOCATION_TERMS
    base_location_terms = list(
        dict.fromkeys(
            clean_whitespace(value) for value in raw_location_values if clean_whitespace(value)
        )
    )
    custom_location_aliases = [
        clean_whitespace(value) for value in args.location_alias if clean_whitespace(value)
    ]
    if args.match_mode == "expanded":
        role_terms = expand_aliases(base_role_terms, ROLE_ALIAS_MAP, role_aliases)
        discovery_role_terms = canonical_discovery_terms(
            base_role_terms,
            ROLE_ALIAS_MAP,
            role_aliases,
        )
        location_terms = expand_aliases(
            base_location_terms,
            LOCATION_ALIAS_MAP,
            custom_location_aliases,
        )
    else:
        role_terms = list(dict.fromkeys([*base_role_terms, *role_aliases]))
        discovery_role_terms = list(role_terms)
        location_terms = list(dict.fromkeys([*base_location_terms, *custom_location_aliases]))
    selected_platforms = tuple(dict.fromkeys(args.ats_platforms))
    allowed_platforms = set(selected_platforms)
    site_hosts = tuple(
        host for platform in selected_platforms for host in ATS_SEARCH_HOSTS[platform]
    )
    discovery_mode = "exhaustive" if args.exhaustive else args.discovery_mode
    discovery_queries = build_discovery_queries(
        role_terms=discovery_role_terms,
        ai_terms=ai_terms,
        location_terms=location_terms,
        extra_phrases=args.search_phrases or [],
        mode=discovery_mode,
    )

    raw_backends = split_repeated_terms(args.search_backend)
    search_backends: list[str] = []
    for backend in raw_backends or ["auto"]:
        search_backends.extend(ALL_DDGS_BACKENDS if backend.casefold() == "all" else [backend])
    search_backends = list(dict.fromkeys(backend.casefold() for backend in search_backends))
    discovery_regions = list(
        dict.fromkeys(
            clean_whitespace(region)
            for region in args.discovery_regions
            if clean_whitespace(region)
        )
    ) or ["wt-wt"]

    if not role_terms:
        raise SystemExit("Provide at least one non-empty --role-type value")
    if not base_location_terms:
        raise SystemExit("Provide at least one non-empty --location value")
    now = datetime.now(UTC)
    live_timeout = args.live_check_timeout or args.timeout
    include_unknown_dates = args.include_unknown_dates or (
        discovery_mode == "exhaustive"
        and args.days == 0
        and posted_since is None
        and posted_until is None
    )
    criteria = SearchCriteria(
        role_terms=tuple(role_terms),
        ai_terms=tuple(ai_terms),
        exclude_terms=tuple(exclude_terms),
        location_terms=tuple(location_terms),
        days=args.days,
        include_unknown_dates=include_unknown_dates,
        posted_since=posted_since,
        posted_until=posted_until,
        match_mode=args.match_mode,
    )
    coverage_criteria = {
        "role_terms": role_terms,
        "discovery_role_terms": discovery_role_terms,
        "ai_terms": ai_terms,
        "location_terms": location_terms,
        "platforms": selected_platforms,
        "posted_since": posted_since.isoformat() if posted_since else "",
        "posted_until": posted_until.isoformat() if posted_until else "",
        "discovery_mode": discovery_mode,
        "match_mode": args.match_mode,
        "include_unknown_dates": include_unknown_dates,
    }

    user_agent = "Mozilla/5.0 (compatible; AccountFreeATSJobSearch/1.0; +https://github.com/)"
    session = make_session(user_agent)

    catalog = DiscoveryCache() if args.clear_cache else load_discovery_cache(args.cache)
    cached_board_count = len(catalog.boards)
    cached_candidate_count = sum(len(items) for items in catalog.candidates_by_board.values())
    boards = {board for board in catalog.boards if board.platform in allowed_platforms}
    candidates_by_board: dict[str, list[SearchCandidate]] = {}
    for bucket in catalog.candidates_by_board.values():
        for candidate in bucket:
            if candidate.board is not None and candidate.board.platform in allowed_platforms:
                add_candidate(candidates_by_board, candidate)

    def add_seed_url(raw_url: str, provenance: str) -> None:
        board = board_from_url(raw_url)
        if board is None:
            LOGGER.warning("Could not recognize board URL: %s", raw_url)
        elif board.platform not in allowed_platforms:
            LOGGER.warning(
                "Skipping %s board URL because --ats-platform is limited to: %s",
                board.platform,
                ", ".join(selected_platforms),
            )
        else:
            boards.add(board)
            catalog.boards.add(board)
            if looks_like_job_url(raw_url):
                candidate = SearchCandidate(
                    url=raw_url,
                    board=board,
                    provenance=[provenance],
                    first_seen_at=iso_or_blank(now),
                    last_seen_at=iso_or_blank(now),
                )
                add_candidate(candidates_by_board, candidate)
                add_candidate(catalog.candidates_by_board, candidate)

    seed_urls = list(args.board_url)
    for boards_file in args.boards_file:
        seed_urls.extend(load_url_file(boards_file))
    for board_url in seed_urls:
        add_seed_url(board_url, "seed_url")

    career_pages = list(args.career_page)
    for career_pages_file in args.career_pages_file:
        career_pages.extend(load_url_file(career_pages_file))
    if career_pages:
        career_boards, career_candidates = discover_boards_from_career_pages(
            session,
            career_pages,
            allowed_platforms=allowed_platforms,
            timeout=args.timeout,
            delay=args.delay,
            max_pages=args.max_career_pages,
        )
        boards.update(career_boards)
        catalog.boards.update(career_boards)
        merge_candidates(candidates_by_board, career_candidates)
        merge_candidates(catalog.candidates_by_board, career_candidates)

    discovery_stats = DiscoveryStats()

    def checkpoint_discovery(
        checkpoint_boards: set[Board],
        checkpoint_candidates: dict[str, list[SearchCandidate]],
        checkpoint_stats: DiscoveryStats,
    ) -> None:
        """Persist discovered artifacts and request history during a long run."""
        catalog.boards.update(checkpoint_boards)
        merge_candidates(catalog.candidates_by_board, checkpoint_candidates)
        catalog.query_history = list(checkpoint_stats.query_log)
        try:
            save_discovery_cache(args.cache, catalog)
        except OSError as exc:
            LOGGER.warning("Could not checkpoint discovery cache %s: %s", args.cache, exc)

    if not args.skip_search:
        # Discovery is intentionally decoupled from final posted-date filters:
        # an old indexed board can contain a brand-new role in its live feed.
        timelimit = (
            discovery_timelimit(
                days=args.days,
                now=now,
                posted_since=posted_since,
                posted_until=posted_until,
            )
            if args.discovery_timelimit == "auto"
            else None
        )

        discovered, discovered_candidates, discovery_stats = discover_boards(
            queries=discovery_queries,
            site_hosts=site_hosts,
            allowed_platforms=allowed_platforms,
            regions=discovery_regions,
            timelimit=timelimit,
            results_per_query=args.results_per_query,
            backends=search_backends,
            timeout=args.timeout,
            delay=args.delay,
            max_queries=args.max_discovery_queries,
            search_retries=args.search_retries,
            on_progress=checkpoint_discovery,
        )
        boards.update(discovered)
        catalog.boards.update(discovered)
        merge_candidates(candidates_by_board, discovered_candidates)
        merge_candidates(catalog.candidates_by_board, discovered_candidates)
        catalog.query_history = list(discovery_stats.query_log)

    save_discovery_cache(args.cache, catalog)

    if not boards:
        write_csv(args.output, [])
        if args.private_generation_output:
            write_private_generation_json(args.private_generation_output, [])
        if not args.no_coverage_report:
            write_coverage_report(
                args.coverage_report,
                {
                    "version": 1,
                    "generated_at": iso_or_blank(now),
                    "criteria": coverage_criteria,
                    "cache": {
                        "boards_loaded": cached_board_count,
                        "candidate_urls_loaded": cached_candidate_count,
                        "boards_saved": len(catalog.boards),
                        "candidate_urls_saved": sum(
                            len(items) for items in catalog.candidates_by_board.values()
                        ),
                    },
                    "discovery": asdict(discovery_stats),
                    "feed_fetch": {
                        "boards_checked": 0,
                        "boards_succeeded": 0,
                        "jobs_from_feeds": 0,
                        "failed_boards": [],
                    },
                    "fallback": {"attempted": 0, "matched": 0, "failed": 0},
                    "results": {
                        "collected_before_deduplication": 0,
                        "deduplicated": 0,
                        "returned": 0,
                        "live_status_counts": {},
                    },
                },
            )
        print(
            "No ATS boards were discovered. Try --search-backend auto, add custom "
            "--search phrases, career pages, or seed one or more --board-url values.",
            file=sys.stderr,
        )
        return 1

    LOGGER.info("Checking %d cached/discovered boards", len(boards))
    collected: list[Job] = []
    failed_boards: set[str] = set()
    fetch_stats = {"boards_checked": 0, "boards_succeeded": 0, "jobs_from_feeds": 0}

    fetch_context = FetchContext(
        criteria=criteria,
        now=now,
        timeout=args.timeout,
        delay=args.delay,
        max_lever_pages=args.max_lever_pages,
    )

    for board in sorted(boards, key=lambda item: (item.platform, item.token.lower())):
        LOGGER.info("Fetching %s board: %s", board.platform, board.token)
        fetch_stats["boards_checked"] += 1
        try:
            board_jobs = fetch_board_jobs(session, board, fetch_context)
            collected.extend(board_jobs)
            fetch_stats["boards_succeeded"] += 1
            fetch_stats["jobs_from_feeds"] += len(board_jobs)
            catalog.board_status[board.key] = {
                **catalog.board_status.get(board.key, {}),
                "last_checked_at": iso_or_blank(now),
                "last_success_at": iso_or_blank(now),
                "last_job_count": len(board_jobs),
                "last_error": "",
            }
        except Exception as exc:
            failed_boards.add(board.key)
            catalog.board_status[board.key] = {
                **catalog.board_status.get(board.key, {}),
                "last_checked_at": iso_or_blank(now),
                "last_failure_at": iso_or_blank(now),
                "last_error": clean_whitespace(exc),
            }
            LOGGER.warning("Board fetch failed for %s: %s", board.key, exc)
        if args.delay > 0:
            time.sleep(args.delay)

    candidate_board_keys = fallback_candidate_board_keys(
        candidates_by_board,
        failed_boards,
        args.scrape_discovered_pages,
    )

    fallback_stats = {"attempted": 0, "matched": 0, "failed": 0}
    seen_fallback_urls: set[str] = set()
    for candidate in iter_candidates_round_robin(candidates_by_board, candidate_board_keys):
        normalized_url = discovery_url_key(candidate.url)
        if normalized_url in seen_fallback_urls:
            continue
        seen_fallback_urls.add(normalized_url)
        if args.max_fallback_pages > 0 and fallback_stats["attempted"] >= args.max_fallback_pages:
            break
        fallback_stats["attempted"] += 1
        try:
            page_jobs = scrape_jsonld_jobs(
                session,
                candidate,
                timeout=args.timeout,
                now=now,
                criteria=criteria,
            )
            collected.extend(page_jobs)
            fallback_stats["matched"] += len(page_jobs)
        except Exception as exc:
            fallback_stats["failed"] += 1
            LOGGER.warning("Fallback page failed for %s: %s", candidate.url, exc)
        if args.delay > 0:
            time.sleep(args.delay)

    jobs = deduplicate_jobs(collected)
    deduplicated_count = len(jobs)
    if args.require_live:
        args.verify_live = True
    if args.verify_live:
        verify_live_jobs(
            session,
            jobs,
            timeout=live_timeout,
            delay=args.delay,
            now=datetime.now(UTC),
            target=args.live_check_target,
        )
    if args.require_live:
        jobs = [job for job in jobs if job.live_status == "live"]
    jobs = sort_jobs(jobs)

    live_status_counts: dict[str, int] = {}
    for job in jobs:
        live_status_counts[job.live_status] = live_status_counts.get(job.live_status, 0) + 1
    if not args.no_coverage_report:
        write_coverage_report(
            args.coverage_report,
            {
                "version": 1,
                "generated_at": iso_or_blank(datetime.now(UTC)),
                "criteria": coverage_criteria,
                "cache": {
                    "boards_loaded": cached_board_count,
                    "candidate_urls_loaded": cached_candidate_count,
                    "boards_saved": len(catalog.boards),
                    "candidate_urls_saved": sum(
                        len(items) for items in catalog.candidates_by_board.values()
                    ),
                },
                "discovery": asdict(discovery_stats),
                "feed_fetch": {**fetch_stats, "failed_boards": sorted(failed_boards)},
                "fallback": fallback_stats,
                "results": {
                    "collected_before_deduplication": len(collected),
                    "deduplicated": deduplicated_count,
                    "returned": len(jobs),
                    "live_status_counts": live_status_counts,
                },
            },
        )

    save_discovery_cache(args.cache, catalog)
    write_csv(args.output, jobs)
    if args.json_output:
        write_json(args.json_output, jobs)
    if args.private_generation_output:
        write_private_generation_json(args.private_generation_output, jobs)
    print_summary(jobs, args.output, max(0, args.show))

    if failed_boards:
        print(
            f"\nNote: {len(failed_boards)} board feed(s) failed. Candidate pages "
            "were retained for additive JSON-LD fallback and future retries.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
