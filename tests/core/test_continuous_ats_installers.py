from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def test_all_provider_wrappers_use_guarded_generic_supervision() -> None:
    ashby_wrapper = (SCRIPTS / "install_vps_continuous_ashby.ps1").read_text(encoding="utf-8")
    greenhouse_wrapper = (SCRIPTS / "install_vps_continuous_greenhouse.ps1").read_text(
        encoding="utf-8"
    )
    lever_wrapper = (SCRIPTS / "install_vps_continuous_lever.ps1").read_text(encoding="utf-8")
    installer = (SCRIPTS / "install_vps_continuous_ats.ps1").read_text(encoding="utf-8")
    unit = (SCRIPTS / "templates" / "job-app-continuous-ats.service.template").read_text(
        encoding="utf-8"
    )

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
    assert "OtherAtsService" not in installer
    assert "Restart=always" in unit
    assert "job_application_automation.core.continuous_ats" in unit
    assert "--ats-platform __ATS_PLATFORM__" in unit
    assert "/usr/bin/xvfb-run" in unit
    assert "EnvironmentFile=-/etc/job-application-automation/observability.env" in unit


def test_all_unattended_worker_units_load_optional_observability_environment() -> None:
    for filename in (
        "templates/job-app-continuous-ats.service.template",
        "templates/job-app-greenhouse-source.service.template",
        "templates/job-app-greenhouse.service.template",
    ):
        unit = (SCRIPTS / filename).read_text(encoding="utf-8")
        assert "EnvironmentFile=-/etc/job-application-automation/observability.env" in unit


def test_parallel_status_probe_discovers_all_services_and_redacts_email() -> None:
    script = (SCRIPTS / "check_vps_parallel_ats.ps1").read_text(encoding="utf-8")

    assert "list-unit-files 'job-app-*.service'" in script
    assert "continuous_*_state.json" in script
    assert "continuous_ats" in script
    assert "continuous-lever" in script
    assert "email_selection=" in script
    assert '"outside_pool"' in script
    assert "grep -v '[b]ash -c set -eu repo='" in script
    assert "[REDACTED]" in script
    assert "free -h" in script
    assert "Invoke-ExternalCommandWithTimeout" in script
    assert '"possible_spam_circuit_open"' in script
    assert "_POSSIBLE_SPAM_CIRCUIT_OPEN" in script
    assert '"application_rate_limit_open"' in script
    assert "_APPLICATION_RATE_LIMIT_OPEN" in script


def test_targeted_greenhouse_retry_requires_safe_pre_submit_evidence() -> None:
    script = (SCRIPTS / "retry_vps_greenhouse_job.ps1").read_text(encoding="utf-8")

    assert 'ValidatePattern("^\\d+$")' in script
    assert 'result.get("submitted") is not False' in script
    assert 'item.get("status") == "SUBMITTED & CONFIRMED"' in script
    assert "target already exists in the confirmed ledger" in script
    assert "refusing overlap" in script
    assert "systemctl stop job-app-greenhouse.service" in script
    assert "trap restart_worker EXIT INT TERM" in script
    assert "--once" in script
    assert "greenhouse-targeted-retry" in script
    assert "InspectLatest" in script
    assert "[REDACTED_EMAIL]" in script
    assert "ScreenshotOutputPath" not in script
    assert "latest_prefilled_screenshot=" not in script


def test_vps_screenshot_cleanup_is_output_bound_and_application_safe() -> None:
    script = (SCRIPTS / "cleanup_vps_application_screenshots.ps1").read_text(encoding="utf-8")

    assert 'expected="`$repo/output"' in script
    assert "readlink -f" in script
    assert "pgrep -f '[j]ob_automation.py apply'" in script
    assert "repo_output = Path(sys.argv[2]).resolve()" in script
    assert '".jpeg", ".jpg", ".png", ".webp"' in script
    assert "path.relative_to(expected)" in script
    assert '"screenshots_remaining": len(remaining)' in script
    assert "-hostkey" in script
