from __future__ import annotations

import logging
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.search import job_boards as search  # noqa: E402
from job_application_automation.search import cache as search_cache  # noqa: E402
from job_application_automation.search import discovery as search_discovery  # noqa: E402
from job_application_automation.search import jsonld as search_jsonld  # noqa: E402
from job_application_automation.search import liveness as search_liveness  # noqa: E402
from job_application_automation.search import models as search_models  # noqa: E402
from job_application_automation.search import serialization as search_serialization  # noqa: E402


UTC = timezone.utc


def make_job() -> search.Job:
    return search.Job(
        platform="greenhouse",
        company="Example",
        title="Product Manager",
        posted_at="2026-07-28T00:00:00Z",
        days_old=0,
        location="Remote",
        workplace_type="Remote",
        employment_type="Full time",
        department="Product",
        team="AI",
        salary="",
        job_url="https://boards.greenhouse.io/example/jobs/123",
        apply_url="https://boards.greenhouse.io/example/jobs/123",
        board_token="example",
        date_source="first_published",
        match_reason="role=Product Manager | AI=AI | location=Remote",
        platform_job_id="123",
        provider_id_trusted=True,
    )


class SearchBoundaryTests(unittest.TestCase):
    def test_search_models_are_reexported_from_the_data_only_module(self) -> None:
        self.assertIs(search.Board, search_models.Board)
        self.assertIs(search.SearchCandidate, search_models.SearchCandidate)
        self.assertIs(search.Job, search_models.Job)
        candidate = search.SearchCandidate(
            url="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fboards.greenhouse.io%2Fexample%2Fjobs%2F123"
        )
        self.assertEqual(candidate.cache_key, search.discovery_url_key(candidate.url))

    def test_cache_codec_keeps_versioned_schema_and_candidate_provenance(self) -> None:
        board = search.Board("greenhouse", "example")
        candidate = search.SearchCandidate(
            url="https://boards.greenhouse.io/example/jobs/123",
            board=board,
            provenance=["role|auto|wt-wt|site:boards.greenhouse.io"],
        )
        cache = search.DiscoveryCache(
            boards={board},
            candidates_by_board={board.key: [candidate]},
            query_history=[{"query": "old"}, {"query": "current"}],
        )

        payload = search_cache.discovery_cache_payload(cache, updated_at="2026-07-28T00:00:00Z")
        decoded = search_cache.decode_discovery_cache(
            payload,
            make_cache=search.DiscoveryCache,
            board_from_cache_value=search.board_from_cache_value,
            make_candidate=search.SearchCandidate,
            add_candidate=search.add_candidate,
            clean_text=search.clean_whitespace,
        )

        self.assertEqual(2, payload["version"])
        self.assertEqual(["old", "current"], [entry["query"] for entry in payload["query_history"]])
        self.assertEqual({board}, decoded.boards)
        self.assertEqual(candidate.provenance, decoded.candidates_by_board[board.key][0].provenance)

    def test_discovery_service_runs_with_injected_search_transport(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_search_text(query: str, **kwargs: object) -> list[dict[str, object]]:
            calls.append({"query": query, **kwargs})
            return [
                {
                    "href": "https://boards.greenhouse.io/example/jobs/123",
                    "title": "Product Manager",
                    "body": "AI role",
                }
            ]

        stats = search.DiscoveryStats()
        boards, candidates_by_board, result_stats = search_discovery.discover_boards(
            queries=[search.DiscoveryQuery('"AI" Product Manager jobs', "role")],
            site_hosts=["site:boards.greenhouse.io"],
            allowed_platforms={"greenhouse"},
            regions=["wt-wt"],
            timelimit=None,
            results_per_query=5,
            backends=["auto"],
            timeout=1,
            delay=0,
            max_queries=1,
            search_retries=0,
            stats=stats,
            now_text="2026-07-28T00:00:00Z",
            search_text=fake_search_text,
            unwrap_url=search.unwrap_search_url,
            board_from_url=search.board_from_url,
            looks_like_job_url=search.looks_like_job_url,
            make_candidate=search.SearchCandidate,
            add_candidate=search.add_candidate,
            clean_text=search.clean_whitespace,
            sleep=lambda _seconds: None,
            logger=logging.getLogger("search-boundaries"),
        )

        board = search.Board("greenhouse", "example")
        self.assertEqual(1, len(calls))
        self.assertEqual({board}, boards)
        self.assertEqual(1, result_stats.candidates_discovered)
        self.assertEqual("Product Manager", candidates_by_board[board.key][0].title)

    def test_jsonld_and_liveness_boundaries_are_network_free(self) -> None:
        page = """
        <script type="application/ld+json">
        {"@graph": [{"@type": "JobPosting", "title": "Product Manager",
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "jobLocationType": "TELECOMMUTE", "validThrough": "2030-01-01"}]}
        </script>
        """
        records = list(search_jsonld.extract_jsonld_objects(page))
        jobs = [record for record in records if search_jsonld.is_jobposting_object(record)]
        self.assertEqual(1, len(jobs))
        self.assertEqual("Remote", search_jsonld.jsonld_location(jobs[0]))

        decision = search_liveness.page_response_decision(
            status_code=200,
            response_url="https://boards.greenhouse.io/example/jobs/123",
            html_text=page,
            job_title="Product Manager",
            job_urls=["https://boards.greenhouse.io/example/jobs/123"],
            existing_status="listed",
            dead_role_markers=search.DEAD_ROLE_MARKERS,
            canonical_url=search.canonical_url,
            clean_text=search.clean_whitespace,
            normalize_text=search.normalize_match_text,
            extract_jsonld_objects=search_jsonld.extract_jsonld_objects,
            is_jobposting_object=search_jsonld.is_jobposting_object,
            is_not_expired=lambda _value, _now: False,
            now=datetime(2026, 7, 28, tzinfo=UTC),
        )

        self.assertEqual("closed", decision.status)
        self.assertEqual("valid_through_elapsed", decision.reason)

    def test_liveness_workflow_keeps_the_public_jsonld_patch_seam(self) -> None:
        job = make_job()

        class Response:
            status_code = 200
            url = job.job_url
            text = "<html>ignored because the workflow extractor is patched</html>"

        class Session:
            def get(self, *_args: object, **_kwargs: object) -> Response:
                return Response()

        record = {
            "@type": "JobPosting",
            "title": job.title,
            "url": job.job_url,
            "validThrough": "2030-01-01",
        }
        with patch.object(search, "extract_jsonld_objects", return_value=[record]) as extractor:
            search.verify_page_job_live(
                Session(),
                job,
                timeout=1,
                now=datetime(2026, 7, 28, tzinfo=UTC),
            )

        extractor.assert_called_once_with(Response.text)
        self.assertEqual("live", job.live_status)

    def test_serialization_boundary_preserves_search_schema(self) -> None:
        rows = search_serialization.job_rows([make_job()])
        csv_text = search_serialization.render_csv(rows, fieldnames=search.CSV_FIELDS)
        json_text = search_serialization.render_json(rows)

        self.assertTrue(csv_text.startswith("platform,company,title"))
        self.assertIn("Product Manager", csv_text)
        self.assertIn('"platform": "greenhouse"', json_text)


if __name__ == "__main__":
    unittest.main()
