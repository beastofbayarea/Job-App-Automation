"""Data-only models and stable output schema for job-board search.

These records intentionally avoid HTTP, DDGS, CLI parsing, and ATS-specific
logic.  ``search_job_boards`` re-exports every class so established imports
continue to work unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlunparse


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
