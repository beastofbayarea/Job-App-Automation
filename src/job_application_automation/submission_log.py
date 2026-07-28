"""Local record of submitted job applications, keyed for later lookup.

Resume and cover letter *files* are expected to live on the deployment VPS
(too large to keep locally at scale); this module persists the small JSON
index that answers "which resume/cover letter/email did I use for role X"
without needing to fetch those files back.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, MutableMapping
from urllib.parse import urlparse

from .artifacts import read_json, write_json

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    slug = _SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("value must contain at least one alphanumeric character")
    return slug


def _require_string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    return normalized


def _require_url(value: object, field_name: str) -> str:
    url = _require_string(value, field_name)
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")
    return url


def _require_email(value: object, field_name: str) -> str:
    email = _require_string(value, field_name)
    if "@" not in email or email.startswith("@"):
        raise ValueError(f"{field_name} must contain a local part and @")
    return email


def make_submission_id(company: str, role: str, *, applied_at: datetime | None = None) -> str:
    """Build the stable ``YYYYMMDD-company-role`` id used as the JSON key."""
    stamp = (applied_at or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"{stamp}-{_slugify(company)}-{_slugify(role)}"


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    """One tracked job application, matching the reviewed schema."""

    company: str
    role: str
    job_url: str
    ats: str
    status: str
    email_used: str
    resume_filename: str
    # Cover letters and VPS storage are not automated yet (see submission_log
    # module docstring); both default to "" so callers can log a submission
    # as soon as it happens instead of waiting on those pipelines to exist.
    cover_letter_filename: str = ""
    remote_path: str = ""
    applied_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "company", _require_string(self.company, "company"))
        object.__setattr__(self, "role", _require_string(self.role, "role"))
        object.__setattr__(self, "job_url", _require_url(self.job_url, "job_url"))
        object.__setattr__(self, "ats", _require_string(self.ats, "ats").lower())
        object.__setattr__(self, "status", _require_string(self.status, "status"))
        object.__setattr__(self, "email_used", _require_email(self.email_used, "email_used"))
        object.__setattr__(
            self, "resume_filename", _require_string(self.resume_filename, "resume_filename")
        )
        object.__setattr__(
            self,
            "cover_letter_filename",
            _require_string(self.cover_letter_filename, "cover_letter_filename", allow_empty=True),
        )
        object.__setattr__(
            self,
            "remote_path",
            _require_string(self.remote_path, "remote_path", allow_empty=True),
        )
        if not isinstance(self.applied_at, datetime):
            raise ValueError("applied_at must be a datetime")

    @property
    def submission_id(self) -> str:
        return make_submission_id(self.company, self.role, applied_at=self.applied_at)

    def to_payload(self) -> dict[str, object]:
        """Serialize to the JSON entry shape stored under ``submission_id``."""
        return {
            "applied_at": self.applied_at.isoformat(),
            "company": self.company,
            "role": self.role,
            "job_url": self.job_url,
            "ats": self.ats,
            "status": self.status,
            "email_used": self.email_used,
            "resume_filename": self.resume_filename,
            "cover_letter_filename": self.cover_letter_filename,
            "remote_path": self.remote_path,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "SubmissionRecord":
        if not isinstance(payload, Mapping):
            raise ValueError("submission record must be an object")
        applied_at = payload.get("applied_at")
        if not isinstance(applied_at, str):
            raise ValueError("applied_at must be a string")
        return cls(
            company=payload.get("company"),  # type: ignore[arg-type]
            role=payload.get("role"),  # type: ignore[arg-type]
            job_url=payload.get("job_url"),  # type: ignore[arg-type]
            ats=payload.get("ats"),  # type: ignore[arg-type]
            status=payload.get("status"),  # type: ignore[arg-type]
            email_used=payload.get("email_used"),  # type: ignore[arg-type]
            resume_filename=payload.get("resume_filename"),  # type: ignore[arg-type]
            cover_letter_filename=payload.get("cover_letter_filename", ""),  # type: ignore[arg-type]
            remote_path=payload.get("remote_path", ""),  # type: ignore[arg-type]
            applied_at=datetime.fromisoformat(applied_at),
        )


class SubmissionLog:
    """A thread-safe, disk-backed index of submitted applications."""

    def __init__(self, entries: MutableMapping[str, dict[str, object]] | None = None) -> None:
        self._entries: MutableMapping[str, dict[str, object]] = (
            entries if entries is not None else {}
        )
        self._lock = threading.Lock()

    def record(self, submission: SubmissionRecord) -> str:
        """Add or overwrite one entry keyed by its computed submission id."""
        with self._lock:
            submission_id = submission.submission_id
            self._entries[submission_id] = submission.to_payload()
            return submission_id

    def get(self, submission_id: str) -> dict[str, object] | None:
        with self._lock:
            entry = self._entries.get(submission_id)
            return dict(entry) if entry is not None else None

    def find_by_company(self, company: str) -> dict[str, dict[str, object]]:
        """Return all entries for ``company`` (case-insensitive), keyed by id."""
        needle = company.strip().lower()
        with self._lock:
            return {
                submission_id: dict(entry)
                for submission_id, entry in self._entries.items()
                if str(entry.get("company", "")).strip().lower() == needle
            }

    def load(self, path: Path) -> int:
        """Merge entries from an existing JSON log and return their count."""
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise ValueError("submission log root must be an object")
        valid = {
            str(key): dict(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        with self._lock:
            self._entries.update(valid)
        return len(valid)

    def save(self, path: Path) -> None:
        """Atomically persist the log, sorted by submission id for stable diffs."""
        with self._lock:
            snapshot = dict(sorted(self._entries.items()))
        write_json(path, snapshot, indent=2, sort_keys=False)
