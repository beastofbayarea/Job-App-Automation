"""Exhaustive contracts for dashboard routing and contained downloads."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from job_application_automation.dashboard import downloads, server
from job_application_automation.dashboard.models import (
    DashboardContext,
    DashboardPaths,
    DashboardRequest,
    HttpResponse,
)
from job_application_automation.dashboard.routes import (
    API_ROUTE_SPECS,
    PREFIX_ROUTE_SPECS,
    STATIC_ROUTE_SPECS,
    ApiRouteMatch,
    DashboardApplication,
    DashboardRouteServices,
    RouteKey,
    StaticRouteMatch,
    match_get_request,
)

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "localhost", "::1"])


def _context(tmp_path: Path) -> DashboardContext:
    project = tmp_path / "project"
    output = project / "output"
    config = project / "config"
    static = project / "static"
    archive = tmp_path / "private-archive"
    for directory in (output, config, static, archive):
        directory.mkdir(parents=True, exist_ok=True)
    return DashboardContext(
        paths=DashboardPaths(
            static_dir=static,
            project_root=project,
            output_dir=output,
            config_dir=config,
            private_archive_dir=archive,
            admin_log_files={"vps-sync": output / "vps_sync.log"},
        ),
        worker_providers=("ashby", "greenhouse", "lever"),
        public_raw_files=frozenset(server._PUBLIC_RAW_FILES),
        public_job_fields=frozenset(server._PUBLIC_JOB_FIELDS),
        public_vps_fields=frozenset(server._PUBLIC_VPS_FIELDS),
        repository_excluded_directories=frozenset(server._REPOSITORY_EXCLUDED_DIRECTORIES),
        repository_private_name_markers=frozenset(server._REPOSITORY_PRIVATE_NAME_MARKERS),
    )


STATIC_ROUTE_MATRIX = [(path, spec.target) for spec in STATIC_ROUTE_SPECS for path in spec.paths]
API_ROUTE_MATRIX = [(path, spec.key) for spec in API_ROUTE_SPECS for path in spec.paths]
PREFIX_ROUTE_MATRIX = [(f"{spec.prefix}artifact.json", spec.key) for spec in PREFIX_ROUTE_SPECS]


@pytest.mark.parametrize(("path", "target"), STATIC_ROUTE_MATRIX)
def test_every_static_alias_has_an_exact_contract(path: str, target: str) -> None:
    match = match_get_request(DashboardRequest.parse(f"{path}?cache_bust=1"))

    assert match == StaticRouteMatch(target)


@pytest.mark.parametrize(("path", "route_key"), API_ROUTE_MATRIX + PREFIX_ROUTE_MATRIX)
def test_every_api_route_has_an_exact_contract(path: str, route_key: RouteKey) -> None:
    match = match_get_request(DashboardRequest.parse(f"{path}?cache_bust=1"))

    assert match == ApiRouteMatch(route_key)


def test_route_registry_has_no_exact_or_prefix_collisions() -> None:
    static_paths = [path for spec in STATIC_ROUTE_SPECS for path in spec.paths]
    api_paths = [path for spec in API_ROUTE_SPECS for path in spec.paths]
    prefixes = [spec.prefix for spec in PREFIX_ROUTE_SPECS]

    assert len(static_paths) == len(set(static_paths))
    assert len(api_paths) == len(set(api_paths))
    assert set(static_paths).isdisjoint(api_paths)
    assert len(prefixes) == len(set(prefixes))
    assert all(not path.startswith(tuple(prefixes)) for path in api_paths)


def test_query_values_cannot_override_the_resolved_path() -> None:
    metrics = match_get_request(
        DashboardRequest.parse("/api/metrics?path=/api/admin/file&scope=repository&route=/search")
    )
    search = match_get_request(
        DashboardRequest.parse("/search?path=/api/metrics&route=/api/admin/overview")
    )
    collision = match_get_request(DashboardRequest.parse("/api/metrics/api/files/job_backlog.json"))

    assert metrics == ApiRouteMatch(RouteKey.METRICS)
    assert search == StaticRouteMatch("/search.html")
    assert collision == ApiRouteMatch(RouteKey.UNKNOWN_API)


def _payload(name: str):
    return lambda: {"route": name}


def test_declarative_application_dispatches_every_exact_api_service() -> None:
    services = DashboardRouteServices(
        admin_overview=_payload("admin_overview"),
        admin_file=lambda _request: HttpResponse.json({"route": "admin_file"}),
        operations=_payload("operations"),
        metrics=_payload("metrics"),
        system_vps=_payload("system_vps"),
        vps_log=_payload("vps_log"),
        section1_jobs=_payload("section1_jobs"),
        section1_backlog=_payload("section1_backlog"),
        section1_coverage=_payload("section1_coverage"),
        section1_cache=_payload("section1_cache"),
        section2_generation=_payload("section2_generation"),
        section2_archives=_payload("section2_archives"),
        section3_submissions=_payload("section3_submissions"),
        section3_failures=_payload("section3_failures"),
        section3_state=_payload("section3_state"),
        public_file=lambda _request: HttpResponse.json({"route": "public_file"}),
        public_download=lambda _request: HttpResponse.json({"route": "public_download"}),
    )
    application = DashboardApplication(services)

    for path, route_key in API_ROUTE_MATRIX + PREFIX_ROUTE_MATRIX:
        request = DashboardRequest.parse(path)
        response = application.dispatch(request, ApiRouteMatch(route_key))
        payload = json.loads(response.body)
        assert payload == {"route": route_key.value}


def test_public_download_resolution_is_allowlisted_and_contained(tmp_path: Path) -> None:
    context = _context(tmp_path)
    artifact = context.paths.output_dir / "job_backlog.json"
    artifact.write_bytes(b'{"jobs": []}')

    response = downloads.render_download(
        downloads.resolve_public_download(
            context,
            DashboardRequest.parse("/api/download/job_backlog.json?filename=../../private.txt"),
        )
    )
    traversal = downloads.resolve_public_download(
        context,
        DashboardRequest.parse("/api/download/../job_backlog.json"),
    )
    nested = downloads.resolve_public_download(
        context,
        DashboardRequest.parse("/api/download/nested/job_backlog.json"),
    )
    private = downloads.resolve_public_download(
        context,
        DashboardRequest.parse("/api/download/private_resume.pdf"),
    )

    assert response.status == 200
    assert response.body == b'{"jobs": []}'
    assert isinstance(traversal, HttpResponse) and traversal.status == 400
    assert isinstance(nested, HttpResponse) and nested.status == 400
    assert isinstance(private, HttpResponse) and private.status == 403


def test_admin_download_resolution_rejects_escape_and_missing_files(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")

    escaped = downloads.resolve_admin_download(
        context,
        DashboardRequest.parse("/api/admin/file?scope=output&path=../../outside.txt"),
        is_repository_file_displayable=lambda _path: True,
    )
    missing = downloads.resolve_admin_download(
        context,
        DashboardRequest.parse("/api/admin/file?scope=output&path=missing.txt"),
        is_repository_file_displayable=lambda _path: True,
    )

    assert isinstance(escaped, HttpResponse) and escaped.status == 400
    assert json.loads(escaped.body) == {"error": "Invalid file path"}
    assert isinstance(missing, HttpResponse) and missing.status == 404
    assert json.loads(missing.body) == {"error": "File not found"}


@pytest.mark.enable_socket
def test_local_http_roundtrip_preserves_routes_headers_and_read_only_contract() -> None:
    httpd = server.ReuseAddrHTTPServer(("127.0.0.1", 0), server.DashboardRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]

    try:
        with patch.object(server, "build_kpi_metrics", return_value={"sentinel": True}):
            connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
            connection.request("GET", "/api/metrics?path=/api/admin/file&route=/search")
            response = connection.getresponse()
            payload = json.loads(response.read())

            assert response.status == 200
            assert payload == {"sentinel": True}
            assert response.getheader("Content-Type") == "application/json"
            assert response.getheader("X-Content-Type-Options") == "nosniff"
            assert response.getheader("X-Frame-Options") == "DENY"
            assert response.getheader("Referrer-Policy") == "no-referrer"
            assert response.getheader("Cache-Control") == "no-store"

            connection.request("GET", "/search?path=/api/metrics")
            static_response = connection.getresponse()
            static_body = static_response.read()
            assert static_response.status == 200
            assert b"Sky Bison" in static_body

            connection.request("OPTIONS", "/api/metrics")
            options_response = connection.getresponse()
            options_response.read()
            assert options_response.status == 204

            connection.request("POST", "/api/metrics", body=b"{}")
            post_response = connection.getresponse()
            post_response.read()
            assert post_response.status == 404
            connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
