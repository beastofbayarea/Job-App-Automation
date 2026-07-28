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
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import re
import sys
import time
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from paths import OUTPUT_DIR

try:
    from ddgs import DDGS
except ImportError:
    try:
        # Compatibility with the package's former name.
        from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        DDGS = None  # type: ignore[assignment,misc]


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

CSV_FIELDS = (
    "platform",
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
    "date_source",
    "match_reason",
    "platform_job_id",
    "live_status",
    "live_checked_at",
    "live_check_source",
    "live_check_http_status",
    "live_check_final_url",
    "live_check_reason",
)


@dataclass(frozen=True)
class Board:
    platform: str
    token: str
    region: str = "global"

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.region}:{self.token}"


@dataclass
class SearchCandidate:
    url: str
    title: str = ""
    snippet: str = ""
    board: Board | None = None
    provenance: list[str] = field(default_factory=list)
    first_seen_at: str = ""
    last_seen_at: str = ""

    @property
    def cache_key(self) -> str:
        return discovery_url_key(self.url)

    def to_cache_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "board": asdict(self.board) if self.board else None,
            "provenance": self.provenance,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(frozen=True)
class DiscoveryQuery:
    text: str
    family: str


@dataclass
class DiscoveryCache:
    boards: set[Board] = field(default_factory=set)
    candidates_by_board: dict[str, list[SearchCandidate]] = field(default_factory=dict)
    board_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    query_history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class DiscoveryStats:
    queries_planned: int = 0
    queries_attempted: int = 0
    query_failures: int = 0
    results_seen: int = 0
    boards_discovered: int = 0
    candidates_discovered: int = 0
    query_log: list[dict[str, str]] = field(default_factory=list)

    def add_query(
        self,
        *,
        query: str,
        family: str,
        backend: str,
        region: str,
        status: str,
    ) -> None:
        self.query_log.append(
            {
                "query": query,
                "family": family,
                "backend": backend,
                "region": region,
                "status": status,
            }
        )


@dataclass
class Job:
    platform: str
    company: str
    title: str
    posted_at: str
    days_old: int | str
    location: str
    workplace_type: str
    employment_type: str
    department: str
    team: str
    salary: str
    job_url: str
    apply_url: str
    board_token: str
    date_source: str
    match_reason: str
    platform_job_id: str = ""
    board_region: str = "global"
    # JSON-LD identifiers are useful for a source page, but are not guaranteed
    # to be the provider's public API ID. Only feed adapters set this flag.
    provider_id_trusted: bool = False
    # A scoped identity for a record parsed from a multi-job JSON-LD document.
    # It is internal-only and intentionally excluded from CSV/JSON output.
    source_identity: str = field(repr=False, default="")
    url_is_record_specific: bool = True
    live_status: str = "not_checked"
    live_checked_at: str = ""
    live_check_source: str = ""
    live_check_http_status: int | str = ""
    live_check_final_url: str = ""
    live_check_reason: str = ""
    unique_id: str = field(repr=False, default="")

    def to_csv_row(self) -> dict[str, Any]:
        raw = asdict(self)
        raw.pop("unique_id", None)
        return {field_name: raw.get(field_name, "") for field_name in CSV_FIELDS}


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


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


class JsonLdExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_json_ld = False
        self.current: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_map = {key.lower(): (value or "") for key, value in attrs}
        script_type = attr_map.get("type", "").lower().split(";", 1)[0].strip()
        if script_type == "application/ld+json":
            self.in_json_ld = True
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.in_json_ld:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.in_json_ld:
            block = "".join(self.current).strip()
            if block:
                self.blocks.append(block)
            self.current = []
            self.in_json_ld = False


