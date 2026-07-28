"""Atomic, dependency-free persistence helpers for workflow artifacts."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence


def _target_path(path: str | Path) -> Path:
    target = Path(path).expanduser()
    if not target.name:
        raise ValueError("artifact path must name a file")
    return target


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Write text by replacing the target only after the temporary file is ready."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    target = _target_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return target


def read_json(path: str | Path) -> object:
    """Read one UTF-8 JSON artifact without adding application-specific defaults."""
    target = _target_path(path)
    with target.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def write_json(
    path: str | Path,
    payload: object,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> Path:
    """Atomically persist JSON while preserving the caller's selected format."""
    serialized = json.dumps(
        payload,
        indent=indent,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
    )
    return atomic_write_text(path, serialized)


def _normalized_fieldnames(
    rows: Sequence[Mapping[str, object]], fieldnames: Sequence[str] | None
) -> list[str]:
    if fieldnames is not None:
        normalized = list(fieldnames)
        if any(not isinstance(name, str) or not name for name in normalized):
            raise ValueError("fieldnames must contain non-empty strings")
        if len(set(normalized)) != len(normalized):
            raise ValueError("fieldnames cannot contain duplicates")
        return normalized
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if not isinstance(key, str) or not key:
                raise ValueError("CSV row keys must be non-empty strings")
            if key not in seen:
                names.append(key)
                seen.add(key)
    return names


def write_csv(
    path: str | Path,
    rows: Iterable[Mapping[str, object]],
    *,
    fieldnames: Sequence[str] | None = None,
) -> Path:
    """Atomically write rows as a UTF-8 CSV with deterministic field ordering.

    With no explicit ``fieldnames``, columns follow first appearance across the
    supplied rows.  This keeps exported records stable without requiring each
    caller to duplicate schema bookkeeping.
    """
    materialized = list(rows)
    if not all(isinstance(row, Mapping) for row in materialized):
        raise ValueError("rows must contain mappings")
    columns = _normalized_fieldnames(materialized, fieldnames)
    for row in materialized:
        unexpected = set(row).difference(columns)
        if unexpected:
            names = ", ".join(sorted(str(name) for name in unexpected))
            raise ValueError(f"CSV row has fields outside fieldnames: {names}")
    buffer = io.StringIO(newline="")
    if columns:
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(materialized)
    return atomic_write_text(path, buffer.getvalue())
