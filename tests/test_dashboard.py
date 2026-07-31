"""Unit tests for VPS Output Monitor Dashboard server."""

import json
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from job_application_automation.dashboard import server
from job_application_automation.dashboard.server import (
    DashboardRequestHandler,
    build_kpi_metrics,
    get_output_file_path,
    load_json_file,
    load_vps_config,
    load_vps_log,
)


def test_get_output_file_path_fallback(tmp_path):
    with patch("job_application_automation.dashboard.server.OUTPUT_DIR", tmp_path):
        filename = "submission_log.json"
        path = get_output_file_path(filename)
        assert path == tmp_path / filename


def test_load_json_file_missing():
    data = load_json_file("non_existent_file.json", default={"test": True})
    assert data == {"test": True}


def test_load_vps_config_exposes_only_operational_metadata(tmp_path):
    config_file = tmp_path / "vps_config.json"
    config_file.write_text(
        json.dumps(
            {
                "vps": {
                    "host": "1.2.3.4",
                    "hostname": "example-vps",
                    "memory_gb": 4,
                    "ssh_user": "root",
                    "ssh_password": {"value": "secret"},
                },
                "hostinger_account": {
                    "owner_name": "Private",
                    "phone": "555-0100",
                },
            }
        ),
        encoding="utf-8",
    )

    with patch("job_application_automation.dashboard.server.CONFIG_DIR", tmp_path):
        data = load_vps_config()
        assert data == {"vps": {"hostname": "example-vps", "memory_gb": 4}}


def test_dashboard_exposes_no_authentication_surface():
    """The dashboard is public by design; no credential gate may reappear."""
    assert not hasattr(DashboardRequestHandler, "_is_authorized")
    assert not hasattr(DashboardRequestHandler, "_require_authorization")

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "WWW-Authenticate" not in source
    assert "JOB_APP_DASHBOARD_USERNAME" not in source
    assert "JOB_APP_DASHBOARD_PASSWORD" not in source


def test_dashboard_rejects_all_post_requests():
    """POST previously shelled out to VPS scripts; unauthenticated, it must not."""
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
    handler.headers = Message()
    errors = []
    handler.send_error = lambda code, message=None: errors.append((code, message))

    for path in ("/api/vps/sync", "/api/vps/status", "/api/metrics", "/"):
        handler.path = path
        handler.do_POST()

    assert [code for code, _ in errors] == [404, 404, 404, 404]

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "_handle_vps_sync" not in source
    assert "_handle_vps_status" not in source


def test_load_vps_log_missing(tmp_path):
    with patch("job_application_automation.dashboard.server.OUTPUT_DIR", tmp_path):
        log = load_vps_log()
        assert "not found" in log


def test_build_kpi_metrics_mocked():
    sample_subs = {
        "20260729-test-job": {
            "applied_at": "2026-07-29T12:00:00+00:00",
            "ats": "greenhouse",
            "company": "TestCo",
            "role": "Product Manager",
            "status": "SUBMITTED & CONFIRMED",
            "resume_filename": "TestCo_Product_Manager_Resume.pdf",
        }
    }
    sample_fails = {
        "failure_count": 1,
        "updated_at": "2026-07-29T12:05:00+00:00",
        "attempted_by_ats": {"greenhouse": 1},
        "confirmed_by_ats": {"greenhouse": 1},
    }
    sample_gen = [{"job": 1}, {"job": 2}]
    sample_arch = {"ja1_123": {}}

    with (
        patch("job_application_automation.dashboard.server.load_json_file") as mock_load,
        patch("job_application_automation.dashboard.server.load_csv_jobs") as mock_csv,
        patch("job_application_automation.dashboard.server.load_vps_config") as mock_cfg,
    ):

        def mock_loader(fname, default=None):
            if fname == "submission_log.json":
                return sample_subs
            if fname == "vps_application_failures.json":
                return sample_fails
            if fname == "vps_generation_jobs.json":
                return sample_gen
            if fname == "vps_document_archive_state.json":
                return sample_arch
            return default or {}

        mock_load.side_effect = mock_loader
        mock_csv.return_value = [{"company": "A"}, {"company": "B"}]
        mock_cfg.return_value = {"vps": {"host": "2.24.28.180"}}

        metrics = build_kpi_metrics()
        assert metrics["total_submissions"] == 1
        assert metrics["failure_count"] == 1
        assert metrics["generation_queue_count"] == 2
        assert metrics["archived_document_sets"] == 1
        assert metrics["ats_submissions"] == {"greenhouse": 1}
        assert metrics["vps_info"]["host"] == "2.24.28.180"
        assert metrics["vps_infra"] == {}


def test_build_kpi_metrics_includes_vps_infra_snapshot_when_present():
    sample_infra = {
        "version": 1,
        "generated_at": "2026-07-31T02:00:00+00:00",
        "active_services": [
            "job-app-ashby",
            "job-app-greenhouse",
            "job-app-lever",
            "job-app-search-sync",
        ],
        "uptime": "up 15 hours, 42 minutes",
    }

    with (
        patch("job_application_automation.dashboard.server.load_json_file") as mock_load,
        patch("job_application_automation.dashboard.server.load_csv_jobs") as mock_csv,
        patch("job_application_automation.dashboard.server.load_vps_config") as mock_cfg,
    ):

        def mock_loader(fname, default=None):
            if fname == "vps_infra_status.json":
                return sample_infra
            return default if default is not None else {}

        mock_load.side_effect = mock_loader
        mock_csv.return_value = []
        mock_cfg.return_value = {}

        metrics = build_kpi_metrics()
        assert metrics["vps_infra"] == sample_infra


def test_dashboard_request_handler_route_mappings():
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)

    routes_to_test = [
        ("/sitemap", "/sitemap.xml"),
        ("/sitemap.xml", "/sitemap.xml"),
        ("/robots", "/robots.txt"),
        ("/robots.txt", "/robots.txt"),
        ("/manifest", "/site.webmanifest"),
        ("/site.webmanifest", "/site.webmanifest"),
        ("/search", "/search.html"),
        ("/generation", "/generation.html"),
        ("/logs", "/logs.html"),
        ("/inspector", "/inspector.html"),
        ("/system-status", "/system-status.html"),
        ("/system-status/", "/system-status.html"),
        ("/submissions", "/index.html"),
    ]

    for req_path, expected_path in routes_to_test:
        handler.path = req_path
        with (
            patch.object(DashboardRequestHandler, "_handle_api_get"),
            patch.object(DashboardRequestHandler, "_handle_file_download"),
            patch("http.server.SimpleHTTPRequestHandler.do_GET"),
        ):
            handler.do_GET()
            assert handler.path == expected_path
