"""Per-application screenshot isolation and cleanup."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .paths import OUTPUT_DIR


APPLICATION_SCREENSHOT_DIR_ENV = "JOB_APP_SCREENSHOT_DIR"
APPLICATION_SCREENSHOT_PARENT = ".application_screenshots"


def active_screenshot_directory(default: str | Path) -> Path:
    """Return the isolated directory selected by the application orchestrator."""
    override = os.environ.get(APPLICATION_SCREENSHOT_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else Path(default).expanduser()


def _validated_child(path: str | Path, output_root: str | Path) -> tuple[Path, Path]:
    target = Path(path).expanduser().resolve()
    root = Path(output_root).expanduser().resolve()
    if target == root:
        raise ValueError("application screenshot directory cannot be the output root")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"application screenshot directory must be inside the output root: {target}"
        ) from exc
    return target, root


def create_application_screenshot_directory(
    *,
    output_root: str | Path = OUTPUT_DIR,
    inherited: str | Path | None = None,
) -> Path:
    """Create or reuse one output-bound screenshot directory for an application."""
    if inherited is not None and str(inherited).strip():
        target, _ = _validated_child(inherited, output_root)
        target.mkdir(parents=True, exist_ok=True)
        return target

    root = Path(output_root).expanduser().resolve()
    parent = root / APPLICATION_SCREENSHOT_PARENT
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="application-", dir=parent))


def cleanup_application_screenshot_directory(
    directory: str | Path,
    *,
    output_root: str | Path = OUTPUT_DIR,
) -> tuple[int, int]:
    """Delete one isolated screenshot directory and return file/byte totals."""
    target, root = _validated_child(directory, output_root)
    if not target.exists():
        return 0, 0
    if not target.is_dir():
        raise ValueError(f"application screenshot path is not a directory: {target}")

    files_deleted = 0
    bytes_deleted = 0
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        files_deleted += 1
        try:
            bytes_deleted += path.stat().st_size
        except OSError:
            pass

    shutil.rmtree(target)
    parent = target.parent
    if parent != root:
        try:
            parent.rmdir()
        except OSError:
            pass
    return files_deleted, bytes_deleted
