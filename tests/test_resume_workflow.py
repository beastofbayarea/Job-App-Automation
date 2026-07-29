from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.resume import generate as resume_generate  # noqa: E402
from job_application_automation.resume.cache import ResumeCache, cache_key  # noqa: E402
from job_application_automation.resume.rendering import ResumeRenderRequest  # noqa: E402
from job_application_automation.resume.scoring import (  # noqa: E402
    ResumeScorePolicy,
    score_pdf,
)
from job_application_automation.resume.validation import (  # noqa: E402
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

    def test_load_counts_only_the_entries_it_actually_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm_cache_v2.json"
            path.write_text(
                json.dumps({"good": {"header_name": "Candidate"}, "bad": "not-an-object"}),
                encoding="utf-8",
            )
            cache = ResumeCache()

            merged = cache.load(path)

        self.assertEqual(merged, 1)
        self.assertEqual(set(cache.entries), {"good"})


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


def _job_info(**overrides: object) -> resume_generate.JobInfo:
    defaults: dict[str, object] = dict(
        company="Example Co",
        role_title="Product Manager",
        keywords="Product Strategy, Roadmapping",
        jd_overview="",
        jd_responsibilities="",
        jd_requirements="",
    )
    defaults.update(overrides)
    return resume_generate.JobInfo(**defaults)  # type: ignore[arg-type]


class KeywordAndBoldingTests(unittest.TestCase):
    def test_build_keyword_set_collects_explicit_quoted_and_acronym_terms(self) -> None:
        job = _job_info(
            jd_overview='We need "Customer Discovery" skills and API experience.',
        )

        keywords = resume_generate._build_keyword_set(job)

        self.assertIn("Product Strategy", keywords)
        self.assertIn("Roadmapping", keywords)
        self.assertIn("Customer Discovery", keywords)
        self.assertIn("API", keywords)

    def test_bold_keywords_in_text_bolds_markdown_keywords_and_metrics_without_double_wrapping(
        self,
    ) -> None:
        text = "Grew **revenue** using Analytics by 42% and $1.2M in savings."

        result = resume_generate._bold_keywords_in_text(text, {"Analytics"})

        self.assertIn("<b>revenue</b>", result)
        self.assertIn("<b>Analytics</b>", result)
        self.assertIn("<b>42%</b>", result)
        self.assertIn("<b>$1.2M</b>", result)
        self.assertNotIn("<b><b>", result)

    def test_bold_keywords_in_text_returns_empty_string_for_empty_input(self) -> None:
        self.assertEqual(resume_generate._bold_keywords_in_text("", {"Analytics"}), "")


def _identity(data: object, *_args: object, **_kwargs: object) -> object:
    return data


@contextlib.contextmanager
def _patched_generation_pipeline(
    *,
    call_llm,
    render_pdf,
    score_pdf,
    set_cached=None,
    validate_llm_data=None,
    max_retries: int = 3,
    min_score: int = 90,
):
    """Isolate the retry-loop control flow from the real LLM/render/score/source steps."""
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(resume_generate, "_ensure_resume_source", return_value=None))
        enter(patch.object(resume_generate, "_load_disk_cache", return_value=None))
        enter(patch.object(resume_generate, "_save_disk_cache", return_value=None))
        enter(
            patch.object(
                resume_generate, "_set_cached", side_effect=set_cached or (lambda *a, **k: None)
            )
        )
        enter(patch.object(resume_generate, "_call_llm", side_effect=call_llm))
        enter(patch.object(resume_generate, "_enforce_candidate_identity", side_effect=_identity))
        enter(patch.object(resume_generate, "_normalize_experience", side_effect=_identity))
        enter(patch.object(resume_generate, "_repair_experience", side_effect=_identity))
        enter(patch.object(resume_generate, "_enforce_source_invariants", side_effect=_identity))
        enter(patch.object(resume_generate, "_repair_education", side_effect=_identity))
        enter(patch.object(resume_generate, "_ensure_min_bullets", side_effect=_identity))
        enter(
            patch.object(
                resume_generate,
                "_validate_llm_data",
                side_effect=validate_llm_data or (lambda data: []),
            )
        )
        enter(patch.object(resume_generate, "render_pdf", side_effect=render_pdf))
        enter(patch.object(resume_generate, "_score_pdf", side_effect=score_pdf))
        enter(patch.object(resume_generate, "_build_feedback", return_value="feedback"))
        enter(patch.object(resume_generate, "LLM_MIN_INTERVAL", 0.0))
        enter(patch.object(resume_generate, "MAX_RETRIES", max_retries))
        enter(patch.object(resume_generate, "MIN_SCORE", min_score))
        yield


