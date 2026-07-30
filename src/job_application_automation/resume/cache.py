"""Thread-safe, explicit persistence for generated-resume payloads.

The resume generator historically kept a module-level dictionary and mixed its
disk I/O with retry orchestration.  This module retains the same JSON format
while making cache ownership and persistence deterministic for callers/tests.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Mapping, MutableMapping

from ..core.artifacts import read_json, write_json


class ResumeCacheJob(Protocol):
    """The stable job fields used to identify a generated resume."""

    company: str
    role_title: str
    keywords: str
    jd_overview: str
    jd_responsibilities: str
    jd_requirements: str


def cache_key(job: ResumeCacheJob) -> str:
    """Return the legacy SHA-256 key for a job's tailoring context."""
    context = "\n".join(
        (
            str(job.company),
            str(job.role_title),
            str(job.keywords),
            str(job.jd_overview),
            str(job.jd_responsibilities),
            str(job.jd_requirements),
        )
    )
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


class ResumeCache:
    """A thread-safe cache that preserves the established ``dict`` JSON schema."""

    def __init__(
        self,
        entries: MutableMapping[str, dict[str, Any]] | None = None,
        *,
        lock: threading.Lock | None = None,
    ) -> None:
        # Retaining a caller-provided mapping keeps the generator's legacy
        # ``_llm_cache`` patch point observable to existing callers.
        self._entries: MutableMapping[str, dict[str, Any]] = entries if entries is not None else {}
        self._lock = lock or threading.Lock()

    @property
    def entries(self) -> MutableMapping[str, dict[str, Any]]:
        """Expose the backing mapping for compatible cache inspection/clearing."""
        return self._entries

    def get(self, job: ResumeCacheJob) -> dict[str, Any] | None:
        """Return an isolated cached payload for ``job`` if one exists."""
        with self._lock:
            data = self._entries.get(cache_key(job))
            return copy.deepcopy(data) if isinstance(data, dict) else None

    def set(self, job: ResumeCacheJob, data: Mapping[str, Any]) -> None:
        """Store an isolated copy of one generated payload."""
        with self._lock:
            self._entries[cache_key(job)] = copy.deepcopy(dict(data))

    def discard(self, job: ResumeCacheJob) -> None:
        """Remove a stale payload for ``job`` without failing if absent."""
        with self._lock:
            self._entries.pop(cache_key(job), None)

    def load(self, path: Path) -> int:
        """Merge valid object entries from a JSON cache and return their count.

        Invalid roots intentionally raise ``ValueError`` so the application can
        retain its existing warning policy at the workflow boundary.
        """
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
        return len(valid)

    def save(self, path: Path) -> None:
        """Atomically write a snapshot using the previous compact JSON format."""
        with self._lock:
            snapshot = copy.deepcopy(dict(self._entries))
        write_json(path, snapshot, indent=None)
