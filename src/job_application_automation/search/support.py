"""Consolidated search models, parsing, discovery, liveness, cache, and serialization."""

from __future__ import annotations

import csv
import html
import io
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

from ..core.artifacts import interprocess_file_lock, read_json, write_json
from ..core.identity import canonical_job_url
from ..core.runtime_config import RUNTIME_CONFIG, SearchDefaultsSettings, resolve_runtime_path

"""Data-only models and stable output schema for job-board search.

These records intentionally avoid HTTP, DDGS, CLI parsing, and ATS-specific
logic.  ``search_job_boards`` re-exports every class so established imports
continue to work unchanged.
"""


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


def discovery_url_key(url: str) -> str:
    """Build the candidate-level key without importing CLI URL helpers."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("uddg", "u", "url", "target"):
        values = query.get(key)
        if values:
            candidate = unquote(values[0])
            if candidate.startswith(("http://", "https://")):
                parsed = urlparse(candidate)
                break
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


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
    description: str = field(repr=False, default="")
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

    def to_private_dict(self) -> dict[str, Any]:
        """Return generation inputs that must stay outside the sync branch."""
        return {
            **self.to_csv_row(),
            "description": self.description,
        }


"""Pure term parsing, normalization, alias expansion, and job matching.

Provider discovery and HTTP code deliberately stay out of this module so
matching behavior can be reused and tested without network dependencies.
"""


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
    aliases: Mapping[str, Sequence[str]],
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
    aliases: Mapping[str, Sequence[str]],
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


"""Pure JSON-LD extraction plus an injected page-to-job adapter.

The public search module supplies its existing models, URL rules, and transport
objects at the boundary.  Keeping this module free of requests and CLI imports
makes Schema.org parsing deterministic and reusable by provider fallbacks.
"""


class TextExtractor(HTMLParser):
    """Collect visible text from a HTML fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


class JsonLdExtractor(HTMLParser):
    """Collect application/ld+json script blocks from a document."""

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


def strip_html(value: Any) -> str:
    """Decode a possibly nested HTML fragment into readable plain text."""
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


def extract_jsonld_objects(html_text: str) -> Iterator[dict[str, Any]]:
    """Yield objects from script blocks, including nested Schema.org graphs."""
    parser = JsonLdExtractor()
    parser.feed(html_text)
    parser.close()

    def walk(node: Any) -> Iterator[dict[str, Any]]:
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from walk(value)
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
    """Recognize both bare and fully-qualified Schema.org JobPosting types."""

    def is_jobposting_type(item_type: Any) -> bool:
        normalized = str(item_type).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        return normalized.casefold() == "jobposting"

    item_type = value.get("@type")
    if isinstance(item_type, list):
        return any(is_jobposting_type(part) for part in item_type)
    return is_jobposting_type(item_type)


