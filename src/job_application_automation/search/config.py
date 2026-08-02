"""Typed access to the validated runtime configuration for job search."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..core.runtime_config import RUNTIME_CONFIG, SearchDefaultsSettings, resolve_runtime_path


SEARCH_SETTINGS = RUNTIME_CONFIG.search

SEARCH_PHRASE_TEMPLATES = SEARCH_SETTINGS.search_phrase_templates
DEFAULT_AI_TERMS = SEARCH_SETTINGS.ai_terms
AI_DISCOVERY_TERMS = SEARCH_SETTINGS.ai_discovery_terms
DEFAULT_LOCATION_TERMS = SEARCH_SETTINGS.default_locations
ROLE_FAMILY_VARIANTS = dict(SEARCH_SETTINGS.role_families)
ROLE_FAMILY_INPUT_ALIASES = dict(SEARCH_SETTINGS.role_family_input_aliases)
LOCATION_ALIAS_MAP = dict(SEARCH_SETTINGS.location_aliases)
GENERIC_ATS_HOST_SUFFIXES = SEARCH_SETTINGS.generic_ats_host_suffixes
ATS_SEARCH_HOSTS = dict(SEARCH_SETTINGS.ats_hosts)
SUPPORTED_ATS_PLATFORMS = tuple(ATS_SEARCH_HOSTS)
ALL_DDGS_BACKENDS = SEARCH_SETTINGS.ddgs_backends
DEAD_ROLE_MARKERS = SEARCH_SETTINGS.dead_role_markers
RESTRICTED_URL_PATTERNS = SEARCH_SETTINGS.restricted_url_patterns
WORKABLE_SHORT_LINK_BOARD = SEARCH_SETTINGS.workable_short_link_board
PROVIDER_API_URLS = dict(SEARCH_SETTINGS.provider_api_urls)
RESTRICTED_BOARD_KEYS = {
    (platform, token)
    for platform, tokens in SEARCH_SETTINGS.restricted_board_tokens.items()
    for token in tokens
}


def build_role_alias_map() -> dict[str, tuple[str, ...]]:
    """Build role aliases from configured families to prevent drift."""
    aliases: dict[str, tuple[str, ...]] = {
        "program": ("Program", "Programme"),
        "programme": ("Program", "Programme"),
    }
    for family_name, input_aliases in ROLE_FAMILY_INPUT_ALIASES.items():
        aliases.update(
            {input_alias: ROLE_FAMILY_VARIANTS[family_name] for input_alias in input_aliases}
        )
    return aliases


ROLE_ALIAS_MAP = build_role_alias_map()


@dataclass(frozen=True, slots=True)
class SearchDefaults:
    days: int
    discovery_mode: str
    max_discovery_queries: int
    discovery_timelimit: str
    match_mode: str
    max_career_pages: int
    scrape_discovered_pages: str
    live_check_target: str
    output_file: Path
    coverage_report_file: Path
    cache_file: Path
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

    @classmethod
    def from_settings(cls, values: SearchDefaultsSettings) -> SearchDefaults:
        """Resolve typed runtime defaults into command-ready values."""
        return cls(
            days=values.days,
            discovery_mode=values.discovery_mode,
            max_discovery_queries=values.max_discovery_queries,
            discovery_timelimit=values.discovery_timelimit,
            match_mode=values.match_mode,
            max_career_pages=values.max_career_pages,
            scrape_discovered_pages=values.scrape_discovered_pages,
            live_check_target=values.live_check_target,
            output_file=resolve_runtime_path(values.output_file),
            coverage_report_file=resolve_runtime_path(values.coverage_report_file),
            cache_file=resolve_runtime_path(values.cache_file),
            discovery_region=values.discovery_region,
            search_backend=values.search_backend,
            search_retries=values.search_retries,
            results_per_query=values.results_per_query,
            timeout_seconds=values.timeout_seconds,
            delay_seconds=values.delay_seconds,
            max_lever_pages=values.max_lever_pages,
            max_fallback_pages=values.max_fallback_pages,
            show_results=values.show_results,
            async_timeout_seconds=values.async_timeout_seconds,
            user_agent=values.user_agent,
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> SearchDefaults:
        """Retain the version-one mapping constructor during migration."""
        return cls.from_settings(SearchDefaultsSettings.from_mapping(values))


DEFAULTS = SearchDefaults.from_settings(SEARCH_SETTINGS.defaults)
