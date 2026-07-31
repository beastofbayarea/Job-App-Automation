from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from job_application_automation.core import continuous_ats as worker
from job_application_automation.core.engine_shared import configured_answer
from job_application_automation.engines import _browser_form as browser_form


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_smartrecruiters_and_workable_wrappers_use_generic_supervisor() -> None:
    smartrecruiters = (SCRIPTS / "install_vps_continuous_smartrecruiters.ps1").read_text(
        encoding="utf-8"
    )
    workable = (SCRIPTS / "install_vps_continuous_workable.ps1").read_text(encoding="utf-8")

    assert "-AtsPlatform smartrecruiters" in smartrecruiters
    assert "-AtsPlatform workable" in workable


def test_status_probe_normalizes_and_discovers_every_provider() -> None:
    script = (SCRIPTS / "check_vps_parallel_ats.ps1").read_text(encoding="utf-8")

    assert '"smartrecruiters"' in script
    assert '"workable"' in script
    assert "for unit in sys.argv[1:]" in script
    assert "sed -e 's/^job-app-//' -e 's/-/_/g'" in script


def test_continuous_application_runs_headed_inside_xvfb(tmp_path: Path) -> None:
    expected = worker.CommandOutcome(0, "", "")
    with patch.object(worker, "_run_command", return_value=expected) as run_command:
        actual = worker._apply(
            job={
                "job_url": "https://apply.workable.com/example/j/ABC123/",
                "company": "Example",
                "title": "Product Manager",
            },
            email="candidate@example.test",
            launcher=tmp_path / "launcher.py",
            profile=tmp_path / "profile.json",
            resume_path=tmp_path / "resume.pdf",
            cover_letter_path=tmp_path / "cover_letter.pdf",
            result_path=tmp_path / "result.json",
            submission_log=tmp_path / "submission_log.json",
            screenshot_dir=tmp_path / "screenshots",
            engine_timeout_seconds=30,
            process_timeout_seconds=60,
        )

    assert actual == expected
    assert "--headed" in run_command.call_args.args[0]


def test_country_scoped_work_rights_and_residence_answers_fail_closed() -> None:
    profile = {"country": "United States"}
    rules = {
        "work_authorization_countries": [
            "Australia",
            "United Kingdom",
            "United States",
        ],
        "target_work_country": "Australia",
        "work_authorization": "Yes",
        "target_country_work_authorization": "Yes",
        "based_in_target_country": "Yes",
    }

    assert configured_answer("Do you have Australian working rights?", profile, rules, {}) == "Yes"
    assert configured_answer("Do you have the right to work in the UK?", profile, rules, {}) == "Yes"
    assert configured_answer("Are you authorized to work in Canada?", profile, rules, {}) == "No"
    assert configured_answer("Are you currently based in Australia?", profile, rules, {}) == "No"
    assert configured_answer("Are you currently based in the US?", profile, rules, {}) == "Yes"

    rules.pop("target_work_country")
    assert configured_answer("Are you eligible to work here?", profile, rules, {}) is None


def test_salary_answers_are_not_reinterpreted_across_time_periods() -> None:
    profile = {"compensation": "120000"}
    salary_rules = {
        "current_salary": "Prefer not to disclose",
        "salary_expectation": "Negotiable",
    }
    salary_matchers = {
        "current_salary": ["current monthly salary"],
        "salary_expectation": ["expected salary"],
    }

    current = configured_answer(
        "Current Monthly Salary (USD)",
        profile,
        salary_rules,
        {},
        salary_matchers,
    )
    expected = configured_answer(
        "Current Expected Salary (USD)",
        profile,
        salary_rules,
        {},
        salary_matchers,
    )

    assert (
        browser_form._salary_answer(
            "Current Monthly Salary (USD)",
            current,
            profile,
        )
        == "Prefer not to disclose"
    )
    assert (
        browser_form._salary_answer(
            "Current Expected Salary (USD)",
            expected,
            profile,
        )
        == "Negotiable"
    )
    assert browser_form._salary_answer("Expected annual salary", None, profile) == "120000"
    assert browser_form._salary_answer("Expected monthly salary", None, profile) is None
