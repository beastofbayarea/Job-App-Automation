from __future__ import annotations

# ruff: noqa: S101 - pytest assertions express the focused safety contracts.

import hashlib
from pathlib import Path
from typing import Any

import pytest

from job_application_automation.core.engine_shared import require_submission_allowed
from job_application_automation.engines import browser_controls


class FakeControl:
    def __init__(self, selected: object) -> None:
        self.selected = selected
        self.uploads = 0

    def evaluate(self, _script: str) -> object:
        return self.selected

    def set_input_files(self, _path: str) -> None:
        self.uploads += 1


class FakeLocatorList:
    def __init__(self, controls: list[FakeControl]) -> None:
        self.controls = controls

    def count(self) -> int:
        return len(self.controls)

    def nth(self, index: int) -> FakeControl:
        return self.controls[index]


class FakePage:
    def __init__(self, selectors: dict[str, list[FakeControl]]) -> None:
        self.selectors = selectors

    def locator(self, selector: str) -> FakeLocatorList:
        return FakeLocatorList(self.selectors.get(selector, []))


def _selected(path: Path, *, name: str | None = None, size: int | None = None) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "name": name or path.name,
        "size": len(content) if size is None else size,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def test_input_file_match_requires_name_size_and_hash(tmp_path: Path) -> None:
    document = tmp_path / "personalized-resume.pdf"
    document.write_bytes(b"exact personalized resume")

    assert browser_controls.input_file_matches(FakeControl(_selected(document)), document)  # type: ignore[arg-type]
    assert not browser_controls.input_file_matches(
        FakeControl(_selected(document, size=document.stat().st_size + 1)),  # type: ignore[arg-type]
        document,
    )
    assert not browser_controls.input_file_matches(
        FakeControl([_selected(document), _selected(document)]),  # type: ignore[arg-type]
        document,
    )


def test_upload_first_has_one_retry_total_across_duplicate_controls(tmp_path: Path) -> None:
    document = tmp_path / "personalized-resume.pdf"
    document.write_bytes(b"resume")
    first = FakeControl(None)
    duplicate = FakeControl(_selected(document))
    page = FakePage({"input[type=file]": [first, duplicate]})

    assert not browser_controls.upload_first(  # type: ignore[arg-type]
        page,
        ("input[type=file]",),
        document,
    )
    assert first.uploads == 2
    assert duplicate.uploads == 0


def test_submission_guard_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_APP_FORBID_SUBMIT", "1")
    with pytest.raises(RuntimeError, match="submission is forbidden"):
        require_submission_allowed()
