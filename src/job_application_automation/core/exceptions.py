"""Dependency-free exception taxonomy for workflow boundaries.

The compatibility bases are intentional.  Existing callers commonly catch
``ValueError``, ``OSError``, or ``RuntimeError`` at command boundaries; the
specialized errors preserve those contracts while allowing new code to make
precise recovery decisions.
"""

from __future__ import annotations


class JobAutomationError(Exception):
    """Base class for expected failures raised by this package."""


class ConfigurationError(JobAutomationError, ValueError):
    """A configuration document is missing, malformed, or internally invalid."""


class InputContractError(JobAutomationError, ValueError):
    """Caller-supplied data violates a public model or function contract."""


class ArtifactError(JobAutomationError, OSError, ValueError):
    """A persisted artifact cannot be read, validated, or written safely."""


class ExternalServiceError(JobAutomationError, RuntimeError):
    """A required subprocess, network API, or SDK operation failed."""


class BrowserAutomationError(ExternalServiceError):
    """A browser session or browser-control boundary could not complete."""


class ApplicationBlockedError(BrowserAutomationError):
    """The application cannot proceed without manual or external intervention."""


class SubmissionOutcomeUnknown(BrowserAutomationError):
    """A submit action may have occurred, but confirmation is inconclusive."""
