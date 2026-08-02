from __future__ import annotations

import json
from pathlib import Path

from job_application_automation.core.continuous_worker_candidates import (
    choose_resumable_or_fresh,
    load_exact_confirmed_ledger_index,
    partition_candidate_state,
)


def _job(job_id: str) -> dict[str, str]:
    url = f"https://boards.greenhouse.io/example/jobs/{job_id}"
    return {"job_url": url, "_canonical_url": url}


def test_exact_confirmed_ledger_index_excludes_near_miss_and_other_provider(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "submission_log.json"
    ledger.write_text(
        json.dumps(
            {
                "confirmed": {
                    "status": "SUBMITTED & CONFIRMED",
                    "ats": "greenhouse",
                    "job_url": _job("1")["job_url"],
                    "company": "Example",
                    "role": "Product Manager",
                    "applied_at": "2026-08-02T00:00:00+00:00",
                },
                "ambiguous": {
                    "status": "SUBMISSION_UNCONFIRMED",
                    "ats": "greenhouse",
                    "job_url": _job("2")["job_url"],
                },
                "other_provider": {
                    "status": "SUBMITTED & CONFIRMED",
                    "ats": "lever",
                    "job_url": "https://jobs.lever.co/example/3",
                },
            }
        ),
        encoding="utf-8",
    )

    index = load_exact_confirmed_ledger_index(
        ledger,
        "greenhouse",
        identity_for_url=lambda url: f"identity:{url.rsplit('/', 1)[-1]}",
    )

    assert index.identities == frozenset({"identity:1"})
    assert index.records["identity:1"].company == "Example"
    assert index.records["identity:1"].title == "Product Manager"


def test_candidate_state_partition_prioritizes_resume_and_excludes_terminal_work() -> None:
    jobs = [_job("1"), _job("2"), _job("3"), _job("4"), _job("5")]
    state = {
        jobs[0]["_canonical_url"]: {"status": "documents_ready"},
        jobs[1]["_canonical_url"]: {"status": "failed"},
    }

    pools = partition_candidate_state(
        jobs,
        state,
        state_key=lambda job: job["_canonical_url"],
        identity=lambda job: f"job:{job['job_url'].rsplit('/', 1)[-1]}",
        confirmed_identities={"job:3"},
        blocked_identities={"job:4"},
    )

    assert pools.resumable == (jobs[0],)
    assert pools.fresh == (jobs[4],)
    assert (
        choose_resumable_or_fresh(
            pools,
            choice=lambda candidates: candidates[-1],
        )
        == jobs[0]
    )
