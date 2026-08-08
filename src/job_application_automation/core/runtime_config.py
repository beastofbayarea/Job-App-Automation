"""Load typed operational settings from one checkout configuration file."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .foundation import ConfigurationError
from .foundation import CONFIG_DIR, PROJECT_ROOT
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


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    """Load the required checkout configuration or an explicit file."""
    requested_path = RUNTIME_CONFIG_FILE if path is None else Path(path)
    config_path = requested_path.expanduser().resolve()
    return RuntimeConfig.from_mapping(_read_json_file(config_path))


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
    "GmailSettings",
    "ObservabilitySettings",
    "RUNTIME_CONFIG",
    "RUNTIME_CONFIG_FILE",
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
