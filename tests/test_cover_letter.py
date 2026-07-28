from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.resume.career_narrative import (  # noqa: E402
    CareerNarrative,
    load_career_narrative,
)
from job_application_automation.resume.cover_letter_claims import (  # noqa: E402
    known_claim_ids,
    validate_claim_ids,
)
from job_application_automation.resume.cover_letter_models import CoverLetterJob  # noqa: E402
import tempfile  # noqa: E402

from job_application_automation.resume.cover_letter_cache import (  # noqa: E402
    CoverLetterCache,
    cover_letter_cache_key,
)


class CareerNarrativeTests(unittest.TestCase):
    def test_missing_fields_are_omitted_not_inferred(self) -> None:
        narrative = load_career_narrative({})

        self.assertEqual(narrative.reason_for_change, "")
        self.assertEqual(narrative.next_role_priorities, ())
        self.assertEqual(narrative.tone, "")
        self.assertEqual(narrative.default_salutation, "Hiring Team")
        self.assertEqual(narrative.do_not_claim, ())

    def test_present_fields_are_parsed_and_trimmed(self) -> None:
        narrative = load_career_narrative(
            {
                "career_narrative": {
                    "reason_for_change": "  Candidate-approved wording only  ",
                    "next_role_priorities": ["AI product ownership", "  ", "customer impact"],
                    "tone": "direct",
                    "default_salutation": "Hiring Manager",
                    "do_not_claim": ["People-management experience"],
                }
            }
        )

        self.assertEqual(narrative.reason_for_change, "Candidate-approved wording only")
        self.assertEqual(
            narrative.next_role_priorities, ("AI product ownership", "customer impact")
        )
        self.assertEqual(narrative.default_salutation, "Hiring Manager")
        self.assertEqual(narrative.do_not_claim, ("People-management experience",))

    def test_malformed_narrative_block_is_ignored_like_a_missing_one(self) -> None:
        narrative = load_career_narrative({"career_narrative": "not an object"})

        self.assertEqual(narrative, CareerNarrative())


class CoverLetterJobTests(unittest.TestCase):
    def test_job_defaults_url_to_empty_string(self) -> None:
        job = CoverLetterJob(company="Example Co", role="Product Manager", jd_text="Build things.")

        self.assertEqual(job.url, "")


class CoverLetterClaimsTests(unittest.TestCase):
    def test_known_claim_ids_collects_every_tagged_claim(self) -> None:
        experience = [
            {"claims": [{"id": "AWS-1", "text": "..."}, {"id": "AWS-2", "text": "..."}]},
            {"claims": [{"id": "META-1", "text": "..."}]},
        ]

        self.assertEqual(known_claim_ids(experience), {"AWS-1", "AWS-2", "META-1"})

    def test_validate_claim_ids_returns_only_the_unknown_ones(self) -> None:
        known = {"AWS-1", "AWS-2"}

        invalid = validate_claim_ids(["AWS-1", "MADE-UP-9"], known)

        self.assertEqual(invalid, ["MADE-UP-9"])

    def test_validate_claim_ids_accepts_an_all_known_list(self) -> None:
        known = {"AWS-1"}

        self.assertEqual(validate_claim_ids(["AWS-1"], known), [])


class CoverLetterCacheTests(unittest.TestCase):
    def test_cache_key_changes_when_any_hash_input_changes(self) -> None:
        base = dict(
            job_identity="Example Co|Product Manager",
            jd_sha256="a" * 64,
            source_sha256="b" * 64,
            narrative_sha256="c" * 64,
            template_version="cover-letter-v1",
        )

        key = cover_letter_cache_key(**base)
        changed = dict(base, jd_sha256="d" * 64)

        self.assertEqual(len(key), 64)
        self.assertNotEqual(key, cover_letter_cache_key(**changed))

    def test_cache_round_trips_through_disk(self) -> None:
        cache = CoverLetterCache()
        cache.set("key-1", {"salutation": "Hiring Team"})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cover_letter_cache.json"
            cache.save(path)

            restored = CoverLetterCache()
            self.assertEqual(restored.load(path), 1)

        self.assertEqual(restored.get("key-1"), {"salutation": "Hiring Team"})
        restored.discard("key-1")
        self.assertIsNone(restored.get("key-1"))

    def test_cache_returns_isolated_copies(self) -> None:
        cache = CoverLetterCache()
        payload = {"paragraphs": ["one"]}
        cache.set("key-1", payload)
        payload["paragraphs"].append("mutated by caller")

        cached = cache.get("key-1")
        assert cached is not None
        cached["paragraphs"].append("mutated by reader")

        self.assertEqual(cache.get("key-1")["paragraphs"], ["one"])


