"""Declarative route registry for the public, read-only dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias
from collections.abc import Callable, Mapping

from .models import DashboardRequest, HttpResponse

PayloadProvider: TypeAlias = Callable[[], Any]
ResponseProvider: TypeAlias = Callable[[DashboardRequest], HttpResponse]


class RouteKey(str, Enum):
    """Stable identifiers for dashboard API route handlers."""

    ADMIN_OVERVIEW = "admin_overview"
    ADMIN_FILE = "admin_file"
    OPERATIONS = "operations"
    METRICS = "metrics"
    SYSTEM_VPS = "system_vps"
    VPS_LOG = "vps_log"
    SECTION1_JOBS = "section1_jobs"
    SECTION1_BACKLOG = "section1_backlog"
    SECTION1_COVERAGE = "section1_coverage"
    SECTION1_CACHE = "section1_cache"
    SECTION2_GENERATION = "section2_generation"
    SECTION2_ARCHIVES = "section2_archives"
    SECTION3_SUBMISSIONS = "section3_submissions"
    SECTION3_FAILURES = "section3_failures"
    SECTION3_STATE = "section3_state"
    PUBLIC_FILE = "public_file"
    PUBLIC_DOWNLOAD = "public_download"
    UNKNOWN_API = "unknown_api"


@dataclass(frozen=True, slots=True)
class StaticRouteSpec:
    """One static asset and every clean URL that aliases it."""

    target: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApiRouteSpec:
    """One API handler and its exact registered paths."""

    key: RouteKey
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrefixRouteSpec:
    """One dynamic API handler selected by an unambiguous path prefix."""

    key: RouteKey
    prefix: str


STATIC_ROUTE_SPECS: tuple[StaticRouteSpec, ...] = (
    StaticRouteSpec("/admin.html", ("/admin", "/admin/")),
    StaticRouteSpec("/search.html", ("/search", "/search/")),
    StaticRouteSpec("/generation.html", ("/generation", "/generation/")),
    StaticRouteSpec("/logs.html", ("/logs", "/logs/")),
    StaticRouteSpec("/inspector.html", ("/inspector", "/inspector/")),
    StaticRouteSpec(
        "/cent-capital.html",
        ("/cent-capital", "/cent-capital/", "/cent-capital.html"),
    ),
    StaticRouteSpec("/system-status.html", ("/system-status", "/system-status/")),
    StaticRouteSpec("/index.html", ("/submissions", "/submissions/")),
    StaticRouteSpec("/sitemap.xml", ("/sitemap", "/sitemap/", "/sitemap.xml")),
    StaticRouteSpec("/robots.txt", ("/robots", "/robots.txt")),
    StaticRouteSpec(
        "/site.webmanifest",
        ("/manifest", "/manifest.json", "/site.webmanifest"),
    ),
)

API_ROUTE_SPECS: tuple[ApiRouteSpec, ...] = (
    ApiRouteSpec(RouteKey.ADMIN_OVERVIEW, ("/api/admin/overview",)),
    ApiRouteSpec(RouteKey.ADMIN_FILE, ("/api/admin/file",)),
    ApiRouteSpec(RouteKey.OPERATIONS, ("/api/operations",)),
    ApiRouteSpec(RouteKey.METRICS, ("/api/metrics",)),
    ApiRouteSpec(RouteKey.SYSTEM_VPS, ("/api/system/vps",)),
    ApiRouteSpec(RouteKey.VPS_LOG, ("/api/vps/log",)),
    ApiRouteSpec(RouteKey.SECTION1_JOBS, ("/api/section1/jobs",)),
    ApiRouteSpec(RouteKey.SECTION1_BACKLOG, ("/api/section1/backlog",)),
    ApiRouteSpec(RouteKey.SECTION1_COVERAGE, ("/api/section1/coverage",)),
    ApiRouteSpec(RouteKey.SECTION1_CACHE, ("/api/section1/cache",)),
    ApiRouteSpec(RouteKey.SECTION2_GENERATION, ("/api/section2/generation",)),
    ApiRouteSpec(RouteKey.SECTION2_ARCHIVES, ("/api/section2/archives",)),
    ApiRouteSpec(RouteKey.SECTION3_SUBMISSIONS, ("/api/section3/submissions",)),
    ApiRouteSpec(RouteKey.SECTION3_FAILURES, ("/api/section3/failures",)),
    ApiRouteSpec(RouteKey.SECTION3_STATE, ("/api/section3/state",)),
)

PREFIX_ROUTE_SPECS: tuple[PrefixRouteSpec, ...] = (
    PrefixRouteSpec(RouteKey.PUBLIC_DOWNLOAD, "/api/download/"),
    PrefixRouteSpec(RouteKey.PUBLIC_FILE, "/api/files/"),
)


def _exact_registry(
    specs: tuple[StaticRouteSpec, ...] | tuple[ApiRouteSpec, ...],
) -> dict[str, str | RouteKey]:
    registry: dict[str, str | RouteKey] = {}
    for spec in specs:
        value: str | RouteKey = spec.target if isinstance(spec, StaticRouteSpec) else spec.key
        for path in spec.paths:
            if path in registry:
                raise ValueError(f"Duplicate dashboard route: {path}")
            registry[path] = value
    return registry


STATIC_ROUTE_REGISTRY: Mapping[str, str] = {
    path: value
    for path, value in _exact_registry(STATIC_ROUTE_SPECS).items()
    if isinstance(value, str)
}
API_ROUTE_REGISTRY: Mapping[str, RouteKey] = {
    path: value
    for path, value in _exact_registry(API_ROUTE_SPECS).items()
    if isinstance(value, RouteKey)
}


@dataclass(frozen=True, slots=True)
class StaticRouteMatch:
    """A request that should be delegated to the static file handler."""

    target: str


@dataclass(frozen=True, slots=True)
class ApiRouteMatch:
    """A request resolved to one registered API service."""

    key: RouteKey


@dataclass(frozen=True, slots=True)
class StaticPassthrough:
    """An unaliased request delegated unchanged to the static handler."""

    raw_target: str


RouteMatch: TypeAlias = StaticRouteMatch | ApiRouteMatch | StaticPassthrough


def match_get_request(request: DashboardRequest) -> RouteMatch:
    """Resolve a GET request using exact-path tables before dynamic prefixes."""

    static_target = STATIC_ROUTE_REGISTRY.get(request.path)
    if static_target is not None:
        return StaticRouteMatch(static_target)
    api_key = API_ROUTE_REGISTRY.get(request.path)
    if api_key is not None:
        return ApiRouteMatch(api_key)
    for spec in PREFIX_ROUTE_SPECS:
        if request.path.startswith(spec.prefix):
            return ApiRouteMatch(spec.key)
    if request.path.startswith("/api/"):
        return ApiRouteMatch(RouteKey.UNKNOWN_API)
    return StaticPassthrough(request.raw_target)


@dataclass(frozen=True, slots=True)
class DashboardRouteServices:
    """Typed service callbacks consumed by the route registry."""

    admin_overview: PayloadProvider
    admin_file: ResponseProvider
    operations: PayloadProvider
    metrics: PayloadProvider
    system_vps: PayloadProvider
    vps_log: PayloadProvider
    section1_jobs: PayloadProvider
    section1_backlog: PayloadProvider
    section1_coverage: PayloadProvider
    section1_cache: PayloadProvider
    section2_generation: PayloadProvider
    section2_archives: PayloadProvider
    section3_submissions: PayloadProvider
    section3_failures: PayloadProvider
    section3_state: PayloadProvider
    public_file: ResponseProvider
    public_download: ResponseProvider


class DashboardApplication:
    """Transport-neutral dispatcher backed by the declarative route registry."""

    def __init__(self, services: DashboardRouteServices) -> None:
        json_handlers: dict[RouteKey, PayloadProvider] = {
            RouteKey.ADMIN_OVERVIEW: services.admin_overview,
            RouteKey.OPERATIONS: services.operations,
            RouteKey.METRICS: services.metrics,
            RouteKey.SYSTEM_VPS: services.system_vps,
            RouteKey.VPS_LOG: services.vps_log,
            RouteKey.SECTION1_JOBS: services.section1_jobs,
            RouteKey.SECTION1_BACKLOG: services.section1_backlog,
            RouteKey.SECTION1_COVERAGE: services.section1_coverage,
            RouteKey.SECTION1_CACHE: services.section1_cache,
            RouteKey.SECTION2_GENERATION: services.section2_generation,
            RouteKey.SECTION2_ARCHIVES: services.section2_archives,
            RouteKey.SECTION3_SUBMISSIONS: services.section3_submissions,
            RouteKey.SECTION3_FAILURES: services.section3_failures,
            RouteKey.SECTION3_STATE: services.section3_state,
        }
        self._handlers: dict[RouteKey, ResponseProvider] = {
            key: self._json_response(provider) for key, provider in json_handlers.items()
        }
        self._handlers.update(
            {
                RouteKey.ADMIN_FILE: services.admin_file,
                RouteKey.PUBLIC_FILE: services.public_file,
                RouteKey.PUBLIC_DOWNLOAD: services.public_download,
                RouteKey.UNKNOWN_API: lambda _request: HttpResponse.json(
                    {"error": "Unknown API route"}, status=404
                ),
            }
        )

    @staticmethod
    def _json_response(provider: PayloadProvider) -> ResponseProvider:
        def respond(_request: DashboardRequest) -> HttpResponse:
            return HttpResponse.json(provider())

        return respond

    def dispatch(self, request: DashboardRequest, match: ApiRouteMatch) -> HttpResponse:
        """Invoke the service registered for an already-resolved API route."""

        handler = self._handlers.get(match.key)
        if handler is None:
            return HttpResponse.json({"error": "Unknown API route"}, status=404)
        return handler(request)
