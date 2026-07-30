"""Local record of confirmed job applications, keyed for later lookup.

The private ``documents`` workflow owns VPS document storage and retrieval.
This log remains a small, backward-compatible record of confirmed submissions;
it is not the source of truth for archive paths or document integrity.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, MutableMapping

from .artifacts import read_json, write_json
from .identity import canonical_job_url, normalize_email

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_LOCK_TIMEOUT_SECONDS = 30.0
_STALE_LOCK_SECONDS = 300.0


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
    try:
        canonical_job_url(url)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid absolute HTTPS URL") from exc
    return url


def _require_email(value: object, field_name: str) -> str:
    email = _require_string(value, field_name)
    normalize_email(email, field_name)
    return email


def make_submission_id(company: str, role: str, *, applied_at: datetime | None = None) -> str:
    """Build the stable ``YYYYMMDD-company-role`` id used as the JSON key."""
    stamp = (applied_at or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"{stamp}-{_slugify(company)}-{_slugify(role)}"


@contextmanager
def _interprocess_lock(path: Path):
    """Serialize ledger merges across independently supervised ATS workers."""
    lock_path = path.with_name(f"{path.name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            candidate = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(candidate, f"{os.getpid()}\n".encode("ascii"))
            except Exception:
                os.close(candidate)
                lock_path.unlink(missing_ok=True)
                raise
            descriptor = candidate
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > _STALE_LOCK_SECONDS
            except FileNotFoundError:
                continue
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for submission ledger lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


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
    # Archive-aware callers may populate these values. They remain optional so
    # confirmed resume-only submissions retain their legacy shape.
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
            cover_letter_filename=(payload.get("cover_letter_filename") or ""),  # type: ignore[arg-type]
            remote_path=(payload.get("remote_path") or ""),  # type: ignore[arg-type]
            applied_at=datetime.fromisoformat(applied_at),
        )


class SubmissionLog:
    """A thread- and process-safe, disk-backed index of submitted applications."""

    def __init__(self, entries: MutableMapping[str, dict[str, object]] | None = None) -> None:
        self._entries: MutableMapping[str, dict[str, object]] = (
            entries if entries is not None else {}
        )
        self._lock = threading.Lock()

    def record(self, submission: SubmissionRecord) -> str:
        """Add one entry without overwriting a distinct same-day application."""
        with self._lock:
            payload = submission.to_payload()
            base_id = submission.submission_id
            existing = self._entries.get(base_id)
            if existing is None or existing == payload:
                submission_id = base_id
            else:
                discriminator = hashlib.sha256(
                    (
                        f"{submission.job_url}\0{submission.email_used}\0"
                        f"{submission.applied_at.isoformat()}"
                    ).encode("utf-8")
                ).hexdigest()[:12]
                submission_id = f"{base_id}-{discriminator}"
                suffix = 2
                while submission_id in self._entries and self._entries[submission_id] != payload:
                    submission_id = f"{base_id}-{discriminator}-{suffix}"
                    suffix += 1
            self._entries[submission_id] = payload
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
        """Merge and atomically persist without losing another process's entries."""
        with self._lock:
            local_entries = {
                str(key): dict(value)
                for key, value in self._entries.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
        with _interprocess_lock(path):
            disk_entries: dict[str, dict[str, object]] = {}
            if path.exists():
                payload = read_json(path)
                if not isinstance(payload, dict):
                    raise ValueError("submission log root must be an object")
                disk_entries = {
                    str(key): dict(value)
                    for key, value in payload.items()
                    if isinstance(key, str) and isinstance(value, dict)
                }

            merged = SubmissionLog(disk_entries)
            for payload in local_entries.values():
                merged.record(SubmissionRecord.from_payload(payload))
            with merged._lock:
                snapshot = dict(sorted(merged._entries.items()))
            write_json(path, snapshot, indent=2, sort_keys=False)
        with self._lock:
            self._entries.clear()
            self._entries.update(snapshot)