def _writing_render(resume_data: Mapping[str, object], path: Path, keywords: object) -> bool:
    Path(path).write_text(str(resume_data.get("header_name", "")), encoding="utf-8")
    return True


class GenerateWithRetriesTests(unittest.TestCase):
    def test_succeeds_on_first_attempt_and_populates_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()
            set_cached_calls: list[tuple[object, object]] = []

            with _patched_generation_pipeline(
                call_llm=lambda job, feedback: {"header_name": "Candidate"},
                render_pdf=_writing_render,
                score_pdf=lambda path: (95, []),
                set_cached=lambda job, data: set_cached_calls.append((job, data)),
            ):
                result = resume_generate._generate_with_retries(job, output_path)

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            self.assertEqual(len(set_cached_calls), 1)

    def test_retries_after_a_render_failure_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()
            render_calls: list[Path] = []

            def flaky_render(resume_data, path, keywords):
                render_calls.append(Path(path))
                if len(render_calls) == 1:
                    return False
                return _writing_render(resume_data, path, keywords)

            with _patched_generation_pipeline(
                call_llm=lambda job, feedback: {"header_name": "Candidate"},
                render_pdf=flaky_render,
                score_pdf=lambda path: (95, []),
            ):
                result = resume_generate._generate_with_retries(job, output_path)

            self.assertEqual(result, output_path)
            self.assertEqual(len(render_calls), 2)

    def test_treats_an_llm_exception_as_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()
            attempts = {"count": 0}

            def flaky_call_llm(job, feedback):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("gateway timeout")
                return {"header_name": "Candidate"}

            with _patched_generation_pipeline(
                call_llm=flaky_call_llm,
                render_pdf=_writing_render,
                score_pdf=lambda path: (95, []),
            ):
                result = resume_generate._generate_with_retries(job, output_path)

            self.assertEqual(result, output_path)
            self.assertEqual(attempts["count"], 2)

    def test_treats_a_critical_validation_issue_as_retryable_before_any_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()
            issues_by_attempt = iter([["Missing required key: header_name"], []])
            render_calls: list[Path] = []

            def counting_render(resume_data, path, keywords):
                render_calls.append(Path(path))
                return _writing_render(resume_data, path, keywords)

            with _patched_generation_pipeline(
                call_llm=lambda job, feedback: {"header_name": "Candidate"},
                render_pdf=counting_render,
                score_pdf=lambda path: (95, []),
                validate_llm_data=lambda data: next(issues_by_attempt),
            ):
                result = resume_generate._generate_with_retries(job, output_path)

            self.assertEqual(result, output_path)
            self.assertEqual(len(render_calls), 1)

    def test_falls_back_to_the_highest_scoring_attempt_after_exhausting_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()
            scores = iter([40, 70, 55])
            llm_calls: list[str] = []

            def counting_call_llm(job, feedback):
                llm_calls.append(feedback)
                return {"header_name": f"Candidate-{len(llm_calls)}"}

            with _patched_generation_pipeline(
                call_llm=counting_call_llm,
                render_pdf=_writing_render,
                score_pdf=lambda path: (next(scores), ["Needs more metrics."]),
                max_retries=3,
                min_score=90,
            ):
                result = resume_generate._generate_with_retries(job, output_path)

            self.assertEqual(result, output_path)
            self.assertEqual(len(llm_calls), 3)
            # The second attempt scored highest (70), so its data is what gets
            # promoted to output_path once every attempt has been exhausted.
            self.assertEqual(output_path.read_text(encoding="utf-8"), "Candidate-2")

    def test_falls_back_to_rule_based_data_when_the_llm_never_returns_usable_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()

            with _patched_generation_pipeline(
                call_llm=lambda job, feedback: None,
                render_pdf=_writing_render,
                score_pdf=lambda path: (100, []),
                max_retries=2,
            ):
                result = resume_generate._generate_with_retries(job, output_path)

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())

    def test_returns_none_when_the_llm_fails_and_the_fallback_render_also_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()

            with _patched_generation_pipeline(
                call_llm=lambda job, feedback: None,
                render_pdf=lambda data, path, keywords: False,
                score_pdf=lambda path: (100, []),
                max_retries=2,
            ):
                result = resume_generate._generate_with_retries(job, output_path)

            self.assertIsNone(result)
            self.assertFalse(output_path.exists())


