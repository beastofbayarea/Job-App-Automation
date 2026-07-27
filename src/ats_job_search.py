#!/usr/bin/env python3
"""
Account-free search for open AI Product, Marketing, and Program roles hosted on
Greenhouse, Lever, and Ashby.

How it works:
1. Uses DDGS (DuckDuckGo by default) to discover public ATS job-board URLs.
2. Extracts each company's public board identifier.
3. Pulls currently published jobs from the ATS's public, unauthenticated feed.
4. Filters by role, AI relevance, location, and posting age.
5. Falls back to JobPosting JSON-LD parsing if a discovered board feed fails.
6. Caches discovered boards and writes normalized results to CSV and/or JSON.

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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from project_paths import OUTPUT_DIR

try:
    from ddgs import DDGS
except ImportError:
    try:
        # Compatibility with the package's former name.
        from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        DDGS = None  # type: ignore[assignment,misc]


LOGGER = logging.getLogger("ats_job_search")
UTC = timezone.utc

DEFAULT_SEARCH_PHRASES = (
    '"AI" product jobs',
    '"AI" marketing jobs',
    '"AI" program jobs',
    '"artificial intelligence" product jobs',
    '"generative AI" program jobs',
)

DEFAULT_ROLE_TERMS = (
    "product",
    "marketing",
    "program",
    "programme",
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
)

SEARCH_SITE_GROUPS = (
    "(site:job-boards.greenhouse.io OR site:boards.greenhouse.io)",
    "(site:jobs.lever.co OR site:jobs.eu.lever.co)",
    "site:jobs.ashbyhq.com",
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
    unique_id: str = field(repr=False, default="")

    def to_csv_row(self) -> dict[str, Any]:
        raw = asdict(self)
        raw.pop("unique_id", None)
        return {field_name: raw.get(field_name, "") for field_name in CSV_FIELDS}


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
) -> bool:
    if days <= 0:
        return True
    if dt is None:
        return include_unknown_dates
    cutoff = now - timedelta(days=days)
    # Allow a small future-clock skew.
    return cutoff <= dt <= now + timedelta(days=1)


def split_terms(raw: str | None, defaults: Sequence[str]) -> list[str]:
    if raw is None:
        return list(defaults)
    return [part.strip() for part in raw.split(",") if part.strip()]


def term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term.strip())
    if re.fullmatch(r"[A-Za-z0-9]+", term.strip()):
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def matching_terms(text: str, terms: Sequence[str]) -> list[str]:
    return [term for term in terms if term_pattern(term).search(text)]


def job_match_reason(
    *,
    title: str,
    description: str,
    location: str,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    location_terms: Sequence[str],
) -> str | None:
    title_text = clean_whitespace(title)
    full_text = clean_whitespace(f"{title_text} {description}")

    roles = matching_terms(title_text, role_terms)
    if not roles:
        return None

    ai_matches = matching_terms(full_text, ai_terms)
    if not ai_matches:
        return None

    excluded = matching_terms(full_text, exclude_terms)
    if excluded:
        return None

    if location_terms:
        location_haystack = clean_whitespace(location)
        if not any(term_pattern(term).search(location_haystack) for term in location_terms):
            return None

    return f"role={'; '.join(roles)} | AI={'; '.join(ai_matches)}"


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
    path = re.sub(r"/apply/?$", "", parsed.path, flags=re.IGNORECASE)
    path = path.rstrip("/") or "/"
    # Preserve query only for Greenhouse embed URLs where it can identify the job.
    query = parsed.query if "greenhouse.io/embed/" in url.lower() else ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def board_from_url(raw_url: str) -> Board | None:
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
        for key in ("for", "board", "board_token"):
            values = query.get(key)
            if values and values[0]:
                region = "eu" if ".eu." in host else "global"
                return Board("greenhouse", unquote(values[0]), region)
    return None


def looks_like_job_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    if host == "jobs.ashbyhq.com":
        return len(parts) >= 2
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return len(parts) >= 2 and parts[-1].lower() != "apply"
    if host in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        return "jobs" in [part.lower() for part in parts] or "gh_jid" in query or "token" in query
    return False


def load_board_cache(path: Path) -> set[Board]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("boards", payload) if isinstance(payload, dict) else payload
        boards = {
            Board(
                platform=str(item["platform"]),
                token=str(item["token"]),
                region=str(item.get("region", "global")),
            )
            for item in items
            if isinstance(item, dict) and item.get("platform") and item.get("token")
        }
        return boards
    except (OSError, ValueError, TypeError, KeyError) as exc:
        LOGGER.warning("Could not read board cache %s: %s", path, exc)
        return set()


def save_board_cache(path: Path, boards: Iterable[Board]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(boards, key=lambda board: (board.platform, board.region, board.token.lower()))
    payload = {
        "version": 1,
        "updated_at": iso_or_blank(datetime.now(UTC)),
        "boards": [asdict(board) for board in ordered],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def ddgs_text_search(
    query: str,
    *,
    region: str,
    timelimit: str | None,
    max_results: int,
    backend: str,
    timeout: float,
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

    try:
        results = client.text(query, **kwargs)
    except TypeError:
        # Older duckduckgo_search versions do not accept every current option.
        kwargs.pop("backend", None)
        results = client.text(query, **kwargs)
    return list(results or [])


def discover_boards(
    *,
    search_phrases: Sequence[str],
    region: str,
    timelimit: str | None,
    results_per_query: int,
    backend: str,
    timeout: float,
    delay: float,
) -> tuple[set[Board], dict[str, list[SearchCandidate]]]:
    boards: set[Board] = set()
    candidates_by_board: dict[str, list[SearchCandidate]] = {}
    seen_urls: set[str] = set()

    for phrase in search_phrases:
        for site_group in SEARCH_SITE_GROUPS:
            query = f"{site_group} {phrase}"
            LOGGER.info("Searching: %s", query)
            try:
                results = ddgs_text_search(
                    query,
                    region=region,
                    timelimit=timelimit,
                    max_results=results_per_query,
                    backend=backend,
                    timeout=timeout,
                )
            except Exception as exc:
                LOGGER.warning("Search failed for %r: %s", query, exc)
                continue

            for result in results:
                raw_url = str(result.get("href") or result.get("url") or "").strip()
                if not raw_url:
                    continue
                url = unwrap_search_url(raw_url)
                normalized = canonical_url(url)
                if normalized in seen_urls:
                    continue
                seen_urls.add(normalized)

                board = board_from_url(url)
                if board is None:
                    continue
                boards.add(board)
                candidate = SearchCandidate(
                    url=url,
                    title=clean_whitespace(result.get("title", "")),
                    snippet=clean_whitespace(result.get("body", "")),
                    board=board,
                )
                if looks_like_job_url(url):
                    candidates_by_board.setdefault(board.key, []).append(candidate)

            if delay > 0:
                time.sleep(delay)

    return boards, candidates_by_board


def ensure_not_expired(value: Any, now: datetime) -> bool:
    valid_through = parse_datetime(value)
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
    item_type = value.get("@type")
    if isinstance(item_type, list):
        return any(str(part).lower() == "jobposting" for part in item_type)
    return str(item_type).lower() == "jobposting"


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


def scrape_jsonld_job(
    session: requests.Session,
    candidate: SearchCandidate,
    *,
    timeout: float,
    now: datetime,
    days: int,
    include_unknown_dates: bool,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    location_terms: Sequence[str],
) -> Job | None:
    response = session.get(candidate.url, timeout=timeout, headers={"Accept": "text/html,*/*;q=0.8"})
    if response.status_code in {404, 410}:
        return None
    response.raise_for_status()

    for value in extract_jsonld_objects(response.text):
        if not is_jobposting_object(value):
            continue
        if not ensure_not_expired(value.get("validThrough"), now):
            return None

        title = clean_whitespace(value.get("title") or candidate.title)
        description = strip_html(value.get("description") or candidate.snippet)
        location = jsonld_location(value)
        reason = job_match_reason(
            title=title,
            description=description,
            location=location,
            role_terms=role_terms,
            ai_terms=ai_terms,
            exclude_terms=exclude_terms,
            location_terms=location_terms,
        )
        if reason is None:
            return None

        posted_dt = parse_datetime(value.get("datePosted"))
        if not is_recent(
            posted_dt,
            days=days,
            now=now,
            include_unknown_dates=include_unknown_dates,
        ):
            return None

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

        job_url = clean_whitespace(value.get("url") or response.url or candidate.url)
        workplace_type = (
            "Remote"
            if str(value.get("jobLocationType", "")).upper() == "TELECOMMUTE"
            else ""
        )
        unique = clean_whitespace(
            (value.get("identifier") or {}).get("value", "")
            if isinstance(value.get("identifier"), dict)
            else value.get("identifier", "")
        )
        if not unique:
            unique = canonical_url(job_url)

        return Job(
            platform=platform_from_url(job_url),
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
            board_token=board.token if board else "",
            date_source="jsonld.datePosted",
            match_reason=reason,
            unique_id=unique,
        )
    return None


def greenhouse_base_url(board: Board) -> str:
    # Greenhouse's documented public Job Board API uses this base URL. The board
    # token remains the same even when the hosted board URL is regional.
    return f"https://boards-api.greenhouse.io/v1/boards/{quote(board.token, safe='')}"


def fetch_greenhouse_jobs(
    session: requests.Session,
    board: Board,
    *,
    timeout: float,
    delay: float,
    now: datetime,
    days: int,
    include_unknown_dates: bool,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    location_terms: Sequence[str],
) -> list[Job]:
    base = greenhouse_base_url(board)
    payload = get_json(session, f"{base}/jobs", params={"content": "true"}, timeout=timeout)
    jobs_raw = payload.get("jobs", []) if isinstance(payload, dict) else []
    normalized: list[Job] = []

    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        title = clean_whitespace(item.get("title"))
        description = strip_html(item.get("content"))
        location = clean_whitespace((item.get("location") or {}).get("name", ""))
        reason = job_match_reason(
            title=title,
            description=description,
            location=location,
            role_terms=role_terms,
            ai_terms=ai_terms,
            exclude_terms=exclude_terms,
            location_terms=location_terms,
        )
        if reason is None:
            continue

        detail = item
        date_source = "updated_at_fallback"
        job_id = item.get("id")
        if job_id is not None:
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

        deadline = parse_datetime(detail.get("application_deadline"))
        if deadline is not None and deadline < now:
            continue

        posted_dt = parse_datetime(detail.get("first_published"))
        if posted_dt is None:
            posted_dt = parse_datetime(detail.get("updated_at"))
            date_source = "updated_at_fallback"
        if not is_recent(
            posted_dt,
            days=days,
            now=now,
            include_unknown_dates=include_unknown_dates,
        ):
            continue

        departments = " | ".join(
            clean_whitespace(dep.get("name"))
            for dep in detail.get("departments", [])
            if isinstance(dep, dict) and dep.get("name")
        )
        offices = " | ".join(
            clean_whitespace(office.get("name"))
            for office in detail.get("offices", [])
            if isinstance(office, dict) and office.get("name")
        )
        location_full = location
        if offices and offices.lower() not in location_full.lower():
            location_full = " | ".join(part for part in (location_full, offices) if part)

        company = clean_whitespace(detail.get("company_name")) or prettify_slug(board.token)
        job_url = clean_whitespace(detail.get("absolute_url") or item.get("absolute_url"))
        unique = f"greenhouse:{board.token}:{job_id}" if job_id is not None else canonical_url(job_url)

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
    *,
    timeout: float,
    delay: float,
    max_pages: int,
    now: datetime,
    days: int,
    include_unknown_dates: bool,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    location_terms: Sequence[str],
) -> list[Job]:
    base = lever_api_base(board)
    page_size = 100
    skip = 0
    all_items: list[dict[str, Any]] = []

    for _ in range(max_pages):
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
        if delay > 0:
            time.sleep(delay)

    normalized: list[Job] = []
    for item in all_items:
        title = clean_whitespace(item.get("text"))
        lists_text = " ".join(
            f"{clean_whitespace(section.get('text'))} {strip_html(section.get('content'))}"
            for section in item.get("lists", [])
            if isinstance(section, dict)
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

        reason = job_match_reason(
            title=title,
            description=description,
            location=location,
            role_terms=role_terms,
            ai_terms=ai_terms,
            exclude_terms=exclude_terms,
            location_terms=location_terms,
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
                    fallback = scrape_jsonld_job(
                        session,
                        candidate,
                        timeout=timeout,
                        now=now,
                        days=days,
                        include_unknown_dates=include_unknown_dates,
                        role_terms=role_terms,
                        ai_terms=ai_terms,
                        exclude_terms=exclude_terms,
                        location_terms=location_terms,
                    )
                except Exception as exc:
                    LOGGER.warning("Lever date fallback failed for %s: %s", hosted_url, exc)
                    fallback = None
                if fallback is not None:
                    posted_dt = parse_datetime(fallback.posted_at)
                    date_source = "jsonld.datePosted"

        if not is_recent(
            posted_dt,
            days=days,
            now=now,
            include_unknown_dates=include_unknown_dates,
        ):
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
                workplace_type=clean_whitespace(item.get("workplaceType")),
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
    *,
    timeout: float,
    now: datetime,
    days: int,
    include_unknown_dates: bool,
    role_terms: Sequence[str],
    ai_terms: Sequence[str],
    exclude_terms: Sequence[str],
    location_terms: Sequence[str],
) -> list[Job]:
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
            for location in item.get("secondaryLocations", [])
            if isinstance(location, dict) and location.get("location")
        ]
        location = " | ".join(
            dict.fromkeys(part for part in [primary_location, *secondary] if part)
        )

        reason = job_match_reason(
            title=title,
            description=description,
            location=location,
            role_terms=role_terms,
            ai_terms=ai_terms,
            exclude_terms=exclude_terms,
            location_terms=location_terms,
        )
        if reason is None:
            continue

        posted_dt = parse_datetime(item.get("publishedAt"))
        if not is_recent(
            posted_dt,
            days=days,
            now=now,
            include_unknown_dates=include_unknown_dates,
        ):
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
                workplace_type=clean_whitespace(item.get("workplaceType")),
                employment_type=clean_whitespace(item.get("employmentType")),
                department=clean_whitespace(item.get("department")),
                team=clean_whitespace(item.get("team")),
                salary=ashby_salary(item.get("compensation")),
                job_url=job_url,
                apply_url=apply_url,
                board_token=board.token,
                date_source="publishedAt",
                match_reason=reason,
                unique_id=unique,
            )
        )

    return normalized


def fetch_board_jobs(
    session: requests.Session,
    board: Board,
    **kwargs: Any,
) -> list[Job]:
    platform_kwargs = dict(kwargs)
    if board.platform == "greenhouse":
        platform_kwargs.pop("max_pages", None)
        return fetch_greenhouse_jobs(session, board, **platform_kwargs)
    if board.platform == "lever":
        return fetch_lever_jobs(session, board, **platform_kwargs)
    if board.platform == "ashby":
        platform_kwargs.pop("delay", None)
        platform_kwargs.pop("max_pages", None)
        return fetch_ashby_jobs(session, board, **platform_kwargs)
    raise ValueError(f"Unsupported platform: {board.platform}")


def deduplicate_jobs(jobs: Iterable[Job]) -> list[Job]:
    result: dict[str, Job] = {}
    for job in jobs:
        key = job.unique_id or canonical_url(job.job_url)
        if not key:
            key = f"{job.company.lower()}|{job.title.lower()}|{job.location.lower()}"
        existing = result.get(key)
        if existing is None:
            result[key] = job
            continue
        # Prefer records with a known posting date and richer metadata.
        existing_score = sum(
            bool(value)
            for value in (
                existing.posted_at,
                existing.company,
                existing.location,
                existing.department,
                existing.salary,
                existing.apply_url,
            )
        )
        new_score = sum(
            bool(value)
            for value in (
                job.posted_at,
                job.company,
                job.location,
                job.department,
                job.salary,
                job.apply_url,
            )
        )
        if new_score > existing_score:
            result[key] = job
    return list(result.values())


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([job.to_csv_row() for job in jobs], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def print_summary(jobs: Sequence[Job], output: Path, limit: int) -> None:
    print(f"\nFound {len(jobs)} matching, currently published jobs.")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover and retrieve open AI Product, Marketing, and Program jobs "
            "from public Greenhouse, Lever, and Ashby boards without API keys."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Keep jobs posted within this many days; 0 disables date filtering (default: 14).",
    )
    parser.add_argument(
        "--search",
        action="append",
        dest="search_phrases",
        help="Custom web-search phrase; repeat for multiple phrases.",
    )
    parser.add_argument(
        "--role-terms",
        help="Comma-separated terms that must appear in the job title.",
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
        default=[],
        help="Keep jobs whose location contains this term; repeat for OR matching.",
    )
    parser.add_argument(
        "--board-url",
        action="append",
        default=[],
        help="Seed a known ATS board or job URL; repeat for multiple boards.",
    )
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Skip DuckDuckGo discovery and use only cached/seeded boards.",
    )
    parser.add_argument(
        "--include-unknown-dates",
        action="store_true",
        help="Keep matching jobs when the platform exposes no reliable posting date.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("ai_jobs.csv"),
        help="CSV output path (default: ai_jobs.csv).",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional JSON output path.",
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
        default="wt-wt",
        help="DDGS search region, such as wt-wt, us-en, or in-en (default: wt-wt).",
    )
    parser.add_argument(
        "--search-backend",
        default="duckduckgo",
        help="DDGS text-search backend (default: duckduckgo; use auto as fallback).",
    )
    parser.add_argument(
        "--results-per-query",
        type=int,
        default=20,
        help="Maximum web results per discovery query (default: 20).",
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
        default=50,
        help="Maximum 100-job Lever pages per company (default: 50).",
    )
    parser.add_argument(
        "--max-fallback-pages",
        type=int,
        default=30,
        help="Maximum discovered job pages to parse after a board-feed failure (default: 30).",
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

    role_terms = split_terms(args.role_terms, DEFAULT_ROLE_TERMS)
    ai_terms = split_terms(args.ai_terms, DEFAULT_AI_TERMS)
    exclude_terms = split_terms(args.exclude_terms, ())
    location_terms = [clean_whitespace(value) for value in args.location if value.strip()]
    search_phrases = args.search_phrases or list(DEFAULT_SEARCH_PHRASES)
    now = datetime.now(UTC)

    user_agent = (
        "Mozilla/5.0 (compatible; AccountFreeATSJobSearch/1.0; "
        "+https://github.com/)"
    )
    session = make_session(user_agent)

    boards = set() if args.clear_cache else load_board_cache(args.cache)
    candidates_by_board: dict[str, list[SearchCandidate]] = {}

    for board_url in args.board_url:
        board = board_from_url(board_url)
        if board is None:
            LOGGER.warning("Could not recognize board URL: %s", board_url)
        else:
            boards.add(board)
            if looks_like_job_url(board_url):
                candidates_by_board.setdefault(board.key, []).append(
                    SearchCandidate(url=board_url, board=board)
                )

    if not args.skip_search:
        if args.days <= 1:
            timelimit = "d"
        elif args.days <= 31:
            timelimit = "m"
        elif args.days <= 366:
            timelimit = "y"
        else:
            timelimit = None

        discovered, discovered_candidates = discover_boards(
            search_phrases=search_phrases,
            region=args.region,
            timelimit=timelimit,
            results_per_query=args.results_per_query,
            backend=args.search_backend,
            timeout=args.timeout,
            delay=args.delay,
        )
        boards.update(discovered)
        for key, candidates in discovered_candidates.items():
            candidates_by_board.setdefault(key, []).extend(candidates)

    save_board_cache(args.cache, boards)

    if not boards:
        write_csv(args.output, [])
        print(
            "No ATS boards were discovered. Try --search-backend auto, add custom "
            "--search phrases, or seed one or more --board-url values.",
            file=sys.stderr,
        )
        return 1

    LOGGER.info("Checking %d cached/discovered boards", len(boards))
    collected: list[Job] = []
    failed_boards: set[str] = set()

    common_kwargs = {
        "timeout": args.timeout,
        "delay": args.delay,
        "max_pages": args.max_lever_pages,
        "now": now,
        "days": args.days,
        "include_unknown_dates": args.include_unknown_dates,
        "role_terms": role_terms,
        "ai_terms": ai_terms,
        "exclude_terms": exclude_terms,
        "location_terms": location_terms,
    }

    for board in sorted(boards, key=lambda item: (item.platform, item.token.lower())):
        LOGGER.info("Fetching %s board: %s", board.platform, board.token)
        try:
            collected.extend(fetch_board_jobs(session, board, **common_kwargs))
        except Exception as exc:
            failed_boards.add(board.key)
            LOGGER.warning("Board fetch failed for %s: %s", board.key, exc)
        if args.delay > 0:
            time.sleep(args.delay)

    # Only scrape individual pages when the preferred public board feed failed.
    fallback_count = 0
    seen_fallback_urls: set[str] = set()
    for board_key in failed_boards:
        for candidate in candidates_by_board.get(board_key, []):
            normalized_url = canonical_url(candidate.url)
            if normalized_url in seen_fallback_urls:
                continue
            seen_fallback_urls.add(normalized_url)
            if fallback_count >= args.max_fallback_pages:
                break
            fallback_count += 1
            try:
                job = scrape_jsonld_job(
                    session,
                    candidate,
                    timeout=args.timeout,
                    now=now,
                    days=args.days,
                    include_unknown_dates=args.include_unknown_dates,
                    role_terms=role_terms,
                    ai_terms=ai_terms,
                    exclude_terms=exclude_terms,
                    location_terms=location_terms,
                )
                if job is not None:
                    collected.append(job)
            except Exception as exc:
                LOGGER.warning("Fallback page failed for %s: %s", candidate.url, exc)
            if args.delay > 0:
                time.sleep(args.delay)

    jobs = sort_jobs(deduplicate_jobs(collected))
    write_csv(args.output, jobs)
    if args.json_output:
        write_json(args.json_output, jobs)
    print_summary(jobs, args.output, max(0, args.show))

    if failed_boards:
        print(
            f"\nNote: {len(failed_boards)} board feed(s) failed; discovered job pages "
            "were used as a limited fallback. Re-run later to retry them.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
