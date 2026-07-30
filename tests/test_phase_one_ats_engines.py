from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from job_application_automation.engines import (
    bamboohr,
    breezy,
    jazzhr,
    recruitee,
    smartrecruiters,
    workable,
)
from job_application_automation.core.engine_shared import (
    detect_ats_job_url,
    validate_ats_job_url,
)


ENGINES = (
    (workable, "workable", "https://apply.workable.com/example/j/ABC123/"),
    (
        smartrecruiters,
        "smartrecruiters",
        "https://jobs.smartrecruiters.com/Example/744000123-role",
    ),
    (recruitee, "recruitee", "https://example.recruitee.com/o/product-manager"),
    (bamboohr, "bamboohr", "https://example.bamboohr.com/careers/123"),
    (breezy, "breezy", "https://example.breezy.hr/p/123-product-manager/apply"),
    (jazzhr, "jazzhr", "https://example.applytojob.com/apply/ABC123/Product-Manager"),
)


@pytest.mark.parametrize(("module", "ats", "url"), ENGINES)
def test_phase_one_engine_parser_and_spec(module, ats: str, url: str) -> None:
    assert module.SPEC.ats == ats
    assert module._parser().prog
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
    assert url


@pytest.mark.parametrize(("module", "ats", "url"), ENGINES)
def test_phase_one_engine_run_delegates_complete_contract(
    module,
    ats: str,
    url: str,
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    cover_letter = tmp_path / "cover-letter.pdf"
    expected = {"success": True, "status": "PREFILLED_ONLY", "ats": ats}
    with patch.object(module, "run_browser_form_engine", return_value=expected) as shared:
        result = module.run(
            url=url,
            resume=resume,
            cover_letter=cover_letter,
            email_override="candidate@example.com",
            config={"candidate": {}},
            company="Example",
            role="Product Manager",
            live_submit=False,
            screenshot_dir=tmp_path,
            timeout=1234,
        )

    assert result == expected
    shared.assert_called_once_with(
        module.SPEC,
        url=url,
        resume=resume,
        cover_letter=cover_letter,
        email_override="candidate@example.com",
        config={"candidate": {}},
        company="Example",
        role="Product Manager",
        live_submit=False,
        headless=True,
        screenshot_dir=tmp_path,
        timeout=1234,
    )


def test_extract_workable_ids_only_accepts_job_paths() -> None:
    assert workable._extract_workable_ids(
        "https://apply.workable.com/example-company/j/ABC123XYZ/"
    ) == ("example-company", "ABC123XYZ")
    assert workable._extract_workable_ids("https://apply.workable.com/j/ABC123XYZ/apply") == (
        None,
        "ABC123XYZ",
    )
    assert workable._extract_workable_ids("https://apply.workable.com/example-company/") == (
        None,
        None,
    )


@pytest.mark.parametrize(("module", "ats", "url"), ENGINES)
def test_job_url_validation_accepts_each_engine_job(module, ats: str, url: str) -> None:
    assert validate_ats_job_url(url, ats)
    assert detect_ats_job_url(url) == ats


@pytest.mark.parametrize(
    ("ats", "url"),
    (
        ("workable", "https://apply.workable.com/example/"),
        ("smartrecruiters", "https://jobs.smartrecruiters.com/Example/"),
        ("recruitee", "https://example.recruitee.com/"),
        ("bamboohr", "https://example.bamboohr.com/careers/"),
        ("bamboohr", "https://example.bamboohr.com/careers/list"),
        ("breezy", "https://example.breezy.hr/"),
        ("jazzhr", "https://example.applytojob.com/apply/"),
    ),
)
def test_company_board_roots_are_not_job_urls(ats: str, url: str) -> None:
    assert not validate_ats_job_url(url, ats)
    assert detect_ats_job_url(url) is None


@pytest.mark.parametrize(
    ("ats", "job_url", "board_url"),
    (
        (
            "ashby",
            "https://jobs.ashbyhq.com/example/123",
            "https://jobs.ashbyhq.com/example",
        ),
        (
            "greenhouse",
            "https://job-boards.greenhouse.io/example/jobs/123",
            "https://job-boards.greenhouse.io/example",
        ),
        (
            "lever",
            "https://jobs.lever.co/example/123/apply",
            "https://jobs.lever.co/example",
        ),
    ),
)
def test_original_engine_board_roots_are_also_rejected(
    ats: str,
    job_url: str,
    board_url: str,
) -> None:
    assert validate_ats_job_url(job_url, ats)
    assert not validate_ats_job_url(board_url, ats)


def test_greenhouse_embedded_and_custom_domain_jobs_remain_supported() -> None:
    assert validate_ats_job_url(
        "https://boards.greenhouse.io/embed/job_app?for=example&token=123",
        "greenhouse",
    )
    assert validate_ats_job_url(
        "https://careers.example.com/open-role?gh_jid=123",
        "greenhouse",
    )
