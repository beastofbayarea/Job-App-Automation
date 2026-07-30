from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_all_provider_wrappers_use_guarded_generic_supervision() -> None:
    ashby_wrapper = (SCRIPTS / "install_vps_continuous_ashby.ps1").read_text(encoding="utf-8")
    greenhouse_wrapper = (SCRIPTS / "install_vps_continuous_greenhouse.ps1").read_text(
        encoding="utf-8"
    )
    lever_wrapper = (SCRIPTS / "install_vps_continuous_lever.ps1").read_text(encoding="utf-8")
    installer = (SCRIPTS / "install_vps_continuous_ats.ps1").read_text(encoding="utf-8")
    unit = (SCRIPTS / "job-app-continuous-ats.service.template").read_text(encoding="utf-8")

    assert "-AtsPlatform ashby" in ashby_wrapper
    assert "-AtsPlatform greenhouse" in greenhouse_wrapper
    assert "-AtsPlatform lever" in lever_wrapper
    assert "engines/$AtsPlatform.py" in installer
    assert "candidate_email_pool.json" in installer
    assert "-hostkey" in installer
    assert "-pwfile" in installer
    assert "[j]ob_automation.py apply" in installer
    assert "exit 76" in installer
    assert "disable --now" not in installer
    assert "continuous job-search service" in installer
    assert "OtherAtsService" not in installer
    assert "Restart=always" in unit
    assert "job_application_automation.core.continuous_ats" in unit
    assert "--ats-platform __ATS_PLATFORM__" in unit
    assert "/usr/bin/xvfb-run" in unit


def test_parallel_status_probe_discovers_all_services_and_redacts_email() -> None:
    script = (SCRIPTS / "check_vps_parallel_ats.ps1").read_text(encoding="utf-8")

    assert "list-unit-files 'job-app-*.service'" in script
    assert "continuous_*_state.json" in script
    assert "continuous_ats" in script
    assert "continuous-lever" in script
    assert "email_selection=" in script
    assert '"outside_pool"' in script
    assert "job-app-search-sync.service" in script
    assert "[v]ps_continuous_search_sync" in script
    assert "grep -v '[b]ash -c set -eu repo='" in script
    assert "[REDACTED]" in script
    assert "free -h" in script
    assert "Invoke-ExternalCommandWithTimeout" in script


def test_targeted_greenhouse_retry_requires_safe_pre_submit_evidence() -> None:
    script = (SCRIPTS / "retry_vps_greenhouse_job.ps1").read_text(encoding="utf-8")

    assert 'ValidatePattern("^\\d+$")' in script
    assert "result.get(\"submitted\") is not False" in script
    assert 'item.get("status") == "SUBMITTED & CONFIRMED"' in script
    assert "target already exists in the confirmed ledger" in script
    assert "refusing overlap" in script
    assert "systemctl stop job-app-greenhouse.service" in script
    assert "trap restart_worker EXIT INT TERM" in script
    assert "--once" in script
    assert "greenhouse-targeted-retry" in script
    assert "InspectLatest" in script
    assert "[REDACTED_EMAIL]" in script
    assert "latest_prefilled_screenshot=" in script
