"""Dependency-free contracts shared by application automation workflows.

The command workflows retain their public arguments and wire format. This
module provides a typed internal boundary so
new orchestration and ATS code can be tested without importing Playwright,
Google SDKs, or subprocess implementations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from .foundation import InputContractError
from .foundation import _require_string


ENGINE_RESULT_PREFIX = "ENGINE_RESULT_JSON:"


class EngineMode(str, Enum):
    """The mutually exclusive execution modes accepted by ATS engines."""

    DRY_RUN = "dry-run"
    FILL_ONLY = "fill-only"
    LIVE_SUBMIT = "live-submit"

    @property
    def cli_flag(self) -> str:
        """Return the established command-line flag for this mode."""
        return f"--{self.value}"

    @classmethod
    def parse(cls, value: object) -> EngineMode:
        """Parse a serialized mode and reject ambiguous values."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise InputContractError("engine mode must be a string")
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            choices = ", ".join(member.value for member in cls)
            raise InputContractError(
                f"unsupported engine mode {value!r}; expected one of {choices}"
            ) from exc


class EngineStatus(str, Enum):
    """Canonical statuses emitted by the current ATS engines.

    Results retain their original string status so provider-specific failures
    such as ``FAILED: TimeoutError`` remain lossless.  ``known_status`` can be
    used by callers that only need to branch on these stable values.
    """

    PREFILLED_ONLY = "PREFILLED_ONLY"
    SUBMITTED_CONFIRMED = "SUBMITTED & CONFIRMED"
    SUBMISSION_UNCONFIRMED = "SUBMISSION_UNCONFIRMED"
    SHELL_TEST_VALIDATED = "SHELL_TEST_VALIDATED"
    SHELL_TEST_VALIDATION_FAILED = "SHELL_TEST_VALIDATION_FAILED"
    INVALID_ENGINE_RESULT = "INVALID_ENGINE_RESULT"
    ENGINE_EXECUTION_ERROR = "ENGINE_EXECUTION_ERROR"
    FAILED = "FAILED"

    @classmethod
    def from_value(cls, value: str) -> EngineStatus | None:
        """Return a canonical status when *value* is known, otherwise None."""
        try:
            return cls(value)
        except ValueError:
            return None


def _require_bool(value: object, field_name: str) -> bool:
    # bool is a subclass of int, so identity is intentional here.
    if not isinstance(value, bool):
        raise InputContractError(f"{field_name} must be a boolean")
    return value


def _ensure_json_value(value: object, field_name: str) -> None:
    """Fail early when metadata could not be represented on the wire."""
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise InputContractError(f"{field_name} must be JSON serializable") from exc


