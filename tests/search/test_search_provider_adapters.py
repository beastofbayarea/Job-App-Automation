from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from job_application_automation.search import job_boards as search
from job_application_automation.search.providers import ashby, greenhouse, lever, registry
from job_application_automation.search.providers import smartrecruiters, workable
from job_application_automation.search.providers.contracts import FetchContext


NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)
PROVIDER_MODULES = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
}


def make_context() -> search.FetchContext:
    return search.FetchContext(
        criteria=search.SearchCriteria(
            role_terms=("Product Manager",),
            ai_terms=("AI",),
            exclude_terms=(),
            location_terms=("New York",),
            days=0,
            include_unknown_dates=False,
        ),
        now=NOW,
        timeout=1,
        delay=0,
        max_lever_pages=1,
    )


def make_job(**overrides: object) -> search.Job:
    values: dict[str, object] = {
        "platform": "greenhouse",
        "company": "Example",
        "title": "Product Manager",
        "posted_at": "2026-07-20T00:00:00Z",
        "days_old": 8,
        "location": "New York",
        "workplace_type": "",
        "employment_type": "Full-time",
        "department": "Product",
        "team": "",
        "salary": "",
        "job_url": "https://boards.greenhouse.io/example/jobs/123",
        "apply_url": "https://boards.greenhouse.io/example/jobs/123",
        "board_token": "example",
        "date_source": "first_published",
        "match_reason": "role=Product Manager | AI=AI",
        "platform_job_id": "123",
        "board_region": "global",
        "provider_id_trusted": True,
        "unique_id": "greenhouse:global:example:123",
    }
    values.update(overrides)
    return search.Job(**values)  # type: ignore[arg-type]


def test_registry_exposes_one_typed_adapter_per_supported_feed() -> None:
    assert set(registry.PROVIDER_ADAPTERS) == set(PROVIDER_MODULES)
    for platform, module in PROVIDER_MODULES.items():
        adapter = registry.PROVIDER_ADAPTERS[platform]
        assert adapter.platform == platform
        assert adapter.fetch is module.fetch_jobs
        assert (adapter.verify_one is None) != (adapter.verify_many is None)


def test_registry_dispatch_preserves_restricted_web_and_unsupported_semantics() -> None:
    calls: list[search.Board] = []

    def fetch(_session: object, board: search.Board, _context: FetchContext) -> list[search.Job]:
        calls.append(board)
        return [make_job()]

    board = search.Board("greenhouse", "example")
    jobs = registry.fetch_board_jobs(
        object(),  # type: ignore[arg-type]
        board,
        make_context(),
        fetchers={"greenhouse": fetch},  # type: ignore[dict-item]
        is_restricted_board=lambda _board: False,
    )
    assert len(jobs) == 1
    assert calls == [board]

    assert (
        registry.fetch_board_jobs(
            object(),  # type: ignore[arg-type]
            board,
            make_context(),
            fetchers={"greenhouse": fetch},  # type: ignore[dict-item]
            is_restricted_board=lambda _board: True,
        )
        == []
    )
    assert (
        registry.fetch_board_jobs(
            object(),  # type: ignore[arg-type]
            search.Board("web", "careers.example.com"),
            make_context(),
            fetchers={},
            is_restricted_board=lambda _board: False,
        )
        == []
    )
    with pytest.raises(ValueError, match="Unsupported platform: unknown"):
        registry.fetch_board_jobs(
            object(),  # type: ignore[arg-type]
            search.Board("unknown", "example"),
            make_context(),
            fetchers={},
            is_restricted_board=lambda _board: False,
        )


def test_historic_provider_symbols_remain_available_from_facade() -> None:
    symbols = {
        "greenhouse_base_url",
        "fetch_greenhouse_jobs",
        "lever_api_base",
        "format_lever_salary",
        "fetch_lever_jobs",
        "ashby_salary",
        "fetch_ashby_jobs",
        "smartrecruiters_api_base",
        "smartrecruiters_description",
        "fetch_smartrecruiters_jobs",
        "workable_api_url",
        "workable_location",
        "fetch_workable_jobs",
        "fetch_board_jobs",
        "set_live_status",
        "response_or_none",
        "verify_greenhouse_job_live",
        "verify_lever_job_live",
        "verify_ashby_jobs_live",
        "verify_smartrecruiters_job_live",
        "verify_workable_jobs_live",
    }
    assert search.FetchContext is FetchContext
    assert set(search.BOARD_FETCHERS) == set(PROVIDER_MODULES)
    assert all(callable(getattr(search, symbol, None)) for symbol in symbols)


def test_lever_facade_injects_patched_json_and_jsonld_seams() -> None:
    payload = [
        {
            "id": "lever-123",
            "text": "Product Manager",
            "descriptionPlain": "Build an AI platform",
            "categories": {"location": "New York"},
            "hostedUrl": "https://jobs.lever.co/example/lever-123",
            "applyUrl": "https://jobs.lever.co/example/lever-123/apply",
        }
    ]
    fallback = make_job(
        platform="lever",
        posted_at="2026-07-20T00:00:00Z",
        board_token="example",
        platform_job_id="lever-123",
    )

    with (
        patch.object(search, "get_json", return_value=payload) as get_json,
        patch.object(search, "scrape_jsonld_jobs", return_value=[fallback]) as scrape_jsonld,
    ):
        jobs = search.fetch_lever_jobs(
            object(),  # type: ignore[arg-type]
            search.Board("lever", "example"),
            make_context(),
        )

    assert len(jobs) == 1
    assert jobs[0].posted_at == "2026-07-20T00:00:00Z"
    assert jobs[0].date_source == "jsonld.datePosted"
    get_json.assert_called_once()
    scrape_jsonld.assert_called_once()


def test_provider_liveness_facade_injects_patched_transport_and_mutator() -> None:
    response = SimpleNamespace(status_code=404, url="https://example.test/closed")
    job = make_job()

    with (
        patch.object(search, "response_or_none", return_value=response) as request,
        patch.object(search, "set_live_status", wraps=search.set_live_status) as mutate,
    ):
        search.verify_greenhouse_job_live(object(), job, timeout=1, now=NOW)  # type: ignore[arg-type]

    assert job.live_status == "closed"
    request.assert_called_once()
    mutate.assert_called_once()


def test_provider_modules_do_not_import_the_compatibility_facade() -> None:
    provider_dir = Path(search.__file__).with_name("providers")
    for path in provider_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert all("job_boards" not in module for module in imported_modules), path.name
