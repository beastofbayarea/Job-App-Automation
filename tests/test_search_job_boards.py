from __future__ import annotations

import sys
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.search import job_boards as search  # noqa: E402


UTC = timezone.utc
NOW = datetime(2026, 7, 28, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        url: str = "https://example.test/job",
        payload: object | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> object:
        if self.payload is None:
            raise ValueError("no JSON")
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response

    def get(self, *_args, **_kwargs) -> FakeResponse:
        return self.response


def make_job(**overrides: object) -> search.Job:
    values: dict[str, object] = {
        "platform": "greenhouse",
        "company": "Example",
        "title": "Product Manager",
        "posted_at": "2026-07-20T00:00:00Z",
        "days_old": 1,
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


def make_criteria(**overrides: object) -> search.SearchCriteria:
    values: dict[str, object] = {
        "role_terms": ("Product Manager",),
        "ai_terms": ("AI",),
        "exclude_terms": (),
        "location_terms": ("New York",),
        "days": 0,
        "include_unknown_dates": False,
    }
    values.update(overrides)
    return search.SearchCriteria(**values)  # type: ignore[arg-type]


def make_fetch_context(**overrides: object) -> search.FetchContext:
    values: dict[str, object] = {
        "criteria": make_criteria(),
        "now": NOW,
        "timeout": 1,
        "delay": 0,
        "max_lever_pages": 0,
    }
    values.update(overrides)
    return search.FetchContext(**values)  # type: ignore[arg-type]


class SearchJobBoardsTests(unittest.TestCase):
    def test_role_family_aliases_expand_to_intended_titles(self) -> None:
        cases = {
            "Grwoth Mkt": ("Growth Marketing Manager", "Growth Marketing"),
            "Performace Mkt": ("Performance Marketer", "Performance Marketing"),
            "Paid Media": ("Paid Social Manager", "Paid Search"),
            "Marketing Operations": ("Marketing Ops Manager", "Marketing Automation"),
            "Management Consukting": ("Strategy Consultant", "Management Consulting"),
            "Corp Dev": ("Corporate Development Associate", "Mergers & Acquisitions", "M&A"),
            "Venture Capital": ("VC Associate", "Venture Investor", "Venture Capitalist"),
        }

        for requested_role, expected_titles in cases.items():
            with self.subTest(requested_role=requested_role):
                role_terms = search.expand_aliases([requested_role], search.ROLE_ALIAS_MAP)
                for title in expected_titles:
                    self.assertTrue(
                        search.matching_terms(title, role_terms),
                        f"{requested_role!r} did not match {title!r}: {role_terms}",
                    )

    def test_corporate_development_family_excludes_business_development(self) -> None:
        role_terms = search.expand_aliases(["Corp Dev"], search.ROLE_ALIAS_MAP)
        self.assertFalse(search.matching_terms("Business Development Manager", role_terms))

    def test_canonical_discovery_terms_skip_typos_and_cover_each_role_family(self) -> None:
        requested = [
            "Grwoth Mkt",
            "Performace Mkt",
            "Paid Media",
            "Marketing Operations",
            "Management Consukting",
            "Corp Dev",
            "Venture Capital",
        ]
        discovery_terms = search.canonical_discovery_terms(requested, search.ROLE_ALIAS_MAP)
        self.assertEqual(
            [
                "Growth Marketing",
                "Performance Marketing",
                "Paid Media",
                "Marketing Operations",
                "Management Consulting",
                "Corporate Development",
                "Venture Capital",
            ],
            discovery_terms,
        )

        queries = search.build_discovery_queries(
            role_terms=discovery_terms,
            ai_terms=["AI"],
            location_terms=["New York"],
            extra_phrases=[],
            mode="exhaustive",
        )
        first_wave = [query for query in queries if query.family == "role_ai_location"]
        self.assertEqual(len(discovery_terms), len(first_wave))
        for role in discovery_terms:
            self.assertTrue(any(role in query.text for query in first_wave))
        self.assertFalse(any("Grwoth" in query.text for query in queries))

    def test_capped_discovery_attempts_each_requested_role_in_first_wave(self) -> None:
        role_terms = ["Growth Marketing", "Corporate Development", "Venture Capital"]
        queries = search.build_discovery_queries(
            role_terms=role_terms,
            ai_terms=["AI"],
            location_terms=["New York"],
            extra_phrases=[],
            mode="exhaustive",
        )
        with self.assertLogs(search.LOGGER, level="WARNING"):
            with patch.object(search, "ddgs_text_search", return_value=[]):
                _, _, stats = search.discover_boards(
                    queries=queries,
                    site_hosts=["site:boards.greenhouse.io", "site:jobs.lever.co"],
                    allowed_platforms={"greenhouse", "lever"},
                    regions=["wt-wt"],
                    timelimit=None,
                    results_per_query=1,
                    backends=["auto"],
                    timeout=1,
                    delay=0,
                    max_queries=len(role_terms),
                    search_retries=0,
                )
        self.assertEqual(len(role_terms), stats.queries_attempted)
        for role in role_terms:
            self.assertTrue(any(role in item["query"] for item in stats.query_log))
        self.assertTrue(any("boards.greenhouse.io" in item["query"] for item in stats.query_log))
        self.assertTrue(any("jobs.lever.co" in item["query"] for item in stats.query_log))

    def test_location_alias_nyc_matches_new_york_city(self) -> None:
        locations = search.expand_aliases(["NYC"], search.LOCATION_ALIAS_MAP)
        reason = search.job_match_reason(
            title="Product-Manager",
            description="Builds AI products",
            location="New York City, NY",
            role_terms=["Product Manager"],
            ai_terms=["AI"],
            exclude_terms=[],
            location_terms=locations,
            match_mode="expanded",
        )
        self.assertIsNotNone(reason)
        self.assertIn("location=New York", reason or "")

    def test_remote_matches_workplace_type_when_location_is_blank(self) -> None:
        reason = search.job_match_reason(
            title="Product Manager",
            description="AI platform role",
            location="",
            workplace_type="Remote",
            role_terms=["Product Manager"],
            ai_terms=["AI"],
            exclude_terms=[],
            location_terms=search.expand_aliases(["Remote"], search.LOCATION_ALIAS_MAP),
            match_mode="expanded",
        )
        self.assertIsNotNone(reason)

    def test_exhaustive_query_plan_contains_broad_discovery_families(self) -> None:
        queries = search.build_discovery_queries(
            role_terms=["Product Manager"],
            ai_terms=search.DEFAULT_AI_TERMS,
            location_terms=["New York"],
            extra_phrases=[],
            mode="exhaustive",
        )
        families = {query.family for query in queries}
        self.assertTrue(
            {"role_ai_location", "role_ai", "role_location", "role_only", "careers_role"}
            <= families
        )

    def test_parser_writes_default_results_under_output_directory(self) -> None:
        parser = search.build_parser()
        args = parser.parse_args(
            [
                "--role-type",
                "Product Manager",
                "--ats-platform",
                "greenhouse",
                "--location",
                "New York",
            ]
        )
        self.assertEqual(search.OUTPUT_DIR / "ai_jobs.csv", args.output)

    def test_parser_allows_default_locations_when_location_is_omitted(self) -> None:
        args = search.build_parser().parse_args(
            [
                "--role-type",
                "Product Manager",
                "--ats-platform",
                "greenhouse",
            ]
        )
        self.assertIsNone(args.location)

    def test_parser_supports_first_class_smartrecruiters_and_workable_search(self) -> None:
        args = search.build_parser().parse_args(
            [
                "--role-type",
                "Product Manager",
                "--ats-platform",
                "smartrecruiters",
                "--ats-platform",
                "workable",
            ]
        )
        self.assertEqual(["smartrecruiters", "workable"], args.ats_platforms)

    def test_smartrecruiters_and_workable_urls_use_provider_boards(self) -> None:
        smartrecruiters = search.board_from_url(
            "https://jobs.smartrecruiters.com/Example/123-product-manager"
        )
        workable = search.board_from_url("https://apply.workable.com/example-company/j/ABC123/")
        workable_short = search.board_from_url("https://apply.workable.com/j/ABC123/")

        self.assertEqual(search.Board("smartrecruiters", "Example"), smartrecruiters)
        self.assertEqual(search.Board("workable", "example-company"), workable)
        self.assertEqual(
            search.Board("workable", "apply.workable.com"),
            workable_short,
        )
        self.assertTrue(
            search.looks_like_job_url(
                "https://jobs.smartrecruiters.com/Example/123-product-manager"
            )
        )
        self.assertTrue(
            search.looks_like_job_url("https://apply.workable.com/example-company/j/ABC123/")
        )

    def test_generic_ats_url_becomes_web_candidate(self) -> None:
        board = search.board_from_url(
            "https://example.wd1.myworkdayjobs.com/en-US/Careers/job/New-York/Product-Manager_123"
        )
        self.assertIsNotNone(board)
        self.assertEqual("web", board.platform if board else "")

    def test_jsonld_skips_expired_record_and_uses_later_live_record(self) -> None:
        html = """
        <script type="application/ld+json">
        [
          {"@type": "JobPosting", "title": "Product Manager", "description": "AI role", "datePosted": "2026-07-20", "validThrough": "2020-01-01", "jobLocation": {"address": {"addressLocality": "New York"}}},
          {"@type": "JobPosting", "title": "Product Manager", "description": "AI role", "datePosted": "2026-07-20", "validThrough": "2030-01-01", "jobLocation": {"address": {"addressLocality": "New York"}}}
        ]
        </script>
        """
        job = search.scrape_jsonld_job(
            FakeSession(FakeResponse(text=html)),
            search.SearchCandidate(url="https://example.test/jobs/1"),
            timeout=1,
            now=datetime(2026, 7, 28, tzinfo=UTC),
            days=0,
            include_unknown_dates=False,
            role_terms=["Product Manager"],
            ai_terms=["AI"],
            exclude_terms=[],
            location_terms=["New York"],
        )
        self.assertIsNotNone(job)
        self.assertEqual("Product Manager", job.title if job else "")

    def test_date_only_expiry_is_inclusive(self) -> None:
        self.assertTrue(search.ensure_not_expired("2026-07-28", NOW))
        self.assertFalse(search.ensure_not_expired("2026-07-28", datetime(2026, 7, 29, tzinfo=UTC)))

    def test_jsonld_parser_returns_multiple_qualified_jobposting_records(self) -> None:
        html = """
        <script type="application/ld+json">
        [
          {"@type": "https://schema.org/JobPosting", "title": "Product Manager", "description": "AI role", "datePosted": "2026-07-20", "validThrough": "2030-01-01", "identifier": "one", "jobLocation": {"address": {"addressLocality": "New York"}}},
          {"@type": "JobPosting", "title": "Product Manager II", "description": "AI role", "datePosted": "2026-07-21", "validThrough": "2030-01-01", "identifier": "two", "jobLocation": {"address": {"addressLocality": "New York"}}}
        ]
        </script>
        """
        jobs = search.scrape_jsonld_jobs(
            FakeSession(FakeResponse(text=html)),
            search.SearchCandidate(url="https://example.test/jobs"),
            timeout=1,
            now=NOW,
            criteria=make_criteria(),
        )
        self.assertEqual(["Product Manager", "Product Manager II"], [job.title for job in jobs])
        self.assertTrue(all(not job.provider_id_trusted for job in jobs))
        self.assertEqual(2, len(search.deduplicate_jobs(jobs)))

    def test_api_and_jsonld_versions_merge_by_canonical_url(self) -> None:
        api_job = make_job(live_status="listed")
        jsonld_job = make_job(
            platform="web",
            platform_job_id="bare-id",
            provider_id_trusted=False,
            unique_id="bare-id",
            date_source="jsonld.datePosted",
            live_status="listed",
        )
        merged = search.deduplicate_jobs([api_job, jsonld_job])
        self.assertEqual(1, len(merged))
        self.assertEqual("greenhouse", merged[0].platform)

    def test_page_404_marks_role_closed(self) -> None:
        job = make_job(live_status="listed")
        search.verify_page_job_live(
            FakeSession(FakeResponse(status_code=404)),
            job,
            timeout=1,
            now=datetime(2026, 7, 28, tzinfo=UTC),
        )
        self.assertEqual("closed", job.live_status)

    def test_page_liveness_rejects_same_title_from_a_different_job_url(self) -> None:
        job = make_job(live_status="not_checked", provider_id_trusted=False)
        html = """
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Product Manager", "url": "https://boards.greenhouse.io/example/jobs/999", "validThrough": "2030-01-01"}
        </script>
        """
        search.verify_page_job_live(
            FakeSession(
                FakeResponse(
                    text=html,
                    url="https://boards.greenhouse.io/example/jobs/123",
                )
            ),
            job,
            timeout=1,
            now=NOW,
        )
        self.assertEqual("unknown", job.live_status)

    def test_greenhouse_job_endpoint_confirms_live_role(self) -> None:
        job = make_job(live_status="listed")
        response = FakeResponse(payload={"id": 123, "application_deadline": "2030-01-01"})
        search.verify_greenhouse_job_live(
            FakeSession(response),
            job,
            timeout=1,
            now=datetime(2026, 7, 28, tzinfo=UTC),
        )
        self.assertEqual("live", job.live_status)
        self.assertEqual("greenhouse_job_api", job.live_check_source)

    def test_smartrecruiters_job_endpoint_confirms_live_role(self) -> None:
        job = make_job(
            platform="smartrecruiters",
            board_token="example",
            platform_job_id="123",
            provider_id_trusted=True,
            live_status="listed",
        )
        response = FakeResponse(payload={"id": "123", "active": True, "visibility": "PUBLIC"})
        search.verify_smartrecruiters_job_live(
            FakeSession(response),
            job,
            timeout=1,
            now=NOW,
        )
        self.assertEqual("live", job.live_status)
        self.assertEqual("smartrecruiters_posting_api", job.live_check_source)

    def test_workable_account_endpoint_confirms_live_role(self) -> None:
        job = make_job(
            platform="workable",
            board_token="example",
            platform_job_id="ABC123",
            provider_id_trusted=True,
            live_status="listed",
        )
        response = FakeResponse(payload={"jobs": [{"shortcode": "ABC123"}]})
        search.verify_workable_jobs_live(
            FakeSession(response),
            [job],
            timeout=1,
            now=NOW,
        )
        self.assertEqual("live", job.live_status)
        self.assertEqual("workable_account_api", job.live_check_source)

    def test_ashby_missing_provider_id_is_unknown(self) -> None:
        job = make_job(
            platform="ashby",
            board_token="example",
            platform_job_id="",
            provider_id_trusted=True,
        )
        search.verify_ashby_jobs_live(FakeSession(FakeResponse()), [job], timeout=1, now=NOW)
        self.assertEqual("unknown", job.live_status)

    def test_ashby_unlisted_job_is_not_confirmed_live(self) -> None:
        job = make_job(
            platform="ashby",
            board_token="example",
            platform_job_id="job-1",
            provider_id_trusted=True,
        )
        response = FakeResponse(payload={"jobs": [{"id": "job-1", "isListed": False}]})
        search.verify_ashby_jobs_live(FakeSession(response), [job], timeout=1, now=NOW)
        self.assertEqual("closed", job.live_status)

    def test_listing_verification_uses_page_for_untrusted_jsonld_job(self) -> None:
        job = make_job(platform="web", provider_id_trusted=False, live_status="listed")
        with patch.object(search, "verify_page_job_live") as verify_page:
            search.verify_live_jobs(
                FakeSession(FakeResponse()),
                [job],
                timeout=1,
                delay=0,
                now=NOW,
                target="listing",
            )
        verify_page.assert_called_once()

    def test_deduplication_preserves_zero_days_old(self) -> None:
        today = make_job(days_old=0, posted_at="2026-07-28T00:00:00Z")
        stale = make_job(days_old=3, posted_at="2026-07-20T00:00:00Z")
        merged = search.deduplicate_jobs([today, stale])
        self.assertEqual(1, len(merged))
        self.assertEqual(0, merged[0].days_old)

    def test_deduplication_keeps_provider_platform_with_trusted_identity(self) -> None:
        provider = make_job(
            platform="greenhouse",
            date_source="updated_at_fallback",
            provider_id_trusted=True,
        )
        jsonld = make_job(
            platform="web",
            date_source="jsonld.datePosted",
            provider_id_trusted=False,
            platform_job_id="generic-id",
            unique_id="generic-id",
        )
        merged = search.deduplicate_jobs([jsonld, provider])
        self.assertEqual(1, len(merged))
        self.assertEqual("greenhouse", merged[0].platform)
        self.assertTrue(merged[0].provider_id_trusted)
        self.assertEqual("123", merged[0].platform_job_id)

    def test_lever_region_is_part_of_provider_identity(self) -> None:
        global_job = make_job(
            platform="lever",
            board_token="example",
            board_region="global",
            platform_job_id="same-id",
            unique_id="lever:global:example:same-id",
            job_url="https://jobs.lever.co/example/same-id",
            apply_url="https://jobs.lever.co/example/same-id/apply",
        )
        eu_job = make_job(
            platform="lever",
            board_token="example",
            board_region="eu",
            platform_job_id="same-id",
            unique_id="lever:eu:example:same-id",
            job_url="https://jobs.eu.lever.co/example/same-id",
            apply_url="https://jobs.eu.lever.co/example/same-id/apply",
        )
        self.assertEqual(2, len(search.deduplicate_jobs([global_job, eu_job])))

    def test_version_one_board_cache_migrates_to_candidate_aware_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "boards": [
                            {"platform": "greenhouse", "token": "example", "region": "global"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cache = search.load_discovery_cache(cache_path)
            self.assertEqual({search.Board("greenhouse", "example")}, cache.boards)
            candidate = search.SearchCandidate(
                url="https://boards.greenhouse.io/example/jobs/123",
                board=search.Board("greenhouse", "example"),
            )
            search.add_candidate(cache.candidates_by_board, candidate)
            search.save_discovery_cache(cache_path, cache)
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(2, payload["version"])
        self.assertEqual(1, len(payload["candidates"]))

    def test_main_writes_live_fields_and_coverage_without_network_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_path = root / "jobs.csv"
            cache_path = root / "cache.json"
            coverage_path = root / "coverage.json"
            with patch.object(
                search, "fetch_board_jobs", return_value=[make_job(live_status="listed")]
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
                        "--cache",
                        str(cache_path),
                        "--output",
                        str(output_path),
                        "--coverage-report",
                        str(coverage_path),
                    ]
                )
            self.assertEqual(0, exit_code)
            self.assertIn("live_status", output_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(
                1, json.loads(coverage_path.read_text(encoding="utf-8"))["results"]["returned"]
            )

    def test_main_uses_default_locations_only_when_location_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            captured_contexts: list[search.FetchContext] = []

            def capture_fetch(
                _session: object,
                _board: search.Board,
                context: search.FetchContext,
            ) -> list[search.Job]:
                captured_contexts.append(context)
                return []

            with patch.object(search, "fetch_board_jobs", side_effect=capture_fetch):
                exit_code = search.main(
                    [
                        "--role-type",
                        "Product Manager",
                        "--ats-platform",
                        "greenhouse",
                        "--skip-search",
                        "--board-url",
                        "https://boards.greenhouse.io/example",
                        "--scrape-discovered-pages",
                        "none",
                        "--cache",
                        str(root / "cache.json"),
                        "--output",
                        str(root / "jobs.csv"),
                        "--no-coverage-report",
                    ]
                )
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(captured_contexts))
        self.assertEqual(
            list(search.DEFAULT_LOCATION_TERMS),
            list(captured_contexts[0].criteria.location_terms),
        )

    def test_explicit_location_replaces_default_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            captured_contexts: list[search.FetchContext] = []

            def capture_fetch(
                _session: object,
                _board: search.Board,
                context: search.FetchContext,
            ) -> list[search.Job]:
                captured_contexts.append(context)
                return []

            with patch.object(search, "fetch_board_jobs", side_effect=capture_fetch):
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
                        "--cache",
                        str(root / "cache.json"),
                        "--output",
                        str(root / "jobs.csv"),
                        "--no-coverage-report",
                    ]
                )
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(captured_contexts))
        self.assertIn("New York", captured_contexts[0].criteria.location_terms)
        self.assertNotIn("US Remote", captured_contexts[0].criteria.location_terms)

    def test_recent_only_run_excludes_unknown_dates_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            captured_contexts: list[search.FetchContext] = []

            def capture_fetch(
                _session: object,
                _board: search.Board,
                context: search.FetchContext,
            ) -> list[search.Job]:
                captured_contexts.append(context)
                return []

            with patch.object(search, "fetch_board_jobs", side_effect=capture_fetch):
                exit_code = search.main(
                    [
                        "--role-type",
                        "Product Manager",
                        "--ats-platform",
                        "greenhouse",
                        "--location",
                        "New York",
                        "--days",
                        "7",
                        "--skip-search",
                        "--board-url",
                        "https://boards.greenhouse.io/example",
                        "--scrape-discovered-pages",
                        "none",
                        "--cache",
                        str(root / "cache.json"),
                        "--output",
                        str(root / "jobs.csv"),
                        "--no-coverage-report",
                    ]
                )
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(captured_contexts))
        self.assertFalse(captured_contexts[0].criteria.include_unknown_dates)

    def test_no_board_coverage_report_uses_the_standard_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            coverage_path = root / "coverage.json"
            exit_code = search.main(
                [
                    "--role-type",
                    "Product Manager",
                    "--ats-platform",
                    "greenhouse",
                    "--location",
                    "New York",
                    "--skip-search",
                    "--cache",
                    str(root / "cache.json"),
                    "--output",
                    str(root / "jobs.csv"),
                    "--coverage-report",
                    str(coverage_path),
                ]
            )
            report = json.loads(coverage_path.read_text(encoding="utf-8"))
        self.assertEqual(1, exit_code)
        self.assertEqual(1, report["version"])
        self.assertEqual(0, report["results"]["returned"])
        self.assertTrue(
            {"criteria", "cache", "discovery", "feed_fetch", "fallback", "results"} <= report.keys()
        )

    def test_failed_feed_mode_includes_generic_web_candidates(self) -> None:
        web_board = search.Board("web", "example.wd1.myworkdayjobs.com")
        candidates = {
            web_board.key: [
                search.SearchCandidate(
                    url="https://example.wd1.myworkdayjobs.com/en-US/Careers/job/New-York/Role_1",
                    board=web_board,
                )
            ]
        }
        self.assertEqual(
            [web_board.key],
            search.fallback_candidate_board_keys(candidates, set(), "failed-feed"),
        )

    def test_career_link_extraction_handles_protocol_relative_urls(self) -> None:
        urls = search.extract_ats_urls_from_html(
            '<a href="//jobs.lever.co/example">Open roles</a>',
            base_url="https://careers.example.com",
        )
        self.assertEqual(["https://jobs.lever.co/example"], urls)

    def test_greenhouse_checks_offices_before_location_filtering(self) -> None:
        list_payload = {
            "jobs": [
                {
                    "id": 123,
                    "title": "Product Manager",
                    "content": "AI platform role",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/example/jobs/123",
                }
            ]
        }
        detail_payload = {
            "id": 123,
            "title": "Product Manager",
            "content": "AI platform role",
            "first_published": "2026-07-20T00:00:00Z",
            "offices": [{"name": "New York"}],
            "absolute_url": "https://boards.greenhouse.io/example/jobs/123",
        }
        with patch.object(search, "get_json", side_effect=[list_payload, detail_payload]):
            jobs = search.fetch_greenhouse_jobs(
                FakeSession(FakeResponse()),
                search.Board("greenhouse", "example"),
                make_fetch_context(),
            )
        self.assertEqual(1, len(jobs))
        self.assertIn("New York", jobs[0].location)

    def test_greenhouse_uses_detail_location_before_final_filtering(self) -> None:
        list_payload = {
            "jobs": [
                {
                    "id": 123,
                    "title": "Product Manager",
                    "content": "AI platform role",
                    "location": {"name": ""},
                    "absolute_url": "https://boards.greenhouse.io/example/jobs/123",
                }
            ]
        }
        detail_payload = {
            "id": 123,
            "title": "Product Manager",
            "content": "AI platform role",
            "first_published": "2026-07-20T00:00:00Z",
            "location": {"name": "New York"},
            "absolute_url": "https://boards.greenhouse.io/example/jobs/123",
        }
        with patch.object(search, "get_json", side_effect=[list_payload, detail_payload]):
            jobs = search.fetch_greenhouse_jobs(
                FakeSession(FakeResponse()),
                search.Board("greenhouse", "example"),
                make_fetch_context(),
            )
        self.assertEqual(1, len(jobs))
        self.assertEqual("New York", jobs[0].location)

    def test_lever_api_base_and_salary_format(self) -> None:
        eu_board = search.Board("lever", "acme", region="eu")
        us_board = search.Board("lever", "acme", region="us")
        self.assertEqual(
            "https://api.eu.lever.co/v0/postings/acme", search.lever_api_base(eu_board)
        )
        self.assertEqual("https://api.lever.co/v0/postings/acme", search.lever_api_base(us_board))

        salary_dict = {"currency": "USD", "min": 120000, "max": 150000, "interval": "per year"}
        self.assertEqual("USD 120000 - 150000 per year", search.format_lever_salary(salary_dict))
        self.assertEqual("", search.format_lever_salary(None))

    def test_smartrecruiters_public_feed_normalizes_matching_job(self) -> None:
        listing = {
            "totalFound": 1,
            "content": [
                {
                    "id": "123",
                    "name": "Product Manager",
                    "releasedDate": "2026-07-20T00:00:00Z",
                }
            ],
        }
        detail = {
            "id": "123",
            "name": "Product Manager",
            "active": True,
            "visibility": "PUBLIC",
            "releasedDate": "2026-07-20T00:00:00Z",
            "company": {"name": "Example"},
            "location": {"fullLocation": "New York", "hybrid": True},
            "department": {"label": "Product"},
            "typeOfEmployment": {"label": "Full-time"},
            "postingUrl": "https://jobs.smartrecruiters.com/example/123-product-manager",
            "applyUrl": "https://jobs.smartrecruiters.com/example/123-product-manager?oga=true",
            "jobAd": {"sections": {"jobDescription": {"text": "<p>Build an AI platform.</p>"}}},
        }
        with patch.object(search, "get_json", side_effect=[listing, detail]):
            jobs = search.fetch_smartrecruiters_jobs(
                FakeSession(FakeResponse()),
                search.Board("smartrecruiters", "example"),
                make_fetch_context(),
            )

        self.assertEqual(1, len(jobs))
        self.assertEqual("smartrecruiters", jobs[0].platform)
        self.assertEqual("123", jobs[0].platform_job_id)
        self.assertTrue(jobs[0].provider_id_trusted)
        self.assertEqual("Hybrid", jobs[0].workplace_type)

    def test_workable_public_feed_normalizes_matching_job(self) -> None:
        payload = {
            "name": "Example",
            "jobs": [
                {
                    "title": "Product Manager",
                    "shortcode": "ABC123",
                    "employment_type": "Full-time",
                    "telecommuting": True,
                    "department": "Product",
                    "shortlink": "https://apply.workable.com/j/ABC123",
                    "application_url": "https://apply.workable.com/j/ABC123/apply",
                    "published_on": "2026-07-20",
                    "locations": [
                        {
                            "city": "New York",
                            "region": "New York",
                            "country": "United States",
                        }
                    ],
                    "description": "<p>Own the AI product roadmap.</p>",
                }
            ],
        }
        with patch.object(search, "get_json", return_value=payload):
            jobs = search.fetch_workable_jobs(
                FakeSession(FakeResponse()),
                search.Board("workable", "example"),
                make_fetch_context(),
            )

        self.assertEqual(1, len(jobs))
        self.assertEqual("workable", jobs[0].platform)
        self.assertEqual("ABC123", jobs[0].platform_job_id)
        self.assertTrue(jobs[0].provider_id_trusted)
        self.assertEqual("Remote", jobs[0].workplace_type)

    def test_restricted_urls_and_boards(self) -> None:
        self.assertTrue(search.is_restricted_url("https://jobgether.com/"))
        self.assertTrue(search.is_restricted_url("https://jobgether.com/job/123"))
        self.assertTrue(search.is_restricted_url("https://jobs.lever.co/jobgether"))
        self.assertTrue(search.is_restricted_url("https://jobs.lever.co/jobgether/123-456"))
        self.assertTrue(search.is_restricted_url("https://jobs.eu.lever.co/jobgether"))
        self.assertTrue(search.is_restricted_url("https://jobtogether.com/role/xyz"))

        self.assertFalse(search.is_restricted_url("https://jobs.lever.co/stripe/123"))
        self.assertFalse(search.is_restricted_url("https://boards.greenhouse.io/openai/jobs/456"))

        self.assertTrue(search.is_restricted_board(search.Board("lever", "jobgether")))
        self.assertTrue(search.is_restricted_board(search.Board("web", "jobgether.com")))
        self.assertFalse(search.is_restricted_board(search.Board("lever", "stripe")))

        restricted_job = make_job(
            job_url="https://jobs.lever.co/jobgether/abc",
            board_token="jobgether",
            platform="lever",
        )
        self.assertTrue(search.is_restricted_job(restricted_job))

        self.assertIsNone(search.board_from_url("https://jobgether.com/"))
        self.assertIsNone(search.board_from_url("https://jobs.lever.co/jobgether"))

    def test_load_logged_job_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "submission_log.json"
            log_path.write_text(
                json.dumps(
                    {
                        "sub1": {
                            "job_url": "https://boards.greenhouse.io/acme/jobs/999",
                            "status": "SUBMITTED & CONFIRMED",
                        }
                    }
                ),
                encoding="utf-8",
            )
            logged = search.load_logged_job_urls([log_path])
            self.assertIn("https://boards.greenhouse.io/acme/jobs/999", logged)


if __name__ == "__main__":
    unittest.main()
