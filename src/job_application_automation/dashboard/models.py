"""Typed contracts for the public, read-only dashboard.

The dashboard intentionally publishes every route without authentication.  The
models in this module keep the filesystem roots, request parsing, and HTTP
responses explicit so service code can be tested without constructing an HTTP
handler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias
from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

Header: TypeAlias = tuple[str, str]


@dataclass(frozen=True, slots=True)
class DashboardPaths:
    """Filesystem roots used by dashboard read services."""

    static_dir: Path
    project_root: Path
    output_dir: Path
    config_dir: Path
    private_archive_dir: Path
    admin_log_files: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class DashboardContext:
    """Immutable configuration passed to dashboard services."""

    paths: DashboardPaths
    worker_providers: tuple[str, ...]
    public_raw_files: frozenset[str]
    public_job_fields: frozenset[str]
    public_vps_fields: frozenset[str]
    repository_excluded_directories: frozenset[str]
    repository_private_name_markers: frozenset[str]


@dataclass(frozen=True, slots=True)
class DashboardRequest:
    """Normalized request target with path and query kept separate."""

    raw_target: str
    path: str
    query: Mapping[str, tuple[str, ...]]

    @classmethod
    def parse(cls, raw_target: str) -> DashboardRequest:
        """Parse an origin-form HTTP request target without route ambiguity."""

        split = urlsplit(raw_target)
        parsed_query = parse_qs(split.query)
        query = {key: tuple(values) for key, values in parsed_query.items()}
        return cls(raw_target=raw_target, path=split.path, query=query)

    def first_query_value(self, name: str) -> str:
        """Return the first query value, matching the dashboard's old behavior."""

        values = self.query.get(name, ())
        return values[0] if values else ""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Transport-neutral HTTP response emitted by the request handler adapter."""

    status: int
    body: bytes
    headers: tuple[Header, ...]

    @classmethod
    def json(cls, data: Any, *, status: int = 200) -> HttpResponse:
        """Serialize a JSON response using the established indented wire format."""

        body = json.dumps(data, indent=2).encode("utf-8")
        return cls(
            status=status,
            body=body,
            headers=(
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ),
        )

    @classmethod
    def binary(
        cls,
        body: bytes,
        *,
        content_type: str,
        disposition: str,
        status: int = 200,
    ) -> HttpResponse:
        """Build an inline or attachment response for a resolved file."""

        return cls(
            status=status,
            body=body,
            headers=(
                ("Content-Type", content_type),
                ("Content-Disposition", disposition),
                ("Content-Length", str(len(body))),
            ),
        )