class GeneratePersonalizedResumeCacheTests(unittest.TestCase):
    def test_returns_the_cached_pdf_immediately_when_it_passes_the_score_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(patch.object(resume_generate, "_ensure_resume_source", return_value=None))
                enter(patch.object(resume_generate, "_load_disk_cache", return_value=None))
                enter(
                    patch.object(
                        resume_generate, "_get_cached", return_value={"header_name": "Cached"}
                    )
                )
                enter(
                    patch.object(
                        resume_generate, "_enforce_candidate_identity", side_effect=_identity
                    )
                )
                enter(patch.object(resume_generate, "render_pdf", side_effect=_writing_render))
                enter(patch.object(resume_generate, "_score_pdf", return_value=(96, [])))
                retries_mock = enter(patch.object(resume_generate, "_generate_with_retries"))

                result = resume_generate.generate_personalized_resume(job, output_path)

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            retries_mock.assert_not_called()

    def test_discards_a_low_scoring_cache_hit_and_falls_back_to_fresh_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(patch.object(resume_generate, "_ensure_resume_source", return_value=None))
                enter(patch.object(resume_generate, "_load_disk_cache", return_value=None))
                enter(
                    patch.object(
                        resume_generate, "_get_cached", return_value={"header_name": "Cached"}
                    )
                )
                enter(
                    patch.object(
                        resume_generate, "_enforce_candidate_identity", side_effect=_identity
                    )
                )
                enter(patch.object(resume_generate, "render_pdf", side_effect=_writing_render))
                enter(patch.object(resume_generate, "_score_pdf", return_value=(10, ["Thin"])))
                discard_mock = enter(patch.object(resume_generate._resume_cache, "discard"))
                retries_mock = enter(
                    patch.object(
                        resume_generate, "_generate_with_retries", return_value=output_path
                    )
                )

                result = resume_generate.generate_personalized_resume(job, output_path)

            self.assertEqual(result, output_path)
            discard_mock.assert_called_once_with(job)
            retries_mock.assert_called_once_with(job, output_path, "")

    def test_delegates_straight_to_the_retry_loop_when_nothing_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            job = _job_info()

            with contextlib.ExitStack() as stack:
                enter = stack.enter_context
                enter(patch.object(resume_generate, "_ensure_resume_source", return_value=None))
                enter(patch.object(resume_generate, "_load_disk_cache", return_value=None))
                enter(patch.object(resume_generate, "_get_cached", return_value=None))
                retries_mock = enter(
                    patch.object(
                        resume_generate, "_generate_with_retries", return_value=output_path
                    )
                )

                result = resume_generate.generate_personalized_resume(
                    job, output_path, "override@example.test"
                )

            self.assertEqual(result, output_path)
            retries_mock.assert_called_once_with(job, output_path, "override@example.test")


class ResumeGenerateCliTests(unittest.TestCase):
    def test_main_rejects_a_malformed_email_before_any_generation_work(self) -> None:
        with patch.object(resume_generate, "generate_personalized_resume") as mock_generate:
            errors = StringIO()
            with contextlib.redirect_stderr(errors):
                exit_code = resume_generate.main(
                    ["--company", "Example Co", "--role", "PM", "--email", "not-an-email"]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("Invalid --email", errors.getvalue())
        mock_generate.assert_not_called()

    def test_main_reports_success_and_the_final_score_on_a_successful_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_Resume.pdf"
            with (
                patch.object(
                    resume_generate, "generate_personalized_resume", return_value=output_path
                ) as mock_generate,
                patch.object(resume_generate, "_score_pdf", return_value=(91, [])),
            ):
                out = StringIO()
                with contextlib.redirect_stdout(out):
                    exit_code = resume_generate.main(
                        ["--company", "Example Co", "--role", "PM", "--output", str(output_path)]
                    )

            self.assertEqual(exit_code, 0)
            self.assertIn("SUCCESS", out.getvalue())
            self.assertIn("Final score: 91/100", out.getvalue())
            mock_generate.assert_called_once()

    def test_main_returns_a_failure_exit_code_when_generation_fails(self) -> None:
        with patch.object(resume_generate, "generate_personalized_resume", return_value=None):
            out = StringIO()
            with contextlib.redirect_stdout(out):
                exit_code = resume_generate.main(["--company", "Example Co", "--role", "PM"])

        self.assertEqual(exit_code, 1)
        self.assertIn("FAILED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
