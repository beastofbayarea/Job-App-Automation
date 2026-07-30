from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_ashby_installer_uses_guarded_generic_supervision() -> None:
    wrapper = (SCRIPTS / "install_vps_continuous_ashby.ps1").read_text(
        encoding="utf-8"
    )
    installer = (SCRIPTS / "install_vps_continuous_ats.ps1").read_text(
        encoding="utf-8"
    )
    unit = (SCRIPTS / "job-app-continuous-ats.service.template").read_text(
        encoding="utf-8"
    )

    assert "-AtsPlatform ashby" in wrapper
    assert "continuous_$AtsPlatform.py" in installer
    assert "candidate_email_pool.json" in installer
    assert "-hostkey" in installer
    assert "-pwfile" in installer
    assert "[j]ob_automation.py apply" in installer
    assert "exit 76" in installer
    assert "job-app-greenhouse.service" in installer
    assert "job-app-search-sync.service" in installer
    assert 'systemctl restart "$OtherAtsService"' in installer
    assert 'systemctl disable --now "$OtherAtsService"' not in installer
    assert "Restart=always" in unit
    assert "continuous-__ATS_PLATFORM__" in unit
    assert "/usr/bin/xvfb-run" in unit


def test_parallel_status_probe_reports_both_services_and_redacts_email() -> None:
    script = (SCRIPTS / "check_vps_parallel_ats.ps1").read_text(encoding="utf-8")

    assert "job-app-ashby.service job-app-greenhouse.service" in script
    assert "continuous_ashby_state.json" in script
    assert "continuous_greenhouse_state.json" in script
    assert "[REDACTED]" in script
    assert "free -h" in script
    assert "Invoke-ExternalCommandWithTimeout" in script
