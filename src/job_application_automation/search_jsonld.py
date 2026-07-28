"""Pure JSON-LD extraction plus an injected page-to-job adapter.

The public search module supplies its existing models, URL rules, and transport
objects at the boundary.  Keeping this module free of requests and CLI imports
makes Schema.org parsing deterministic and reusable by provider fallbacks.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Iterator


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


def clean_whitespace(value: Any) -> str:
    """Normalize a text-like JSON-LD field without importing search matching."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


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