def jsonld_location(
    value: dict[str, Any], *, clean_text: Callable[[Any], str] = clean_whitespace
) -> str:
    """Render Schema.org job and applicant location forms into one stable field."""
    locations: list[str] = []

    def add_address(address: Any) -> None:
        if isinstance(address, str):
            locations.append(clean_text(address))
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
        rendered = ", ".join(clean_text(part) for part in parts if part)
        if rendered:
            locations.append(rendered)

    job_location = value.get("jobLocation")
    items = job_location if isinstance(job_location, list) else [job_location]
    for item in items:
        if isinstance(item, dict):
            add_address(item.get("address", item))
        elif item:
            locations.append(clean_text(item))

    applicant_location = value.get("applicantLocationRequirements")
    items = applicant_location if isinstance(applicant_location, list) else [applicant_location]
    for item in items:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                locations.append(clean_text(name))
        elif item:
            locations.append(clean_text(item))

    if str(value.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        locations.append("Remote")

    return " | ".join(dict.fromkeys(location for location in locations if location))


def jsonld_salary(value: Any, *, clean_text: Callable[[Any], str] = clean_whitespace) -> str:
    """Render a Schema.org MonetaryAmount-like baseSalary value."""
    if not isinstance(value, dict):
        return ""
    currency = clean_text(value.get("currency", ""))
    interval = ""
    amount: Any = value.get("value")
    if isinstance(amount, dict):
        minimum = amount.get("minValue")
        maximum = amount.get("maxValue")
        unit = amount.get("unitText") or amount.get("unitCode")
        interval = clean_text(unit)
        if minimum is not None and maximum is not None:
            amount_text = f"{minimum} - {maximum}"
        else:
            amount_text = str(minimum if minimum is not None else maximum or "")
    else:
        amount_text = clean_text(amount)
    return clean_text(" ".join(part for part in (currency, amount_text, interval) if part))


def scrape_jsonld_jobs(
    session: Any,
    candidate: Any,
    *,
    timeout: float,
    now: Any,
    criteria: Any,
    extract_objects: Callable[[str], Iterable[dict[str, Any]]],
    is_jobposting: Callable[[dict[str, Any]], bool],
    is_not_expired: Callable[[Any, Any], bool],
    clean_text: Callable[[Any], str],
    strip_html_text: Callable[[Any], str],
    location_from_jsonld: Callable[[dict[str, Any]], str],
    board_from_url: Callable[[str], Any | None],
    prettify_board: Callable[[str], str],
    parse_datetime: Callable[[Any], Any],
    format_datetime: Callable[[Any], str],
    age_in_days: Callable[[Any, Any], int | str],
    salary_from_jsonld: Callable[[Any], str],
    canonical_url: Callable[[str], str],
    normalize_text: Callable[[Any], str],
    make_job: Callable[..., Any],
) -> list[Any]:
    """Fetch one candidate page and construct every matching JobPosting record."""
    response = session.get(
        candidate.url, timeout=timeout, headers={"Accept": "text/html,*/*;q=0.8"}
    )
    if response.status_code in {404, 410}:
        return []
    response.raise_for_status()

    jobs: list[Any] = []
    for value in extract_objects(response.text):
        if not is_jobposting(value):
            continue
        if not is_not_expired(value.get("validThrough"), now):
            # A multi-role page can contain an expired record before a live one.
            continue

        title = clean_text(value.get("title") or candidate.title)
        description = strip_html_text(value.get("description") or candidate.snippet)
        location = location_from_jsonld(value)
        workplace_type = (
            "Remote" if str(value.get("jobLocationType", "")).upper() == "TELECOMMUTE" else ""
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
            company = clean_text(organization.get("name", ""))
        else:
            company = clean_text(organization)
        board = candidate.board or board_from_url(candidate.url)
        if not company and board:
            company = prettify_board(board.token)

        employment = value.get("employmentType", "")
        if isinstance(employment, list):
            employment_text = " | ".join(clean_text(item) for item in employment)
        else:
            employment_text = clean_text(employment)

        record_url = clean_text(value.get("url"))
        response_url = clean_text(getattr(response, "url", ""))
        job_url = record_url or response_url or candidate.url
        job_board = board_from_url(job_url) or board
        identifier = clean_text(
            (value.get("identifier") or {}).get("value", "")
            if isinstance(value.get("identifier"), dict)
            else value.get("identifier", "")
        )
        unique = identifier or canonical_url(job_url)
        source_page = canonical_url(response_url or candidate.url)
        record_identity = (
            identifier
            or (canonical_url(record_url) if record_url else "")
            or f"{normalize_text(title)}:{format_datetime(posted_dt)}:{normalize_text(location)}"
        )
        source_identity = f"jsonld:{source_page}:{record_identity}"

        jobs.append(
            make_job(
                platform=job_board.platform if job_board else "web",
                company=company,
                title=title,
                posted_at=format_datetime(posted_dt),
                days_old=age_in_days(posted_dt, now),
                location=location,
                workplace_type=workplace_type,
                employment_type=employment_text,
                department="",
                team="",
                salary=salary_from_jsonld(value.get("baseSalary")),
                job_url=job_url,
                apply_url="",
                board_token=job_board.token if job_board else "",
                date_source="jsonld.datePosted",
                match_reason=reason,
                description=description,
                platform_job_id=unique,
                board_region=job_board.region if job_board else "global",
                provider_id_trusted=False,
                source_identity=source_identity,
                url_is_record_specific=bool(record_url),
                live_status="listed",
                live_checked_at=format_datetime(now),
                live_check_source="jsonld_page",
                live_check_http_status=response.status_code,
                live_check_final_url=response_url,
                live_check_reason="jobposting_present_and_not_expired",
                unique_id=unique,
            )
        )
    return jobs


"""Versioned, dependency-injected persistence for search discovery state.

The command module retains its long-standing ``Board`` and ``SearchCandidate``
classes.  This module deliberately operates on factories and small callbacks
instead of importing those classes, which keeps cache migration testable without
pulling in HTTP, DDGS, or CLI dependencies.
"""


DISCOVERY_CACHE_VERSION = 2


def _payload_sections(payload: Any) -> tuple[Any, Any, Any, Any] | None:
    """Return compatible cache sections for version 0 through version 2 data."""
    if isinstance(payload, list):
        # Version 0 stored a bare board list.
        return payload, [], {}, []
    if isinstance(payload, dict):
        return (
            payload.get("boards", []),
            payload.get("candidates", []),
            payload.get("board_status", {}),
            payload.get("query_history", []),
        )
    return None


def decode_discovery_cache(
    payload: Any,
    *,
    make_cache: Callable[..., Any],
    board_from_cache_value: Callable[[Any], Any | None],
    make_candidate: Callable[..., Any],
    add_candidate: Callable[[dict[str, list[Any]], Any], bool],
    clean_text: Callable[[Any], str],
) -> Any:
    """Decode legacy and current discovery cache payloads into caller models.

    Invalid individual entries are ignored, matching the forgiving behavior of
    the original cache reader.  A non-cache payload returns an empty cache.
    """
    sections = _payload_sections(payload)
    if sections is None:
        return make_cache()
    board_items, candidate_items, board_status, query_history = sections

    boards: set[Any] = set()
    if isinstance(board_items, list):
        for item in board_items:
            board = board_from_cache_value(item)
            if board is not None:
                boards.add(board)

    candidates_by_board: dict[str, list[Any]] = {}
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
                make_candidate(
                    url=clean_text(item.get("url")),
                    title=clean_text(item.get("title")),
                    snippet=clean_text(item.get("snippet")),
                    board=board,
                    provenance=[clean_text(value) for value in provenance if value]
                    if isinstance(provenance, list)
                    else [],
                    first_seen_at=clean_text(item.get("first_seen_at")),
                    last_seen_at=clean_text(item.get("last_seen_at")),
                ),
            )

    return make_cache(
        boards=boards,
        candidates_by_board=candidates_by_board,
        board_status=board_status if isinstance(board_status, dict) else {},
        query_history=query_history if isinstance(query_history, list) else [],
    )


def load_discovery_cache(
    path: Path,
    *,
    make_cache: Callable[..., Any],
    decode: Callable[[Any], Any],
    on_error: Callable[[Exception], None],
) -> Any:
    """Read one cache file while isolating filesystem/JSON failures at the edge."""
    if not path.exists():
        return make_cache()
    try:
        return decode(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        on_error(exc)
        return make_cache()


def discovery_cache_payload(cache: Any, *, updated_at: str) -> dict[str, Any]:
    """Encode cache state in the stable version-2 on-disk schema."""
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
    return {
        "version": DISCOVERY_CACHE_VERSION,
        "updated_at": updated_at,
        "boards": [asdict(board) for board in ordered_boards],
        "candidates": candidates,
        "board_status": cache.board_status,
        "query_history": cache.query_history[-1000:],
    }


def save_discovery_cache(
    path: Path,
    cache: Any,
    *,
    updated_at: str,
    write_json: Callable[..., Any],
) -> None:
    """Atomically persist a discovery cache through the caller's storage adapter."""
    write_json(
        path,
        discovery_cache_payload(cache, updated_at=updated_at),
        indent=2,
        ensure_ascii=False,
    )


"""Deterministic discovery planning and one-hop career-page extraction.

Network access remains owned by the compatible CLI module.  The routines here
accept transport and model callbacks so discovery waves can be tested with
simple fakes and never need to instantiate DDGS in unit tests.
"""


class LinkExtractor(HTMLParser):
    """Collect link-like HTML attributes without crawling beyond one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src", "data-url"} and value:
                self.urls.append(value)


def iter_discovery_requests(
    queries: Sequence[Any],
    site_hosts: Sequence[str],
    regions: Sequence[str],
    backends: Sequence[str],
) -> Iterator[tuple[Any, str, str, str]]:
    """Yield a fair diagonal ordering of query/host/region/backend requests."""
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


def extract_ats_urls_from_html(
    html_text: str,
    *,
    base_url: str,
    discovery_url_key: Callable[[str], str],
) -> list[str]:
    """Return deduplicated HTTP(S) ATS links from static page markup."""
    decoded = html.unescape(html_text).replace(r"\/", "/")
    url_pattern = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
    urls: list[str] = []
    seen: set[str] = set()

    parser = LinkExtractor()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception:
        # A raw absolute-url scan still finds useful links in malformed markup.
        parser.urls = []

    raw_urls = [match.group(0) for match in url_pattern.finditer(decoded)]
    raw_urls.extend(parser.urls)
    for raw_url in raw_urls:
        url = urljoin(base_url, raw_url).rstrip('.,;:)]}"')
        if urlparse(url).scheme.lower() not in {"http", "https"}:
            continue
        key = discovery_url_key(url)
        if key and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def discover_boards(
    *,
    queries: Sequence[Any],
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
    stats: Any,
    now_text: str,
    search_text: Callable[..., list[dict[str, Any]]],
    unwrap_url: Callable[[str], str],
    board_from_url: Callable[[str], Any | None],
    looks_like_job_url: Callable[[str], bool],
    make_candidate: Callable[..., Any],
    add_candidate: Callable[[dict[str, list[Any]], Any], bool],
    clean_text: Callable[[Any], str],
    sleep: Callable[[float], None],
    logger: Any,
    on_progress: Callable[[set[Any], dict[str, list[Any]], Any], None] | None = None,
) -> tuple[set[Any], dict[str, list[Any]], Any]:
    """Run discovery using injected I/O while retaining original cache semantics."""
    boards: set[Any] = set()
    candidates_by_board: dict[str, list[Any]] = {}
    stats.queries_planned = len(queries) * len(site_hosts) * len(regions) * len(backends)

    for discovery_query, site_host, region, backend in iter_discovery_requests(
        queries,
        site_hosts,
        regions,
        backends,
    ):
        if max_queries > 0 and stats.queries_attempted >= max_queries:
            logger.warning(
                "Discovery query budget reached (%d of %d planned). Use "
                "--max-discovery-queries 0 to run every planned query.",
                stats.queries_attempted,
                stats.queries_planned,
            )
            return boards, candidates_by_board, stats
        query = f"{site_host} {discovery_query.text}"
        logger.info("Searching [%s/%s]: %s", backend, region, query)
        stats.queries_attempted += 1
        try:
            results = search_text(
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
                status=f"error: {clean_text(exc)}",
            )
            logger.warning("Search failed for %r: %s", query, exc)
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
            if not isinstance(result, dict):
                continue
            raw_url = str(result.get("href") or result.get("url") or "").strip()
            if not raw_url:
                continue
            url = unwrap_url(raw_url)
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
                make_candidate(
                    url=url,
                    title=clean_text(result.get("title", "")),
                    snippet=clean_text(result.get("body", "")),
                    board=board,
                    provenance=[provenance],
                    first_seen_at=now_text,
                    last_seen_at=now_text,
                ),
            )
            stats.candidates_discovered += int(added)

        if delay > 0:
            sleep(delay)
        if on_progress is not None:
            on_progress(boards, candidates_by_board, stats)

    return boards, candidates_by_board, stats


def discover_boards_from_career_pages(
    session: Any,
    page_urls: Sequence[str],
    *,
    allowed_platforms: set[str],
    timeout: float,
    delay: float,
    max_pages: int,
    now_text: str,
    extract_urls: Callable[..., list[str]],
    board_from_url: Callable[[str], Any | None],
    looks_like_job_url: Callable[[str], bool],
    make_candidate: Callable[..., Any],
    add_candidate: Callable[[dict[str, list[Any]], Any], bool],
    sleep: Callable[[float], None],
    logger: Any,
) -> tuple[set[Any], dict[str, list[Any]]]:
    """Discover boards from explicitly supplied career pages with injectable I/O."""
    boards: set[Any] = set()
    candidates_by_board: dict[str, list[Any]] = {}
    checked = 0
    for raw_url in page_urls:
        if max_pages > 0 and checked >= max_pages:
            break
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"}:
            logger.warning("Skipping non-HTTP career page: %s", raw_url)
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
            logger.warning("Career-page fetch failed for %s: %s", raw_url, exc)
            continue

        for url in [
            response.url,
            *extract_urls(response.text, base_url=response.url),
        ]:
            board = board_from_url(url)
            if board is None or board.platform not in allowed_platforms:
                continue
            boards.add(board)
            if looks_like_job_url(url):
                add_candidate(
                    candidates_by_board,
                    make_candidate(
                        url=url,
                        board=board,
                        provenance=[f"career_page|{raw_url}"],
                        first_seen_at=now_text,
                        last_seen_at=now_text,
                    ),
                )
        if delay > 0:
            sleep(delay)
    return boards, candidates_by_board


"""Pure liveness decisions for public ATS listings and job pages.

HTTP remains in the compatibility module; these functions only classify
already-fetched response data.  That keeps the cautious "listed is not
discarded on a bot-blocked page" rule independently testable.
"""


@dataclass(frozen=True)
class LivenessDecision:
    """A side-effect-free update to apply to a search ``Job`` record."""

    status: str | None
    source: str
    reason: str
    http_status: int | str = ""
    final_url: str = ""
    preserve_listing: bool = False


def page_uncertainty(
    *,
    existing_status: str,
    reason: str,
    http_status: int | str = "",
    final_url: str = "",
) -> LivenessDecision:
    """Return a conservative decision when a page cannot prove job identity."""
    # A page-level timeout or bot block cannot overturn either a positive
    # provider listing or an authoritative provider closure.
    preserve_listing = existing_status in {"listed", "live", "closed"}
    return LivenessDecision(
        status=None if preserve_listing else "unknown",
        source="job_page",
        reason=reason,
        http_status=http_status,
        final_url=final_url,
        preserve_listing=preserve_listing,
    )


def page_jsonld_decision(
    value: dict[str, Any],
    *,
    now: Any,
    is_not_expired: Callable[[Any, Any], bool],
    http_status: int | str,
    final_url: str,
) -> LivenessDecision:
    """Classify a JSON-LD JobPosting once its URL/title identity was verified."""
    if not is_not_expired(value.get("validThrough"), now):
        return LivenessDecision(
            status="closed",
            source="job_page_jsonld",
            reason="valid_through_elapsed",
            http_status=http_status,
            final_url=final_url,
        )
    return LivenessDecision(
        status="live",
        source="job_page_jsonld",
        reason="matching_unexpired_jobposting",
        http_status=http_status,
        final_url=final_url,
    )


def page_response_decision(
    *,
    status_code: int,
    response_url: str,
    html_text: str,
    job_title: str,
    job_urls: Iterable[str],
    existing_status: str,
    dead_role_markers: Sequence[str],
    canonical_url: Callable[[str], str],
    clean_text: Callable[[Any], str],
    normalize_text: Callable[[Any], str],
    extract_jsonld_objects: Callable[[str], Iterable[dict[str, Any]]],
    is_jobposting_object: Callable[[dict[str, Any]], bool],
    is_not_expired: Callable[[Any, Any], bool],
    now: Any,
) -> LivenessDecision:
    """Classify a job-page response without performing requests or mutations."""
    if status_code in {404, 410}:
        return LivenessDecision(
            status="closed",
            source="job_page",
            reason=f"http_{status_code}",
            http_status=status_code,
            final_url=response_url,
        )
    if status_code >= 400:
        return page_uncertainty(
            existing_status=existing_status,
            reason=f"http_{status_code}",
            http_status=status_code,
            final_url=response_url,
        )
    if any(marker in html_text.casefold() for marker in dead_role_markers):
        return LivenessDecision(
            status="closed",
            source="job_page",
            reason="explicit_closed_message",
            http_status=status_code,
            final_url=response_url,
        )

    expected_urls = {canonical_url(value) for value in job_urls if clean_text(value)}
    response_targets_job = canonical_url(response_url) in expected_urls
    title_only_records: list[dict[str, Any]] = []
    for value in extract_jsonld_objects(html_text):
        if not is_jobposting_object(value):
            continue
        title = clean_text(value.get("title"))
        if not title or normalize_text(title) != normalize_text(job_title):
            continue
        record_url = clean_text(value.get("url"))
        if record_url:
            if canonical_url(record_url) not in expected_urls:
                # An index page can mention a similarly titled but different role.
                continue
            return page_jsonld_decision(
                value,
                now=now,
                is_not_expired=is_not_expired,
                http_status=status_code,
                final_url=response_url,
            )
        if response_targets_job:
            title_only_records.append(value)

    if len(title_only_records) == 1:
        return page_jsonld_decision(
            title_only_records[0],
            now=now,
            is_not_expired=is_not_expired,
            http_status=status_code,
            final_url=response_url,
        )
    return page_uncertainty(
        existing_status=existing_status,
        reason="no_positive_job_identity_evidence",
        http_status=status_code,
        final_url=response_url,
    )


def page_jobs_for_target(jobs: Sequence[Any], target: str) -> list[Any]:
    """Select records needing a page request after provider-listing checks."""
    if target in {"application", "both"}:
        return list(jobs)
    return [job for job in jobs if not job.provider_id_trusted]


"""Search-result serialization free of filesystem and CLI side effects."""


def job_rows(jobs: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert compatible search Job objects to the established output schema."""
    return [job.to_csv_row() for job in jobs]


def render_csv(rows: Iterable[dict[str, Any]], *, fieldnames: Sequence[str]) -> str:
    """Render CSV text with the legacy column order and newline behavior."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def render_json(payload: Any) -> str:
    """Render the UTF-8 JSON shape historically written by search commands."""
    return json.dumps(payload, indent=2, ensure_ascii=False)


"""Typed access to the validated runtime configuration for job search."""


SEARCH_SETTINGS = RUNTIME_CONFIG.search

SEARCH_PHRASE_TEMPLATES = SEARCH_SETTINGS.search_phrase_templates
DEFAULT_AI_TERMS = SEARCH_SETTINGS.ai_terms
AI_DISCOVERY_TERMS = SEARCH_SETTINGS.ai_discovery_terms
DEFAULT_LOCATION_TERMS = SEARCH_SETTINGS.default_locations
ROLE_FAMILY_VARIANTS = dict(SEARCH_SETTINGS.role_families)
ROLE_FAMILY_INPUT_ALIASES = dict(SEARCH_SETTINGS.role_family_input_aliases)
LOCATION_ALIAS_MAP = dict(SEARCH_SETTINGS.location_aliases)
GENERIC_ATS_HOST_SUFFIXES = SEARCH_SETTINGS.generic_ats_host_suffixes
ATS_SEARCH_HOSTS = dict(SEARCH_SETTINGS.ats_hosts)
SUPPORTED_ATS_PLATFORMS = tuple(ATS_SEARCH_HOSTS)
ALL_DDGS_BACKENDS = SEARCH_SETTINGS.ddgs_backends
DEAD_ROLE_MARKERS = SEARCH_SETTINGS.dead_role_markers
RESTRICTED_URL_PATTERNS = SEARCH_SETTINGS.restricted_url_patterns
WORKABLE_SHORT_LINK_BOARD = SEARCH_SETTINGS.workable_short_link_board
PROVIDER_API_URLS = dict(SEARCH_SETTINGS.provider_api_urls)
RESTRICTED_BOARD_KEYS = {
    (platform, token)
    for platform, tokens in SEARCH_SETTINGS.restricted_board_tokens.items()
    for token in tokens
}


def build_role_alias_map() -> dict[str, tuple[str, ...]]:
    """Build role aliases from configured families to prevent drift."""
    aliases: dict[str, tuple[str, ...]] = {
        "program": ("Program", "Programme"),
        "programme": ("Program", "Programme"),
    }
    for family_name, input_aliases in ROLE_FAMILY_INPUT_ALIASES.items():
        aliases.update(
            {input_alias: ROLE_FAMILY_VARIANTS[family_name] for input_alias in input_aliases}
        )
    return aliases


ROLE_ALIAS_MAP = build_role_alias_map()


@dataclass(frozen=True, slots=True)
class SearchDefaults:
    days: int
    discovery_mode: str
    max_discovery_queries: int
    discovery_timelimit: str
    match_mode: str
    max_career_pages: int
    scrape_discovered_pages: str
    live_check_target: str
    output_file: Path
    coverage_report_file: Path
    cache_file: Path
    discovery_region: str
    search_backend: str
    search_retries: int
    results_per_query: int
    timeout_seconds: float
    delay_seconds: float
    max_lever_pages: int
    max_fallback_pages: int
    show_results: int
    async_timeout_seconds: float
    user_agent: str

    @classmethod
    def from_settings(cls, values: SearchDefaultsSettings) -> SearchDefaults:
        """Resolve typed runtime defaults into command-ready values."""
        return cls(
            days=values.days,
            discovery_mode=values.discovery_mode,
            max_discovery_queries=values.max_discovery_queries,
            discovery_timelimit=values.discovery_timelimit,
            match_mode=values.match_mode,
            max_career_pages=values.max_career_pages,
            scrape_discovered_pages=values.scrape_discovered_pages,
            live_check_target=values.live_check_target,
            output_file=resolve_runtime_path(values.output_file),
            coverage_report_file=resolve_runtime_path(values.coverage_report_file),
            cache_file=resolve_runtime_path(values.cache_file),
            discovery_region=values.discovery_region,
            search_backend=values.search_backend,
            search_retries=values.search_retries,
            results_per_query=values.results_per_query,
            timeout_seconds=values.timeout_seconds,
            delay_seconds=values.delay_seconds,
            max_lever_pages=values.max_lever_pages,
            max_fallback_pages=values.max_fallback_pages,
            show_results=values.show_results,
            async_timeout_seconds=values.async_timeout_seconds,
            user_agent=values.user_agent,
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> SearchDefaults:
        """Retain the version-one mapping constructor during migration."""
        return cls.from_settings(SearchDefaultsSettings.from_mapping(values))


DEFAULTS = SearchDefaults.from_settings(SEARCH_SETTINGS.defaults)


"""Persistent active-job backlog shared by search and application workers."""


UTC = timezone.utc
BACKLOG_VERSION = 1
CONFIRMED_STATUS = "SUBMITTED & CONFIRMED"


@dataclass
class BacklogEntry:
    """One active job plus discovery timestamps retained across searches."""

    job: Job
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class BacklogUpdate:
    """Summary of one atomic backlog reconciliation."""

    loaded: int
    candidates: int
    removed_confirmed: int
    removed_closed: int
    retained: int


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _boolean(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def _scalar(value: object, default: int | str = "") -> int | str:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, str)):
        return value
    return default


def _ashby_url_proves_provider_identity(job: Job) -> bool:
    """Recognize an exact Ashby record even when its source omitted trust flags."""
    if job.platform.casefold() != "ashby" or not job.board_token or not job.platform_job_id:
        return False
    for value in (job.job_url, job.apply_url):
        parsed = urlparse(value)
        if (parsed.hostname or "").casefold() != "jobs.ashbyhq.com":
            continue
        segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
        if (
            len(segments) >= 2
            and segments[0].casefold() == job.board_token.casefold()
            and segments[1].casefold() == job.platform_job_id.casefold()
        ):
            return True
    return False


def _normalize_exact_ashby_identity(job: Job) -> None:
    if not job.provider_id_trusted and _ashby_url_proves_provider_identity(job):
        job.provider_id_trusted = True
        job.url_is_record_specific = True


def _prefer_current_ashby_metadata(current: Job, incoming: Job) -> bool:
    """Keep authoritative Ashby API metadata over duplicate page-discovery rows."""
    if not (
        _ashby_url_proves_provider_identity(current)
        and _ashby_url_proves_provider_identity(incoming)
    ):
        return False
    source_rank = {
        # Both are provider-authoritative. Equal rank preserves the normal
        # incoming-wins refresh behavior between current feed observations.
        "ashby_board_api": 2,
        "ashby_public_board": 2,
    }
    current_rank = source_rank.get(
        current.live_check_source.casefold(),
        1 if current.provider_id_trusted else 0,
    )
    incoming_rank = source_rank.get(
        incoming.live_check_source.casefold(),
        1 if incoming.provider_id_trusted else 0,
    )
    return current_rank > incoming_rank


def job_from_mapping(value: Mapping[str, object]) -> Job:
    """Rebuild the public, liveness-capable portion of a serialized job."""
    required = {
        field_name: _text(value.get(field_name))
        for field_name in ("platform", "company", "title", "job_url")
    }
    missing = [field_name for field_name, field_value in required.items() if not field_value]
    if missing:
        raise ValueError(f"backlog job is missing required fields: {', '.join(missing)}")
    job = Job(
        platform=required["platform"].casefold(),
        company=required["company"],
        title=required["title"],
        posted_at=_text(value.get("posted_at")),
        days_old=_scalar(value.get("days_old")),
        location=_text(value.get("location")),
        workplace_type=_text(value.get("workplace_type")),
        employment_type=_text(value.get("employment_type")),
        department=_text(value.get("department")),
        team=_text(value.get("team")),
        salary=_text(value.get("salary")),
        job_url=required["job_url"],
        apply_url=_text(value.get("apply_url")) or required["job_url"],
        board_token=_text(value.get("board_token")),
        date_source=_text(value.get("date_source")),
        match_reason=_text(value.get("match_reason")),
        description="",
        platform_job_id=_text(value.get("platform_job_id")),
        board_region=_text(value.get("board_region"), "global") or "global",
        provider_id_trusted=_boolean(value.get("provider_id_trusted"), False),
        source_identity=_text(value.get("source_identity")),
        url_is_record_specific=_boolean(value.get("url_is_record_specific"), True),
        live_status=_text(value.get("live_status"), "unknown") or "unknown",
        live_checked_at=_text(value.get("live_checked_at")),
        live_check_source=_text(value.get("live_check_source")),
        live_check_http_status=_scalar(value.get("live_check_http_status")),
        live_check_final_url=_text(value.get("live_check_final_url")),
        live_check_reason=_text(value.get("live_check_reason")),
        unique_id=_text(value.get("unique_id")),
    )
    if not job.provider_id_trusted and _ashby_url_proves_provider_identity(job):
        # Older CSV/JSON-LD artifacts did not serialize provider trust. The
        # exact board-token/job-id URL is itself record-specific evidence and
        # lets the migrated row merge with the authoritative Ashby API row.
        _normalize_exact_ashby_identity(job)
    return job


def _entry_from_mapping(value: Mapping[str, object], *, fallback_seen_at: str) -> BacklogEntry:
    first_seen_at = _text(value.get("first_seen_at")) or fallback_seen_at
    last_seen_at = _text(value.get("last_seen_at")) or first_seen_at
    return BacklogEntry(
        job=job_from_mapping(value),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


def _entry_payload(entry: BacklogEntry) -> dict[str, object]:
    return {
        **entry.job.to_csv_row(),
        "board_region": entry.job.board_region,
        "provider_id_trusted": entry.job.provider_id_trusted,
        "source_identity": entry.job.source_identity,
        "url_is_record_specific": entry.job.url_is_record_specific,
        "unique_id": entry.job.unique_id,
        "first_seen_at": entry.first_seen_at,
        "last_seen_at": entry.last_seen_at,
    }


def canonical_job_aliases(job: Job) -> set[str]:
    """Return ledger-compatible identities for both listing and apply URLs."""
    if not (
        job.url_is_record_specific
        or job.provider_id_trusted
        or _ashby_url_proves_provider_identity(job)
    ):
        # A multi-record JSON-LD page can give several jobs the same generic
        # careers-page URL. Only its scoped source identity can distinguish
        # those records; a URL match must never merge or delete every sibling.
        return set()
    aliases: set[str] = set()
    for value in (job.job_url, job.apply_url):
        if not value:
            continue
        try:
            aliases.add(canonical_job_url(value))
        except ValueError:
            continue
    return aliases


def _provider_identity(job: Job) -> str:
    if not (
        (job.provider_id_trusted or _ashby_url_proves_provider_identity(job))
        and job.platform
        and job.board_token
        and job.platform_job_id
    ):
        return ""
    return (
        f"provider:{job.platform.casefold()}:{job.board_region.casefold()}:"
        f"{job.board_token.casefold()}:{job.platform_job_id}"
    )


def _identity_keys(job: Job) -> set[str]:
    keys = {f"url:{alias}" for alias in canonical_job_aliases(job)}
    provider_identity = _provider_identity(job)
    if provider_identity:
        keys.add(provider_identity)
    if job.source_identity:
        keys.add(f"source:{job.source_identity}")
    if job.unique_id and ":" in job.unique_id:
        keys.add(f"unique:{job.unique_id}")
    return keys


def _earliest(left: str, right: str) -> str:
    values = [value for value in (left, right) if value]
    return min(values) if values else ""


def _latest(left: str, right: str) -> str:
    values = [value for value in (left, right) if value]
    return max(values) if values else ""


def _timestamp(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _observation_time(entry: BacklogEntry) -> datetime:
    return max(
        _timestamp(entry.job.live_checked_at),
        _timestamp(entry.last_seen_at),
        _timestamp(entry.first_seen_at),
    )


def merge_entries(entries: Iterable[BacklogEntry]) -> list[BacklogEntry]:
    """Deduplicate entries while preferring the newest supplied job metadata."""
    merged: list[BacklogEntry | None] = []
    identity_index: dict[str, int] = {}
    for incoming in entries:
        keys = _identity_keys(incoming.job)
        matching = sorted({identity_index[key] for key in keys if key in identity_index})
        if not matching:
            index = len(merged)
            _normalize_exact_ashby_identity(incoming.job)
            merged.append(incoming)
        else:
            index = matching[0]
            current = merged[index]
            if current is None:
                raise RuntimeError("backlog identity index referenced an empty entry")
            selected_job = (
                current.job
                if _prefer_current_ashby_metadata(current.job, incoming.job)
                else incoming.job
            )
            _normalize_exact_ashby_identity(selected_job)
            incoming = BacklogEntry(
                job=selected_job,
                first_seen_at=_earliest(current.first_seen_at, incoming.first_seen_at),
                last_seen_at=_latest(current.last_seen_at, incoming.last_seen_at),
            )
            for duplicate_index in matching[1:]:
                duplicate = merged[duplicate_index]
                if duplicate is None:
                    continue
                incoming.first_seen_at = _earliest(incoming.first_seen_at, duplicate.first_seen_at)
                incoming.last_seen_at = _latest(incoming.last_seen_at, duplicate.last_seen_at)
                merged[duplicate_index] = None
                for identity, existing_index in list(identity_index.items()):
                    if existing_index == duplicate_index:
                        identity_index[identity] = index
            merged[index] = incoming
        for key in _identity_keys(incoming.job):
            identity_index[key] = index
    return [entry for entry in merged if entry is not None]


def load_backlog(path: Path) -> list[BacklogEntry]:
    """Load and validate the active backlog, returning an empty list if absent."""
    if not path.exists():
        return []
    payload = read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError("job backlog root must be an object")
    if payload.get("version") != BACKLOG_VERSION:
        raise ValueError(f"unsupported job backlog version: {payload.get('version')!r}")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("job backlog jobs must be an array")
    fallback_seen_at = _text(payload.get("updated_at"))
    entries: list[BacklogEntry] = []
    for index, value in enumerate(raw_jobs):
        if not isinstance(value, Mapping):
            raise ValueError(f"job backlog entry {index} must be an object")
        entries.append(_entry_from_mapping(value, fallback_seen_at=fallback_seen_at))
    return merge_entries(entries)


def _write_backlog(path: Path, entries: Sequence[BacklogEntry], *, updated_at: str) -> None:
    ordered = sorted(
        entries,
        key=lambda entry: (
            entry.job.platform.casefold(),
            entry.job.company.casefold(),
            entry.job.title.casefold(),
            min(canonical_job_aliases(entry.job), default=entry.job.job_url),
        ),
    )
    write_json(
        path,
        {
            "version": BACKLOG_VERSION,
            "updated_at": updated_at,
            "jobs": [_entry_payload(entry) for entry in ordered],
        },
        indent=2,
        sort_keys=False,
    )


def prepare_candidates(
    existing: Sequence[BacklogEntry],
    discovered: Sequence[Job],
    *,
    now: datetime,
) -> list[BacklogEntry]:
    """Merge newly discovered jobs into the persistent candidates to recheck."""
    seen_at = _now_iso(now)
    additions = [
        BacklogEntry(job=job, first_seen_at=seen_at, last_seen_at=seen_at) for job in discovered
    ]
    return merge_entries([*existing, *additions])


def load_confirmed_urls(paths: Iterable[Path]) -> set[str]:
    """Read only exact confirmed-submission evidence from permanent ledgers."""
    confirmed: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        payload = read_json(path)
        if not isinstance(payload, Mapping):
            raise ValueError(f"submission ledger root must be an object: {path}")
        raw_entries = payload.get("jobs", payload)
        entries: Iterable[object]
        if isinstance(raw_entries, Mapping):
            entries = raw_entries.values()
        elif isinstance(raw_entries, list):
            entries = raw_entries
        else:
            continue
        for value in entries:
            if not isinstance(value, Mapping):
                continue
            if _text(value.get("status")) != CONFIRMED_STATUS:
                continue
            job_url = _text(value.get("job_url") or value.get("url"))
            try:
                confirmed.add(canonical_job_url(job_url))
            except ValueError:
                continue
    return confirmed


def _matches_confirmed(job: Job, confirmed_urls: set[str]) -> bool:
    return bool(canonical_job_aliases(job).intersection(confirmed_urls))


def reconcile_backlog(
    path: Path,
    candidates: Sequence[BacklogEntry],
    *,
    admitted_jobs: Sequence[Job],
    submission_logs: Iterable[Path],
    now: datetime,
) -> BacklogUpdate:
    """Atomically merge, prune, and replace the active-only backlog.

    ``candidates`` can include a snapshot read before slow liveness checks.
    Only jobs discovered or migrated by this run may be admitted when they are
    no longer on disk. This prevents a stale concurrent worker from
    resurrecting a job another worker conclusively removed.
    """
    updated_at = _now_iso(now)
    with interprocess_file_lock(path):
        disk_entries = load_backlog(path)
        confirmed_urls = load_confirmed_urls(submission_logs)
        disk_identities = {
            identity for entry in disk_entries for identity in _identity_keys(entry.job)
        }
        disk_observations: dict[str, datetime] = {}
        for entry in disk_entries:
            observation = _observation_time(entry)
            for identity in _identity_keys(entry.job):
                disk_observations[identity] = max(
                    observation,
                    disk_observations.get(identity, datetime.min.replace(tzinfo=UTC)),
                )
        admitted_identities = {
            identity for job in admitted_jobs for identity in _identity_keys(job)
        }
        applicable_candidates: list[BacklogEntry] = []
        for entry in candidates:
            identities = _identity_keys(entry.job)
            matching_disk = identities.intersection(disk_identities)
            if matching_disk:
                current_observation = max(disk_observations[identity] for identity in matching_disk)
                if _observation_time(entry) < current_observation:
                    continue
                applicable_candidates.append(entry)
            elif identities.intersection(admitted_identities):
                applicable_candidates.append(entry)
        combined = merge_entries([*disk_entries, *applicable_candidates])
        retained: list[BacklogEntry] = []
        removed_confirmed = 0
        removed_closed = 0
        for entry in combined:
            if _matches_confirmed(entry.job, confirmed_urls):
                removed_confirmed += 1
                continue
            if entry.job.live_status == "closed":
                removed_closed += 1
                continue
            retained.append(entry)
        _write_backlog(path, retained, updated_at=updated_at)
    return BacklogUpdate(
        loaded=len(disk_entries),
        candidates=len(candidates),
        removed_confirmed=removed_confirmed,
        removed_closed=removed_closed,
        retained=len(retained),
    )


def remove_confirmed_job(path: Path, job_url: str) -> bool:
    """Remove one ledger-confirmed URL without racing another backlog writer."""
    if not path.exists():
        return False
    canonical = canonical_job_url(job_url)
    with interprocess_file_lock(path):
        entries = load_backlog(path)
        retained = [entry for entry in entries if canonical not in canonical_job_aliases(entry.job)]
        if len(retained) == len(entries):
            return False
        _write_backlog(path, retained, updated_at=_now_iso())
    return True


def load_legacy_jobs(path: Path) -> list[Job]:
    """Read safe job metadata from a former CSV, JSON array, or state object."""
    if not path.exists():
        return []
    if path.suffix.casefold() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            values: Iterable[object] = list(csv.DictReader(stream))
    else:
        payload = read_json(path)
        if isinstance(payload, list):
            values = payload
        elif isinstance(payload, Mapping) and isinstance(payload.get("jobs"), Mapping):
            values = payload["jobs"].values()
        else:
            return []
    jobs: list[Job] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        try:
            jobs.append(job_from_mapping(value))
        except ValueError:
            continue
    return jobs
