"""Provider-neutral normalization, transport, and liveness helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import requests

from .. import jsonld as _search_jsonld
from ..models import Job
from ..terms import clean_whitespace


UTC = timezone.utc


def strip_html(value: Any) -> str:
    """Convert an optional HTML fragment to normalized plain text."""
    return _search_jsonld.strip_html(value)


def mapping_text(value: Any, key: str = "name") -> str:
    """Safely read a text field from an optional provider response object."""
    return clean_whitespace(value.get(key)) if isinstance(value, dict) else ""


def mapping_items(value: Any) -> list[dict[str, Any]]:
    """Return only mappings from an optional list-like API field."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def prettify_slug(slug: str) -> str:
    value = unquote(slug).replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in value.split()) or slug


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
    """Parse an epoch number, ISO 8601, or RFC 2822 timestamp into UTC."""
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        number = float(value)
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
    """Parse an expiration value, treating a calendar date as inclusive."""
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


def canonical_url(url: str) -> str:
    """Build the established final-result URL identity key."""
    parsed = urlparse(url)
    path = re.sub(r"/(?:apply|application)/?$", "", parsed.path, flags=re.IGNORECASE)
    path = path.rstrip("/") or "/"
    query = parsed.query if "greenhouse.io/embed/" in url.lower() else ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


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