class LinkExtractor(HTMLParser):
    """Collect link-like HTML attributes without crawling beyond one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src", "data-url"} and value:
                self.urls.append(value)


def strip_html(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    # Greenhouse content can be HTML-escaped more than once.
    text = html.unescape(text)
    parser = TextExtractor()
    try:
        parser.feed(text)
        parser.close()
        return " ".join(parser.parts)
    except Exception:
        return re.sub(r"<[^>]+>", " ", text)


def clean_whitespace(value: Any) -> str:
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
        raise RuntimeError(
            f"Expected JSON from {response.url}, received {content_type}"
        ) from exc


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


def term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term.strip())
    if re.fullmatch(r"[A-Za-z0-9]+", term.strip()):
        # Single alphanumeric terms (e.g. "AI", "ML") need boundaries so they don't
        # match inside unrelated words like "said" or "main"; multi-word/symbol
        # terms (e.g. "generative AI") are matched as a plain substring instead.
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def normalize_match_text(value: Any) -> str:
    """Normalize punctuation and whitespace without turning short terms into substrings."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Hyphens, slashes, and underscores commonly vary between ATSs (e.g.
    # Product-Manager, AI/ML, machine_learning).
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
    if not path.exists():
        return DiscoveryCache()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            # Version 0 list format.
            board_items: Any = payload
            candidate_items: Any = []
            board_status: Any = {}
            query_history: Any = []
        elif isinstance(payload, dict):
            board_items = payload.get("boards", [])
            candidate_items = payload.get("candidates", [])
            board_status = payload.get("board_status", {})
            query_history = payload.get("query_history", [])
        else:
            return DiscoveryCache()

        boards: set[Board] = set()
        if isinstance(board_items, list):
            for item in board_items:
                board = board_from_cache_value(item)
                if board is not None:
                    boards.add(board)
        candidates_by_board: dict[str, list[SearchCandidate]] = {}
        if isinstance(candidate_items, list):
            for item in candidate_items:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                board = board_from_cache_value(item.get("board"))
                if board is None:
                    continue
                boards.add(board)
                provenance = item.get("provenance", [])
                add_candidate(
                    candidates_by_board,
                    SearchCandidate(
                        url=clean_whitespace(item.get("url")),
                        title=clean_whitespace(item.get("title")),
                        snippet=clean_whitespace(item.get("snippet")),
                        board=board,
                        provenance=[clean_whitespace(value) for value in provenance if value]
                        if isinstance(provenance, list)
                        else [],
                        first_seen_at=clean_whitespace(item.get("first_seen_at")),
                        last_seen_at=clean_whitespace(item.get("last_seen_at")),
                    ),
                )
        return DiscoveryCache(
            boards=boards,
            candidates_by_board=candidates_by_board,
            board_status=board_status if isinstance(board_status, dict) else {},
            query_history=query_history if isinstance(query_history, list) else [],
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        LOGGER.warning("Could not read discovery cache %s: %s", path, exc)
        return DiscoveryCache()


def save_discovery_cache(path: Path, cache: DiscoveryCache) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered_boards = sorted(
        cache.boards,
        key=lambda board: (board.platform, board.region, board.token.lower()),
    )
    candidates = sorted(
        (
            candidate.to_cache_dict()
            for bucket in cache.candidates_by_board.values()
            for candidate in bucket
        ),
        key=lambda candidate: (str(candidate["board"]), str(candidate["url"])),
    )
    payload = {
        "version": 2,
        "updated_at": iso_or_blank(datetime.now(UTC)),
        "boards": [asdict(board) for board in ordered_boards],
        "candidates": candidates,
        "board_status": cache.board_status,
        "query_history": cache.query_history[-1000:],
    }
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary_path.replace(path)


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
        raise RuntimeError(
            "DDGS is not installed. Run: python -m pip install -U requests ddgs"
        )
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
    """Expand a query plan without starving roles or hosts under a request cap.

    Each pass gives every planned query one request, while a diagonal rotation
    spreads that pass across the requested hosts, regions, and backends. Later
    passes cover each query's remaining transport combinations. This prioritizes
    role-query waves before repeating one query across every search provider.
    """
    variants = [
        (site_host, region, backend)
        for site_host in site_hosts
        for region in regions
        for backend in backends
    ]
    if not variants:
        return
    for offset in range(len(variants)):
        for query_index, discovery_query in enumerate(queries):
            site_host, region, backend = variants[(query_index + offset) % len(variants)]
            yield discovery_query, site_host, region, backend


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
    on_progress: Callable[[set[Board], dict[str, list[SearchCandidate]], DiscoveryStats], None] | None = None,
) -> tuple[set[Board], dict[str, list[SearchCandidate]], DiscoveryStats]:
    """Discover boards and job pages across a phased, provenance-aware query plan."""
    boards: set[Board] = set()
    candidates_by_board: dict[str, list[SearchCandidate]] = {}
    stats = stats or DiscoveryStats()
    stats.queries_planned = len(queries) * len(site_hosts) * len(regions) * len(backends)
    now_text = iso_or_blank(datetime.now(UTC))

    for discovery_query, site_host, region, backend in iter_discovery_requests(
        queries,
        site_hosts,
        regions,
        backends,
    ):
        if max_queries > 0 and stats.queries_attempted >= max_queries:
            LOGGER.warning(
                "Discovery query budget reached (%d of %d planned). Use "
                "--max-discovery-queries 0 to run every planned query.",
                stats.queries_attempted,
                stats.queries_planned,
            )
            return boards, candidates_by_board, stats
        query = f"{site_host} {discovery_query.text}"
        LOGGER.info("Searching [%s/%s]: %s", backend, region, query)
        stats.queries_attempted += 1
        try:
            results = ddgs_text_search(
                query,
                region=region,
                timelimit=timelimit,
                max_results=results_per_query,
                backend=backend,
                timeout=timeout,
                retries=search_retries,
                retry_delay=max(delay, 0.25),
            )
        except Exception as exc:
            stats.query_failures += 1
            stats.add_query(
                query=query,
                family=discovery_query.family,
                backend=backend,
                region=region,
                status=f"error: {clean_whitespace(exc)}",
            )
            LOGGER.warning("Search failed for %r: %s", query, exc)
            if on_progress is not None:
                on_progress(boards, candidates_by_board, stats)
            continue

        stats.add_query(
            query=query,
            family=discovery_query.family,
            backend=backend,
            region=region,
            status=f"ok:{len(results)}",
        )
        stats.results_seen += len(results)
        provenance = f"{discovery_query.family}|{backend}|{region}|{site_host}"
        for result in results:
            raw_url = str(result.get("href") or result.get("url") or "").strip()
            if not raw_url:
                continue
            url = unwrap_search_url(raw_url)
            board = board_from_url(url)
            if board is None or board.platform not in allowed_platforms:
                continue
            before = len(boards)
            boards.add(board)
            stats.boards_discovered += int(len(boards) > before)
            if not looks_like_job_url(url):
                continue
            added = add_candidate(
                candidates_by_board,
                SearchCandidate(
                    url=url,
                    title=clean_whitespace(result.get("title", "")),
                    snippet=clean_whitespace(result.get("body", "")),
                    board=board,
                    provenance=[provenance],
                    first_seen_at=now_text,
                    last_seen_at=now_text,
                ),
            )
            stats.candidates_discovered += int(added)

        if delay > 0:
            time.sleep(delay)
        if on_progress is not None:
            on_progress(boards, candidates_by_board, stats)

    return boards, candidates_by_board, stats


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
    decoded = html.unescape(html_text).replace(r"\/", "/")
    url_pattern = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
    urls: list[str] = []
    seen: set[str] = set()

    parser = LinkExtractor()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception:
        # The raw absolute-URL scan remains useful for malformed career pages.
        parser.urls = []

    raw_urls = [match.group(0) for match in url_pattern.finditer(decoded)]
    raw_urls.extend(parser.urls)
    for raw_url in raw_urls:
        url = urljoin(base_url, raw_url).rstrip(".,;:)]}\"")
        if urlparse(url).scheme.lower() not in {"http", "https"}:
            continue
        key = discovery_url_key(url)
        if key and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def discover_boards_from_career_pages(
    session: requests.Session,
    page_urls: Sequence[str],
    *,
    allowed_platforms: set[str],
    timeout: float,
    delay: float,
    max_pages: int,
) -> tuple[set[Board], dict[str, list[SearchCandidate]]]:
    """Extract ATS links from explicitly supplied company career pages.

    This is intentionally one-hop only: it improves coverage for custom career
    domains without turning the tool into a general-purpose crawler.
    """
    boards: set[Board] = set()
    candidates_by_board: dict[str, list[SearchCandidate]] = {}
    checked = 0
    now_text = iso_or_blank(datetime.now(UTC))
    for raw_url in page_urls:
        if max_pages > 0 and checked >= max_pages:
            break
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"}:
            LOGGER.warning("Skipping non-HTTP career page: %s", raw_url)
            continue
        checked += 1
        try:
            response = session.get(
                raw_url,
                timeout=timeout,
                headers={"Accept": "text/html,*/*;q=0.8"},
            )
            response.raise_for_status()
        except Exception as exc:
            LOGGER.warning("Career-page fetch failed for %s: %s", raw_url, exc)
            continue

        for url in [response.url, *extract_ats_urls_from_html(response.text, base_url=response.url)]:
            board = board_from_url(url)
            if board is None or board.platform not in allowed_platforms:
                continue
            boards.add(board)
            if looks_like_job_url(url):
                add_candidate(
                    candidates_by_board,
                    SearchCandidate(
                        url=url,
                        board=board,
                        provenance=[f"career_page|{raw_url}"],
                        first_seen_at=now_text,
                        last_seen_at=now_text,
                    ),
                )
        if delay > 0:
            time.sleep(delay)
    return boards, candidates_by_board


