"""Validated non-secret operational settings loaded from ``config/runtime_config.json``.

Candidate identity and application-answer policies remain in the ignored
candidate profile.  This module owns the separately tracked, deployment-level
defaults shared by the application, browser, resume, and mail workflows.
"""

from __future__ import annotations

import json
from importlib import resources
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from collections.abc import Mapping

from .paths import CONFIG_DIR, PROJECT_ROOT


RUNTIME_CONFIG_FILE = CONFIG_DIR / "runtime_config.json"
DEFAULT_RUNTIME_CONFIG_FILE = Path(
    str(resources.files("job_application_automation").joinpath("resources/runtime_config.json"))
)


def _mapping(document: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"runtime config {key} must be an object")
    return MappingProxyType(dict(value))


def _string(section: Mapping[str, Any], section_name: str, key: str) -> None:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"runtime config {section_name}.{key} must be a non-empty string")


def _integer(section: Mapping[str, Any], section_name: str, key: str) -> None:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"runtime config {section_name}.{key} must be a positive integer")


def _nonnegative_integer(section: Mapping[str, Any], section_name: str, key: str) -> None:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"runtime config {section_name}.{key} must be a non-negative integer")


def _number(section: Mapping[str, Any], section_name: str, key: str) -> None:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"runtime config {section_name}.{key} must be a non-negative number")


def _positive_number(section: Mapping[str, Any], section_name: str, key: str) -> None:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"runtime config {section_name}.{key} must be a positive number")


def _boolean(section: Mapping[str, Any], section_name: str, key: str) -> None:
    if not isinstance(section.get(key), bool):
        raise ValueError(f"runtime config {section_name}.{key} must be a boolean")