from job_application_automation.resume.cover_letter_validation import (  # noqa: E402
    CoverLetterValidationPolicy,
    validate_cover_letter_pdf,
)


class _FakeCoverLetterPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self, mode: str) -> str:
        assert mode == "text"
        return self._text


class _FakeCoverLetterDocument:
    def __init__(self, pages: list[str]) -> None:
        self._pages = [_FakeCoverLetterPage(text) for text in pages]
        self.closed = False

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int) -> _FakeCoverLetterPage:
        return self._pages[index]

    def close(self) -> None:
        self.closed = True


class _FakeCoverLetterFitz:
    def __init__(self, pages: list[str]) -> None:
        self.document = _FakeCoverLetterDocument(pages)

    def open(self, path: str) -> _FakeCoverLetterDocument:
        assert path == "letter.pdf"
        return self.document


def _policy() -> CoverLetterValidationPolicy:
    return CoverLetterValidationPolicy(
        minimum_words=3, maximum_words=9, required_signature="Shivam Singh"
    )


class CoverLetterValidationTests(unittest.TestCase):
    def test_a_valid_one_page_letter_passes_and_closes_the_document(self) -> None:
        fake_fitz = _FakeCoverLetterFitz(
            ["Dear Team, I bring proven product results. Shivam Singh"]
        )

        valid, issues = validate_cover_letter_pdf("letter.pdf", _policy(), fitz_module=fake_fitz)

        self.assertTrue(valid)
        self.assertEqual(issues, [])
        self.assertTrue(fake_fitz.document.closed)

    def test_a_two_page_letter_fails_and_is_never_treated_as_passing(self) -> None:
        fake_fitz = _FakeCoverLetterFitz(["Page one text here Shivam Singh", "Page two overflow"])

        valid, issues = validate_cover_letter_pdf("letter.pdf", _policy(), fitz_module=fake_fitz)

        self.assertFalse(valid)
        self.assertIn("2 pages", issues[0])

    def test_missing_signature_and_out_of_budget_word_count_are_both_reported(self) -> None:
        fake_fitz = _FakeCoverLetterFitz(["One two three four five six seven eight nine ten"])

        valid, issues = validate_cover_letter_pdf("letter.pdf", _policy(), fitz_module=fake_fitz)

        self.assertFalse(valid)
        self.assertTrue(any("Too long" in issue for issue in issues))
        self.assertTrue(any("signature" in issue for issue in issues))

    def test_policy_rejects_an_inverted_word_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "maximum_words"):
            CoverLetterValidationPolicy(minimum_words=10, maximum_words=5, required_signature="X")


from job_application_automation.resume.cover_letter_rendering import (  # noqa: E402
    CoverLetterRenderRequest,
    render_cover_letter,
)


class _FakeCoverLetterRenderer:
    def __init__(self) -> None:
        self.request: CoverLetterRenderRequest | None = None

    def render(self, request: CoverLetterRenderRequest) -> bool:
        self.request = request
        return True


class CoverLetterRenderingTests(unittest.TestCase):
    def test_render_cover_letter_builds_a_request_and_delegates_to_the_renderer(self) -> None:
        renderer = _FakeCoverLetterRenderer()
        letter = {"salutation": "Dear Team,"}
        candidate = {"name": "Shivam Singh"}

        rendered = render_cover_letter(renderer, letter, candidate, Path("letter.pdf"))

        self.assertTrue(rendered)
        assert renderer.request is not None
        self.assertIs(renderer.request.letter, letter)
        self.assertIs(renderer.request.candidate, candidate)
        self.assertEqual(renderer.request.output_path, Path("letter.pdf"))


from job_application_automation.core.adapters import LLMSettings  # noqa: E402
from job_application_automation.resume.cover_letter_ai import (  # noqa: E402
    call_cover_letter_llm,
)


class _FakeCoverLetterGateway:
    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[tuple[str, str, LLMSettings, bool]] = []

    def generate(
        self,
        prompt: str,
        *,
        system: str,
        settings: LLMSettings,
        json_mode: bool = False,
    ) -> str:
        self.calls.append((prompt, system, settings, json_mode))
        return self._response


