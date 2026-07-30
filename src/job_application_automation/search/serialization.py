"""Search-result serialization free of filesystem and CLI side effects."""

from __future__ import annotations

import csv
import io
import json
from typing import Any
from collections.abc import Iterable, Sequence


def job_rows(jobs: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert compatible search Job objects to the established output schema."""
    return [job.to_csv_row() for job in jobs]


def render_csv(rows: Iterable[dict[str, Any]], *, fieldnames: Sequence[str]) -> str:
    """Render CSV text with the legacy column order and newline behavior."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def render_json(payload: Any) -> str:
    """Render the UTF-8 JSON shape historically written by search commands."""
    return json.dumps(payload, indent=2, ensure_ascii=False)