def _strings(section: Mapping[str, Any], section_name: str, key: str) -> None:
    value = section.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"runtime config {section_name}.{key} must be a non-empty string array")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Validated runtime configuration with immutable top-level sections."""

    application: Mapping[str, Any]
    browser: Mapping[str, Any]
    vertex: Mapping[str, Any]
    resume: Mapping[str, Any]
    cover_letter: Mapping[str, Any]
    ashby: Mapping[str, Any]
    gmail: Mapping[str, Any]

    def get_section(self, name: str) -> Mapping[str, Any]:
        """Return a named top-level config section or an empty mapping if unknown."""
        section = getattr(self, name, None)
        return section if isinstance(section, Mapping) else MappingProxyType({})

    def get_setting(self, section: str, key: str, default: Any = None) -> Any:
        """Return a validated setting value from a section, falling back to default."""
        return self.get_section(section).get(key, default)


def load_runtime_config(path: Path | None = None) -> RuntimeConfig:
    """Load local settings, falling back to the packaged safe defaults.

    Source checkouts normally provide ``config/runtime_config.json``. Installed
    commands instead look for that file in the current working directory and
    use the bundled defaults when a local override has not been created yet.
    """
    requested_path = RUNTIME_CONFIG_FILE if path is None else Path(path)
    config_path = requested_path.expanduser().resolve()
    if path is None and not config_path.is_file():
        config_path = DEFAULT_RUNTIME_CONFIG_FILE
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        if (
            path is None
            and config_path != DEFAULT_RUNTIME_CONFIG_FILE
            and DEFAULT_RUNTIME_CONFIG_FILE.is_file()
        ):
            with DEFAULT_RUNTIME_CONFIG_FILE.open("r", encoding="utf-8") as stream:
                document = json.load(stream)
        else:
            raise ValueError(
                f"runtime config contains invalid JSON or cannot be read: {config_path}"
            ) from exc
    if not isinstance(document, Mapping):
        raise ValueError("runtime config root must be an object")
    if document.get("schema_version") != 1:
        raise ValueError("runtime config schema_version must be 1")

    application = _mapping(document, "application")
    browser = _mapping(document, "browser")
    vertex = _mapping(document, "vertex")
    resume = _mapping(document, "resume")
    cover_letter = _mapping(document, "cover_letter")
    ashby = _mapping(document, "ashby")
    gmail = _mapping(document, "gmail")

    for key in (
        "tracker_file",
        "base_resume_file",
        "resume_source_file",
        "results_file",
        "submission_log_file",
        "queue_progress_file",
        "vps_application_results_dir",
        "vps_application_state_file",
        "vps_application_failure_report",
        "vps_job_backlog_file",
        "candidate_email_pool_file",
        "seo_config_file",
    ):
        _string(application, "application", key)
    for key in (
        "engine_timeout_seconds",
        "resume_timeout_seconds",
        "queue_timeout_seconds",
        "vps_max_document_jobs",
        "vps_max_attempts_per_ats",
        "default_start_date_offset_days",
    ):
        _integer(application, "application", key)
    _nonnegative_integer(application, "application", "vps_document_retry_jobs")
    if application["vps_document_retry_jobs"] > application["vps_max_document_jobs"]:
        raise ValueError(
            "runtime config application.vps_document_retry_jobs cannot exceed vps_max_document_jobs"
        )

    _string(browser, "browser", "cdp_endpoint")

    for key in ("project_id", "location", "model", "service_account_file"):
        _string(vertex, "vertex", key)
    for key in ("max_attempts", "job_text_limit", "job_navigation_timeout_ms"):
        _integer(vertex, "vertex", key)
    _number(vertex, "vertex", "retry_delay_seconds")

    _string(resume, "resume", "cache_file")
    for key in (
        "max_retries",
        "minimum_score",
        "minimum_total_bullets",
        "original_character_count",
    ):
        _integer(resume, "resume", key)
    _number(resume, "resume", "llm_min_interval_seconds")
    _positive_number(resume, "resume", "original_page_height")
    _boolean(resume, "resume", "persistent_cache_enabled")

    _string(cover_letter, "cover_letter", "cache_file")
    for key in ("max_retries", "minimum_words", "maximum_words"):
        _integer(cover_letter, "cover_letter", key)
    if cover_letter["maximum_words"] <= cover_letter["minimum_words"]:
        raise ValueError(
            "runtime config cover_letter.maximum_words must be greater than minimum_words"
        )

    for key in (
        "default_timeout_ms",
        "navigation_timeout_ms",
        "network_idle_timeout_ms",
        "max_form_steps",
        "max_submit_attempts",
    ):
        _integer(ashby, "ashby", key)
    for key in (
        "continuous_sleep_min_seconds",
        "continuous_sleep_max_seconds",
        "continuous_application_window_seconds",
        "spam_rejection_cooldown_seconds",
        "spam_rejection_threshold",
    ):
        if key in ashby:
            _integer(ashby, "ashby", key)
    if "continuous_application_limit" in ashby:
        _nonnegative_integer(ashby, "ashby", "continuous_application_limit")
    for key in ("submission_result_timeout_seconds", "submission_result_poll_seconds"):
        if key in ashby:
            _positive_number(ashby, "ashby", key)
    sleep_min_seconds = int(ashby.get("continuous_sleep_min_seconds", 120))
    sleep_max_seconds = int(ashby.get("continuous_sleep_max_seconds", 300))
    if sleep_min_seconds > sleep_max_seconds:
        raise ValueError(
            "runtime config ashby.continuous_sleep_min_seconds cannot exceed "
            "continuous_sleep_max_seconds"
        )
    result_timeout_seconds = float(ashby.get("submission_result_timeout_seconds", 15))
    result_poll_seconds = float(ashby.get("submission_result_poll_seconds", 0.5))
    if result_poll_seconds > result_timeout_seconds:
        raise ValueError(
            "runtime config ashby.submission_result_poll_seconds cannot exceed "
            "submission_result_timeout_seconds"
        )
    _string(ashby, "ashby", "screenshot_dir")
    for key in ("submission_confirmation_phrases", "submission_failure_phrases"):
        _strings(ashby, "ashby", key)
    if "submission_spam_phrases" in ashby:
        _strings(ashby, "ashby", "submission_spam_phrases")

    for key in ("credentials_file", "token_file", "verification_history_file"):
        _string(gmail, "gmail", key)
    for key in (
        "verification_poll_timeout_seconds",
        "greenhouse_security_code_poll_timeout_seconds",
        "greenhouse_security_code_wait_ms",
    ):
        _integer(gmail, "gmail", key)

    return RuntimeConfig(
        application=application,
        browser=browser,
        vertex=vertex,
        resume=resume,
        cover_letter=cover_letter,
        ashby=ashby,
        gmail=gmail,
    )


def resolve_runtime_path(value: str | Path) -> Path:
    """Resolve a runtime-config filesystem value relative to the project root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


try:
    RUNTIME_CONFIG = load_runtime_config()
except Exception:
    RUNTIME_CONFIG = load_runtime_config(DEFAULT_RUNTIME_CONFIG_FILE)