class CoverLetterAiTests(unittest.TestCase):
    def test_call_cover_letter_llm_parses_the_structured_json_payload(self) -> None:
        gateway = _FakeCoverLetterGateway(
            '{"salutation": "Dear Team,", "paragraphs": ["One.", "Two."], '
            '"closing": "Sincerely,", "signature": "Shivam Singh", '
            '"evidence_claim_ids": ["AWS-1"]}'
        )

        payload = call_cover_letter_llm(
            CoverLetterJob(company="Example Co", role="Product Manager", jd_text="Build things."),
            CareerNarrative(),
            source_text="[COMPANY] Example Co ... [CLAIM AWS-1] Shipped a thing.",
            gateway=gateway,
        )

        self.assertEqual(payload["evidence_claim_ids"], ["AWS-1"])
        self.assertTrue(gateway.calls[0][3])  # json_mode

    def test_call_cover_letter_llm_strips_a_markdown_json_fence(self) -> None:
        gateway = _FakeCoverLetterGateway('```json\n{"salutation": "Dear Team,"}\n```')

        payload = call_cover_letter_llm(
            CoverLetterJob(company="Example Co", role="Product Manager", jd_text="Build things."),
            CareerNarrative(),
            source_text="source",
            gateway=gateway,
        )

        self.assertEqual(payload["salutation"], "Dear Team,")

    def test_call_cover_letter_llm_rejects_a_non_object_json_root(self) -> None:
        gateway = _FakeCoverLetterGateway("[1, 2, 3]")

        with self.assertRaisesRegex(ValueError, "JSON object"):
            call_cover_letter_llm(
                CoverLetterJob(company="Example Co", role="PM", jd_text="Build things."),
                CareerNarrative(),
                source_text="source",
                gateway=gateway,
            )


import json as json_module  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from job_application_automation.resume import cover_letter  # noqa: E402
from job_application_automation.resume.source import ResumeSource  # noqa: E402


def _fake_source() -> ResumeSource:
    return ResumeSource(
        text="[COMPANY] Example Co\n[CLAIM AWS-1] Shipped a measurable product.",
        experience=(
            {
                "company": "Example Co",
                "location": "Remote",
                "title": "PM",
                "dates": "2020 - Present",
                "tags": [],
                "claims": [{"id": "AWS-1", "text": "Shipped a measurable product."}],
                "bullets": ["Shipped a measurable product."],
            },
        ),
        education=({"school": "State U", "degree": "BA", "dates": "2016-2020", "details": ""},),
        candidate={
            "name": "Shivam Singh",
            "location": "SF",
            "email": "shiv@example.test",
            "phone": "555",
            "linkedin": "https://example.test/in/shiv",
        },
    )


class _RecordingGateway:
    def __init__(self, response: str) -> None:
        self._response = response
        self.call_count = 0

    def generate(self, prompt, *, system, settings, json_mode=False):
        self.call_count += 1
        return self._response


class _RecordingRenderer:
    def __init__(self, page_texts: list[str]) -> None:
        self._page_texts = page_texts
        self.render_count = 0
        self.last_candidate = None

    def render(self, request) -> bool:
        self.render_count += 1
        self.last_candidate = dict(request.candidate)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text("stub-pdf", encoding="utf-8")
        return True


class _FakeGenPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self, mode: str) -> str:
        return self._text


class _FakeGenDocument:
    def __init__(self, pages: list[str]) -> None:
        self._pages = [_FakeGenPage(text) for text in pages]

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int):
        return self._pages[index]

    def close(self) -> None:
        pass


class _FakeGenFitz:
    def __init__(self, pages_by_call: list[list[str]]) -> None:
        self._pages_by_call = list(pages_by_call)

    def open(self, path: str) -> _FakeGenDocument:
        return _FakeGenDocument(self._pages_by_call.pop(0))


_VALID_RESPONSE = json_module.dumps(
    {
        "salutation": "Dear Hiring Team,",
        "paragraphs": [
            "I bring proven product ownership to this role.",
            "At Example Co I shipped measurable outcomes for customers.",
            "I would welcome the chance to bring that same rigor to your team.",
        ],
        "closing": "Sincerely,",
        "signature": "Shivam Singh",
        "evidence_claim_ids": ["AWS-1"],
    }
)


