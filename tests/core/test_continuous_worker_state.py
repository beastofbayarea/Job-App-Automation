from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_application_automation.core.continuous_worker_state import (
    load_worker_state,
    reconcile_interrupted_submissions,
    save_worker_state,
)


def test_state_round_trip_preserves_version_one_wire_schema(tmp_path: Path) -> None:
    state_path = tmp_path / "continuous_greenhouse_state.json"
    state = load_worker_state(state_path, "greenhouse")
    state["jobs"]["https://boards.greenhouse.io/example/jobs/123"] = {
        "status": "documents_ready",
        "job_url": "https://boards.greenhouse.io/example/jobs/123",
    }

    save_worker_state(state_path, state)

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["updated_at"]
    assert payload["jobs"] == state["jobs"]
    assert load_worker_state(state_path, "greenhouse")["jobs"] == state["jobs"]


def test_state_loader_rejects_an_unknown_schema_version(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"version":2,"jobs":{}}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported continuous ashby state version"):
        load_worker_state(state_path, "ashby")


def test_interrupted_application_reconciliation_is_idempotent() -> None:
    state = {
        "version": 1,
        "jobs": {
            "started": {"status": "application_started"},
            "ready": {"status": "documents_ready"},
        },
    }

    assert reconcile_interrupted_submissions(state) == 1
    assert reconcile_interrupted_submissions(state) == 0
    assert state["jobs"]["started"]["status"] == "manual_review"
    assert state["jobs"]["started"]["result_status"] == (
        "INTERRUPTED_AFTER_APPLICATION_START"
    )
    assert state["jobs"]["ready"]["status"] == "documents_ready"

