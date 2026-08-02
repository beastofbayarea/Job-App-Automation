from __future__ import annotations

from pathlib import Path

import pytest

from job_application_automation.core.continuous_worker_sources import (
    SourceServices,
    load_source_jobs,
    validate_worker_platform,
)


def _unused(_path: Path) -> object:
    raise AssertionError("source service should not be called")


def test_missing_search_snapshot_returns_no_work_without_reading(tmp_path: Path) -> None:
    jobs = load_source_jobs(
        source="search",
        ats_platform="greenhouse",
        input_path=tmp_path / "missing.json",
        tracker_path=None,
        services=SourceServices(
            read_json=_unused,
            read_tracker=lambda _path: (),
            detect_ats=lambda _url: None,
        ),
    )

    assert jobs == []


def test_search_strategy_normalizes_only_live_declared_provider_jobs(tmp_path: Path) -> None:
    snapshot = tmp_path / "jobs.json"
    snapshot.write_text("[]\n", encoding="utf-8")
    payload = [
        {
            "platform": "greenhouse",
            "company": "Example",
            "title": "Product Manager",
            "description": "Build AI products",
            "job_url": "https://boards.greenhouse.io/example/jobs/12345",
            "apply_url": "https://boards.greenhouse.io/example/jobs/12345",
            "live_status": "live",
        },
        {
            "platform": "lever",
            "company": "Other",
            "title": "Product Manager",
            "job_url": "https://jobs.lever.co/other/abc",
            "live_status": "live",
        },
    ]

    jobs = load_source_jobs(
        source="search",
        ats_platform="greenhouse",
        input_path=snapshot,
        tracker_path=None,
        services=SourceServices(
            read_json=lambda _path: payload,
            read_tracker=lambda _path: (),
            detect_ats=lambda _url: None,
        ),
    )

    assert len(jobs) == 1
    assert jobs[0]["platform"] == "greenhouse"
    assert jobs[0]["_canonical_url"] == ("https://boards.greenhouse.io/example/jobs/12345")


def test_tracker_strategy_requires_declared_and_detected_provider_match(
    tmp_path: Path,
) -> None:
    tracker = tmp_path / "jobs.xlsx"
    records = (
        {
            "url": "https://boards.greenhouse.io/example/jobs/12345",
            "company": "Example",
            "role": "Product Manager",
            "ats": "greenhouse",
            "row_number": 2,
        },
        {
            "url": "https://jobs.lever.co/example/abc",
            "company": "Mismatch",
            "role": "Product Manager",
            "ats": "greenhouse",
            "row_number": 3,
        },
    )

    jobs = load_source_jobs(
        source="tracker",
        ats_platform="greenhouse",
        input_path=tmp_path / "unused.json",
        tracker_path=tracker,
        services=SourceServices(
            read_json=_unused,
            read_tracker=lambda path: records if path == tracker else (),
            detect_ats=lambda url: "greenhouse" if "greenhouse.io" in url else "lever",
        ),
    )

    assert jobs == [
        {
            "job_url": "https://boards.greenhouse.io/example/jobs/12345",
            "company": "Example",
            "title": "Product Manager",
            "platform": "greenhouse",
            "tracker_row": 2,
        }
    ]


def test_tracker_and_source_contracts_fail_closed(tmp_path: Path) -> None:
    services = SourceServices(
        read_json=_unused,
        read_tracker=lambda _path: (),
        detect_ats=lambda _url: None,
    )
    with pytest.raises(ValueError, match="--tracker is required"):
        load_source_jobs(
            source="tracker",
            ats_platform="greenhouse",
            input_path=tmp_path / "unused.json",
            tracker_path=None,
            services=services,
        )
    with pytest.raises(ValueError, match="unsupported continuous worker source"):
        load_source_jobs(
            source="unknown",
            ats_platform="greenhouse",
            input_path=tmp_path / "unused.json",
            tracker_path=None,
            services=services,
        )


def test_platform_validation_uses_the_installed_engine_registry() -> None:
    looked_up: list[str] = []

    def find_module(module_name: str) -> object | None:
        looked_up.append(module_name)
        return object() if module_name.endswith(".lever") else None

    assert validate_worker_platform(" Lever ", find_module=find_module) == "lever"
    assert looked_up == ["job_application_automation.engines.lever"]
    with pytest.raises(ValueError, match="engine is not installed"):
        validate_worker_platform("futureats", find_module=find_module)
    with pytest.raises(ValueError, match="lowercase letters and digits"):
        validate_worker_platform("lever;stop", find_module=find_module)
