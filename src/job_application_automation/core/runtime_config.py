"""Load typed non-secret operational settings from ``config/runtime``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

from .exceptions import ConfigurationError
from .paths import CONFIG_DIR, PROJECT_ROOT
from .runtime_config_models import (
    ApplicationSettings,
    AshbyEngineSettings,
    BrowserRuntimeSettings,
    ContinuousWorkerSettings,
    CoverLetterSettings,
    GmailSettings,
    ObservabilitySettings,
    ResumeSettings,
    RuntimeConfig,
    SearchDefaultsSettings,
    SearchSettings,
    VertexSettings,
    WorkerControlOverrides,
    WorkerControlSettings,
    WorkerSourceSettings,
)


RUNTIME_SECTION_NAMES = (
    "application",
    "browser",
    "vertex",
    "resume",
    "cover_letter",
    "search",
    "ashby",
    "gmail",
    "observability",
    "continuous_worker",
)
OPTIONAL_RUNTIME_SECTION_NAMES = frozenset({"observability", "continuous_worker"})
RUNTIME_CONFIG_DIR = CONFIG_DIR / "runtime"
DEFAULT_RUNTIME_CONFIG_DIR = Path(
    str(resources.files("job_application_automation").joinpath("resources/runtime"))
)
# Compatibility path for callers that explicitly provide a legacy monolithic file.
RUNTIME_CONFIG_FILE = CONFIG_DIR / "runtime_config.json"


def _read_json_file(path: Path) -> Mapping[str, object]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            document: object = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"runtime config contains invalid JSON or cannot be read: {path}"
        ) from exc
    if not isinstance(document, Mapping):
        raise ConfigurationError(f"runtime config file root must be an object: {path}")
    normalized: dict[str, object] = {}
    for key, value in document.items():
        if not isinstance(key, str):
            raise ConfigurationError(f"runtime config file keys must be strings: {path}")
        normalized[key] = value
    return normalized


def _load_split_document(directory: Path) -> Mapping[str, object]:
    allowed_files = {"schema_version.json", *(f"{name}.json" for name in RUNTIME_SECTION_NAMES)}
    required_files = allowed_files.difference(
        f"{name}.json" for name in OPTIONAL_RUNTIME_SECTION_NAMES
    )
    try:
        actual_files = {path.name for path in directory.glob("*.json") if path.is_file()}
    except OSError as exc:
        raise ConfigurationError(f"runtime config directory cannot be read: {directory}") from exc
    missing = required_files.difference(actual_files)
    unexpected = actual_files.difference(allowed_files)
    if missing:
        raise ConfigurationError(
            f"runtime config directory is missing: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise ConfigurationError(
            f"runtime config directory has unexpected JSON files: {', '.join(sorted(unexpected))}"
        )

    document = dict(_read_json_file(directory / "schema_version.json"))
    for section_name in RUNTIME_SECTION_NAMES:
        section_path = directory / f"{section_name}.json"
        if not section_path.is_file() and section_name in OPTIONAL_RUNTIME_SECTION_NAMES:
            continue
        section_document = _read_json_file(section_path)
        if set(section_document) != {section_name}:
            raise ConfigurationError(
                f"runtime config {section_name}.json must contain only the {section_name} object"
            )
        document[section_name] = section_document[section_name]
    return document


def _load_runtime_document(path: Path) -> Mapping[str, object]:
    return _load_split_document(path) if path.is_dir() else _read_json_file(path)


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    """Load checkout settings, package defaults, or a legacy monolithic file."""
    requested_path = RUNTIME_CONFIG_DIR if path is None else Path(path)
    config_path = requested_path.expanduser().resolve()
    if path is None and not config_path.is_dir():
        legacy_path = RUNTIME_CONFIG_FILE.expanduser().resolve()
        config_path = legacy_path if legacy_path.is_file() else DEFAULT_RUNTIME_CONFIG_DIR
    return RuntimeConfig.from_mapping(_load_runtime_document(config_path))


def resolve_runtime_path(value: str | Path) -> Path:
    """Resolve a runtime-config filesystem value relative to the project root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


RUNTIME_CONFIG = load_runtime_config()


__all__ = [
    "ApplicationSettings",
    "AshbyEngineSettings",
    "BrowserRuntimeSettings",
    "ContinuousWorkerSettings",
    "CoverLetterSettings",
    "DEFAULT_RUNTIME_CONFIG_DIR",
    "GmailSettings",
    "ObservabilitySettings",
    "OPTIONAL_RUNTIME_SECTION_NAMES",
    "RUNTIME_CONFIG",
    "RUNTIME_CONFIG_DIR",
    "RUNTIME_CONFIG_FILE",
    "RUNTIME_SECTION_NAMES",
    "ResumeSettings",
    "RuntimeConfig",
    "SearchDefaultsSettings",
    "SearchSettings",
    "VertexSettings",
    "WorkerControlOverrides",
    "WorkerControlSettings",
    "WorkerSourceSettings",
    "load_runtime_config",
    "resolve_runtime_path",
]
