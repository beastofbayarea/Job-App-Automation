"""Backward-compatible facade for project path helpers."""

from __future__ import annotations

import sys as _sys

from job_application_automation import paths as _implementation

if __name__ == "__main__":
    raise SystemExit(0)

_sys.modules[__name__] = _implementation
