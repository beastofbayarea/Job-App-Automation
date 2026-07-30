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
from typing import Any
from collections.abc import Mapping, MutableMapping

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
