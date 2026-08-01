"""Lightweight validation and detection for supported ATS job URLs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse


ATS_HOST_MARKERS: Mapping[str, tuple[str, ...]] = {
    "ashby": ("ashbyhq.com",),
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "workable": ("workable.com", "apply.workable.com"),
    "smartrecruiters": ("smartrecruiters.com", "jobs.smartrecruiters.com"),
}

# Requiring provider-owned job path shapes prevents a company board root from
# being mistaken for an individual application. Greenhouse embedded and custom
# domain forms are handled separately in ``validate_ats_job_url``.
ATS_JOB_PATH_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "ashby": (re.compile(r"^/[^/]+/[^/]+(?:/application)?/?$", re.I),),
    "greenhouse": (re.compile(r"^/[^/]+/jobs/[^/]+/?$", re.I),),
    "lever": (re.compile(r"^/[^/]+/[^/]+(?:/apply)?/?$", re.I),),
    "workable": (re.compile(r"^/(?:[^/]+/)?(?:j|jobs)/[^/]+(?:/(?:apply|application))?/?$", re.I),),
    "smartrecruiters": (
        re.compile(r"^/[^/]+/[^/]+/?$", re.I),
        re.compile(r"^/oneclick-ui/company/[^/]+/publication/[^/]+/?$", re.I),
    ),
}


def _host_matches(host: str, marker: str) -> bool:
    return host == marker or host.endswith(f".{marker}")


def validate_ats_url(url: str, ats: str) -> bool:
    """Return whether *url* belongs to the requested supported ATS."""
    if ats not in ATS_HOST_MARKERS:
        return False
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    greenhouse_job_id = parse_qs(parsed.query).get("gh_jid", [])
    # Some companies embed the Greenhouse form on their own career site. A
    # numeric gh_jid is the only provider-owned signal on those custom domains.
    custom_greenhouse_url = (
        ats == "greenhouse" and len(greenhouse_job_id) == 1 and greenhouse_job_id[0].isdigit()
    )
    return (
        parsed.scheme.lower() == "https"
        and bool(host)
        and (
            any(_host_matches(host, marker) for marker in ATS_HOST_MARKERS[ats])
            or custom_greenhouse_url
        )
    )


def validate_ats_job_url(url: str, ats: str) -> bool:
    """Return whether *url* identifies a job, not merely an ATS company board."""
    if not validate_ats_url(url, ats):
        return False
    patterns = ATS_JOB_PATH_PATTERNS.get(ats)
    if not patterns:
        return True
    try:
        parsed = urlparse(str(url).strip())
        path = parsed.path or "/"
    except ValueError:
        return False
    if ats == "greenhouse":
        query = parse_qs(parsed.query)
        gh_jid = query.get("gh_jid", [])
        embed_token = query.get("token", [])
        if len(gh_jid) == 1 and gh_jid[0].isdigit():
            return True
        if (
            path.rstrip("/").casefold() == "/embed/job_app"
            and len(embed_token) == 1
            and embed_token[0].isdigit()
        ):
            return True
    return any(pattern.fullmatch(path) for pattern in patterns)


def detect_ats_job_url(url: str) -> str | None:
    """Return the supported ATS that owns a job-specific URL."""
    if not isinstance(url, str) or not url.strip():
        return None
    return next(
        (name for name in ATS_HOST_MARKERS if validate_ats_job_url(url, name)),
        None,
    )
