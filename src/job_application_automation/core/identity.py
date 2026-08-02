"""Shared normalization and validation for application identities."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .exceptions import InputContractError


_TRACKING_QUERY_KEYS = {
    "campaign",
    "fbclid",
    "gclid",
    "ref",
    "referrer",
    "source",
}
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_PART = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")


def _require_string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InputContractError(f"{field_name} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise InputContractError(f"{field_name} cannot be empty")
    return normalized


def normalize_lookup_text(value: object, field_name: str) -> str:
    """Return a case-insensitive, whitespace-stable lookup value."""
    if not isinstance(value, str):
        raise InputContractError(f"{field_name} must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if not normalized:
        raise InputContractError(f"{field_name} cannot be empty")
    return normalized


def normalize_email(value: object, field_name: str = "email") -> str:
    """Validate and normalize an email address for deterministic lookup.

    The local part is case-folded deliberately: candidate addresses in this
    project are account identifiers rather than case-sensitive SMTP routes.
    """
    if not isinstance(value, str):
        raise InputContractError(f"{field_name} must be a string")
    email = unicodedata.normalize("NFKC", value).strip()
    if (
        not email
        or len(email) > 254
        or any(character.isspace() for character in email)
        or email.count("@") != 1
    ):
        raise InputContractError(f"{field_name} must be a valid email address")
    local_part, domain = email.rsplit("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not _LOCAL_PART.fullmatch(local_part)
    ):
        raise InputContractError(f"{field_name} must be a valid email address")
    try:
        ascii_domain = domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InputContractError(f"{field_name} must contain a valid domain") from exc
    labels = ascii_domain.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise InputContractError(f"{field_name} must contain a valid domain")
    return f"{local_part.casefold()}@{ascii_domain}"


def _uppercase_percent_escape(match: re.Match[str]) -> str:
    return match.group(0).upper()


def canonical_job_url(value: object) -> str:
    """Canonicalize an absolute HTTPS job URL without losing identity queries."""
    if not isinstance(value, str):
        raise InputContractError("job_url must be a string")
    raw_url = unicodedata.normalize("NFKC", value).strip()
    if not raw_url or any(character.isspace() or ord(character) < 32 for character in raw_url):
        raise InputContractError("job_url cannot be empty or contain whitespace/control characters")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise InputContractError("job_url must be a valid absolute HTTPS URL") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise InputContractError("job_url must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise InputContractError("job_url cannot contain credentials")

    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InputContractError("job_url must contain a valid hostname") from exc
    host_for_url = f"[{host}]" if ":" in host else host
    netloc = host_for_url if port in (None, 443) else f"{host_for_url}:{port}"

    path = re.sub(r"/+", "/", parsed.path or "/")
    path = re.sub(r"/(?:apply|application)/?$", "", path, flags=re.IGNORECASE)
    path = path.rstrip("/") or "/"
    path = _PERCENT_ESCAPE.sub(_uppercase_percent_escape, path)

    retained_query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lookup_key = key.casefold()
        if lookup_key.startswith("utm_") or lookup_key in _TRACKING_QUERY_KEYS:
            continue
        retained_query.append((key, item))
    retained_query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    query = urlencode(retained_query, doseq=True)
    return urlunsplit(("https", netloc, path, query, ""))