@dataclass(frozen=True, slots=True)
class EngineRequest:
    """Validated input required to invoke one ATS automation engine."""

    ats: str
    url: str
    resume_path: Path
    cover_letter_path: Path | None = None
    company: str = ""
    role: str = ""
    email: str = ""
    mode: EngineMode = EngineMode.DRY_RUN
    headed: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ats = _require_string(self.ats, "ats")
        url = _require_string(self.url, "url")
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise InputContractError("url must be an absolute HTTPS URL")
        if not self.resume_path or not str(self.resume_path).strip():
            raise InputContractError("resume_path cannot be empty")
        resume_path = Path(self.resume_path).expanduser()
        cover_letter_path = (
            Path(self.cover_letter_path).expanduser()
            if self.cover_letter_path is not None
            else None
        )
        if not isinstance(self.mode, EngineMode):
            raise InputContractError("mode must be an EngineMode")
        _require_bool(self.headed, "headed")
        for field_name, value in (
            ("company", self.company),
            ("role", self.role),
            ("email", self.email),
        ):
            _require_string(value, field_name, allow_empty=True)
        if self.email and ("@" not in self.email or self.email.startswith("@")):
            raise InputContractError("email must be empty or contain a local part and @")
        if not isinstance(self.metadata, Mapping):
            raise InputContractError("metadata must be an object")
        metadata = dict(self.metadata)
        _ensure_json_value(metadata, "metadata")
        object.__setattr__(self, "ats", ats.lower())
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "resume_path", resume_path)
        object.__setattr__(self, "cover_letter_path", cover_letter_path)
        object.__setattr__(self, "company", self.company.strip())
        object.__setattr__(self, "role", self.role.strip())
        object.__setattr__(self, "email", self.email.strip())
        object.__setattr__(self, "metadata", metadata)

    def to_payload(self) -> dict[str, object]:
        """Serialize to the repository's CLI-oriented request field names."""
        payload: dict[str, object] = {
            "ats": self.ats,
            "url": self.url,
            "resume": str(self.resume_path),
            "company": self.company,
            "role": self.role,
            "email": self.email,
            "mode": self.mode.value,
            "headed": self.headed,
        }
        if self.cover_letter_path is not None:
            payload["cover_letter"] = str(self.cover_letter_path)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> EngineRequest:
        """Construct a request from a JSON-decoded mapping."""
        if not isinstance(payload, Mapping):
            raise InputContractError("engine request must be an object")
        resume = payload.get("resume", payload.get("resume_path"))
        if resume is None:
            raise InputContractError("engine request is missing resume")
        cover_letter = payload.get("cover_letter", payload.get("cover_letter_path"))
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise InputContractError("metadata must be an object")
        return cls(
            ats=_require_string(payload.get("ats"), "ats"),
            url=_require_string(payload.get("url"), "url"),
            resume_path=Path(_require_string(resume, "resume")),
            cover_letter_path=(
                Path(_require_string(cover_letter, "cover_letter"))
                if cover_letter is not None
                else None
            ),
            company=_require_string(payload.get("company", ""), "company", allow_empty=True),
            role=_require_string(payload.get("role", ""), "role", allow_empty=True),
            email=_require_string(payload.get("email", ""), "email", allow_empty=True),
            mode=EngineMode.parse(payload.get("mode", EngineMode.DRY_RUN.value)),
            headed=_require_bool(payload.get("headed", False), "headed"),
            metadata=dict(metadata),
        )

    def cli_arguments(self) -> tuple[str, ...]:
        """Return the stable engine-specific part of a CLI invocation."""
        arguments: tuple[str, ...] = (
            "--url",
            self.url,
            "--resume",
            str(self.resume_path),
        )
        if self.cover_letter_path is not None:
            arguments += ("--cover-letter", str(self.cover_letter_path))
        arguments += (
            "--company",
            self.company,
            "--role",
            self.role,
            "--email",
            self.email,
            self.mode.cli_flag,
        )
        return arguments + (("--headed",) if self.headed else ())


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Lossless structured result emitted by an ATS engine.

    ``extra`` carries provider-specific fields (screenshots, missing fields,
    form maps, and similar diagnostics) without widening the shared contract.
    """

    success: bool
    status: str
    ats: str
    submitted: bool = False
    confirmed: bool = False
    test_mode: bool = True
    error: str = ""
    detail: str = ""
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        status = _require_string(self.status, "status")
        ats = _require_string(self.ats, "ats")
        for field_name, value in (
            ("success", self.success),
            ("submitted", self.submitted),
            ("confirmed", self.confirmed),
            ("test_mode", self.test_mode),
        ):
            _require_bool(value, field_name)
        _require_string(self.error, "error", allow_empty=True)
        _require_string(self.detail, "detail", allow_empty=True)
        if self.confirmed and not self.submitted:
            raise InputContractError("confirmed results must also be submitted")
        known = EngineStatus.from_value(status)
        if known is EngineStatus.SUBMITTED_CONFIRMED:
            if not (self.success and self.submitted and self.confirmed):
                raise InputContractError(
                    "SUBMITTED & CONFIRMED requires success, submitted, and confirmed"
                )
        if known is EngineStatus.PREFILLED_ONLY and (self.submitted or self.confirmed):
            raise InputContractError("PREFILLED_ONLY cannot be submitted or confirmed")
        if not isinstance(self.extra, Mapping):
            raise InputContractError("extra must be an object")
        extra = dict(self.extra)
        reserved = set(self._reserved_payload_keys())
        collisions = reserved.intersection(extra)
        if collisions:
            names = ", ".join(sorted(collisions))
            raise InputContractError(f"extra cannot overwrite reserved result fields: {names}")
        _ensure_json_value(extra, "extra")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "ats", ats.lower())
        object.__setattr__(self, "error", self.error.strip())
        object.__setattr__(self, "detail", self.detail.strip())
        object.__setattr__(self, "extra", extra)

    @staticmethod
    def _wire_payload_keys() -> tuple[str, ...]:
        return (
            "success",
            "status",
            "ats",
            "submitted",
            "confirmed",
            "test_mode",
            "error",
            "detail",
        )

    @classmethod
    def _reserved_payload_keys(cls) -> tuple[str, ...]:
        """Protect engine and pipeline-owned fields from provider extras."""
        return cls._wire_payload_keys() + (
            "row",
            "company",
            "role",
            "url",
            "engine",
            "resume",
            "cover_letter",
            "email",
            "already_submitted",
            "ledger_persisted",
            "manual_review_required",
            "retry_safe",
            "quarantine_persisted",
            "quarantine_path",
            "ledger_error",
            "quarantine_error",
            "engine_result",
            "engine_details",
        )

    @property
    def known_status(self) -> EngineStatus | None:
        """Return the canonical enum when this result has a known status."""
        return EngineStatus.from_value(self.status)

    @property
    def is_confirmed_submission(self) -> bool:
        """True only for the exact, safe-to-count completed-application state."""
        return (
            self.success
            and self.submitted
            and self.confirmed
            and not self.test_mode
            and self.known_status is EngineStatus.SUBMITTED_CONFIRMED
        )

    def to_payload(self) -> dict[str, object]:
        """Serialize using the established engine-result output schema."""
        payload: dict[str, object] = dict(self.extra)
        payload.update(
            {
                "success": self.success,
                "status": self.status,
                "ats": self.ats,
                "submitted": self.submitted,
                "confirmed": self.confirmed,
                "test_mode": self.test_mode,
            }
        )
        if self.error:
            payload["error"] = self.error
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def to_wire_line(self) -> str:
        """Serialize to the existing stdout marker format used by the orchestrator."""
        return f"{ENGINE_RESULT_PREFIX}{json.dumps(self.to_payload(), sort_keys=True)}"

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> EngineResult:
        """Parse and validate the established engine-result JSON object."""
        if not isinstance(payload, Mapping):
            raise InputContractError("engine result must be an object")
        known_keys = set(cls._wire_payload_keys())
        extras = {key: value for key, value in payload.items() if key not in known_keys}
        return cls(
            success=_require_bool(payload.get("success"), "success"),
            status=_require_string(payload.get("status"), "status"),
            ats=_require_string(payload.get("ats"), "ats"),
            submitted=_require_bool(payload.get("submitted", False), "submitted"),
            confirmed=_require_bool(payload.get("confirmed", False), "confirmed"),
            test_mode=_require_bool(payload.get("test_mode", True), "test_mode"),
            error=_require_string(payload.get("error", ""), "error", allow_empty=True),
            detail=_require_string(payload.get("detail", ""), "detail", allow_empty=True),
            extra=extras,
        )

    @classmethod
    def from_wire_line(cls, line: str) -> EngineResult:
        """Parse one ``ENGINE_RESULT_JSON:`` line emitted by an engine."""
        if not isinstance(line, str) or not line.startswith(ENGINE_RESULT_PREFIX):
            raise InputContractError("line does not start with ENGINE_RESULT_JSON:")
        try:
            payload = json.loads(line[len(ENGINE_RESULT_PREFIX) :])
        except json.JSONDecodeError as exc:
            raise InputContractError("engine result contains invalid JSON") from exc
        if not isinstance(payload, dict):
            raise InputContractError("engine result JSON must be an object")
        return cls.from_payload(payload)


@runtime_checkable
class ATSEngine(Protocol):
    """Injectable ATS implementation boundary used by orchestration code."""

    def run(self, request: EngineRequest) -> EngineResult:
        """Run one engine request and return its structured result."""


def result_from_legacy_payload(payload: Mapping[str, object], *, ats: str) -> EngineResult:
    """Adapt a permissive legacy payload at one explicitly named boundary.

    Old orchestrator parsing supplies defaults for malformed or incomplete
    engine output.  New code can use this helper while migrating without
    changing legacy script behavior.
    """
    normalized = dict(payload)
    normalized.setdefault("success", False)
    normalized.setdefault("status", EngineStatus.INVALID_ENGINE_RESULT.value)
    normalized.setdefault("ats", ats)
    normalized.setdefault("submitted", False)
    normalized.setdefault("confirmed", False)
    normalized.setdefault("test_mode", True)
    return EngineResult.from_payload(normalized)


def command_with_request(executable: str, request: EngineRequest) -> tuple[str, ...]:
    """Build an immutable command sequence for a specific engine request."""
    return (executable, *request.cli_arguments())


def validate_engine_command(command: Sequence[str]) -> tuple[str, ...]:
    """Validate a process command before an injected runner receives it."""
    if not command:
        raise InputContractError("command must contain at least one argument")
    normalized = tuple(_require_string(part, "command argument") for part in command)
    return normalized
