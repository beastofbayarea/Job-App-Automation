from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation import resume_generate  # noqa: E402
from job_application_automation.resume_cache import ResumeCache, cache_key  # noqa: E402
from job_application_automation.resume_rendering import ResumeRenderRequest  # noqa: E402
from job_application_automation.resume_scoring import (  # noqa: E402
    ResumeScorePolicy,
    score_pdf,
)
from job_application_automation.resume_validation import (  # noqa: E402
    enforce_candidate_identity,
    enforce_source_invariants,
    ensure_minimum_bullets,
    normalize_experience,
    repair_missing_experience,
    validate_resume_data,
)


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        company="Example Co",
        role_title="Product Manager",
        keywords="product, analytics",
        jd_overview="overview",
        jd_responsibilities="responsibilities",
        jd_requirements="requirements",
    )


def _source_experience() -> list[dict[str, object]]:
    return [
        {
            "company": "Example Co",
            "location": "Remote",
            "title": "Product Manager",
            "dates": "2020 - Present",
            "bullets": ["Built an analytics product with measurable outcomes."] * 4,
        },
        {
            "company": "Earlier Co",
            "location": "New York",
            "title": "Analyst",
            "dates": "2018 - 2020",
            "bullets": ["Delivered an earlier source-backed result."] * 4,
        },
    ]


class ResumeCacheTests(unittest.TestCase):
    def test_cache_key_is_context_sensitive_and_cache_isolated(self) -> None:
        cache = ResumeCache()
        job = _job()
        payload = {"experience": [{"bullets": ["original"]}]}

        cache.set(job, payload)
        payload["experience"][0]["bullets"][0] = "mutated source"
        cached = cache.get(job)
        assert cached is not None
        cached["experience"][0]["bullets"][0] = "mutated caller"

        self.assertEqual(cache.get(job)["experience"][0]["bullets"], ["original"])
        self.assertEqual(len(cache_key(job)), 64)

    def test_cache_round_trip_uses_the_existing_compact_json_schema(self) -> None:
        job = _job()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm_cache_v2.json"
            cache = ResumeCache()
            cache.set(job, {"header_name": "Candidate"})
            cache.save(path)

            restored = ResumeCache()
            self.assertEqual(restored.load(path), 1)

        self.assertEqual(restored.get(job), {"header_name": "Candidate"})


class ResumeValidationTests(unittest.TestCase):
    def test_validation_normalizes_repairs_and_restores_source_facts(self) -> None:
        source = _source_experience()
        payload: dict[str, object] = {
            "experience_data": [
                {
                    "company": "Example Co",
                    "title": "Tailored Product Lead",
                    "projects": [{"bullet_points": ["  Tailored outcome.  "]}],
                }
            ],
            "skills": ["product"] * 8,
            "professional_summary": "Summary",
            "education": [],
            "header_tagline": "Product leader",
        }

        normalize_experience(payload)
        repaired, restored_companies = repair_missing_experience(payload, source)
        enforce_source_invariants(repaired, source)
        enforce_candidate_identity(
            repaired,
            {
                "name": "Candidate Name",
                "location": "Remote",
                "email": "candidate@example.test",
                "phone": "555",
                "linkedin": "https://example.test/in/candidate",
            },
        )
        topped_up, initial_count = ensure_minimum_bullets(repaired, source, minimum_total=8)

        self.assertEqual(restored_companies, ("Earlier Co",))
        self.assertEqual(initial_count, 5)
        self.assertEqual(
            [entry["company"] for entry in topped_up["experience"]],
            ["Example Co", "Earlier Co"],
        )
        self.assertEqual(topped_up["experience"][0]["location"], "Remote")
        self.assertEqual(topped_up["header_name"], "Candidate Name")
        self.assertEqual(
            validate_resume_data(topped_up, ["Example Co", "Earlier Co"]),
            ["Low bullet count (8). Need 14-18 bullets total."],
        )


class _FakePage:
    def get_text(self, mode: str) -> dict[str, object]:
        assert mode == "dict"
        lines = [
            {
                "spans": [
                    {
                        "text": f"• Supported Company {index} with detailed measurable result.",
                        "bbox": (0, 750, 0, 0),
                        "font": "BoldItalic",
                    }
                ]
            }
            for index in range(12)
        ]
        return {"blocks": [{"type": 0, "lines": lines}]}


class _FakeDocument:
    closed = False

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int) -> _FakePage:
        assert index == 0
        return _FakePage()

    def close(self) -> None:
        self.closed = True


class _FakeFitz:
    def __init__(self) -> None:
        self.document = _FakeDocument()

    def open(self, path: str) -> _FakeDocument:
        assert path == "resume.pdf"
        return self.document


class ResumeScoringTests(unittest.TestCase):
    def test_scorer_accepts_a_fake_pdf_backend_and_closes_the_document(self) -> None:
        fake_fitz = _FakeFitz()
        score, issues = score_pdf(
            "resume.pdf",
            ResumeScorePolicy(
                original_character_count=100,
                page_height=792,
                source_companies=("Company",),
            ),
            fitz_module=fake_fitz,
        )

        self.assertEqual(score, 100)
        self.assertEqual(issues, [])
        self.assertTrue(fake_fitz.document.closed)


class _FakeRenderer:
    def __init__(self) -> None:
        self.request: ResumeRenderRequest | None = None

    def render(self, request: ResumeRenderRequest) -> bool:
        self.request = request
        return True


class ResumeRenderingTests(unittest.TestCase):
    def test_legacy_render_helper_accepts_an_injected_renderer(self) -> None:
        renderer = _FakeRenderer()
        payload = {"header_name": "Candidate"}

        rendered = resume_generate.render_pdf(
            payload,
            Path("generated.pdf"),
            {"Product", "Analytics"},
            renderer=renderer,
        )

        self.assertTrue(rendered)
        assert renderer.request is not None
        self.assertIs(renderer.request.resume, payload)
        self.assertEqual(renderer.request.output_path, Path("generated.pdf"))
        self.assertEqual(renderer.request.bold_keywords, frozenset({"Product", "Analytics"}))


if __name__ == "__main__":
    unittest.main()
