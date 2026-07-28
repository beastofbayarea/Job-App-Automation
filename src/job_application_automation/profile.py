"""Typed, lossless runtime view of a normalized candidate profile.

ATS providers intentionally still receive dictionaries: that preserves the
configuration-v2 shape and existing provider patch points.  This value object
gives shared loaders a validated boundary without discarding provider-specific
or future configuration fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


def _frozen_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"profile {field_name} must be an object")
    return MappingProxyType(dict(value))


def _optional_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    return _frozen_mapping(value, field_name)


@dataclass(frozen=True, slots=True)
class AutomationProfile:
    """Lossless normalized configuration used by ATS automation workflows."""

    candidate: Mapping[str, Any]
    rules: Mapping[str, Any]
    eeo_defaults: Mapping[str, Any]
    field_matchers: Mapping[str, Any]
    answer_variants: Mapping[str, Any]
    defaults: Mapping[str, Any]
    paths: Mapping[str, Any]
    company_overrides: Mapping[str, Any]
    document: Mapping[str, Any]

    @classmethod
    def from_runtime_mapping(cls, config: Mapping[str, Any]) -> "AutomationProfile":
        """Freeze a normalized config while retaining unknown top-level fields."""
        if not isinstance(config, Mapping):
            raise ValueError("profile config must be an object")
        document = dict(config)
        return cls(
            candidate=_frozen_mapping(document.get("candidate"), "candidate"),
            rules=_optional_mapping(document.get("rules"), "rules"),
            eeo_defaults=_optional_mapping(document.get("eeo_defaults"), "eeo_defaults"),
            field_matchers=_optional_mapping(document.get("field_matchers"), "field_matchers"),
            answer_variants=_optional_mapping(document.get("answer_variants"), "answer_variants"),
            defaults=_optional_mapping(document.get("defaults"), "defaults"),
            paths=_optional_mapping(document.get("paths"), "paths"),
            company_overrides=_optional_mapping(
                document.get("company_overrides"), "company_overrides"
            ),
            document=MappingProxyType(document),
        )

    def to_runtime_mapping(self) -> dict[str, Any]:
        """Return the legacy mutable dictionary shape expected by providers."""
        runtime = dict(self.document)
        runtime["candidate"] = dict(self.candidate)
        for name, value in (
            ("rules", self.rules),
            ("eeo_defaults", self.eeo_defaults),
            ("field_matchers", self.field_matchers),
            ("answer_variants", self.answer_variants),
            ("defaults", self.defaults),
            ("paths", self.paths),
            ("company_overrides", self.company_overrides),
        ):
            if name in self.document:
                runtime[name] = dict(value)
        return runtime
