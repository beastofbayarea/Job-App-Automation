"""Typed, ordered form-section execution shared by ATS engines.

Section handlers encapsulate one coherent fill operation while provider runners
retain control of navigation, rerender repair, safety gates, and submission.
The dependency-free contracts make ordering and aggregation testable without a
live browser.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable


def _stable_strings(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


@dataclass(frozen=True, slots=True)
class FormSectionOutcome:
    """Lossless outcome of one named form section.

    ``section`` and ``critical_fields`` preserve Ashby's established constructor
    and attribute names. ``name`` and ``fields`` are provider-neutral aliases.
    """

    section: str
    critical_fields: Mapping[str, bool] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized_section = str(self.section).strip()
        if not normalized_section:
            raise ValueError("form section name cannot be empty")
        normalized_fields: dict[str, bool] = {}
        for raw_name, value in self.critical_fields.items():
            field_name = str(raw_name).strip()
            if not field_name:
                raise ValueError("form section field names cannot be empty")
            if not isinstance(value, bool):
                raise ValueError(f"form section field {field_name!r} must be a boolean")
            normalized_fields[field_name] = value
        object.__setattr__(self, "section", normalized_section)
        object.__setattr__(self, "critical_fields", MappingProxyType(normalized_fields))
        object.__setattr__(self, "missing", _stable_strings(self.missing))
        object.__setattr__(self, "completed", _stable_strings(self.completed))

    @property
    def name(self) -> str:
        return self.section

    @property
    def fields(self) -> Mapping[str, bool]:
        return self.critical_fields


@runtime_checkable
class FormSectionHandler(Protocol):
    """Executes one named form section."""

    section: str

    def handle(self) -> FormSectionOutcome:
        """Fill or inspect the section and return its typed outcome."""


@dataclass(frozen=True, slots=True)
class CallableSectionHandler:
    """Adapt a closure into a named section handler."""

    section: str
    action: Callable[[], FormSectionOutcome]

    def handle(self) -> FormSectionOutcome:
        outcome = self.action()
        if outcome.section != self.section:
            raise ValueError(
                f"form section handler {self.section!r} returned outcome {outcome.section!r}"
            )
        return outcome


@dataclass(frozen=True, slots=True)
class FormSectionReport:
    """Ordered outcomes with stable aggregate views."""

    outcomes: tuple[FormSectionOutcome, ...]

    @property
    def fields(self) -> dict[str, bool]:
        return aggregate_section_outcomes(self.outcomes)

    @property
    def missing(self) -> list[str]:
        return list(
            _stable_strings(tuple(item for outcome in self.outcomes for item in outcome.missing))
        )

    @property
    def completed(self) -> list[str]:
        return list(
            _stable_strings(tuple(item for outcome in self.outcomes for item in outcome.completed))
        )

    def outcome(self, section: str) -> FormSectionOutcome:
        normalized = str(section).strip()
        for outcome in self.outcomes:
            if outcome.section == normalized:
                return outcome
        raise KeyError(normalized)


def aggregate_section_outcomes(
    outcomes: Iterable[FormSectionOutcome],
) -> dict[str, bool]:
    """Merge section fields in execution order so later verification wins."""
    merged: dict[str, bool] = {}
    for outcome in outcomes:
        merged.update(outcome.critical_fields)
    return merged


def run_section_handlers(handlers: Sequence[FormSectionHandler]) -> FormSectionReport:
    """Execute handlers exactly once and preserve their declared order."""
    outcomes: list[FormSectionOutcome] = []
    sections: set[str] = set()
    for handler in handlers:
        section = str(handler.section).strip()
        if not section:
            raise ValueError("form section handler name cannot be empty")
        if section in sections:
            raise ValueError(f"duplicate form section handler: {section}")
        sections.add(section)
        outcomes.append(handler.handle())
    return FormSectionReport(tuple(outcomes))
