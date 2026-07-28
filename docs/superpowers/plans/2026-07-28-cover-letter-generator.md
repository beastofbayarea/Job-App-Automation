# Cover Letter Generator (PRD F2) Implementation Plan

> **Historical implementation record (completed 2026-07-28).** File paths,
> intermediate code samples, and commands below describe the implementation
> sequence at that time. Use the root README and `docs/cli-reference.md` for the
> current interface.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone `cover-letter` CLI command that generates a one-page,
fact-grounded, claim-evidenced cover letter PDF from the existing tagged resume
source and an LLM, with a strict page/word validator and an audit sidecar.

**Architecture:** Mirror the existing `resume/` package split (`generate.py` +
`ai_client.py` + `rendering.py` + `scoring.py` + `cache.py`). Add seven small,
single-responsibility modules under `resume/` plus one orchestration/CLI module
(`cover_letter.py`), all built on the project's existing injectable ports
(`LLMClient`, an injectable PDF renderer, an injectable PyMuPDF module,
`core/artifacts.py` atomic JSON writes) so every unit is testable without a
network call, browser, or real PDF library.

**Tech Stack:** Python 3.10+, ReportLab (rendering), PyMuPDF/`fitz` (validation,
injected), the existing `LLMClient`/`ask_gemini` gateway, `core/artifacts.py`
atomic writes, stdlib `argparse`/`hashlib`/`unittest`.

## Global Constraints

- No live LLM, PDF-library, or network calls in tests — inject fakes for the
  LLM gateway, renderer, and `fitz` module (matches existing `resume/` tests).
