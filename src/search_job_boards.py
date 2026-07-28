"""Backward-compatible facade for ATS job-board discovery."""

from __future__ import annotations

import sys as _sys

from job_application_automation import search_job_boards as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
