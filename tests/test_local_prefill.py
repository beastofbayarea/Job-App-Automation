from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from job_application_automation.core import local_prefill
from job_application_automation.core.foundation import canonical_job_url


ASHBY_URL_1 = "https://jobs.ashbyhq.com/example/11111111-1111-1111-1111-111111111111"
ASHBY_URL_2 = "https://jobs.ashbyhq.com/example/22222222-2222-2222-2222-222222222222"
SMARTRECRUITERS_POSTING_URL = (
    "https://jobs.smartrecruiters.com/Example/744000141243734-product-manager"
)
SMARTRECRUITERS_ONECLICK_URL = (
    "https://jobs.smartrecruiters.com/oneclick-ui/company/Example/publication/744000141243734"
)


def _args(*, limit: int | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        limit=limit,
        render_timeout_ms=12_000,
        engine_timeout=180,
        resume_timeout=300,
        job_timeout=900,
    )


def _prefilled_result() -> dict[str, object]:
    return {
        "success": True,
        "status": local_prefill.PREFILLED_STATUS,
        "submitted": False,
        "confirmed": False,
        "test_mode": True,
        "detail": "",
        "resume": "resume.pdf",
        "cover_letter": "cover-letter.pdf",
    }


class LocalPrefillStateTests(unittest.TestCase):
    def test_greenhouse_canonical_url_drops_only_redundant_gh_jid(self) -> None:
        path_url = "https://job-boards.greenhouse.io/example/jobs/123456"

        self.assertEqual(
            canonical_job_url(f"{path_url}?gh_jid=123456&utm_source=test"),
            path_url,
        )
        self.assertEqual(
            canonical_job_url(f"{path_url}?gh_jid=654321"),
            f"{path_url}?gh_jid=654321",
        )
        self.assertEqual(
            canonical_job_url("https://careers.example.com/example/jobs/123456?gh_jid=123456"),
            "https://careers.example.com/example/jobs/123456?gh_jid=123456",
        )

    def test_queue_digest_covers_company_title_and_canonical_url(self) -> None:
        base = [{"company": "Example", "title": "Product Manager", "url": ASHBY_URL_1}]
        tracking_variant = [
            {
                "company": "Example",
                "title": "Product Manager",
                "url": f"{ASHBY_URL_1}?utm_source=test",
            }
        ]
        company_variant = [{**base[0], "company": "Different"}]
        title_variant = [{**base[0], "title": "Senior Product Manager"}]

        self.assertEqual(
            local_prefill._queue_digest(base),
            local_prefill._queue_digest(tracking_variant),
        )
        self.assertNotEqual(
            local_prefill._queue_digest(base),
            local_prefill._queue_digest(company_variant),
        )
        self.assertNotEqual(
            local_prefill._queue_digest(base),
            local_prefill._queue_digest(title_variant),
        )

    def test_load_state_validates_schema_path_revision_and_record_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue_path = (root / "queue.json").resolve()
            state_path = root / "state.json"
            queue_key = canonical_job_url(ASHBY_URL_1)
            valid = {
                "schema_version": local_prefill.TERMINAL_STATE_VERSION,
                "queue": str(queue_path),
                "queue_digest": "digest",
                "ats": "ashby",
                "records": {queue_key: {"terminal": False}},
            }

            for name, mutation in (
                ("schema", {"schema_version": 1}),
                ("path", {"queue": str(root / "other.json")}),
                ("digest", {"queue_digest": "other"}),
                ("ats", {"ats": "lever"}),
                ("extra", {"records": {ASHBY_URL_2: {"terminal": True}}}),
            ):
                with self.subTest(name=name):
                    payload = {**deepcopy(valid), **mutation}
                    state_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        local_prefill._load_state(
                            state_path,
                            queue_path=queue_path,
                            ats="ashby",
                            digest="digest",
                            queue_keys={queue_key},
                        )

    def test_worker_lock_rejects_a_second_same_platform_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "ashby.lock"
            with local_prefill._single_worker_lock(lock_path):
                with self.assertRaises(RuntimeError):
                    with local_prefill._single_worker_lock(lock_path):
                        self.fail("a second worker acquired the same ATS lock")


