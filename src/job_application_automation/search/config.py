"""Typed access to the validated runtime configuration for job search."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path


def _tuple(key: str) -> tuple[str, ...]:
    return tuple(RUNTIME_CONFIG.search[key])


def _tuple_map(key: str) -> dict[str, tuple[str, ...]]:
    values: Mapping[str, list[str]] = RUNTIME_CONFIG.search[key]
    return {name: tuple(items) for name, items in values.items()}


SEARCH_PHRASE_TEMPLATES = _tuple("search_phrase_templates")
DEFAULT_AI_TERMS = _tuple("ai_terms")
AI_DISCOVERY_TERMS = _tuple("ai_discovery_terms")
DEFAULT_LOCATION_TERMS = _tuple("default_locations")
ROLE_FAMILY_VARIANTS = _tuple_map("role_families")
ROLE_FAMILY_INPUT_ALIASES = _tuple_map("role_family_input_aliases")
LOCATION_ALIAS_MAP = _tuple_map("location_aliases")
GENERIC_ATS_HOST_SUFFIXES = _tuple("generic_ats_host_suffixes")
ATS_SEARCH_HOSTS = _tuple_map("ats_hosts")
SUPPORTED_ATS_PLATFORMS = tuple(ATS_SEARCH_HOSTS)
ALL_DDGS_BACKENDS = _tuple("ddgs_backends")
DEAD_ROLE_MARKERS = _tuple("dead_role_markers")
RESTRICTED_URL_PATTERNS = _tuple("restricted_url_patterns")
WORKABLE_SHORT_LINK_BOARD = str(RUNTIME_CONFIG.search["workable_short_link_board"])
PROVIDER_API_URLS = dict(RUNTIME_CONFIG.search["provider_api_urls"])
RESTRICTED_BOARD_KEYS = {
    (platform, token)
    for platform, tokens in RUNTIME_CONFIG.search["restricted_board_tokens"].items()
    for token in tokens
}


def build_role_alias_map() -> dict[str, tuple[str, ...]]:
    """Build role aliases from configured families to prevent drift."""
    aliases = {
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
    def from_mapping(cls, values: Mapping[str, Any]) -> SearchDefaults:
        path_keys = {"output_file", "coverage_report_file", "cache_file"}
        normalized = {
            key: resolve_runtime_path(value) if key in path_keys else value
            for key, value in values.items()
        }
        return cls(**normalized)


DEFAULTS = SearchDefaults.from_mapping(RUNTIME_CONFIG.search["defaults"])
