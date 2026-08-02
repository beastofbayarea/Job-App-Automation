from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from job_application_automation.core.continuous_worker_application import (
    SelectedJobApplicationConfig,
    SelectedJobApplicationDependencies,
    SelectedJobApplicationService,
    default_application_dependencies,
    engine_confirmation_view,
    strictly_confirmed,
)
from job_application_automation.core.continuous_worker_models import CommandOutcome


def _job() -> dict[str, str]:
    url = "https://jobs.ashbyhq.com/example/123"
    return {
        "platform": "ashby",
        "company": "Example",
        "title": "Product Manager",
        "description": "Build useful products.",
        "job_url": url,
        "_application_url": url,
        "_canonical_url": url,
        "live_status": "live",
    }


def _pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-" + b"x" * 1500)


def _config(tmp_path: Path) -> SelectedJobApplicationConfig:
    return SelectedJobApplicationConfig(
        ats_platform="ashby",
        profile=tmp_path / "profile.json",
        email_pool=tmp_path / "emails.json",
        launcher=tmp_path / "launcher.py",
        state_path=tmp_path / "state.json",
        results_dir=tmp_path / "results",
        documents_dir=tmp_path / "documents",
        submission_log=tmp_path / "submission_log.json",
        document_timeout_seconds=60,
        engine_timeout_seconds=30,
        application_timeout_seconds=60,
        backlog_path=tmp_path / "backlog.json",
    )


def test_selected_job_service_applies_exact_object_and_reconciles_ledger(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    defaults = default_application_dependencies()
    selected = _job()
    applied: list[Mapping[str, Any]] = []
    screenshot_directories: list[Path] = []
    pruned: list[str] = []

    def prepare_documents(**kwargs: Any) -> CommandOutcome:
        assert kwargs["job"] is selected
        output_dir = Path(kwargs["output_dir"])
        _pdf(output_dir / "resume.pdf")
        _pdf(output_dir / "cover_letter.pdf")
        return CommandOutcome(0, "documents ready", "")

    def apply_job(**kwargs: Any) -> CommandOutcome:
        assert kwargs["job"] is selected
        applied.append(kwargs["job"])
        screenshot_dir = Path(kwargs["screenshot_dir"])
        screenshot_directories.append(screenshot_dir)
        (screenshot_dir / "confirmation.png").write_bytes(b"proof")
        Path(kwargs["result_path"]).write_text(
            json.dumps(
                [
                    {
                        "success": True,
                        "status": "SUBMITTED & CONFIRMED",
                        "ats": "ashby",
                        "submitted": True,
                        "confirmed": True,
                        "test_mode": False,
                    }
                ]
            ),
            encoding="utf-8",
        )
        config.submission_log.write_text(
            json.dumps(
                {
                    "confirmed": {
                        "status": "SUBMITTED & CONFIRMED",
                        "ats": "ashby",
                        "job_url": selected["job_url"],
                    }
                }
            ),
            encoding="utf-8",
        )
        return CommandOutcome(0, "submitted", "")

    service = SelectedJobApplicationService(
        config=config,
        dependencies=SelectedJobApplicationDependencies(
            prepare_documents=prepare_documents,
            apply_job=apply_job,
            load_email_pool=lambda _path: ["candidate@example.test"],
            choose_email=lambda emails: emails[0],
            now=lambda: "2026-08-02T00:00:00+00:00",
            create_screenshot_directory=defaults.create_screenshot_directory,
            cleanup_screenshot_directory=defaults.cleanup_screenshot_directory,
            prune_backlog=lambda _path, url: pruned.append(url) is None,
        ),
    )

    assert service.process(selected) == "confirmed"
    assert applied == [selected]
    assert pruned == [selected["job_url"]]
    assert len(screenshot_directories) == 1
    assert not screenshot_directories[0].exists()
    state = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert state["jobs"][selected["job_url"]]["status"] == "confirmed"
    assert state["jobs"][selected["job_url"]]["ledger_confirmed"] is True


def test_selected_job_service_skips_existing_exact_confirmation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    selected = _job()
    config.submission_log.write_text(
        json.dumps(
            {
                "confirmed": {
                    "status": "SUBMITTED & CONFIRMED",
                    "ats": "ashby",
                    "job_url": selected["job_url"],
                }
            }
        ),
        encoding="utf-8",
    )

    assert SelectedJobApplicationService(config=config).process(selected) == "no_work"
    assert not config.state_path.exists()


def test_confirmation_view_accepts_realistic_full_orchestrator_snapshot() -> None:
    result = {
        "row": 17,
        "company": "Example",
        "role": "Product Manager",
        "url": _job()["job_url"],
        "ats": "ashby",
        "engine": "ashby.py",
        "resume": "documents/resume.pdf",
        "cover_letter": "documents/cover_letter.pdf",
        "email": "candidate@example.test",
        "success": True,
        "status": "SUBMITTED & CONFIRMED",
        "submitted": True,
        "confirmed": True,
        "test_mode": False,
        "ledger_persisted": True,
        "manual_review_required": False,
        "retry_safe": False,
        "engine_details": {"confirmation_text": "Thank you for applying"},
    }

    assert set(engine_confirmation_view(result)) == {
        "success",
        "status",
        "ats",
        "submitted",
        "confirmed",
        "test_mode",
    }
    assert strictly_confirmed(result) is True


def test_selected_job_service_cleans_screenshots_when_application_raises(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    selected = _job()
    document_dir = config.documents_dir / "ready"
    _pdf(document_dir / "resume.pdf")
    _pdf(document_dir / "cover_letter.pdf")
    config.state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": {
                    selected["job_url"]: {
                        "status": "documents_ready",
                        "job_url": selected["job_url"],
                        "company": selected["company"],
                        "title": selected["title"],
                        "platform": "ashby",
                        "email": "candidate@example.test",
                        "document_dir": str(document_dir),
                        "result_path": str(config.results_dir / "application.json"),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    defaults = default_application_dependencies()
    screenshot_directories: list[Path] = []

    def create_screenshot_directory(*, output_root: str | Path) -> Path:
        path = defaults.create_screenshot_directory(output_root=output_root)
        screenshot_directories.append(path)
        return path

    def raise_during_application(**kwargs: Any) -> CommandOutcome:
        (Path(kwargs["screenshot_dir"]) / "failure.png").write_bytes(b"proof")
        raise RuntimeError("browser crashed")

    service = SelectedJobApplicationService(
        config=config,
        dependencies=SelectedJobApplicationDependencies(
            prepare_documents=lambda **_kwargs: pytest.fail("documents were already ready"),
            apply_job=raise_during_application,
            load_email_pool=lambda _path: pytest.fail("saved email must be reused"),
            choose_email=lambda _emails: pytest.fail("saved email must be reused"),
            now=lambda: "2026-08-02T00:00:00+00:00",
            create_screenshot_directory=create_screenshot_directory,
            cleanup_screenshot_directory=defaults.cleanup_screenshot_directory,
            prune_backlog=lambda _path, _url: False,
        ),
    )

    with pytest.raises(RuntimeError, match="browser crashed"):
        service.process(selected)

    assert len(screenshot_directories) == 1
    assert not screenshot_directories[0].exists()