class LocalPrefillTargetTests(unittest.TestCase):
    def test_saved_target_requires_marker_or_exact_job_url(self) -> None:
        marker = f"{local_prefill.TARGET_MARKER_PREFIX}token"
        with patch.object(
            local_prefill,
            "_live_targets",
            return_value={"target": {"id": "target", "type": "page", "url": marker}},
        ):
            self.assertTrue(
                local_prefill._saved_target_is_owned(
                    "http://localhost:9222",
                    target_id="target",
                    marker=marker,
                    job_url=ASHBY_URL_1,
                )
            )
        with patch.object(
            local_prefill,
            "_live_targets",
            return_value={"target": {"id": "target", "type": "page", "url": ASHBY_URL_2}},
        ):
            self.assertFalse(
                local_prefill._saved_target_is_owned(
                    "http://localhost:9222",
                    target_id="target",
                    marker=marker,
                    job_url=ASHBY_URL_1,
                )
            )

    def test_saved_target_accepts_exact_smartrecruiters_oneclick_publication(self) -> None:
        marker = f"{local_prefill.TARGET_MARKER_PREFIX}token"
        with patch.object(
            local_prefill,
            "_live_targets",
            return_value={
                "target": {
                    "id": "target",
                    "type": "page",
                    "url": SMARTRECRUITERS_ONECLICK_URL,
                }
            },
        ):
            self.assertTrue(
                local_prefill._saved_target_is_owned(
                    "http://localhost:9222",
                    target_id="target",
                    marker=marker,
                    job_url=SMARTRECRUITERS_POSTING_URL,
                )
            )

    def test_saved_target_rejects_other_smartrecruiters_publications(self) -> None:
        marker = f"{local_prefill.TARGET_MARKER_PREFIX}token"
        for current_url in (
            SMARTRECRUITERS_ONECLICK_URL.replace("744000141243734", "744000141243735"),
            SMARTRECRUITERS_ONECLICK_URL.replace("744000141243734", "7440001412437340"),
            "https://example.com/oneclick-ui/company/Example/publication/744000141243734",
        ):
            with self.subTest(current_url=current_url):
                with patch.object(
                    local_prefill,
                    "_live_targets",
                    return_value={"target": {"id": "target", "type": "page", "url": current_url}},
                ):
                    self.assertFalse(
                        local_prefill._saved_target_is_owned(
                            "http://localhost:9222",
                            target_id="target",
                            marker=marker,
                            job_url=SMARTRECRUITERS_POSTING_URL,
                        )
                    )

    def test_attempt_environment_identifies_target_and_redacts_email_log(self) -> None:
        captured: dict[str, object] = {}
        email = "candidate@example.com"
        marker = f"{local_prefill.TARGET_MARKER_PREFIX}token"

        def run(command: object, timeout: int, *, env: object) -> SimpleNamespace:
            captured.update(command=command, timeout=timeout, env=env)
            return SimpleNamespace(
                returncode=0,
                stdout=f"selected {email}",
                stderr=f"retry for {email.upper()}",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "attempt.log"
            with (
                patch.object(local_prefill, "_run_command", side_effect=run),
                patch.object(local_prefill, "_read_result", return_value=_prefilled_result()),
            ):
                result, hung = local_prefill._attempt_command(
                    job={
                        "company": "Example",
                        "title": "Product Manager",
                        "url": ASHBY_URL_1,
                        "ats": "ashby",
                    },
                    email=email,
                    result_path=root / "result.json",
                    log_path=log_path,
                    submission_log=root / "submission.json",
                    config_path=root / "config.json",
                    email_pool=root / "emails.json",
                    resume_path=root / "resume.pdf",
                    target_id="target",
                    target_marker=marker,
                    render_timeout_ms=12_000,
                    engine_timeout_seconds=180,
                    resume_timeout_seconds=300,
                    job_timeout_seconds=900,
                )

            self.assertEqual(result["status"], local_prefill.PREFILLED_STATUS)
            self.assertFalse(hung)
            environment = cast(dict[str, str], captured["env"])
            self.assertIsInstance(environment, dict)
            self.assertEqual(environment["JOB_APP_TARGET_ID"], "target")
            self.assertEqual(environment["JOB_APP_TARGET_MARKER"], marker)
            self.assertEqual(environment["JOB_APP_TARGET_URL"], ASHBY_URL_1)
            self.assertEqual(environment["JOB_APP_CDP_ATTACH_TIMEOUT_MS"], "90000")
            self.assertEqual(environment["JOB_APP_RELOAD_TAB"], "0")
            log = log_path.read_text(encoding="utf-8")
            self.assertNotIn(email.casefold(), log.casefold())
            self.assertEqual(log.count("[REDACTED_EMAIL]"), 2)


class LocalPrefillQueueTests(unittest.TestCase):
    def test_ledger_is_refreshed_immediately_before_each_new_job(self) -> None:
        jobs = [
            {"company": "One", "title": "PM", "url": ASHBY_URL_1, "ats": "ashby"},
            {"company": "Two", "title": "PM", "url": ASHBY_URL_2, "ats": "ashby"},
        ]
        state: dict[str, object] = {"records": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(local_prefill, "_single_worker_lock", return_value=nullcontext()),
                patch.object(local_prefill, "_require_shared_cdp"),
                patch.object(local_prefill, "load_email_pool", return_value=["one@example.com"]),
                patch.object(local_prefill, "_load_state", return_value=state),
                patch.object(local_prefill, "_save_state"),
                patch.object(
                    local_prefill,
                    "_submitted_urls",
                    side_effect=[set(), {canonical_job_url(ASHBY_URL_2)}],
                ) as submitted,
                patch.object(local_prefill, "_saved_target_is_owned", return_value=False),
                patch.object(
                    local_prefill,
                    "_new_target",
                    return_value=(f"{local_prefill.TARGET_MARKER_PREFIX}one", "target-one"),
                ),
                patch.object(
                    local_prefill,
                    "_attempt_command",
                    return_value=(_prefilled_result(), False),
                ) as attempt,
            ):
                status = local_prefill._run_queue(
                    _args(),
                    queue_path=(root / "queue.json").resolve(),
                    ats="ashby",
                    jobs=jobs,
                    digest=local_prefill._queue_digest(jobs),
                    output_root=root / "output",
                    state_path=root / "state.json",
                    results_dir=root / "results",
                    submission_log=root / "submission.json",
                    config_path=root / "config.json",
                    email_pool_path=root / "emails.json",
                    resume_path=root / "resume.pdf",
                )

        self.assertEqual(status, 0)
        self.assertEqual(submitted.call_count, 2)
        self.assertEqual(attempt.call_count, 1)
        records = cast(dict[str, dict[str, object]], state["records"])
        self.assertEqual(
            records[canonical_job_url(ASHBY_URL_2)]["status"],
            "SKIPPED_SUBMITTED",
        )

    def test_reload_and_replacement_are_durable_and_reuse_one_email(self) -> None:
        jobs = [{"company": "One", "title": "PM", "url": ASHBY_URL_1, "ats": "ashby"}]
        state: dict[str, object] = {"records": {}}
        saves: list[dict[str, object]] = []
        events: list[str] = []

        def save(_path: Path, current: dict[str, object]) -> None:
            records = cast(dict[str, dict[str, object]], current["records"])
            record = records.get(canonical_job_url(ASHBY_URL_1), {})
            saves.append(deepcopy(record))
            if isinstance(record, dict) and record.get("stale_target_id"):
                events.append("replacement-persisted")

        def close(_endpoint: str, target_id: str) -> None:
            events.append(f"closed:{target_id}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(local_prefill, "_single_worker_lock", return_value=nullcontext()),
                patch.object(local_prefill, "_require_shared_cdp"),
                patch.object(local_prefill, "load_email_pool", return_value=["same@example.com"]),
                patch.object(local_prefill, "_load_state", return_value=state),
                patch.object(local_prefill, "_save_state", side_effect=save),
                patch.object(local_prefill, "_submitted_urls", return_value=set()),
                patch.object(
                    local_prefill,
                    "_saved_target_is_owned",
                    side_effect=[False, True, True],
                ),
                patch.object(
                    local_prefill,
                    "_new_target",
                    side_effect=[
                        (f"{local_prefill.TARGET_MARKER_PREFIX}old", "old-target"),
                        (f"{local_prefill.TARGET_MARKER_PREFIX}new", "new-target"),
                    ],
                ),
                patch.object(local_prefill, "reload_background_tab") as reload_tab,
                patch.object(local_prefill, "_live_targets", return_value={}),
                patch.object(local_prefill, "close_background_tab", side_effect=close),
                patch.object(
                    local_prefill,
                    "_attempt_command",
                    side_effect=[
                        ({"status": "TIMED_OUT", "detail": "timeout"}, True),
                        ({"status": "TIMED_OUT", "detail": "timeout"}, True),
                        (_prefilled_result(), False),
                    ],
                ) as attempt,
            ):
                status = local_prefill._run_queue(
                    _args(),
                    queue_path=(root / "queue.json").resolve(),
                    ats="ashby",
                    jobs=jobs,
                    digest=local_prefill._queue_digest(jobs),
                    output_root=root / "output",
                    state_path=root / "state.json",
                    results_dir=root / "results",
                    submission_log=root / "submission.json",
                    config_path=root / "config.json",
                    email_pool_path=root / "emails.json",
                    resume_path=root / "resume.pdf",
                )

        self.assertEqual(status, 0)
        reload_tab.assert_called_once_with("http://localhost:9222", "old-target")
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(
            [attempt_call.kwargs["email"] for attempt_call in attempt.call_args_list],
            ["same@example.com"] * 3,
        )
        self.assertEqual(
            [attempt_call.kwargs["target_id"] for attempt_call in attempt.call_args_list],
            ["old-target", "old-target", "new-target"],
        )
        self.assertIn("replacement-persisted", events)
        self.assertIn("closed:old-target", events)
        self.assertLess(events.index("replacement-persisted"), events.index("closed:old-target"))
        self.assertTrue(any(saved.get("reload_attempted") for saved in saves))
        self.assertTrue(any(saved.get("reload_completed") for saved in saves))


if __name__ == "__main__":
    unittest.main()
