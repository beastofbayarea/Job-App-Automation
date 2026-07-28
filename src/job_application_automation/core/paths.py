"""Shared filesystem locations for the two-level project layout."""

from __future__ import annotations

from pathlib import Path

# The implementation package lives below ``src`` and is launched through the
# single source-tree command runner. Keep exported paths anchored at the project
# layout so config, data, output, and subprocess references stay predictable.
PACKAGE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PACKAGE_DIR.parent
CLI_ENTRYPOINT = SRC_DIR / "job_automation.py"
PROJECT_ROOT = SRC_DIR.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"


def resolve_existing(path: str | Path, *search_dirs: Path) -> Path:
    """Resolve an absolute path or search named project directories in order."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    for directory in search_dirs:
        resolved = directory / candidate
        if resolved.exists():
            return resolved
    return (search_dirs[0] / candidate) if search_dirs else (PROJECT_ROOT / candidate)


def resolve_project_dir(path: str | Path, default: Path = OUTPUT_DIR) -> Path:
    """Resolve a configurable directory relative to the project root."""
    raw = str(path).strip()
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
