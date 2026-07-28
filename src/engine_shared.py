"""Backward-compatible facade for shared ATS engine helpers."""

from __future__ import annotations

import sys as _sys

from job_application_automation import engine_shared as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
