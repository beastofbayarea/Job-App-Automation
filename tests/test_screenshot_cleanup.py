from __future__ import annotations

from pathlib import Path

import pytest

from job_application_automation.core.screenshots import (
    APPLICATION_SCREENSHOT_DIR_ENV,
    active_screenshot_directory,
    cleanup_application_screenshot_directory,
    create_application_screenshot_directory,
)


def test_active_screenshot_directory_honors_per_application_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "isolated"
    monkeypatch.setenv(APPLICATION_SCREENSHOT_DIR_ENV, str(override))

    assert active_screenshot_directory(tmp_path / "legacy") == override


def test_application_screenshot_directory_is_fully_removed(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    screenshot_dir = create_application_screenshot_directory(output_root=output_root)
    nested = screenshot_dir / "nested"
    nested.mkdir()
    (screenshot_dir / "success.png").write_bytes(b"png")
    (nested / "partial.capture").write_bytes(b"incomplete")

    files_deleted, bytes_deleted = cleanup_application_screenshot_directory(
        screenshot_dir,
        output_root=output_root,
    )

    assert (files_deleted, bytes_deleted) == (2, 13)
    assert not screenshot_dir.exists()


@pytest.mark.parametrize("target_kind", ["root", "outside"])
def test_screenshot_cleanup_refuses_broad_or_external_targets(
    tmp_path: Path,
    target_kind: str,
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    target = output_root if target_kind == "root" else tmp_path / "outside"
    target.mkdir(exist_ok=True)

    with pytest.raises(ValueError):
        cleanup_application_screenshot_directory(target, output_root=output_root)

    assert target.exists()
