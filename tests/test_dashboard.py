"""Unit tests for VPS Output Monitor Dashboard server."""

import json
from unittest.mock import patch
from job_application_automation.dashboard.server import (
    build_kpi_metrics,
    get_output_file_path,
    load_csv_jobs,
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


def test_load_vps_config_redaction(tmp_path):
    config_file = tmp_path / "vps_config.json"
    config_file.write_text(json.dumps({
        "vps": {"host": "1.2.3.4", "ssh_password": {"value": "secret"}}
    }), encoding="utf-8")

    with patch("job_application_automation.dashboard.server.CONFIG_DIR", tmp_path):
        data = load_vps_config()
        assert data["vps"]["host"] == "1.2.3.4"
        assert data["vps"]["ssh_password"] == "******"


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

    with patch("job_application_automation.dashboard.server.load_json_file") as mock_load, \
         patch("job_application_automation.dashboard.server.load_csv_jobs") as mock_csv, \
         patch("job_application_automation.dashboard.server.load_vps_config") as mock_cfg:
        
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
