"""Normalize verified-live application candidates at worker boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .ats_urls import ATS_HOST_MARKERS, detect_ats_job_url
from .identity import canonical_job_url


SUPPORTED_PLATFORMS = frozenset(ATS_HOST_MARKERS)


def normalize_application_candidate(
    value: Mapping[str, Any],
    *,
    expected_platform: str | None = None,
    require_declared_platform: bool = False,
) -> dict[str, Any] | None:
    """Validate one source record and return its canonical application route.

    ``apply_url`` takes precedence when it points to a supported job form. The
    declared provider is treated as a consistency assertion, while generic
    discovery records such as ``platform=web`` may still route through a valid
    provider-owned apply URL.
    """
    if str(value.get("live_status", "")).strip().lower() != "live":
        return None
    if not all(
        str(value.get(key, "")).strip() for key in ("job_url", "company", "title", "description")
    ):
        return None

    declared_platform = str(value.get("platform", "")).strip().lower()
    if require_declared_platform and declared_platform != expected_platform:
        return None

    application_url = next(
        (
            candidate_url
            for candidate_url in (
                str(value.get("apply_url", "")).strip(),
                str(value.get("job_url", "")).strip(),
            )
            if detect_ats_job_url(candidate_url)
        ),
        "",
    )
    detected_platform = detect_ats_job_url(application_url) if application_url else None
    if detected_platform not in SUPPORTED_PLATFORMS:
        return None
    if expected_platform is not None and detected_platform != expected_platform:
        return None
    if declared_platform in SUPPORTED_PLATFORMS and declared_platform != detected_platform:
        return None

    try:
        canonical_url = canonical_job_url(application_url)
    except ValueError:
        return None

    job = dict(value)
    job["platform"] = detected_platform
    job["_application_url"] = application_url
    job["_canonical_url"] = canonical_url
    return job


def eligible_application_jobs(
    payload: Any,
    *,
    expected_platform: str | None = None,
    require_declared_platform: bool = False,
    input_label: str = "application input",
) -> list[dict[str, Any]]:
    """Return complete, live, provider-consistent candidates without duplicates."""
    if not isinstance(payload, list):
        raise ValueError(f"{input_label} must be a JSON array")
    eligible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in payload:
        if not isinstance(value, Mapping):
            continue
        job = normalize_application_candidate(
            value,
            expected_platform=expected_platform,
            require_declared_platform=require_declared_platform,
        )
        if job is None:
            continue
        canonical_url = str(job["_canonical_url"])
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        eligible.append(job)
    return eligible


def application_url(job: Mapping[str, Any]) -> str:
    """Return the validated provider application URL on a normalized record."""
    return str(job.get("_application_url") or job.get("job_url") or "").strip()
