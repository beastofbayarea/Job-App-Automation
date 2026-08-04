from __future__ import annotations

import io
import json
from unittest.mock import MagicMock
import pytest

from job_application_automation.dashboard.server import (
    DashboardRequestHandler,
    archive_entries,
    build_kpi_metrics,
    summarize_archive_status,
    summarize_coverage,
    summarize_submissions,
)


def test_archive_entries_and_summarize() -> None:
    raw_with_jobs = {
        "version": 1,
        "jobs": {"j1": {"status": "archived"}, "j2": {"status": "failed"}},
    }
    entries = archive_entries(raw_with_jobs)
    assert len(entries) == 2
    summary = summarize_archive_status(entries)
    assert summary == {"archived": 1, "failed": 1}

    raw_flat = {"j1": {"status": "archived"}, "version": "scalar"}
    flat_entries = archive_entries(raw_flat)
    assert "j1" in flat_entries
    assert "version" not in flat_entries

    assert archive_entries(None) == {}


def test_summarize_submissions() -> None:
    subs = {
        "1": {
            "status": "Confirmed by ATS",
            "ats": "greenhouse",
            "applied_at": "2026-07-01T10:00:00Z",
        },
        "2": {"status": "Failed", "ats": "ashby", "applied_at": "2026-07-02T10:00:00Z"},
    }
    res = summarize_submissions(subs)
    assert res["confirmed_by_ats"] == {"greenhouse": 1}
    assert res["latest_applied_at"] == "2026-07-02T10:00:00Z"
    assert summarize_submissions([]) == {
        "confirmed_by_ats": {},
        "by_status": {},
        "latest_applied_at": "",
    }


def test_summarize_coverage() -> None:
    cov_data = {
        "generated_at": "2026-07-30T10:00:00Z",
        "discovery": {"scanned": 10},
        "feed_fetch": {"failed_boards": ["b1", "b2"], "total": 5},
        "criteria": {"role_terms": ["dev"], "discovery_mode": "broad"},
    }
    summary = summarize_coverage(cov_data)
    assert summary["feed_fetch"]["failed_board_count"] == 2
    assert summary["criteria"]["role_terms"] == ["dev"]
    assert summarize_coverage(None) == {}


def test_build_kpi_metrics_smoke() -> None:
    kpi = build_kpi_metrics()
    assert "generated_at" in kpi
    assert "total_submissions" in kpi
    assert "coverage" in kpi


def make_mock_handler(path: str) -> tuple[DashboardRequestHandler, io.BytesIO]:
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
    handler.path = path
    wfile = io.BytesIO()
    handler.wfile = wfile
    handler.send_response = MagicMock()  # type: ignore[assignment]
    handler.send_header = MagicMock()  # type: ignore[assignment]
    handler.end_headers = MagicMock()  # type: ignore[assignment]
    handler.send_error = MagicMock()  # type: ignore[assignment]
    return handler, wfile


def test_dashboard_handler_api_routes() -> None:
    handler, wfile = make_mock_handler("/api/metrics")
    handler._handle_api_get()
    handler.send_response.assert_called_once_with(200)
    data = json.loads(wfile.getvalue().decode("utf-8"))
    assert "total_submissions" in data

    handler, wfile = make_mock_handler("/api/vps/log")
    handler._handle_api_get()
    handler.send_response.assert_called_once_with(404)
    assert json.loads(wfile.getvalue().decode("utf-8")) == {"error": "Unknown API route"}

    handler, wfile = make_mock_handler("/api/vps/sync")
    handler.do_POST()
    handler.send_response.assert_called_once_with(404)
    assert json.loads(wfile.getvalue().decode("utf-8")) == {"error": "Endpoint not found"}


def test_dashboard_handler_get_route_mappings() -> None:
    handler, _ = make_mock_handler("/search")
    with pytest.raises(AttributeError):  # Fails when trying super().do_GET() on mock
        handler.do_GET()
    assert handler.path == "/search.html"
