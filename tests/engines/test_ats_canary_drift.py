from __future__ import annotations

from job_application_automation.core.engine_shared import ATS_HOST_MARKERS, validate_ats_url


ATS_TEST_CANARY_URLS = {
    "ashby": "https://jobs.ashbyhq.com/example-company/12345",
    "greenhouse": "https://boards.greenhouse.io/examplecompany/jobs/67890",
    "lever": "https://jobs.lever.co/examplecompany/abcdef",
    "workable": "https://apply.workable.com/example-company/j/12345",
    "smartrecruiters": "https://jobs.smartrecruiters.com/ExampleCompany/12345",
}


def test_ats_host_markers_registry_complete() -> None:
    for ats_name in ATS_TEST_CANARY_URLS:
        assert ats_name in ATS_HOST_MARKERS
        assert len(ATS_HOST_MARKERS[ats_name]) >= 1


def test_ats_canary_url_validation() -> None:
    for ats_name, canary_url in ATS_TEST_CANARY_URLS.items():
        assert validate_ats_url(canary_url, ats_name) is True