- Never invent career facts: every `evidence_claim_ids` entry the LLM returns
  must match a `[CLAIM <id>]` tag already present in the tagged resume source
  (`resume/source.py`'s `ResumeSource.experience[*]["claims"]`); unmatched IDs
  fail that generation attempt and are never rendered.
- A two-page (or otherwise invalid) PDF must never be promoted to the final
  output path — generation fails clearly instead of shipping a "best effort."
- Missing JD context raises a clear `JDContextUnavailable` error; missing
  narrative fields are omitted from the prompt, never guessed.
- Persistence goes through `core/artifacts.write_json` / the existing atomic
  `os.replace` PDF-promotion pattern already used by `resume/generate.py`.
- **Scope note:** the PRD's shared `job_identity.py` / `job_context.py`
  modules do not exist yet (confirmed absent from the codebase) and are out of
  scope here. This plan reuses the same "caller supplies JD text or a
  `--url` for a bounded Ashby scrape fallback" pattern `resume/generate.py`
  already uses, and uses a simple `company|role` (or `--url` if given) string
  as the job-identity cache input instead of the not-yet-built canonical
  identity module.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/job_application_automation/resume/career_narrative.py` | Parse the optional `career_narrative` config block; omit missing fields. |
| `src/job_application_automation/resume/cover_letter_models.py` | `CoverLetterJob` input dataclass. |
| `src/job_application_automation/resume/cover_letter_claims.py` | Validate LLM-returned claim IDs against the tagged resume source. |
| `src/job_application_automation/resume/cover_letter_cache.py` | Hash-keyed cache (job identity + JD hash + source hash + narrative hash + template version). |
| `src/job_application_automation/resume/cover_letter_validation.py` | Injectable PyMuPDF one-page/word-budget/signature validator. |
| `src/job_application_automation/resume/cover_letter_rendering.py` | Injectable ReportLab one-page letter renderer. |
| `src/job_application_automation/resume/cover_letter_ai.py` | System prompt + structured LLM call (reuses `resume/ai_client.ask_gemini`). |
| `src/job_application_automation/resume/cover_letter.py` | Orchestration pipeline (`generate_cover_letter`) + CLI `main()`. |
| `src/job_application_automation/core/runtime_config.py` | Modify: add validated `cover_letter` config section. |
| `config/runtime_config.json` | Modify: add `cover_letter` section values. |
| `config/candidate_profile_config.example.json` | Modify: document the optional `career_narrative` block. |
| `src/job_application_automation/cli.py` | Modify: register the `cover-letter` command. |
| `tests/test_cover_letter.py` | New: covers every module above with fakes. |
| `tests/test_runtime_config.py` | Modify: assert the new section loads. |
| `tests/test_cli_dispatch.py` | Modify: assert `cover-letter` dispatches. |

---

### Task 1: Runtime config — `cover_letter` section

**Files:**
- Modify: `config/runtime_config.json`
- Modify: `src/job_application_automation/core/runtime_config.py`
- Modify: `tests/test_runtime_config.py`

**Interfaces:**
- Produces: `RuntimeConfig.cover_letter` mapping with keys `cache_file` (str),
  `max_retries` (int), `minimum_words` (int), `maximum_words` (int).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_runtime_config.py`, inside `RuntimeConfigTests`:

```python
    def test_cover_letter_section_loads_with_valid_word_budget(self) -> None:
        config = load_runtime_config()

        self.assertGreater(config.cover_letter["max_retries"], 0)
        self.assertGreater(
            config.cover_letter["maximum_words"], config.cover_letter["minimum_words"]
        )

    def test_cover_letter_word_budget_must_be_ordered(self) -> None:
        document = json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8"))
        document["cover_letter"]["maximum_words"] = document["cover_letter"]["minimum_words"]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "maximum_words"):
                load_runtime_config(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runtime_config.py -v`
Expected: FAIL — `KeyError: 'cover_letter'` (no such section yet).

- [ ] **Step 3: Add the config section**

In `config/runtime_config.json`, add after the `"resume"` block (before `"ashby"`):

```json
  "cover_letter": {
    "cache_file": "output/cover_letter_cache.json",
    "max_retries": 3,
    "minimum_words": 120,
    "maximum_words": 380
  },
```

In `src/job_application_automation/core/runtime_config.py`:

```python
@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Validated runtime configuration with immutable top-level sections."""

    application: Mapping[str, Any]
    browser: Mapping[str, Any]
    vertex: Mapping[str, Any]
    resume: Mapping[str, Any]
    cover_letter: Mapping[str, Any]
    ashby: Mapping[str, Any]
    gmail: Mapping[str, Any]
```

In `load_runtime_config`, after the existing `resume = _mapping(...)` block:

```python
    cover_letter = _mapping(document, "cover_letter")
```

After the existing resume validation block (`_boolean(resume, "resume", "persistent_cache_enabled")`):

```python
    _string(cover_letter, "cover_letter", "cache_file")
    for key in ("max_retries", "minimum_words", "maximum_words"):
        _integer(cover_letter, "cover_letter", key)
    if cover_letter["maximum_words"] <= cover_letter["minimum_words"]:
        raise ValueError(
            "runtime config cover_letter.maximum_words must be greater than minimum_words"
        )
```

And add `cover_letter=cover_letter,` to the final `return RuntimeConfig(...)` call
(between `resume=resume,` and `ashby=ashby,`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_runtime_config.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add config/runtime_config.json src/job_application_automation/core/runtime_config.py tests/test_runtime_config.py
git commit -m "feat(config): add cover_letter runtime settings section"
```

---

### Task 2: `career_narrative.py` — optional candidate narrative

**Files:**
- Create: `src/job_application_automation/resume/career_narrative.py`
- Modify: `config/candidate_profile_config.example.json`
- Create: `tests/test_cover_letter.py`

**Interfaces:**
- Produces: `CareerNarrative` frozen dataclass with fields
  `reason_for_change: str = ""`, `next_role_priorities: tuple[str, ...] = ()`,
  `tone: str = ""`, `default_salutation: str = "Hiring Team"`,
  `do_not_claim: tuple[str, ...] = ()`; and
  `load_career_narrative(profile: Mapping[str, Any]) -> CareerNarrative`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cover_letter.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'job_application_automation.resume.career_narrative'`.

- [ ] **Step 3: Write the implementation**

Create `src/job_application_automation/resume/career_narrative.py`:

```python
"""Optional, candidate-approved narrative used only by cover-letter generation.

Missing fields are omitted rather than inferred: this module never guesses a
reason for leaving, a priority, or a tone from job or resume content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CareerNarrative:
    """Candidate-approved wording for cover-letter tone and framing."""

    reason_for_change: str = ""
    next_role_priorities: tuple[str, ...] = ()
    tone: str = ""
    default_salutation: str = "Hiring Team"
    do_not_claim: tuple[str, ...] = ()


def _trimmed_string(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _trimmed_strings(raw: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    value = raw.get(key)
    if not isinstance(value, list):
        return None
    items = tuple(str(item).strip() for item in value if str(item).strip())
    return items or None


def load_career_narrative(profile: Mapping[str, Any]) -> CareerNarrative:
    """Read the optional ``career_narrative`` block, omitting absent fields."""
    raw = profile.get("career_narrative")
    if not isinstance(raw, Mapping):
        raw = {}

    kwargs: dict[str, Any] = {}
    reason = _trimmed_string(raw, "reason_for_change")
    if reason is not None:
        kwargs["reason_for_change"] = reason
    priorities = _trimmed_strings(raw, "next_role_priorities")
    if priorities is not None:
        kwargs["next_role_priorities"] = priorities
    tone = _trimmed_string(raw, "tone")
    if tone is not None:
        kwargs["tone"] = tone
    salutation = _trimmed_string(raw, "default_salutation")
    if salutation is not None:
        kwargs["default_salutation"] = salutation
    excluded = _trimmed_strings(raw, "do_not_claim")
    if excluded is not None:
        kwargs["do_not_claim"] = excluded

    return CareerNarrative(**kwargs)
```

Add to `config/candidate_profile_config.example.json` (top level, alongside the
existing `"candidate"` key) so the option is discoverable:

```json
  "career_narrative": {
    "reason_for_change": "Candidate-approved wording only",
    "next_role_priorities": ["AI product ownership", "customer impact"],
    "tone": "direct",
    "default_salutation": "Hiring Team",
    "do_not_claim": ["People-management experience"]
  },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/resume/career_narrative.py config/candidate_profile_config.example.json tests/test_cover_letter.py
git commit -m "feat(resume): add optional candidate career-narrative loader"
```

---

### Task 3: `cover_letter_models.py` + `cover_letter_claims.py`

**Files:**
- Create: `src/job_application_automation/resume/cover_letter_models.py`
- Create: `src/job_application_automation/resume/cover_letter_claims.py`
- Modify: `tests/test_cover_letter.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces: `CoverLetterJob(company: str, role: str, jd_text: str, url: str = "")`;
  `known_claim_ids(experience: Sequence[Mapping[str, Any]]) -> set[str]`;
  `validate_claim_ids(evidence_claim_ids: Iterable[str], known_ids: set[str]) -> list[str]`
  (returns the *invalid* IDs).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cover_letter.py`:

```python
from job_application_automation.resume.cover_letter_claims import (  # noqa: E402
    known_claim_ids,
    validate_claim_ids,
)
from job_application_automation.resume.cover_letter_models import CoverLetterJob  # noqa: E402


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: FAIL — `ModuleNotFoundError` for `cover_letter_claims`/`cover_letter_models`.

- [ ] **Step 3: Write the implementation**

Create `src/job_application_automation/resume/cover_letter_models.py`:

```python
"""Pure input value objects for cover-letter generation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CoverLetterJob:
    """The target role a cover letter is generated for."""

    company: str
    role: str
    jd_text: str
    url: str = ""
```

Create `src/job_application_automation/resume/cover_letter_claims.py`:

```python
"""Claim-ID evidence checks against the tagged resume source.

An LLM-returned ``evidence_claim_ids`` entry is trustworthy only if it names a
``[CLAIM <id>]`` already present in the candidate's tagged source material.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


def known_claim_ids(experience: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return every tagged claim ID available across tagged experience entries."""
    ids: set[str] = set()
    for entry in experience:
        for claim in entry.get("claims", []) or []:
            claim_id = str(claim.get("id", "")).strip()
            if claim_id:
                ids.add(claim_id)
    return ids


def validate_claim_ids(evidence_claim_ids: Iterable[str], known_ids: set[str]) -> list[str]:
    """Return the subset of ``evidence_claim_ids`` absent from ``known_ids``."""
    return [str(claim_id) for claim_id in evidence_claim_ids if str(claim_id) not in known_ids]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/resume/cover_letter_models.py src/job_application_automation/resume/cover_letter_claims.py tests/test_cover_letter.py
git commit -m "feat(resume): add cover-letter job model and claim-ID validation"
```

---

### Task 4: `cover_letter_cache.py`

**Files:**
- Create: `src/job_application_automation/resume/cover_letter_cache.py`
- Modify: `tests/test_cover_letter.py`

**Interfaces:**
- Consumes: `core.artifacts.read_json`, `core.artifacts.write_json`.
- Produces: `cover_letter_cache_key(*, job_identity: str, jd_sha256: str,
  source_sha256: str, narrative_sha256: str, template_version: str) -> str`;
  `CoverLetterCache` with `get(key) -> dict | None`, `set(key, data) -> None`,
  `discard(key) -> None`, `load(path) -> int`, `save(path) -> None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cover_letter.py`:

```python
import tempfile  # noqa: E402

from job_application_automation.resume.cover_letter_cache import (  # noqa: E402
    CoverLetterCache,
    cover_letter_cache_key,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named '...cover_letter_cache'`.

- [ ] **Step 3: Write the implementation**

Create `src/job_application_automation/resume/cover_letter_cache.py`:

```python
"""Thread-safe cache for generated cover letters.

Keyed by job identity, JD hash, source hash, narrative hash, and prompt
template version so a cache hit is only reused when every input that could
change the letter's content is unchanged (per PRD F2 caching requirement).
"""

from __future__ import annotations

import copy
import hashlib
import threading
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from ..core.artifacts import read_json, write_json


def cover_letter_cache_key(
    *,
    job_identity: str,
    jd_sha256: str,
    source_sha256: str,
    narrative_sha256: str,
    template_version: str,
) -> str:
    """Return a deterministic key covering every input that affects the letter."""
    context = "\n".join(
        (job_identity, jd_sha256, source_sha256, narrative_sha256, template_version)
    )
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


class CoverLetterCache:
    """A thread-safe, JSON-persistable cache keyed by an explicit string key."""

    def __init__(
        self,
        entries: MutableMapping[str, dict[str, Any]] | None = None,
        *,
        lock: threading.Lock | None = None,
    ) -> None:
        self._entries: MutableMapping[str, dict[str, Any]] = entries if entries is not None else {}
        self._lock = lock or threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._entries.get(key)
            return copy.deepcopy(data) if isinstance(data, dict) else None

    def set(self, key: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            self._entries[key] = copy.deepcopy(dict(data))

    def discard(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def load(self, path: Path) -> int:
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise ValueError("Cache root must be an object")
        valid = {
            str(key): value
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        with self._lock:
            self._entries.update(copy.deepcopy(valid))
        return len(payload)

    def save(self, path: Path) -> None:
        with self._lock:
            snapshot = copy.deepcopy(dict(self._entries))
        write_json(path, snapshot, indent=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/resume/cover_letter_cache.py tests/test_cover_letter.py
git commit -m "feat(resume): add hash-keyed cover-letter cache"
```

---

### Task 5: `cover_letter_validation.py` — one-page/word-budget PDF validator

**Files:**
- Create: `src/job_application_automation/resume/cover_letter_validation.py`
- Modify: `tests/test_cover_letter.py`

**Interfaces:**
- Produces: `CoverLetterValidationPolicy(minimum_words: int, maximum_words: int,
  required_signature: str)`; `validate_cover_letter_pdf(pdf_path, policy, *,
  fitz_module=None) -> tuple[bool, list[str]]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cover_letter.py`:

```python
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
        minimum_words=3, maximum_words=8, required_signature="Shivam Singh"
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: FAIL — `ModuleNotFoundError` for `cover_letter_validation`.

- [ ] **Step 3: Write the implementation**

Create `src/job_application_automation/resume/cover_letter_validation.py`:

```python
"""Injectable PyMuPDF-based validator for the strict one-page cover letter rule.

Mirrors ``resume/scoring.py``'s pattern of accepting an optional ``fitz``-like
module so tests never open a real PDF or import PyMuPDF at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CoverLetterValidationPolicy:
    """Pass/fail thresholds for a rendered cover-letter PDF."""

    minimum_words: int
    maximum_words: int
    required_signature: str

    def __post_init__(self) -> None:
        if self.minimum_words <= 0:
            raise ValueError("minimum_words must be greater than zero")
        if self.maximum_words <= self.minimum_words:
            raise ValueError("maximum_words must be greater than minimum_words")
        if not self.required_signature.strip():
            raise ValueError("required_signature cannot be empty")


def validate_cover_letter_pdf(
    pdf_path: str | Path,
    policy: CoverLetterValidationPolicy,
    *,
    fitz_module: Any | None = None,
) -> tuple[bool, list[str]]:
    """Return ``(is_valid, issues)`` for one rendered cover-letter attempt.

    A two-page (or unreadable) PDF is always invalid; callers must not promote
    it to the final output path.
    """
    if fitz_module is None:
        try:
            import fitz as fitz_module  # PyMuPDF
        except ImportError:
            return False, ["PDF validator dependency is unavailable"]
    try:
        document = fitz_module.open(str(pdf_path))
    except Exception:
        return False, ["PDF corrupt or unreadable"]

    try:
        if len(document) == 0:
            return False, ["Empty PDF"]
        if len(document) > 1:
            return False, [f"Cover letter spans {len(document)} pages; must be exactly one page."]

        text = str(document[0].get_text("text") or "")
        stripped = text.strip()
        if not stripped:
            return False, ["No text extracted from the cover-letter page"]

        issues: list[str] = []
        word_count = len(stripped.split())
        if word_count < policy.minimum_words:
            issues.append(f"Too short: {word_count} words (minimum {policy.minimum_words}).")
        if word_count > policy.maximum_words:
            issues.append(f"Too long: {word_count} words (maximum {policy.maximum_words}).")
        if policy.required_signature.lower() not in stripped.lower():
            issues.append(f"Missing required signature: {policy.required_signature!r}")

        return (len(issues) == 0), issues
    finally:
        document.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/resume/cover_letter_validation.py tests/test_cover_letter.py
git commit -m "feat(resume): add strict one-page cover-letter PDF validator"
```

---

### Task 6: `cover_letter_rendering.py` — injectable ReportLab renderer

**Files:**
- Create: `src/job_application_automation/resume/cover_letter_rendering.py`
- Modify: `tests/test_cover_letter.py`

**Interfaces:**
- Produces: `CoverLetterRenderRequest`, `CoverLetterRenderer` protocol,
  `CallableCoverLetterRenderer`, `render_cover_letter(renderer, letter,
  candidate, output_path) -> bool`, and the production callback
  `render_cover_letter_pdf(letter, candidate, output_path) -> bool`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cover_letter.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: FAIL — `ModuleNotFoundError` for `cover_letter_rendering`.

- [ ] **Step 3: Write the implementation**

Create `src/job_application_automation/resume/cover_letter_rendering.py`:

```python
"""Injectable boundary for the PDF rendering step of cover-letter generation."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CoverLetterRenderRequest:
    """Inputs required to render one cover-letter artifact."""

    letter: Mapping[str, Any]
    candidate: Mapping[str, Any]
    output_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.letter, Mapping):
            raise ValueError("letter must be a mapping")
        if not isinstance(self.candidate, Mapping):
            raise ValueError("candidate must be a mapping")
        object.__setattr__(self, "output_path", Path(self.output_path))


@runtime_checkable
class CoverLetterRenderer(Protocol):
    """Renders an isolated request without depending on orchestration state."""

    def render(self, request: CoverLetterRenderRequest) -> bool:
        """Render one cover letter and return whether the artifact was written."""


class CallableCoverLetterRenderer:
    """Adapter that wraps a plain rendering callback as a ``CoverLetterRenderer``."""

    def __init__(
        self,
        callback: Callable[[Mapping[str, Any], Mapping[str, Any], Path], bool],
    ) -> None:
        self._callback = callback

    def render(self, request: CoverLetterRenderRequest) -> bool:
        return bool(self._callback(request.letter, request.candidate, request.output_path))


def render_cover_letter(
    renderer: CoverLetterRenderer,
    letter: Mapping[str, Any],
    candidate: Mapping[str, Any],
    output_path: Path,
) -> bool:
    """Create a validated render request and delegate it to a renderer port."""
    return bool(
        renderer.render(
            CoverLetterRenderRequest(letter=letter, candidate=candidate, output_path=output_path)
        )
    )


def render_cover_letter_pdf(
    letter: Mapping[str, Any],
    candidate: Mapping[str, Any],
    output_path: Path,
) -> bool:
    """Render a simple, one-page business letter with ReportLab."""
    from reportlab.lib.pagesizes import letter as LETTER_SIZE
    from reportlab.lib.units import inch
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from xml.sax.saxutils import escape as xml_escape

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER_SIZE,
        leftMargin=1.0 * inch,
        rightMargin=1.0 * inch,
        topMargin=1.0 * inch,
        bottomMargin=1.0 * inch,
    )

    body = ParagraphStyle("Body", fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=10)
    header = ParagraphStyle("Header", fontName="Helvetica-Bold", fontSize=11, spaceAfter=14)

    elements: list[Any] = []
    contact_line = " | ".join(
        str(candidate[key])
        for key in ("name", "email", "phone", "location")
        if candidate.get(key)
    )
    if contact_line:
        elements.append(Paragraph(xml_escape(contact_line), header))

    salutation = str(letter.get("salutation", "")).strip()
    if salutation:
        elements.append(Paragraph(xml_escape(salutation), body))

    for paragraph in letter.get("paragraphs", []) or []:
        elements.append(Paragraph(xml_escape(str(paragraph)), body))

    closing = str(letter.get("closing", "")).strip()
    if closing:
        elements.append(Spacer(1, 6))
        elements.append(Paragraph(xml_escape(closing), body))

    signature = str(letter.get("signature", "")).strip()
    if signature:
        elements.append(Paragraph(xml_escape(signature), body))

    try:
        doc.build(elements)
        return True
    except Exception as exc:
        print(f"  [COVER LETTER PDF] Render error: {exc}", flush=True)
        traceback.print_exc()
        return False


DEFAULT_COVER_LETTER_RENDERER = CallableCoverLetterRenderer(render_cover_letter_pdf)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/resume/cover_letter_rendering.py tests/test_cover_letter.py
git commit -m "feat(resume): add injectable ReportLab cover-letter renderer"
```

---

### Task 7: `cover_letter_ai.py` — structured LLM call

**Files:**
- Create: `src/job_application_automation/resume/cover_letter_ai.py`
- Modify: `tests/test_cover_letter.py`

**Interfaces:**
- Consumes: `resume.ai_client.ask_gemini(...)`, `core.adapters.LLMClient`,
  `resume.career_narrative.CareerNarrative`, `resume.cover_letter_models.CoverLetterJob`.
- Produces: `PROMPT_TEMPLATE_VERSION: str`;
  `call_cover_letter_llm(job, narrative, source_text, feedback="", *,
  gateway=None) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cover_letter.py`:

```python
from job_application_automation.core.adapters import LLMSettings  # noqa: E402
from job_application_automation.resume.career_narrative import CareerNarrative  # noqa: E402
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
```

Add the missing top-of-file import for `CoverLetterJob` if not already imported
in this test file from Task 3 (it already is — no change needed there).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: FAIL — `ModuleNotFoundError` for `cover_letter_ai`.

- [ ] **Step 3: Write the implementation**

Create `src/job_application_automation/resume/cover_letter_ai.py`:

```python
"""Structured LLM call for one-page, claim-evidenced cover letters.

Reuses the existing injectable Gemini gateway (``resume/ai_client.ask_gemini``)
so this module never talks to a real LLM SDK in tests.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.adapters import LLMClient
from .ai_client import _strip_json_fence, ask_gemini
from .career_narrative import CareerNarrative
from .cover_letter_models import CoverLetterJob

PROMPT_TEMPLATE_VERSION = "cover-letter-v1"


def build_cover_letter_system_prompt(narrative: CareerNarrative, feedback: str = "") -> str:
    """Strict LLM system prompt for a one-page, evidence-grounded cover letter."""
    fb_suffix = (
        f"\n\n*** PREVIOUS ATTEMPT FAILED. FIX THESE EXACT ISSUES: ***\n{feedback}"
        if feedback
        else ""
    )
    narrative_lines: list[str] = []
    if narrative.reason_for_change:
        narrative_lines.append(f"Reason for seeking a new role: {narrative.reason_for_change}")
    if narrative.next_role_priorities:
        narrative_lines.append(
            "Priorities for the next role: " + ", ".join(narrative.next_role_priorities)
        )
    if narrative.tone:
        narrative_lines.append(f"Requested tone: {narrative.tone}")
    if narrative.do_not_claim:
        narrative_lines.append(
            "Never claim any of the following: " + ", ".join(narrative.do_not_claim)
        )
    narrative_block = "\n".join(narrative_lines) if narrative_lines else "(none supplied)"

    return f"""ROLE
You write a one-page cover letter as the candidate, in first person, addressed
to "{narrative.default_salutation}" unless the job context names a specific
hiring contact.

EVIDENCE RULE
Use only facts present in the supplied CANDIDATE TAGGED SOURCE. Every fact you
reference must come from a "[CLAIM <id>]" line. Never invent a company, date,
metric, tool, or motivation. Do not fabricate personal enthusiasm not grounded
in the supplied narrative.

CANDIDATE-APPROVED NARRATIVE
{narrative_block}
{fb_suffix}

Return a JSON object with this EXACT structure (no markdown fences, plain text
values only, three to four entries in "paragraphs"):
{{
  "salutation": "e.g. Dear Hiring Team,",
  "paragraphs": ["paragraph 1", "paragraph 2", "paragraph 3"],
  "closing": "e.g. Sincerely,",
  "signature": "Candidate's name exactly as it appears in the tagged source",
  "evidence_claim_ids": ["ID-1", "ID-2"]
}}

"evidence_claim_ids" must list every "[CLAIM <id>]" identifier your paragraphs
draw on, and nothing else."""


def call_cover_letter_llm(
    job: CoverLetterJob,
    narrative: CareerNarrative,
    source_text: str,
    feedback: str = "",
    *,
    gateway: LLMClient | None = None,
) -> dict[str, Any]:
    """Invoke the LLM gateway and return the parsed structured payload."""
    user_prompt = f"""
TARGET COMPANY: {job.company}
TARGET ROLE: {job.role}

JOB DESCRIPTION CONTEXT:
{job.jd_text}

CANDIDATE TAGGED SOURCE OF TRUTH:
{source_text}
"""
    system_prompt = build_cover_letter_system_prompt(narrative, feedback)
    raw_content = ask_gemini(
        user_prompt, system=system_prompt, temperature=0.3, json_mode=True, gateway=gateway
    )
    payload = json.loads(_strip_json_fence(raw_content))
    if not isinstance(payload, dict):
        raise ValueError("cover letter payload root must be a JSON object")
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/resume/cover_letter_ai.py tests/test_cover_letter.py
git commit -m "feat(resume): add structured cover-letter LLM prompt and call"
```

---

### Task 8: `cover_letter.py` — orchestration pipeline + CLI

**Files:**
- Create: `src/job_application_automation/resume/cover_letter.py`
- Modify: `tests/test_cover_letter.py`

**Interfaces:**
- Consumes: everything from Tasks 2–7, plus `resume.source.ResumeSource`,
  `core.artifacts.write_json`, `core.paths.OUTPUT_DIR`.
- Produces: `class JDContextUnavailable(RuntimeError)`;
  `generate_cover_letter(job, narrative, source, output_path, *, gateway=None,
  renderer=None, fitz_module=None, cache=None, clock=None, max_retries=None,
  minimum_words=None, maximum_words=None) -> Path | None`; CLI `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cover_letter.py`:

```python
import json as json_module  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from job_application_automation.resume import cover_letter  # noqa: E402
from job_application_automation.resume.cover_letter_cache import CoverLetterCache  # noqa: E402
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

    def render(self, request) -> bool:
        self.render_count += 1
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: FAIL — `ModuleNotFoundError` for `job_application_automation.resume.cover_letter`.

- [ ] **Step 3: Write the implementation**

Create `src/job_application_automation/resume/cover_letter.py`:

```python
#!/usr/bin/env python3
"""Standalone one-page, fact-grounded cover-letter generator (PRD F2)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..core.adapters import LLMClient
from ..core.artifacts import write_json
from ..core.engine_shared import load_json_config
from ..core.paths import CONFIG_DIR, OUTPUT_DIR
from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path
from .ai_client import scrape_ashby_job
from .career_narrative import CareerNarrative, load_career_narrative
from .cover_letter_ai import PROMPT_TEMPLATE_VERSION, call_cover_letter_llm
from .cover_letter_cache import CoverLetterCache, cover_letter_cache_key
from .cover_letter_claims import known_claim_ids, validate_claim_ids
from .cover_letter_models import CoverLetterJob
from .cover_letter_rendering import (
    DEFAULT_COVER_LETTER_RENDERER,
    CoverLetterRenderer,
    render_cover_letter,
)
from .cover_letter_validation import CoverLetterValidationPolicy, validate_cover_letter_pdf
from .source import ResumeSource, load_resume_source

DEFAULT_CONFIG_FILE = CONFIG_DIR / "candidate_profile_config.json"
COVER_LETTER_CACHE_FILE = resolve_runtime_path(RUNTIME_CONFIG.cover_letter["cache_file"])
MAX_RETRIES = int(RUNTIME_CONFIG.cover_letter["max_retries"])
MINIMUM_WORDS = int(RUNTIME_CONFIG.cover_letter["minimum_words"])
MAXIMUM_WORDS = int(RUNTIME_CONFIG.cover_letter["maximum_words"])


class JDContextUnavailable(RuntimeError):
    """Raised when no trustworthy job-description text is available."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _narrative_hash(narrative: CareerNarrative) -> str:
    payload = json.dumps(
        {
            "reason_for_change": narrative.reason_for_change,
            "next_role_priorities": list(narrative.next_role_priorities),
            "tone": narrative.tone,
            "default_salutation": narrative.default_salutation,
            "do_not_claim": list(narrative.do_not_claim),
        },
        sort_keys=True,
    )
    return _sha256(payload)


def _job_identity(job: CoverLetterJob) -> str:
    return job.url.strip() or f"company:{job.company}|role:{job.role}"


def cache_key_for(job: CoverLetterJob, narrative: CareerNarrative, source: ResumeSource) -> str:
    """Return the deterministic cache key for one job/narrative/source combination."""
    return cover_letter_cache_key(
        job_identity=_job_identity(job),
        jd_sha256=_sha256(job.jd_text),
        source_sha256=_sha256(source.text),
        narrative_sha256=_narrative_hash(narrative),
        template_version=PROMPT_TEMPLATE_VERSION,
    )


def _normalize_letter(payload: dict[str, Any]) -> dict[str, Any]:
    paragraphs = payload.get("paragraphs", [])
    return {
        "salutation": str(payload.get("salutation", "")).strip(),
        "paragraphs": [str(p).strip() for p in paragraphs if str(p).strip()]
        if isinstance(paragraphs, list)
        else [],
        "closing": str(payload.get("closing", "")).strip(),
        "signature": str(payload.get("signature", "")).strip(),
        "evidence_claim_ids": [
            str(cid).strip()
            for cid in (payload.get("evidence_claim_ids") or [])
            if str(cid).strip()
        ],
    }


def _structural_issues(letter: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not letter["salutation"]:
        issues.append("Missing required key: salutation")
    if not (3 <= len(letter["paragraphs"]) <= 4):
        issues.append(f"paragraphs must contain 3-4 entries; found {len(letter['paragraphs'])}")
    if not letter["closing"]:
        issues.append("Missing required key: closing")
    if not letter["signature"]:
        issues.append("Missing required key: signature")
    return issues


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def _audit_path_for(output_path: Path) -> Path:
    return output_path.with_name(output_path.stem + ".audit.json")


def generate_cover_letter(
    job: CoverLetterJob,
    narrative: CareerNarrative,
    source: ResumeSource,
    output_path: Path,
    *,
    gateway: LLMClient | None = None,
    renderer: CoverLetterRenderer | None = None,
    fitz_module: Any | None = None,
    cache: CoverLetterCache | None = None,
    clock: Callable[[], datetime] | None = None,
    max_retries: int | None = None,
    minimum_words: int | None = None,
    maximum_words: int | None = None,
) -> Optional[Path]:
    """Generate one validated, one-page cover letter PDF plus its audit sidecar.

    Returns the output path on success or ``None`` after every attempt fails.
    A two-page or otherwise invalid render is never promoted to ``output_path``.
    """
    if not job.jd_text.strip():
        raise JDContextUnavailable(
            f"No job-description context available for {job.company} - {job.role}."
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    active_renderer = renderer or DEFAULT_COVER_LETTER_RENDERER
    active_clock = clock or (lambda: datetime.now(timezone.utc))
    attempts = max_retries if max_retries is not None else MAX_RETRIES
    policy = CoverLetterValidationPolicy(
        minimum_words=minimum_words if minimum_words is not None else MINIMUM_WORDS,
        maximum_words=maximum_words if maximum_words is not None else MAXIMUM_WORDS,
        required_signature=source.candidate.get("name", ""),
    )
    known_ids = known_claim_ids(source.experience)
    key = cache_key_for(job, narrative, source)

    def _finish(letter: dict[str, Any]) -> Optional[Path]:
        attempt_path = output_path.with_name(f".{output_path.stem}.attempt{output_path.suffix}")
        _remove_file(attempt_path)
        if not render_cover_letter(active_renderer, letter, dict(source.candidate), attempt_path):
            _remove_file(attempt_path)
            return None
        valid, _issues = validate_cover_letter_pdf(attempt_path, policy, fitz_module=fitz_module)
        if not valid:
            _remove_file(attempt_path)
            return None
        os.replace(attempt_path, output_path)
        write_json(
            _audit_path_for(output_path),
            {
                "schema_version": 1,
                "evidence_claim_ids": letter["evidence_claim_ids"],
                "prompt_template_version": PROMPT_TEMPLATE_VERSION,
                "jd_sha256": _sha256(job.jd_text),
                "source_sha256": _sha256(source.text),
                "narrative_sha256": _narrative_hash(narrative),
                "generated_at": active_clock().isoformat(),
            },
        )
        if cache is not None:
            cache.set(key, letter)
        return output_path

    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            result = _finish(cached)
            if result is not None:
                return result
            cache.discard(key)

    feedback = ""
    for _attempt in range(1, attempts + 1):
        try:
            payload = call_cover_letter_llm(job, narrative, source.text, feedback, gateway=gateway)
        except Exception as exc:  # noqa: BLE001 - feed the error back as retry guidance
            feedback = f"LLM request failed: {type(exc).__name__}. Return valid JSON."
            continue

        letter = _normalize_letter(payload)
        issues = _structural_issues(letter)
        if issues:
            feedback = "CRITICAL: " + "; ".join(issues)
            continue

        invalid_claims = validate_claim_ids(letter["evidence_claim_ids"], known_ids)
        if invalid_claims:
            feedback = (
                "These evidence_claim_ids do not exist in the tagged source and must be "
                "removed or replaced with real claim IDs: " + ", ".join(invalid_claims)
            )
            continue

        result = _finish(letter)
        if result is not None:
            return result
        feedback = "The rendered PDF failed one-page or word-budget validation. Write fewer words."

    return None


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone one-page cover-letter generator")
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--url", default="", help="Job URL; also used for an Ashby JD fallback")
    parser.add_argument("--jd-overview", default="")
    parser.add_argument("--jd-resp", default="")
    parser.add_argument("--jd-req", default="")
    parser.add_argument("--jd-file", default="", help="Path to a file containing JD text")
    parser.add_argument("--profile", default=str(DEFAULT_CONFIG_FILE))
    parser.add_argument("--output", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argument_parser().parse_args(argv)

    jd_text = "\n".join(part for part in (args.jd_overview, args.jd_resp, args.jd_req) if part)
    if args.jd_file:
        jd_text = Path(args.jd_file).read_text(encoding="utf-8")
    if not jd_text.strip() and args.url:
        try:
            scraped = scrape_ashby_job(args.url)
            jd_text = scraped.get("jd_text", "")
        except Exception as exc:
            print(f"[CONTEXT] Could not load job context: {exc}", file=sys.stderr)

    job = CoverLetterJob(company=args.company, role=args.role, jd_text=jd_text, url=args.url)

    try:
        profile = load_json_config(Path(args.profile))
    except (OSError, ValueError) as exc:
        print(f"Could not load candidate profile: {exc}", file=sys.stderr)
        return 2
    narrative = load_career_narrative(profile)

    try:
        source = load_resume_source(resolve_runtime_path(RUNTIME_CONFIG.application["resume_source_file"]))
    except (OSError, ValueError) as exc:
        print(f"Could not load tagged resume source: {exc}", file=sys.stderr)
        return 2

    slug = f"{args.company}_{args.role}".replace(" ", "_")
    output = Path(args.output or str(OUTPUT_DIR / f"{slug}_Cover_Letter.pdf"))

    cache = CoverLetterCache()
    if COVER_LETTER_CACHE_FILE.exists():
        try:
            cache.load(COVER_LETTER_CACHE_FILE)
        except (OSError, ValueError):
            pass

    try:
        result = generate_cover_letter(job, narrative, source, output, cache=cache)
    except JDContextUnavailable as exc:
        print(f"JD_CONTEXT_UNAVAILABLE: {exc}", file=sys.stderr)
        return 2

    if result:
        cache.save(COVER_LETTER_CACHE_FILE)
        print(f"\nSUCCESS: {result}")
        return 0

    print(f"\nFAILED to generate a valid one-page cover letter for {args.company} - {args.role}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cover_letter.py -v`
Expected: PASS (every test in the file).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/resume/cover_letter.py tests/test_cover_letter.py
git commit -m "feat(resume): add cover-letter generation pipeline and CLI entry point"
```

---

### Task 9: Wire the `cover-letter` command into the CLI dispatcher

**Files:**
- Modify: `src/job_application_automation/cli.py`
- Modify: `tests/test_cli_dispatch.py`

**Interfaces:**
- Consumes: `job_application_automation.resume.cover_letter.main`.

- [ ] **Step 1: Write the failing test**

In `tests/test_cli_dispatch.py`, extend
`test_public_command_forwards_arguments_and_propagates_exit_code`'s companion
assertions by adding a new test method to `UnifiedCliDispatchTests`:

```python
    def test_cover_letter_command_dispatches_to_its_module(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def resolve_main(module_name: str):
            def handler(arguments: list[str] | None) -> int:
                calls.append((module_name, list(arguments or [])))
                return 0

            return handler

        exit_code = cli.dispatch(
            ["cover-letter", "--company", "Example Co", "--role", "PM"],
            resolve_main=resolve_main,
        )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [
                (
                    "job_application_automation.resume.cover_letter",
                    ["--company", "Example Co", "--role", "PM"],
                )
            ],
            calls,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_dispatch.py -v`
Expected: FAIL — `unknown command 'cover-letter'`.

- [ ] **Step 3: Register the command**

In `src/job_application_automation/cli.py`, add to `COMMAND_MODULES`:

```python
COMMAND_MODULES = {
    "apply": "job_application_automation.core.orchestrator",
    "queue": "job_application_automation.core.queue_runner",
    "resume": "job_application_automation.resume.generate",
    "cover-letter": "job_application_automation.resume.cover_letter",
    "search": "job_application_automation.search.job_boards",
    "gmail": "job_application_automation.mail.gmail_client",
    "email-pool": "job_application_automation.mail.pool_select",
}
```

And update the usage text in `_print_usage`:

```python
        "  resume      Generate a personalised resume\n"
        "  cover-letter  Generate a one-page personalised cover letter\n"
        "  search      Search supported ATS job boards\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_dispatch.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add src/job_application_automation/cli.py tests/test_cli_dispatch.py
git commit -m "feat(cli): register the cover-letter command"
```

---

### Task 10: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -q`
Expected: All tests pass, including every module touched above
(`test_cover_letter.py`, `test_runtime_config.py`, `test_cli_dispatch.py`, and
the pre-existing resume/orchestrator suites, which must be unaffected).

- [ ] **Step 2: Run the linter**

Run: `python -m ruff check src/job_application_automation/resume/cover_letter.py src/job_application_automation/resume/cover_letter_ai.py src/job_application_automation/resume/cover_letter_cache.py src/job_application_automation/resume/cover_letter_claims.py src/job_application_automation/resume/cover_letter_models.py src/job_application_automation/resume/cover_letter_rendering.py src/job_application_automation/resume/cover_letter_validation.py src/job_application_automation/resume/career_narrative.py src/job_application_automation/cli.py src/job_application_automation/core/runtime_config.py`
Expected: No findings.

- [ ] **Step 3: Manual smoke check of `--help`**

Run: `python src/job_automation.py cover-letter --help`
Expected: Argument list prints without importing ReportLab/PyMuPDF/Vertex/Playwright (matches the existing lazy-import convention other commands rely on).

- [ ] **Step 4: Commit (only if Steps 1–3 required fixes)**

```bash
git add -A
git commit -m "fix: address full-suite verification findings for the cover-letter feature"
```
