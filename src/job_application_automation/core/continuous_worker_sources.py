"""Typed source strategies shared by continuous application workers."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .application_candidates import eligible_application_jobs


ATS_PLATFORM_PATTERN = re.compile(r"^[a-z][a-z0-9]*$")


class JsonReader(Protocol):
    def __call__(self, path: Path) -> object: ...


class TrackerReader(Protocol):
    def __call__(self, path: Path) -> Sequence[Mapping[str, Any]]: ...


class AtsDetector(Protocol):
    def __call__(self, url: str) -> str | None: ...


class ModuleFinder(Protocol):
    def __call__(self, module_name: str) -> object | None: ...


@dataclass(frozen=True, slots=True)
class SourceServices:
    """Patchable file and provider operations used by source strategies."""

    read_json: JsonReader
    read_tracker: TrackerReader
    detect_ats: AtsDetector


def eligible_provider_jobs(payload: object, ats_platform: str) -> list[dict[str, Any]]:
    """Normalize one provider's live search records for continuous work."""
    return eligible_application_jobs(
        payload,
        expected_platform=ats_platform,
        require_declared_platform=True,
        input_label=f"continuous {ats_platform} input",
    )


def validate_worker_platform(
    ats_platform: str,
    *,
    find_module: ModuleFinder | None = None,
) -> str:
    """Return an installed provider name accepted by continuous workers."""
    normalized = str(ats_platform).strip().lower()
    if not ATS_PLATFORM_PATTERN.fullmatch(normalized):
        raise ValueError("continuous ATS platform must contain only lowercase letters and digits")
    engine_module = f"job_application_automation.engines.{normalized}"
    module_finder = find_module or (lambda name: importlib.util.find_spec(name))
    if module_finder(engine_module) is None:
        raise ValueError(f"continuous ATS engine is not installed: {normalized}")
    return normalized


def load_source_jobs(
    *,
    source: str,
    ats_platform: str,
    input_path: Path,
    tracker_path: Path | None,
    services: SourceServices,
) -> list[dict[str, Any]]:
    """Load provider-consistent jobs from search JSON or an Excel tracker."""
    if source == "search":
        if not input_path.is_file():
            return []
        return eligible_provider_jobs(services.read_json(input_path), ats_platform)
    if source != "tracker":
        raise ValueError(f"unsupported continuous worker source: {source}")
    if tracker_path is None:
        raise ValueError("--tracker is required for tracker source workers")

    eligible: list[dict[str, Any]] = []
    for job in services.read_tracker(tracker_path):
        job_url = str(job.get("url", "")).strip()
        if str(job.get("ats", "")).strip().lower() != ats_platform:
            continue
        if services.detect_ats(job_url) != ats_platform:
            continue
        eligible.append(
            {
                "job_url": job_url,
                "company": str(job.get("company", "")),
                "title": str(job.get("role", "")),
                "platform": ats_platform,
                "tracker_row": int(job.get("row_number", 0)),
            }
        )
    return eligible
