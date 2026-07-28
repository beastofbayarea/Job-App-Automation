"""Local persistence helpers for Gmail OTP history and message exports.

All functions retain the CLI's established JSON and CSV shapes.  File writes
use a same-directory temporary file so a completed write is replaced atomically
on supported filesystems.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core.artifacts import (
    read_json,
    write_csv as write_csv_artifact,
    write_json as write_json_artifact,
)
from .gmail_messages import EmailRecord, VerificationCodeMatch, classify_application_email


def load_used_verification_message_ids(path: Path) -> set[str]:
    """Load locally recorded OTP IDs; malformed history is treated as empty."""
    try:
        payload = read_json(path)
        return {
            str(entry["message_id"])
            for entry in payload.get("used_messages", [])
            if isinstance(entry, dict) and entry.get("message_id")
        }
    except (OSError, json.JSONDecodeError):
        return set()


def _write_json_compat(path: Path, payload: object) -> Path:
    """Persist JSON with the byte representation used by the legacy scripts."""
    return write_json_artifact(path, payload, indent=2, ensure_ascii=True)


def record_used_verification_message(
    path: Path,
    match: VerificationCodeMatch,
    *,
    clock: Callable[[], float] = time.time,
    write_json: Callable[[Path, object], Path] = _write_json_compat,
) -> None:
    """Persist a consumed OTP message ID without storing the OTP itself."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        payload = {"used_messages": []}
    entries = payload.get("used_messages", [])
    if any(
        isinstance(entry, dict) and entry.get("message_id") == match.message_id for entry in entries
    ):
        return
    entries.append(
        {
            "message_id": match.message_id,
            "thread_id": match.thread_id,
            "sender": match.sender,
            "recorded_at": int(clock()),
        }
    )
    payload["used_messages"] = entries
    write_json(path, payload)


def export_rows(records: Iterable[EmailRecord], redact: bool) -> list[dict[str, Any]]:
    """Convert records to the established export rows, optionally redacting PII."""
    rows = [asdict(record) for record in records]
    for row in rows:
        record = EmailRecord(**{key: row[key] for key in EmailRecord.__dataclass_fields__})
        row["classification"] = classify_application_email(record)
        if redact:
            for key in ("message_id", "thread_id", "sender", "body"):
                row[key] = "[redacted]"
    return rows


def _csv_fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    return (
        list(rows[0].keys())
        if rows
        else [
            "message_id",
            "thread_id",
            "sender",
            "recipient",
            "subject",
            "date",
            "labels",
            "snippet",
            "body",
            "classification",
        ]
    )


def write_csv(
    path: Path,
    records: list[EmailRecord],
    redact: bool = False,
    *,
    write_csv_artifact_fn: Callable[..., Path] = write_csv_artifact,
) -> None:
    """Write the legacy CSV export atomically without changing its columns."""
    rows = export_rows(records, redact)
    for row in rows:
        row["labels"] = ";".join(row["labels"])
    write_csv_artifact_fn(path, rows, fieldnames=_csv_fieldnames(rows))


def write_json(
    path: Path,
    records: list[EmailRecord],
    redact: bool = False,
    *,
    write_json: Callable[[Path, object], Path] = _write_json_compat,
) -> None:
    """Write the legacy JSON export atomically without changing its shape."""
    write_json(path, export_rows(records, redact))
