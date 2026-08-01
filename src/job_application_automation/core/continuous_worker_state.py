"""Persistence and recovery operations for continuous worker state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .artifacts import atomic_write_text, read_json
from .continuous_worker_models import WorkerState


UTC = timezone.utc
STATE_VERSION = 1


def utc_now_iso() -> str:
    """Return the worker timestamp format used by existing state files."""
    return datetime.now(UTC).isoformat()


def load_worker_state(path: Path, ats_platform: str) -> WorkerState:
    """Load and validate a version-one provider worker state file."""
    if not path.exists():
        return {"version": STATE_VERSION, "jobs": {}}
    payload: Any = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid continuous {ats_platform} state: {path}")
    if payload.get("version") != STATE_VERSION:
        raise ValueError(f"unsupported continuous {ats_platform} state version: {path}")
    return cast(WorkerState, payload)


def save_worker_state(path: Path, state: WorkerState) -> None:
    """Atomically persist state without changing the established schema."""
    state["updated_at"] = utc_now_iso()
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_worker_state_records(path: Path) -> dict[str, Mapping[str, Any]]:
    """Read state records for cross-worker coordination.

    This deliberately accepts the historic unversioned shape used by the
    source-claim reader while the owning worker performs strict version checks.
    """
    if not path.is_file():
        return {}
    payload: Any = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise ValueError(f"invalid continuous ATS state: {path}")
    jobs = cast(dict[object, object], payload["jobs"])
    return {
        str(url): record
        for url, record in jobs.items()
        if isinstance(record, Mapping)
    }


def reconcile_interrupted_submissions(state: WorkerState) -> int:
    """Quarantine attempts that may have submitted before the process stopped."""
    records = state.get("jobs")
    if not isinstance(records, dict):
        raise ValueError("continuous worker state jobs must be an object")
    reconciled = 0
    for record in records.values():
        if not isinstance(record, dict) or record.get("status") != "application_started":
            continue
        record.update(
            {
                "status": "manual_review",
                "stage": "application",
                "result_status": "INTERRUPTED_AFTER_APPLICATION_START",
                "updated_at": utc_now_iso(),
            }
        )
        reconciled += 1
    return reconciled
