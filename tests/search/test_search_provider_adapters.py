from __future__ import annotations

import ast
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from job_application_automation.search import job_boards as search
from job_application_automation.search.providers import ashby, greenhouse, lever, registry
from job_application_automation.search.providers import smartrecruiters, workable
from job_application_automation.search.providers.contracts import FetchContext, FetchServices


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
        page_limits={"lever": 1},
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
        assert adapter.matches_url is module.matches_url
        assert adapter.board_from_url is module.board_from_url
        assert adapter.looks_like_job_url is module.looks_like_job_url
        assert (adapter.verify_one is None) != (adapter.verify_many is None)


def test_fetch_context_keeps_provider_pagination_out_of_the_shared_schema() -> None:
    context = make_context()

    assert context.max_pages_for("lever") == 1
    assert context.max_pages_for("greenhouse") == 0
    assert not hasattr(context, "max_lever_pages")


def test_registry_dispatch_preserves_restricted_web_and_unsupported_semantics() -> None:
    board = search.Board("greenhouse", "example")
    with patch.object(greenhouse, "fetch_jobs", return_value=[make_job()]) as fetch:
        adapter = registry.PROVIDER_ADAPTERS["greenhouse"]
        with patch.dict(
            registry.PROVIDER_ADAPTERS,
            {
                "greenhouse": registry.ProviderAdapter(
                    platform=adapter.platform,
                    matches_url=adapter.matches_url,
                    board_from_url=adapter.board_from_url,
                    looks_like_job_url=adapter.looks_like_job_url,
                    fetch=greenhouse.fetch_jobs,
                    verify_one=adapter.verify_one,
                )
            },
            clear=False,
        ):
            jobs = registry.fetch_board_jobs(
                object(),  # type: ignore[arg-type]
                board,
                make_context(),
                services=search._provider_fetch_services(),
                is_restricted_board=lambda _board: False,
            )
    assert len(jobs) == 1
    fetch.assert_called_once()

    assert (
        registry.fetch_board_jobs(
            object(),  # type: ignore[arg-type]
            board,
            make_context(),
            services=search._provider_fetch_services(),
            is_restricted_board=lambda _board: True,
        )
        == []
    )
    assert (
        registry.fetch_board_jobs(
            object(),  # type: ignore[arg-type]
            search.Board("web", "careers.example.com"),
            make_context(),
            services=search._provider_fetch_services(),
            is_restricted_board=lambda _board: False,
        )
        == []
    )
    with pytest.raises(ValueError, match="Unsupported platform: unknown"):
        registry.fetch_board_jobs(
            object(),  # type: ignore[arg-type]
            search.Board("unknown", "example"),
            make_context(),
            services=search._provider_fetch_services(),
            is_restricted_board=lambda _board: False,
        )


def test_search_main_dispatches_through_facade_registry_and_provider_adapter() -> None:
    adapter = registry.PROVIDER_ADAPTERS["greenhouse"]
    adapter_calls: list[tuple[search.Board, FetchContext]] = []

    def fetch_from_adapter(
        _session: requests.Session,
        board: search.Board,
        context: FetchContext,
        *,
        services: FetchServices,
    ) -> list[search.Job]:
        assert services is not None
        adapter_calls.append((board, context))
        return [make_job(live_status="listed")]

    replacement = registry.ProviderAdapter(
        platform=adapter.platform,
        matches_url=adapter.matches_url,
        board_from_url=adapter.board_from_url,
        looks_like_job_url=adapter.looks_like_job_url,
        fetch=fetch_from_adapter,
        verify_one=adapter.verify_one,
        verify_many=adapter.verify_many,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        with (
            patch.dict(registry.PROVIDER_ADAPTERS, {"greenhouse": replacement}),
            patch.object(search, "fetch_board_jobs", wraps=search.fetch_board_jobs) as facade,
        ):
            exit_code = search.main(
                [
                    "--role-type",
                    "Product Manager",
                    "--ats-platform",
                    "greenhouse",
                    "--location",
                    "New York",
                    "--skip-search",
                    "--board-url",
                    "https://boards.greenhouse.io/example",
                    "--scrape-discovered-pages",
                    "none",
                    "--no-exclude-logged",
                    "--cache",
                    str(root / "cache.json"),
                    "--output",
                    str(root / "jobs.csv"),
                    "--no-coverage-report",
                ]
            )

        assert exit_code == 0
        assert facade.call_count == 1
        assert len(adapter_calls) == 1
        board, context = adapter_calls[0]
        assert board == search.Board("greenhouse", "example")
        assert context.criteria.role_terms[0] == "Product Manager"
        assert "Product Manager" in (root / "jobs.csv").read_text(encoding="utf-8-sig")


@pytest.mark.parametrize(
    ("url", "platform", "is_job"),
    [
        ("https://jobs.ashbyhq.com/example/role", "ashby", True),
        ("https://jobs.eu.lever.co/example/role", "lever", True),
        ("https://job-boards.eu.greenhouse.io/example/jobs/123", "greenhouse", True),
        ("https://jobs.smartrecruiters.com/example/123-role", "smartrecruiters", True),
        ("https://apply.workable.com/example/j/ABC123", "workable", True),
    ],
)
def test_registry_owns_provider_url_recognition(
    url: str,
    platform: str,
    is_job: bool,
) -> None:
    board = registry.board_from_url(url, generic_host_suffixes=())
    assert board is not None
    assert board.platform == platform
    assert registry.looks_like_job_url(url, generic_host_suffixes=()) is is_job


def test_registry_owns_single_and_batch_liveness_dispatch() -> None:
    single = make_job(platform="greenhouse")
    batch = make_job(platform="ashby")
    services = search._provider_liveness_services()
    sleeps: list[float] = []
    single_calls: list[search.Job] = []
    batch_calls: list[list[search.Job]] = []

    def verify_one(_session: object, job: search.Job, **_kwargs: object) -> None:
        single_calls.append(job)

    def verify_many(_session: object, jobs: list[search.Job], **_kwargs: object) -> None:
        batch_calls.append(jobs)

    greenhouse_adapter = registry.PROVIDER_ADAPTERS["greenhouse"]
    ashby_adapter = registry.PROVIDER_ADAPTERS["ashby"]
    replacements = {
        "greenhouse": registry.ProviderAdapter(
            platform=greenhouse_adapter.platform,
            matches_url=greenhouse_adapter.matches_url,
            board_from_url=greenhouse_adapter.board_from_url,
            looks_like_job_url=greenhouse_adapter.looks_like_job_url,
            fetch=greenhouse_adapter.fetch,
            verify_one=verify_one,  # type: ignore[arg-type]
        ),
        "ashby": registry.ProviderAdapter(
            platform=ashby_adapter.platform,
            matches_url=ashby_adapter.matches_url,
            board_from_url=ashby_adapter.board_from_url,
            looks_like_job_url=ashby_adapter.looks_like_job_url,
            fetch=ashby_adapter.fetch,
            verify_many=verify_many,  # type: ignore[arg-type]
        ),
    }
    with patch.dict(registry.PROVIDER_ADAPTERS, replacements, clear=False):
        registry.verify_jobs_live(
            object(),  # type: ignore[arg-type]
            [single, batch],
            timeout=1,
            delay=0.25,
            now=NOW,
            services=services,
            sleep=sleeps.append,
        )

    assert single_calls == [single]
    assert batch_calls == [[batch]]
    assert sleeps == [0.25, 0.25]


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
