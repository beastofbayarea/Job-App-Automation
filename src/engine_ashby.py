"""Backward-compatible facade for the Ashby application engine."""

from __future__ import annotations

import sys as _sys

from job_application_automation import engine_ashby as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