class GenerateCoverLetterTests(unittest.TestCase):
    def test_generates_writes_pdf_and_audit_sidecar_on_a_valid_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "Example_Co_PM_Cover_Letter.pdf"
            renderer = _RecordingRenderer([])
            fitz_module = _FakeGenFitz([[" ".join(["word"] * 30) + " Shivam Singh"]])

            result = cover_letter.generate_cover_letter(
                CoverLetterJob(company="Example Co", role="PM", jd_text="Build things."),
                CareerNarrative(),
                _fake_source(),
                output_path,
                gateway=_RecordingGateway(_VALID_RESPONSE),
                renderer=renderer,
                fitz_module=fitz_module,
                cache=CoverLetterCache(),
                clock=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
                max_retries=1,
                minimum_words=5,
                maximum_words=100,
            )

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            audit_path = output_path.with_name(output_path.stem + ".audit.json")
            audit = json_module.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["evidence_claim_ids"], ["AWS-1"])
            self.assertEqual(audit["prompt_template_version"], "cover-letter-v1")
            self.assertEqual(audit["generated_at"], "2026-07-28T00:00:00+00:00")

    def test_email_override_is_rendered_without_mutating_the_source(self) -> None:
        source = _fake_source()
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "letter.pdf"
            renderer = _RecordingRenderer([])
            result = cover_letter.generate_cover_letter(
                CoverLetterJob(company="Example Co", role="PM", jd_text="Build things."),
                CareerNarrative(),
                source,
                output_path,
                email_override="Application.Email@Example.com",
                gateway=_RecordingGateway(_VALID_RESPONSE),
                renderer=renderer,
                fitz_module=_FakeGenFitz([[" ".join(["word"] * 30) + " Shivam Singh"]]),
                max_retries=1,
                minimum_words=5,
                maximum_words=100,
            )

        self.assertEqual(result, output_path)
        self.assertEqual(renderer.last_candidate["email"], "application.email@example.com")
        self.assertEqual(source.candidate["email"], "shiv@example.test")

    def test_an_unmatched_claim_id_is_rejected_and_never_rendered(self) -> None:
        bad_response = json_module.dumps(
            {
                "salutation": "Dear Team,",
                "paragraphs": ["A paragraph.", "Another paragraph."],
                "closing": "Sincerely,",
                "signature": "Shivam Singh",
                "evidence_claim_ids": ["MADE-UP-9"],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "letter.pdf"
            renderer = _RecordingRenderer([])

            result = cover_letter.generate_cover_letter(
                CoverLetterJob(company="Example Co", role="PM", jd_text="Build things."),
                CareerNarrative(),
                _fake_source(),
                output_path,
                gateway=_RecordingGateway(bad_response),
                renderer=renderer,
                fitz_module=_FakeGenFitz([]),
                cache=CoverLetterCache(),
                max_retries=1,
                minimum_words=5,
                maximum_words=100,
            )

            self.assertIsNone(result)
            self.assertFalse(output_path.exists())
            self.assertEqual(renderer.render_count, 0)

    def test_a_two_page_render_is_never_promoted_to_the_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "letter.pdf"
            renderer = _RecordingRenderer([])
            fitz_module = _FakeGenFitz([["Page one Shivam Singh", "Page two overflow"]])

            result = cover_letter.generate_cover_letter(
                CoverLetterJob(company="Example Co", role="PM", jd_text="Build things."),
                CareerNarrative(),
                _fake_source(),
                output_path,
                gateway=_RecordingGateway(_VALID_RESPONSE),
                renderer=renderer,
                fitz_module=fitz_module,
                cache=CoverLetterCache(),
                max_retries=1,
                minimum_words=5,
                maximum_words=100,
            )

            self.assertIsNone(result)
            self.assertFalse(output_path.exists())

    def test_missing_jd_text_raises_jd_context_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "letter.pdf"
            with self.assertRaises(cover_letter.JDContextUnavailable):
                cover_letter.generate_cover_letter(
                    CoverLetterJob(company="Example Co", role="PM", jd_text="   "),
                    CareerNarrative(),
                    _fake_source(),
                    output_path,
                    gateway=_RecordingGateway(_VALID_RESPONSE),
                    renderer=_RecordingRenderer([]),
                    fitz_module=_FakeGenFitz([]),
                )

    def test_a_cache_hit_skips_the_llm_call(self) -> None:
        cache = CoverLetterCache()
        job = CoverLetterJob(company="Example Co", role="PM", jd_text="Build things.")
        source = _fake_source()
        cache_key = cover_letter.cache_key_for(job, CareerNarrative(), source)
        cache.set(
            cache_key,
            {
                "salutation": "Dear Team,",
                "paragraphs": ["A paragraph.", "Another paragraph."],
                "closing": "Sincerely,",
                "signature": "Shivam Singh",
                "evidence_claim_ids": ["AWS-1"],
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "letter.pdf"
            gateway = _RecordingGateway(_VALID_RESPONSE)
            fitz_module = _FakeGenFitz([[" ".join(["word"] * 30) + " Shivam Singh"]])

            result = cover_letter.generate_cover_letter(
                job,
                CareerNarrative(),
                source,
                output_path,
                gateway=gateway,
                renderer=_RecordingRenderer([]),
                fitz_module=fitz_module,
                cache=cache,
                max_retries=1,
                minimum_words=5,
                maximum_words=100,
            )

            self.assertEqual(result, output_path)
            self.assertEqual(gateway.call_count, 0)


if __name__ == "__main__":
    unittest.main()
