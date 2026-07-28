"""Backward-compatible facade for the application orchestrator."""

from __future__ import annotations

import sys as _sys

from job_application_automation import orchestrator as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
