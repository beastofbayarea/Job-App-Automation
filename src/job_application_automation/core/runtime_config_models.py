"""Frozen, dependency-free models for schema-version-one runtime settings."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import ClassVar

from .foundation import ConfigurationError


def _object_mapping(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"runtime config {path} must be an object")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigurationError(f"runtime config {path} keys must be non-empty strings")
        normalized[key] = item
    return normalized


def _strict_mapping(
    value: object,
    path: str,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> dict[str, object]:
    values = _object_mapping(value, path)
    required_keys = set(required)
    allowed_keys = required_keys.union(optional)
    missing = required_keys.difference(values)
    unknown = set(values).difference(allowed_keys)
    if missing:
        raise ConfigurationError(
            f"runtime config {path} is missing required keys: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ConfigurationError(
            f"runtime config {path} has unknown keys: {', '.join(sorted(unknown))}"
        )
    return values


def _string(values: Mapping[str, object], path: str, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"runtime config {path}.{key} must be a non-empty string")
    return value


def _choice(
    values: Mapping[str, object],
    path: str,
    key: str,
    *,
    allowed: frozenset[str],
) -> str:
    value = _string(values, path, key)
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigurationError(f"runtime config {path}.{key} must be one of: {choices}")
    return value


def _positive_integer(values: Mapping[str, object], path: str, key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"runtime config {path}.{key} must be a positive integer")
    return value


def _nonnegative_integer(values: Mapping[str, object], path: str, key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(f"runtime config {path}.{key} must be a non-negative integer")
    return value


def _positive_number(values: Mapping[str, object], path: str, key: str) -> float:
    value = values.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise ConfigurationError(f"runtime config {path}.{key} must be a positive number")
    return value


def _nonnegative_number(values: Mapping[str, object], path: str, key: str) -> float:
    value = values.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise ConfigurationError(f"runtime config {path}.{key} must be a non-negative number")
    return value


def _boolean(values: Mapping[str, object], path: str, key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise ConfigurationError(f"runtime config {path}.{key} must be a boolean")
    return value


def _strings(values: Mapping[str, object], path: str, key: str) -> tuple[str, ...]:
    value = values.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ConfigurationError(f"runtime config {path}.{key} must be a non-empty string array")
    return tuple(value)


def _optional_positive_integer(values: Mapping[str, object], path: str, key: str) -> int | None:
    return _positive_integer(values, path, key) if key in values else None


def _optional_nonnegative_integer(values: Mapping[str, object], path: str, key: str) -> int | None:
    return _nonnegative_integer(values, path, key) if key in values else None


def _optional_positive_number(values: Mapping[str, object], path: str, key: str) -> float | None:
    return _positive_number(values, path, key) if key in values else None


def _optional_strings(values: Mapping[str, object], path: str, key: str) -> tuple[str, ...] | None:
    return _strings(values, path, key) if key in values else None


def _string_tuple_mapping(value: object, path: str) -> Mapping[str, tuple[str, ...]]:
    values = _object_mapping(value, path)
    normalized: dict[str, tuple[str, ...]] = {}
    for key, items in values.items():
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            raise ConfigurationError(
                f"runtime config {path}.{key} must be a non-empty string array"
            )
        normalized[key] = tuple(items)
    if not normalized:
        raise ConfigurationError(f"runtime config {path} must be a non-empty object")
    return MappingProxyType(normalized)


def _string_mapping(
    value: object,
    path: str,
    *,
    required: tuple[str, ...] = (),
) -> Mapping[str, str]:
    values = (
        _strict_mapping(value, path, required=required)
        if required
        else _object_mapping(value, path)
    )
    normalized: dict[str, str] = {}
    for key in values:
        normalized[key] = _string(values, path, key)
    if not normalized:
        raise ConfigurationError(f"runtime config {path} must be a non-empty object")
    return MappingProxyType(normalized)


def _mutable_value(value: object) -> object:
    if isinstance(value, RuntimeSection):
        return value.to_mapping()
    if isinstance(value, Mapping):
        return {str(key): _mutable_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_value(item) for item in value]
    return value


class RuntimeSection(Mapping[str, object]):
    """Mapping-compatible base for immutable typed configuration sections."""

    _mapping_fields: ClassVar[tuple[str, ...]] = ()

    def to_mapping(self) -> dict[str, object]:
        return {name: _mutable_value(getattr(self, name)) for name in self._mapping_fields}

    def __getitem__(self, key: str) -> object:
        return self.to_mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_mapping())

    def __len__(self) -> int:
        return len(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ApplicationSettings(RuntimeSection):
    """Paths, process bounds, and batch controls for application workflows."""

    tracker_file: str
    base_resume_file: str
    resume_source_file: str
    results_file: str
    submission_log_file: str
    queue_progress_file: str
    vps_application_results_dir: str
    vps_application_state_file: str
    vps_application_failure_report: str
    vps_job_backlog_file: str
    candidate_email_pool_file: str
    seo_config_file: str
    engine_timeout_seconds: int
    resume_timeout_seconds: int
    queue_timeout_seconds: int
    vps_max_document_jobs: int
    vps_document_retry_jobs: int
    vps_max_attempts_per_ats: int
    default_start_date_offset_days: int

    _mapping_fields = (
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
        "engine_timeout_seconds",
        "resume_timeout_seconds",
        "queue_timeout_seconds",
        "vps_max_document_jobs",
        "vps_document_retry_jobs",
        "vps_max_attempts_per_ats",
        "default_start_date_offset_days",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ApplicationSettings:
        values = _strict_mapping(value, "application", required=cls._mapping_fields)
        settings = cls(
            tracker_file=_string(values, "application", "tracker_file"),
            base_resume_file=_string(values, "application", "base_resume_file"),
            resume_source_file=_string(values, "application", "resume_source_file"),
            results_file=_string(values, "application", "results_file"),
            submission_log_file=_string(values, "application", "submission_log_file"),
            queue_progress_file=_string(values, "application", "queue_progress_file"),
            vps_application_results_dir=_string(
                values, "application", "vps_application_results_dir"
            ),
            vps_application_state_file=_string(values, "application", "vps_application_state_file"),
            vps_application_failure_report=_string(
                values, "application", "vps_application_failure_report"
            ),
            vps_job_backlog_file=_string(values, "application", "vps_job_backlog_file"),
            candidate_email_pool_file=_string(values, "application", "candidate_email_pool_file"),
            seo_config_file=_string(values, "application", "seo_config_file"),
            engine_timeout_seconds=_positive_integer(
                values, "application", "engine_timeout_seconds"
            ),
            resume_timeout_seconds=_positive_integer(
                values, "application", "resume_timeout_seconds"
            ),
            queue_timeout_seconds=_positive_integer(values, "application", "queue_timeout_seconds"),
            vps_max_document_jobs=_positive_integer(values, "application", "vps_max_document_jobs"),
            vps_document_retry_jobs=_nonnegative_integer(
                values, "application", "vps_document_retry_jobs"
            ),
            vps_max_attempts_per_ats=_positive_integer(
                values, "application", "vps_max_attempts_per_ats"
            ),
            default_start_date_offset_days=_positive_integer(
                values, "application", "default_start_date_offset_days"
            ),
        )
        if settings.vps_document_retry_jobs > settings.vps_max_document_jobs:
            raise ConfigurationError(
                "runtime config application.vps_document_retry_jobs cannot exceed "
                "vps_max_document_jobs"
            )
        return settings


@dataclass(frozen=True, slots=True)
class BrowserRuntimeSettings(RuntimeSection):
    """Shared browser-session configuration."""

    cdp_endpoint: str

    _mapping_fields = ("cdp_endpoint",)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BrowserRuntimeSettings:
        values = _strict_mapping(value, "browser", required=cls._mapping_fields)
        return cls(cdp_endpoint=_string(values, "browser", "cdp_endpoint"))


@dataclass(frozen=True, slots=True)
class VertexSettings(RuntimeSection):
    """Vertex/Gemini model and retry settings."""

    project_id: str
    location: str
    model: str
    service_account_file: str
    max_attempts: int
    retry_delay_seconds: float
    request_timeout_ms: int
    job_text_limit: int
    job_navigation_timeout_ms: int

    _mapping_fields = (
        "project_id",
        "location",
        "model",
        "service_account_file",
        "max_attempts",
        "retry_delay_seconds",
        "request_timeout_ms",
        "job_text_limit",
        "job_navigation_timeout_ms",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> VertexSettings:
        values = _strict_mapping(value, "vertex", required=cls._mapping_fields)
        return cls(
            project_id=_string(values, "vertex", "project_id"),
            location=_string(values, "vertex", "location"),
            model=_string(values, "vertex", "model"),
            service_account_file=_string(values, "vertex", "service_account_file"),
            max_attempts=_positive_integer(values, "vertex", "max_attempts"),
            retry_delay_seconds=_nonnegative_number(values, "vertex", "retry_delay_seconds"),
            request_timeout_ms=_positive_integer(values, "vertex", "request_timeout_ms"),
            job_text_limit=_positive_integer(values, "vertex", "job_text_limit"),
            job_navigation_timeout_ms=_positive_integer(
                values, "vertex", "job_navigation_timeout_ms"
            ),
        )


@dataclass(frozen=True, slots=True)
class ResumeSettings(RuntimeSection):
    """Resume generation, scoring, and cache settings."""

    cache_file: str
    llm_min_interval_seconds: float
    max_retries: int
    minimum_score: int
    minimum_total_bullets: int
    original_character_count: int
    original_page_height: float
    persistent_cache_enabled: bool

    _mapping_fields = (
        "cache_file",
        "llm_min_interval_seconds",
        "max_retries",
        "minimum_score",
        "minimum_total_bullets",
        "original_character_count",
        "original_page_height",
        "persistent_cache_enabled",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResumeSettings:
        values = _strict_mapping(value, "resume", required=cls._mapping_fields)
        return cls(
            cache_file=_string(values, "resume", "cache_file"),
            llm_min_interval_seconds=_nonnegative_number(
                values, "resume", "llm_min_interval_seconds"
            ),
            max_retries=_positive_integer(values, "resume", "max_retries"),
            minimum_score=_positive_integer(values, "resume", "minimum_score"),
            minimum_total_bullets=_positive_integer(values, "resume", "minimum_total_bullets"),
            original_character_count=_positive_integer(
                values, "resume", "original_character_count"
            ),
            original_page_height=_positive_number(values, "resume", "original_page_height"),
            persistent_cache_enabled=_boolean(values, "resume", "persistent_cache_enabled"),
        )


@dataclass(frozen=True, slots=True)
class CoverLetterSettings(RuntimeSection):
    """Cover-letter generation and word-budget settings."""

    cache_file: str
    max_retries: int
    minimum_words: int
    maximum_words: int

    _mapping_fields = ("cache_file", "max_retries", "minimum_words", "maximum_words")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CoverLetterSettings:
        values = _strict_mapping(value, "cover_letter", required=cls._mapping_fields)
        settings = cls(
            cache_file=_string(values, "cover_letter", "cache_file"),
            max_retries=_positive_integer(values, "cover_letter", "max_retries"),
            minimum_words=_positive_integer(values, "cover_letter", "minimum_words"),
            maximum_words=_positive_integer(values, "cover_letter", "maximum_words"),
        )
        if settings.maximum_words <= settings.minimum_words:
            raise ConfigurationError(
                "runtime config cover_letter.maximum_words must be greater than minimum_words"
            )
        return settings


@dataclass(frozen=True, slots=True)
class GmailSettings(RuntimeSection):
    """Gmail authentication paths and verification timing controls."""

    credentials_file: str
    token_file: str
    verification_history_file: str
    verification_poll_timeout_seconds: int
    greenhouse_security_code_poll_timeout_seconds: int
    greenhouse_security_code_wait_ms: int

    _mapping_fields = (
        "credentials_file",
        "token_file",
        "verification_history_file",
        "verification_poll_timeout_seconds",
        "greenhouse_security_code_poll_timeout_seconds",
        "greenhouse_security_code_wait_ms",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> GmailSettings:
        values = _strict_mapping(value, "gmail", required=cls._mapping_fields)
        return cls(
            credentials_file=_string(values, "gmail", "credentials_file"),
            token_file=_string(values, "gmail", "token_file"),
            verification_history_file=_string(values, "gmail", "verification_history_file"),
            verification_poll_timeout_seconds=_positive_integer(
                values, "gmail", "verification_poll_timeout_seconds"
            ),
            greenhouse_security_code_poll_timeout_seconds=_positive_integer(
                values,
                "gmail",
                "greenhouse_security_code_poll_timeout_seconds",
            ),
            greenhouse_security_code_wait_ms=_positive_integer(
                values, "gmail", "greenhouse_security_code_wait_ms"
            ),
        )


_ASHBY_REQUIRED_FIELDS = (
    "default_timeout_ms",
    "navigation_timeout_ms",
    "network_idle_timeout_ms",
    "max_form_steps",
    "max_submit_attempts",
    "screenshot_dir",
    "submission_confirmation_phrases",
    "submission_failure_phrases",
)
_ASHBY_LEGACY_WORKER_FIELDS = (
    "continuous_sleep_min_seconds",
    "continuous_sleep_max_seconds",
    "continuous_application_limit",
    "continuous_application_window_seconds",
    "spam_rejection_cooldown_seconds",
    "spam_rejection_threshold",
)
_ASHBY_OPTIONAL_FIELDS = (
    *_ASHBY_LEGACY_WORKER_FIELDS,
    "submission_result_timeout_seconds",
    "submission_result_poll_seconds",
    "submission_spam_phrases",
)


@dataclass(frozen=True, slots=True)
class AshbyEngineSettings(RuntimeSection):
    """Ashby engine controls plus schema-v1 worker compatibility fields."""

    default_timeout_ms: int
    navigation_timeout_ms: int
    network_idle_timeout_ms: int
    max_form_steps: int
    max_submit_attempts: int
    screenshot_dir: str
    submission_confirmation_phrases: tuple[str, ...]
    submission_failure_phrases: tuple[str, ...]
    continuous_sleep_min_seconds: int | None = None
    continuous_sleep_max_seconds: int | None = None
    continuous_application_limit: int | None = None
    continuous_application_window_seconds: int | None = None
    spam_rejection_cooldown_seconds: int | None = None
    spam_rejection_threshold: int | None = None
    submission_result_timeout_seconds: float | None = None
    submission_result_poll_seconds: float | None = None
    submission_spam_phrases: tuple[str, ...] | None = None

    _mapping_fields = (*_ASHBY_REQUIRED_FIELDS, *_ASHBY_OPTIONAL_FIELDS)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AshbyEngineSettings:
        values = _strict_mapping(
            value,
            "ashby",
            required=_ASHBY_REQUIRED_FIELDS,
            optional=_ASHBY_OPTIONAL_FIELDS,
        )
        settings = cls(
            default_timeout_ms=_positive_integer(values, "ashby", "default_timeout_ms"),
            navigation_timeout_ms=_positive_integer(values, "ashby", "navigation_timeout_ms"),
            network_idle_timeout_ms=_positive_integer(values, "ashby", "network_idle_timeout_ms"),
            max_form_steps=_positive_integer(values, "ashby", "max_form_steps"),
            max_submit_attempts=_positive_integer(values, "ashby", "max_submit_attempts"),
            screenshot_dir=_string(values, "ashby", "screenshot_dir"),
            submission_confirmation_phrases=_strings(
                values, "ashby", "submission_confirmation_phrases"
            ),
            submission_failure_phrases=_strings(values, "ashby", "submission_failure_phrases"),
            continuous_sleep_min_seconds=_optional_positive_integer(
                values, "ashby", "continuous_sleep_min_seconds"
            ),
            continuous_sleep_max_seconds=_optional_positive_integer(
                values, "ashby", "continuous_sleep_max_seconds"
            ),
            continuous_application_limit=_optional_nonnegative_integer(
                values, "ashby", "continuous_application_limit"
            ),
            continuous_application_window_seconds=_optional_positive_integer(
                values, "ashby", "continuous_application_window_seconds"
            ),
            spam_rejection_cooldown_seconds=_optional_positive_integer(
                values, "ashby", "spam_rejection_cooldown_seconds"
            ),
            spam_rejection_threshold=_optional_positive_integer(
                values, "ashby", "spam_rejection_threshold"
            ),
            submission_result_timeout_seconds=_optional_positive_number(
                values, "ashby", "submission_result_timeout_seconds"
            ),
            submission_result_poll_seconds=_optional_positive_number(
                values, "ashby", "submission_result_poll_seconds"
            ),
            submission_spam_phrases=_optional_strings(values, "ashby", "submission_spam_phrases"),
        )
        sleep_min = settings.continuous_sleep_min_seconds or 120
        sleep_max = settings.continuous_sleep_max_seconds or 300
        if sleep_min > sleep_max:
            raise ConfigurationError(
                "runtime config ashby.continuous_sleep_min_seconds cannot exceed "
                "continuous_sleep_max_seconds"
            )
        result_timeout = settings.submission_result_timeout_seconds or 15.0
        result_poll = settings.submission_result_poll_seconds or 0.5
        if result_poll > result_timeout:
            raise ConfigurationError(
                "runtime config ashby.submission_result_poll_seconds cannot exceed "
                "submission_result_timeout_seconds"
            )
        return settings

    def to_mapping(self) -> dict[str, object]:
        return {
            key: value
            for key, value in RuntimeSection.to_mapping(self).items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class SearchDefaultsSettings(RuntimeSection):
    """Default command arguments for ATS search."""

    days: int
    discovery_mode: str
    max_discovery_queries: int
    discovery_timelimit: str
    match_mode: str
    max_career_pages: int
    scrape_discovered_pages: str
    live_check_target: str
    output_file: str
    coverage_report_file: str
    cache_file: str
    discovery_region: str
    search_backend: str
    search_retries: int
    results_per_query: int
    timeout_seconds: float
    delay_seconds: float
    max_lever_pages: int
    max_fallback_pages: int
    show_results: int
    async_timeout_seconds: float
    user_agent: str

    _mapping_fields = (
        "days",
        "discovery_mode",
        "max_discovery_queries",
        "discovery_timelimit",
        "match_mode",
        "max_career_pages",
        "scrape_discovered_pages",
        "live_check_target",
        "output_file",
        "coverage_report_file",
        "cache_file",
        "discovery_region",
        "search_backend",
        "search_retries",
        "results_per_query",
        "timeout_seconds",
        "delay_seconds",
        "max_lever_pages",
        "max_fallback_pages",
        "show_results",
        "async_timeout_seconds",
        "user_agent",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SearchDefaultsSettings:
        values = _strict_mapping(value, "search.defaults", required=cls._mapping_fields)
        return cls(
            days=_nonnegative_integer(values, "search.defaults", "days"),
            discovery_mode=_choice(
                values,
                "search.defaults",
                "discovery_mode",
                allowed=frozenset({"focused", "expanded", "exhaustive"}),
            ),
            max_discovery_queries=_nonnegative_integer(
                values, "search.defaults", "max_discovery_queries"
            ),
            discovery_timelimit=_choice(
                values,
                "search.defaults",
                "discovery_timelimit",
                allowed=frozenset({"auto", "none"}),
            ),
            match_mode=_choice(
                values,
                "search.defaults",
                "match_mode",
                allowed=frozenset({"strict", "expanded"}),
            ),
            max_career_pages=_nonnegative_integer(values, "search.defaults", "max_career_pages"),
            scrape_discovered_pages=_choice(
                values,
                "search.defaults",
                "scrape_discovered_pages",
                allowed=frozenset({"none", "failed-feed", "all"}),
            ),
            live_check_target=_choice(
                values,
                "search.defaults",
                "live_check_target",
                allowed=frozenset({"listing", "application", "both"}),
            ),
            output_file=_string(values, "search.defaults", "output_file"),
            coverage_report_file=_string(values, "search.defaults", "coverage_report_file"),
            cache_file=_string(values, "search.defaults", "cache_file"),
            discovery_region=_string(values, "search.defaults", "discovery_region"),
            search_backend=_string(values, "search.defaults", "search_backend"),
            search_retries=_nonnegative_integer(values, "search.defaults", "search_retries"),
            results_per_query=_positive_integer(values, "search.defaults", "results_per_query"),
            timeout_seconds=_positive_number(values, "search.defaults", "timeout_seconds"),
            delay_seconds=_positive_number(values, "search.defaults", "delay_seconds"),
            max_lever_pages=_nonnegative_integer(values, "search.defaults", "max_lever_pages"),
            max_fallback_pages=_nonnegative_integer(
                values, "search.defaults", "max_fallback_pages"
            ),
            show_results=_nonnegative_integer(values, "search.defaults", "show_results"),
            async_timeout_seconds=_positive_number(
                values, "search.defaults", "async_timeout_seconds"
            ),
            user_agent=_string(values, "search.defaults", "user_agent"),
        )


_SEARCH_FIELDS = (
    "search_phrase_templates",
    "ai_terms",
    "ai_discovery_terms",
    "default_locations",
    "role_families",
    "role_family_input_aliases",
    "location_aliases",
    "generic_ats_host_suffixes",
    "ats_hosts",
    "ddgs_backends",
    "dead_role_markers",
    "restricted_url_patterns",
    "restricted_board_tokens",
    "workable_short_link_board",
    "provider_api_urls",
    "defaults",
)
_PROVIDER_API_KEYS = (
    "greenhouse",
    "lever_global",
    "lever_eu",
    "ashby",
    "smartrecruiters",
    "workable",
)


@dataclass(frozen=True, slots=True)
class SearchSettings(RuntimeSection):
    """Search vocabulary, provider endpoints, restrictions, and defaults."""

    search_phrase_templates: tuple[str, ...]
    ai_terms: tuple[str, ...]
    ai_discovery_terms: tuple[str, ...]
    default_locations: tuple[str, ...]
    role_families: Mapping[str, tuple[str, ...]]
    role_family_input_aliases: Mapping[str, tuple[str, ...]]
    location_aliases: Mapping[str, tuple[str, ...]]
    generic_ats_host_suffixes: tuple[str, ...]
    ats_hosts: Mapping[str, tuple[str, ...]]
    ddgs_backends: tuple[str, ...]
    dead_role_markers: tuple[str, ...]
    restricted_url_patterns: tuple[str, ...]
    restricted_board_tokens: Mapping[str, tuple[str, ...]]
    workable_short_link_board: str
    provider_api_urls: Mapping[str, str]
    defaults: SearchDefaultsSettings

    _mapping_fields = _SEARCH_FIELDS

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SearchSettings:
        values = _strict_mapping(value, "search", required=cls._mapping_fields)
        role_families = _string_tuple_mapping(values["role_families"], "search.role_families")
        role_family_input_aliases = _string_tuple_mapping(
            values["role_family_input_aliases"],
            "search.role_family_input_aliases",
        )
        undefined_families = set(role_family_input_aliases).difference(role_families)
        if undefined_families:
            raise ConfigurationError(
                "runtime config search.role_family_input_aliases references undefined "
                f"role_families: {', '.join(sorted(undefined_families))}"
            )
        ddgs_backends = _strings(values, "search", "ddgs_backends")
        defaults = SearchDefaultsSettings.from_mapping(
            _object_mapping(values["defaults"], "search.defaults")
        )
        if defaults.search_backend != "all" and defaults.search_backend not in ddgs_backends:
            raise ConfigurationError(
                "runtime config search.defaults.search_backend must be 'all' or a member "
                "of search.ddgs_backends"
            )
        return cls(
            search_phrase_templates=_strings(values, "search", "search_phrase_templates"),
            ai_terms=_strings(values, "search", "ai_terms"),
            ai_discovery_terms=_strings(values, "search", "ai_discovery_terms"),
            default_locations=_strings(values, "search", "default_locations"),
            role_families=role_families,
            role_family_input_aliases=role_family_input_aliases,
            location_aliases=_string_tuple_mapping(
                values["location_aliases"], "search.location_aliases"
            ),
            generic_ats_host_suffixes=_strings(values, "search", "generic_ats_host_suffixes"),
            ats_hosts=_string_tuple_mapping(values["ats_hosts"], "search.ats_hosts"),
            ddgs_backends=ddgs_backends,
            dead_role_markers=_strings(values, "search", "dead_role_markers"),
            restricted_url_patterns=_strings(values, "search", "restricted_url_patterns"),
            restricted_board_tokens=_string_tuple_mapping(
                values["restricted_board_tokens"],
                "search.restricted_board_tokens",
            ),
            workable_short_link_board=_string(values, "search", "workable_short_link_board"),
            provider_api_urls=_string_mapping(
                values["provider_api_urls"],
                "search.provider_api_urls",
                required=_PROVIDER_API_KEYS,
            ),
            defaults=defaults,
        )


@dataclass(frozen=True, slots=True)
class ObservabilitySettings(RuntimeSection):
    """Non-secret defaults for optional operational telemetry."""

    default_environment: str
    flush_timeout_seconds: float

    _mapping_fields = ("default_environment", "flush_timeout_seconds")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ObservabilitySettings:
        values = _strict_mapping(value, "observability", required=cls._mapping_fields)
        return cls(
            default_environment=_string(values, "observability", "default_environment"),
            flush_timeout_seconds=_positive_number(
                values, "observability", "flush_timeout_seconds"
            ),
        )


_WORKER_CONTROL_FIELDS = (
    "sleep_min_seconds",
    "sleep_max_seconds",
    "application_limit",
    "application_window_seconds",
    "document_timeout_seconds",
    "engine_timeout_seconds",
    "application_timeout_seconds",
    "refresh_timeout_seconds",
    "captcha_cooldown_seconds",
    "captcha_threshold",
    "spam_rejection_cooldown_seconds",
    "spam_rejection_threshold",
)


@dataclass(frozen=True, slots=True)
class WorkerControlSettings(RuntimeSection):
    """Fully resolved controls for one direct continuous worker."""

    sleep_min_seconds: int
    sleep_max_seconds: int
    application_limit: int
    application_window_seconds: int
    document_timeout_seconds: int
    engine_timeout_seconds: int
    application_timeout_seconds: int
    refresh_timeout_seconds: int
    captcha_cooldown_seconds: int
    captcha_threshold: int
    spam_rejection_cooldown_seconds: int
    spam_rejection_threshold: int

    _mapping_fields = _WORKER_CONTROL_FIELDS

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        path: str = "continuous_worker.defaults",
    ) -> WorkerControlSettings:
        values = _strict_mapping(value, path, required=cls._mapping_fields)
        settings = cls(
            sleep_min_seconds=_positive_integer(values, path, "sleep_min_seconds"),
            sleep_max_seconds=_positive_integer(values, path, "sleep_max_seconds"),
            application_limit=_nonnegative_integer(values, path, "application_limit"),
            application_window_seconds=_positive_integer(
                values,
                path,
                "application_window_seconds",
            ),
            document_timeout_seconds=_positive_integer(values, path, "document_timeout_seconds"),
            engine_timeout_seconds=_positive_integer(values, path, "engine_timeout_seconds"),
            application_timeout_seconds=_positive_integer(
                values,
                path,
                "application_timeout_seconds",
            ),
            refresh_timeout_seconds=_positive_integer(values, path, "refresh_timeout_seconds"),
            captcha_cooldown_seconds=_positive_integer(
                values,
                path,
                "captcha_cooldown_seconds",
            ),
            captcha_threshold=_positive_integer(values, path, "captcha_threshold"),
            spam_rejection_cooldown_seconds=_positive_integer(
                values,
                path,
                "spam_rejection_cooldown_seconds",
            ),
            spam_rejection_threshold=_positive_integer(
                values,
                path,
                "spam_rejection_threshold",
            ),
        )
        if settings.sleep_min_seconds > settings.sleep_max_seconds:
            raise ConfigurationError(
                f"runtime config {path}.sleep_min_seconds cannot exceed sleep_max_seconds"
            )
        return settings


@dataclass(frozen=True, slots=True)
class WorkerControlOverrides(RuntimeSection):
    """Sparse provider-specific overrides applied to worker defaults."""

    sleep_min_seconds: int | None = None
    sleep_max_seconds: int | None = None
    application_limit: int | None = None
    application_window_seconds: int | None = None
    document_timeout_seconds: int | None = None
    engine_timeout_seconds: int | None = None
    application_timeout_seconds: int | None = None
    refresh_timeout_seconds: int | None = None
    captcha_cooldown_seconds: int | None = None
    captcha_threshold: int | None = None
    spam_rejection_cooldown_seconds: int | None = None
    spam_rejection_threshold: int | None = None

    _mapping_fields = _WORKER_CONTROL_FIELDS

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        path: str,
    ) -> WorkerControlOverrides:
        values = _strict_mapping(value, path, required=(), optional=cls._mapping_fields)
        settings = cls(
            sleep_min_seconds=_optional_positive_integer(values, path, "sleep_min_seconds"),
            sleep_max_seconds=_optional_positive_integer(values, path, "sleep_max_seconds"),
            application_limit=_optional_nonnegative_integer(values, path, "application_limit"),
            application_window_seconds=_optional_positive_integer(
                values, path, "application_window_seconds"
            ),
            document_timeout_seconds=_optional_positive_integer(
                values, path, "document_timeout_seconds"
            ),
            engine_timeout_seconds=_optional_positive_integer(
                values, path, "engine_timeout_seconds"
            ),
            application_timeout_seconds=_optional_positive_integer(
                values, path, "application_timeout_seconds"
            ),
            refresh_timeout_seconds=_optional_positive_integer(
                values, path, "refresh_timeout_seconds"
            ),
            captcha_cooldown_seconds=_optional_positive_integer(
                values, path, "captcha_cooldown_seconds"
            ),
            captcha_threshold=_optional_positive_integer(values, path, "captcha_threshold"),
            spam_rejection_cooldown_seconds=_optional_positive_integer(
                values, path, "spam_rejection_cooldown_seconds"
            ),
            spam_rejection_threshold=_optional_positive_integer(
                values, path, "spam_rejection_threshold"
            ),
        )
        if (
            settings.sleep_min_seconds is not None
            and settings.sleep_max_seconds is not None
            and settings.sleep_min_seconds > settings.sleep_max_seconds
        ):
            raise ConfigurationError(
                f"runtime config {path}.sleep_min_seconds cannot exceed sleep_max_seconds"
            )
        return settings

    def apply(
        self,
        defaults: WorkerControlSettings,
        *,
        path: str = "continuous_worker.providers",
    ) -> WorkerControlSettings:
        resolved = {
            field: getattr(self, field)
            if getattr(self, field) is not None
            else getattr(defaults, field)
            for field in self._mapping_fields
        }
        return WorkerControlSettings.from_mapping(resolved, path=path)

    def to_mapping(self) -> dict[str, object]:
        return {
            key: value
            for key, value in RuntimeSection.to_mapping(self).items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class WorkerSourceSettings(RuntimeSection):
    """Controls specific to search/tracker source workers."""

    sleep_min_seconds: int
    sleep_max_seconds: int
    document_timeout_seconds: int
    engine_timeout_seconds: int
    application_timeout_seconds: int

    _mapping_fields = (
        "sleep_min_seconds",
        "sleep_max_seconds",
        "document_timeout_seconds",
        "engine_timeout_seconds",
        "application_timeout_seconds",
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WorkerSourceSettings:
        values = _strict_mapping(value, "continuous_worker.source", required=cls._mapping_fields)
        settings = cls(
            sleep_min_seconds=_positive_integer(
                values, "continuous_worker.source", "sleep_min_seconds"
            ),
            sleep_max_seconds=_positive_integer(
                values, "continuous_worker.source", "sleep_max_seconds"
            ),
            document_timeout_seconds=_positive_integer(
                values, "continuous_worker.source", "document_timeout_seconds"
            ),
            engine_timeout_seconds=_positive_integer(
                values, "continuous_worker.source", "engine_timeout_seconds"
            ),
            application_timeout_seconds=_positive_integer(
                values,
                "continuous_worker.source",
                "application_timeout_seconds",
            ),
        )
        if settings.sleep_min_seconds > settings.sleep_max_seconds:
            raise ConfigurationError(
                "runtime config continuous_worker.source.sleep_min_seconds cannot exceed "
                "sleep_max_seconds"
            )
        return settings


@dataclass(frozen=True, slots=True)
class ContinuousWorkerSettings(RuntimeSection):
    """Direct/source worker defaults and sparse provider overrides."""

    defaults: WorkerControlSettings
    source: WorkerSourceSettings
    providers: Mapping[str, WorkerControlOverrides]

    _mapping_fields = ("defaults", "source", "providers")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ContinuousWorkerSettings:
        values = _strict_mapping(value, "continuous_worker", required=cls._mapping_fields)
        defaults = WorkerControlSettings.from_mapping(
            _object_mapping(values["defaults"], "continuous_worker.defaults")
        )
        raw_providers = _object_mapping(values["providers"], "continuous_worker.providers")
        providers: dict[str, WorkerControlOverrides] = {}
        for provider, provider_value in raw_providers.items():
            normalized_provider = provider.strip().lower()
            if provider != normalized_provider:
                raise ConfigurationError(
                    "runtime config continuous_worker.providers keys must be normalized lowercase"
                )
            path = f"continuous_worker.providers.{provider}"
            override = WorkerControlOverrides.from_mapping(
                _object_mapping(
                    provider_value,
                    path,
                ),
                path=path,
            )
            override.apply(defaults, path=path)
            providers[provider] = override
        return cls(
            defaults=defaults,
            source=WorkerSourceSettings.from_mapping(
                _object_mapping(values["source"], "continuous_worker.source")
            ),
            providers=MappingProxyType(providers),
        )

    def for_provider(self, provider: str) -> WorkerControlSettings:
        normalized = provider.strip().lower()
        override = self.providers.get(normalized)
        return (
            override.apply(
                self.defaults,
                path=f"continuous_worker.providers.{normalized}",
            )
            if override is not None
            else self.defaults
        )


def default_observability_mapping() -> dict[str, object]:
    """Return schema-v1 defaults for configs created before this section existed."""
    return {
        "default_environment": "production",
        "flush_timeout_seconds": 2.0,
    }


def default_continuous_worker_mapping(engine_timeout_seconds: int) -> dict[str, object]:
    """Return behavior-preserving defaults for legacy schema-v1 documents."""
    return {
        "defaults": {
            "sleep_min_seconds": 120,
            "sleep_max_seconds": 300,
            "application_limit": 0,
            "application_window_seconds": 86_400,
            "document_timeout_seconds": 1_800,
            "engine_timeout_seconds": engine_timeout_seconds,
            "application_timeout_seconds": 420,
            "refresh_timeout_seconds": 3_600,
            "captcha_cooldown_seconds": 86_400,
            "captcha_threshold": 2,
            "spam_rejection_cooldown_seconds": 86_400,
            "spam_rejection_threshold": 1,
        },
        "source": {
            "sleep_min_seconds": 5,
            "sleep_max_seconds": 15,
            "document_timeout_seconds": 1_800,
            "engine_timeout_seconds": engine_timeout_seconds,
            "application_timeout_seconds": 420,
        },
        "providers": {},
    }


_LEGACY_ASHBY_WORKER_TRANSLATIONS = {
    "continuous_sleep_min_seconds": "sleep_min_seconds",
    "continuous_sleep_max_seconds": "sleep_max_seconds",
    "continuous_application_limit": "application_limit",
    "continuous_application_window_seconds": "application_window_seconds",
    "spam_rejection_cooldown_seconds": "spam_rejection_cooldown_seconds",
    "spam_rejection_threshold": "spam_rejection_threshold",
}


def _legacy_ashby_worker_overrides(settings: AshbyEngineSettings) -> dict[str, object]:
    return {
        output_name: value
        for legacy_name, output_name in _LEGACY_ASHBY_WORKER_TRANSLATIONS.items()
        if (value := getattr(settings, legacy_name)) is not None
    }


def _reject_conflicting_ashby_worker_settings(
    legacy_overrides: Mapping[str, object],
    explicit_overrides: Mapping[str, object],
) -> None:
    legacy_names = {
        output_name: legacy_name
        for legacy_name, output_name in _LEGACY_ASHBY_WORKER_TRANSLATIONS.items()
    }
    for field in sorted(set(legacy_overrides).intersection(explicit_overrides)):
        if legacy_overrides[field] == explicit_overrides[field]:
            continue
        raise ConfigurationError(
            "runtime config worker setting conflict between "
            f"ashby.{legacy_names[field]} and continuous_worker.providers.ashby.{field}"
        )


_RUNTIME_REQUIRED_SECTIONS = (
    "application",
    "browser",
    "vertex",
    "resume",
    "cover_letter",
    "search",
    "ashby",
    "gmail",
)
_RUNTIME_OPTIONAL_SECTIONS = ("observability", "continuous_worker")


@dataclass(frozen=True, slots=True)
class RuntimeConfig(RuntimeSection):
    """Complete immutable schema-v1 runtime configuration."""

    schema_version: int
    application: ApplicationSettings
    browser: BrowserRuntimeSettings
    vertex: VertexSettings
    resume: ResumeSettings
    cover_letter: CoverLetterSettings
    search: SearchSettings
    ashby: AshbyEngineSettings
    gmail: GmailSettings
    observability: ObservabilitySettings
    continuous_worker: ContinuousWorkerSettings

    _mapping_fields = (
        "schema_version",
        *_RUNTIME_REQUIRED_SECTIONS,
        *_RUNTIME_OPTIONAL_SECTIONS,
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RuntimeConfig:
        values = _strict_mapping(
            value,
            "root",
            required=("schema_version", *_RUNTIME_REQUIRED_SECTIONS),
            optional=_RUNTIME_OPTIONAL_SECTIONS,
        )
        schema_version = values["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            raise ConfigurationError("runtime config schema_version must be the integer 1")

        application = ApplicationSettings.from_mapping(
            _object_mapping(values["application"], "application")
        )
        ashby = AshbyEngineSettings.from_mapping(_object_mapping(values["ashby"], "ashby"))
        raw_continuous = (
            _object_mapping(values["continuous_worker"], "continuous_worker")
            if "continuous_worker" in values
            else default_continuous_worker_mapping(application.queue_timeout_seconds)
        )
        providers = _object_mapping(raw_continuous.get("providers"), "continuous_worker.providers")
        legacy_overrides = _legacy_ashby_worker_overrides(ashby)
        explicit_ashby = (
            _object_mapping(providers["ashby"], "continuous_worker.providers.ashby")
            if "ashby" in providers
            else {}
        )
        _reject_conflicting_ashby_worker_settings(legacy_overrides, explicit_ashby)
        if legacy_overrides or explicit_ashby:
            providers["ashby"] = {**legacy_overrides, **explicit_ashby}
        raw_continuous["providers"] = providers

        raw_observability = (
            _object_mapping(values["observability"], "observability")
            if "observability" in values
            else default_observability_mapping()
        )
        return cls(
            schema_version=1,
            application=application,
            browser=BrowserRuntimeSettings.from_mapping(
                _object_mapping(values["browser"], "browser")
            ),
            vertex=VertexSettings.from_mapping(_object_mapping(values["vertex"], "vertex")),
            resume=ResumeSettings.from_mapping(_object_mapping(values["resume"], "resume")),
            cover_letter=CoverLetterSettings.from_mapping(
                _object_mapping(values["cover_letter"], "cover_letter")
            ),
            search=SearchSettings.from_mapping(_object_mapping(values["search"], "search")),
            ashby=ashby,
            gmail=GmailSettings.from_mapping(_object_mapping(values["gmail"], "gmail")),
            observability=ObservabilitySettings.from_mapping(raw_observability),
            continuous_worker=ContinuousWorkerSettings.from_mapping(raw_continuous),
        )

    def get_section(self, name: str) -> Mapping[str, object]:
        """Return a named top-level section or an empty mapping if unknown."""
        section = getattr(self, name, None)
        return section if isinstance(section, RuntimeSection) else MappingProxyType({})

    def get_setting(self, section: str, key: str, default: object = None) -> object:
        """Return a setting through the legacy dynamic mapping interface."""
        return self.get_section(section).get(key, default)