def ensure_not_expired(value: Any, now: datetime) -> bool:
    valid_through = parse_expiry_datetime(value)
    return valid_through is None or valid_through >= now


def extract_jsonld_objects(html_text: str) -> Iterator[dict[str, Any]]:
    parser = JsonLdExtractor()
    parser.feed(html_text)
    parser.close()

    def walk(node: Any) -> Iterator[dict[str, Any]]:
        if isinstance(node, dict):
            yield node
            graph = node.get("@graph")
            if graph is not None:
                yield from walk(graph)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    for block in parser.blocks:
        cleaned = block.strip().lstrip("\ufeff")
        try:
            data = json.loads(cleaned)
        except ValueError:
            continue
        yield from walk(data)


def is_jobposting_object(value: dict[str, Any]) -> bool:
    def is_jobposting_type(item_type: Any) -> bool:
        normalized = str(item_type).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        return normalized.casefold() == "jobposting"

    item_type = value.get("@type")
    if isinstance(item_type, list):
        return any(is_jobposting_type(part) for part in item_type)
    return is_jobposting_type(item_type)


def jsonld_location(value: dict[str, Any]) -> str:
    locations: list[str] = []

    def add_address(address: Any) -> None:
        if isinstance(address, str):
            locations.append(clean_whitespace(address))
            return
        if not isinstance(address, dict):
            return
        parts = [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("postalCode"),
            address.get("addressCountry"),
        ]
        rendered = ", ".join(clean_whitespace(part) for part in parts if part)
        if rendered:
            locations.append(rendered)

    job_location = value.get("jobLocation")
    items = job_location if isinstance(job_location, list) else [job_location]
    for item in items:
        if isinstance(item, dict):
            add_address(item.get("address", item))
        elif item:
            locations.append(clean_whitespace(item))

    applicant_location = value.get("applicantLocationRequirements")
    items = applicant_location if isinstance(applicant_location, list) else [applicant_location]
    for item in items:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                locations.append(clean_whitespace(name))
        elif item:
            locations.append(clean_whitespace(item))

    if str(value.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        locations.append("Remote")

    return " | ".join(dict.fromkeys(location for location in locations if location))


def jsonld_salary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    currency = clean_whitespace(value.get("currency", ""))
    interval = ""
    amount: Any = value.get("value")
    if isinstance(amount, dict):
        minimum = amount.get("minValue")
        maximum = amount.get("maxValue")
        unit = amount.get("unitText") or amount.get("unitCode")
        interval = clean_whitespace(unit)
        if minimum is not None and maximum is not None:
            amount_text = f"{minimum} - {maximum}"
        else:
            amount_text = str(minimum if minimum is not None else maximum or "")
    else:
        amount_text = clean_whitespace(amount)
    return clean_whitespace(" ".join(part for part in (currency, amount_text, interval) if part))


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
    """Parse every matching JobPosting object on a candidate page."""
    response = session.get(candidate.url, timeout=timeout, headers={"Accept": "text/html,*/*;q=0.8"})
    if response.status_code in {404, 410}:
        return []
    response.raise_for_status()

    jobs: list[Job] = []
    for value in extract_jsonld_objects(response.text):
        if not is_jobposting_object(value):
            continue
        if not ensure_not_expired(value.get("validThrough"), now):
            # Multi-role pages can contain an expired record followed by a live
            # one, so do not abandon the full JSON-LD document here.
            continue

        title = clean_whitespace(value.get("title") or candidate.title)
        description = strip_html(value.get("description") or candidate.snippet)
        location = jsonld_location(value)
        workplace_type = (
            "Remote"
            if str(value.get("jobLocationType", "")).upper() == "TELECOMMUTE"
            else ""
        )
        reason = criteria.matches_job(
            title=title,
            description=description,
            location=location,
            workplace_type=workplace_type,
        )
        if reason is None:
            continue

        posted_dt = parse_datetime(value.get("datePosted"))
        if not criteria.includes_posted_at(posted_dt, now=now):
            continue

        organization = value.get("hiringOrganization")
        if isinstance(organization, dict):
            company = clean_whitespace(organization.get("name", ""))
        else:
            company = clean_whitespace(organization)
        board = candidate.board or board_from_url(candidate.url)
        if not company and board:
            company = prettify_slug(board.token)

        employment = value.get("employmentType", "")
        if isinstance(employment, list):
            employment_text = " | ".join(clean_whitespace(item) for item in employment)
        else:
            employment_text = clean_whitespace(employment)

        record_url = clean_whitespace(value.get("url"))
        job_url = record_url or clean_whitespace(response.url or candidate.url)
        job_board = board_from_url(job_url) or board
        identifier = clean_whitespace(
            (value.get("identifier") or {}).get("value", "")
            if isinstance(value.get("identifier"), dict)
            else value.get("identifier", "")
        )
        unique = identifier or canonical_url(job_url)
        source_page = canonical_url(response.url or candidate.url)
        record_identity = (
            identifier
            or (canonical_url(record_url) if record_url else "")
            or f"{normalize_match_text(title)}:{iso_or_blank(posted_dt)}:"
            f"{normalize_match_text(location)}"
        )
        source_identity = f"jsonld:{source_page}:{record_identity}"

        jobs.append(
            Job(
                platform=job_board.platform if job_board else "web",
                company=company,
                title=title,
                posted_at=iso_or_blank(posted_dt),
                days_old=days_old(posted_dt, now),
                location=location,
                workplace_type=workplace_type,
                employment_type=employment_text,
                department="",
                team="",
                salary=jsonld_salary(value.get("baseSalary")),
                job_url=job_url,
                apply_url="",
                board_token=job_board.token if job_board else "",
                date_source="jsonld.datePosted",
                match_reason=reason,
                platform_job_id=unique,
                board_region=job_board.region if job_board else "global",
                provider_id_trusted=False,
                source_identity=source_identity,
                url_is_record_specific=bool(record_url),
                live_status="listed",
                live_checked_at=iso_or_blank(now),
                live_check_source="jsonld_page",
                live_check_http_status=response.status_code,
                live_check_final_url=response.url,
                live_check_reason="jobposting_present_and_not_expired",
                unique_id=unique,
            )
        )
    return jobs


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
        unique = f"lever:{board.region}:{board.token}:{job_id}" if job_id else canonical_url(job_url)

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
        set_live_status(job, status="unknown", source="greenhouse_job_api", now=now, reason="request_failed")
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
        set_live_status(job, status="unknown", source="lever_posting_api", now=now, reason="request_failed")
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
                set_live_status(job, status="unknown", source="ashby_board_api", now=now, reason="missing_board")
            continue
        url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board_token, safe='')}"
        response = response_or_none(session, url, timeout=timeout)
        if response is None:
            for job in board_jobs:
                set_live_status(job, status="unknown", source="ashby_board_api", now=now, reason="request_failed")
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
    job.live_check_reason = " ; ".join(
        value for value in (job.live_check_reason, reason) if value
    )
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
    if not ensure_not_expired(value.get("validThrough"), now):
        set_live_status(
            job,
            status="closed",
            source="job_page_jsonld",
            now=now,
            reason="valid_through_elapsed",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    set_live_status(
        job,
        status="live",
        source="job_page_jsonld",
        now=now,
        reason="matching_unexpired_jobposting",
        http_status=response.status_code,
        final_url=response.url,
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
        if preserve_listing_status_on_page_uncertainty(job, now=now, reason="missing_job_url"):
            return
        set_live_status(job, status="unknown", source="job_page", now=now, reason="missing_job_url")
        return
    response = response_or_none(session, url, timeout=timeout, accept="text/html,*/*;q=0.8")
    if response is None:
        if preserve_listing_status_on_page_uncertainty(job, now=now, reason="request_failed"):
            return
        set_live_status(job, status="unknown", source="job_page", now=now, reason="request_failed")
        return
    if response.status_code in {404, 410}:
        set_live_status(
            job,
            status="closed",
            source="job_page",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    if response.status_code >= 400:
        if preserve_listing_status_on_page_uncertainty(
            job,
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        ):
            return
        set_live_status(
            job,
            status="unknown",
            source="job_page",
            now=now,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            final_url=response.url,
        )
        return
    page_text = response.text.casefold()
    if any(marker in page_text for marker in DEAD_ROLE_MARKERS):
        set_live_status(
            job,
            status="closed",
            source="job_page",
            now=now,
            reason="explicit_closed_message",
            http_status=response.status_code,
            final_url=response.url,
        )
        return

    expected_urls = {
        canonical_url(value)
        for value in (job.job_url, job.apply_url)
        if clean_whitespace(value)
    }
    response_targets_job = canonical_url(response.url) in expected_urls
    title_only_records: list[dict[str, Any]] = []
    for value in extract_jsonld_objects(response.text):
        if not is_jobposting_object(value):
            continue
        title = clean_whitespace(value.get("title"))
        if not title or normalize_match_text(title) != normalize_match_text(job.title):
            continue
        record_url = clean_whitespace(value.get("url"))
        if record_url:
            if canonical_url(record_url) not in expected_urls:
                # A board or index page can contain another open role with the
                # same title. It is not evidence for this job URL.
                continue
            set_page_jsonld_live_status(job, value, now=now, response=response)
            return
        if response_targets_job:
            title_only_records.append(value)

    if len(title_only_records) == 1:
        set_page_jsonld_live_status(job, title_only_records[0], now=now, response=response)
        return
    if preserve_listing_status_on_page_uncertainty(
        job,
        now=now,
        reason="no_positive_job_identity_evidence",
        http_status=response.status_code,
        final_url=response.url,
    ):
        return
    set_live_status(
        job,
        status="unknown",
        source="job_page",
        now=now,
        reason="no_positive_job_identity_evidence",
        http_status=response.status_code,
        final_url=response.url,
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
    page_jobs = jobs if target in {"application", "both"} else [
        job for job in jobs if not job.provider_id_trusted
    ]
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
    preferred.provider_id_trusted = preferred.provider_id_trusted or supplemental.provider_id_trusted
    preferred.url_is_record_specific = (
        preferred.url_is_record_specific or supplemental.url_is_record_specific
    )
    return preferred


def deduplicate_jobs(jobs: Iterable[Job]) -> list[Job]:
    result: list[Job | None] = []
    identity_index: dict[str, int] = {}
    for job in jobs:
        identities = job_identity_keys(job)
        matching_indices = sorted({identity_index[key] for key in identities if key in identity_index})
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        writer.writerows(job.to_csv_row() for job in jobs)


def write_json(path: Path, jobs: Sequence[Job]) -> None:
    write_json_file(path, [job.to_csv_row() for job in jobs])


def write_coverage_report(path: Path, report: dict[str, Any]) -> None:
    write_json_file(path, report)


def write_json_file(path: Path, payload: Any) -> None:
    """Write a UTF-8 JSON artifact shared by result and coverage outputs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def print_summary(jobs: Sequence[Job], output: Path, limit: int) -> None:
    status_counts: dict[str, int] = {}
    for job in jobs:
        status_counts[job.live_status] = status_counts.get(job.live_status, 0) + 1
    status_summary = ", ".join(
        f"{status}={count}" for status, count in sorted(status_counts.items())
    ) or "none"
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
        if any(candidate.board is not None and candidate.board.platform == "web" for candidate in candidates)
    }
    return sorted(failed_boards | generic_web_boards)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover and retrieve open AI jobs from the requested public ATS "
            "platforms, role types, and locations without API keys."
        ),
        epilog=(
            "Example: python src/search_job_boards.py --role-type product "
            "--ats-platform greenhouse --location \"New York\""
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
    return parser


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
        raise SystemExit(
            "--posted-on cannot be combined with --posted-since or --posted-until"
        )

    posted_since = args.posted_on or args.posted_since
    posted_until = args.posted_on or args.posted_until
    if posted_since is not None and posted_until is not None and posted_since > posted_until:
        raise SystemExit("--posted-since must not be later than --posted-until")

    base_role_terms = split_repeated_terms(args.role_types)
    role_aliases = split_repeated_terms(args.role_alias)
    ai_terms = split_terms(args.ai_terms, DEFAULT_AI_TERMS)
    exclude_terms = split_terms(args.exclude_terms, ())
    raw_location_values = (
        args.location if args.location is not None else DEFAULT_LOCATION_TERMS
    )
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
        host
        for platform in selected_platforms
        for host in ATS_SEARCH_HOSTS[platform]
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
            clean_whitespace(region) for region in args.discovery_regions if clean_whitespace(region)
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

    user_agent = (
        "Mozilla/5.0 (compatible; AccountFreeATSJobSearch/1.0; "
        "+https://github.com/)"
    )
    session = make_session(user_agent)

    catalog = DiscoveryCache() if args.clear_cache else load_discovery_cache(args.cache)
    cached_board_count = len(catalog.boards)
    cached_candidate_count = sum(len(items) for items in catalog.candidates_by_board.values())
    boards = {
        board for board in catalog.boards if board.platform in allowed_platforms
    }
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
