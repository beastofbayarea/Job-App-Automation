from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.search import job_boards as search  # noqa: E402
from job_application_automation.search import terms as search_terms  # noqa: E402


UTC = timezone.utc


class SearchCoreTests(unittest.TestCase):
    def test_search_module_exposes_the_internal_matching_module(self) -> None:
        self.assertIs(search.clean_whitespace, search_terms.clean_whitespace)
        self.assertEqual(
            search.canonical_discovery_terms(["Growth Mkt"], search.ROLE_ALIAS_MAP),
            ["Growth Marketing"],
        )
        self.assertEqual(
            search.job_match_reason(
                title="Growth Marketing Manager",
                description="Generative AI experimentation",
                location="Remote",
                workplace_type="Remote",
                role_terms=("Growth Marketing",),
                ai_terms=("AI",),
                exclude_terms=(),
                location_terms=("Remote",),
            ),
            "role=Growth Marketing | AI=AI | location=Remote",
        )

    def test_search_outputs_remain_atomic_and_keep_csv_bom(self) -> None:
        job = search.Job(
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
            date_source="api",
            match_reason="role=Product Manager | AI=AI | location=Remote",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "nested" / "jobs.csv"
            json_path = root / "nested" / "jobs.json"

            search.write_csv(csv_path, [job])
            search.write_json(json_path, [job])

            self.assertTrue(csv_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("Product Manager", csv_path.read_text(encoding="utf-8-sig"))
            self.assertIn("Product Manager", json_path.read_text(encoding="utf-8"))
            self.assertEqual(list(csv_path.parent.glob("*.tmp")), [])

    def test_date_and_term_helpers_stay_network_free(self) -> None:
        now = datetime(2026, 7, 28, tzinfo=UTC)
        self.assertTrue(
            search.is_recent(
                datetime(2026, 7, 27, tzinfo=UTC),
                days=1,
                now=now,
                include_unknown_dates=False,
            )
        )
        self.assertEqual(
            search_terms.split_repeated_terms(["AI, ML", "ML", "GenAI"]),
            ["AI", "ML", "GenAI"],
        )


if __name__ == "__main__":
    unittest.main()
