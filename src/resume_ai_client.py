"""Backward-compatible facade for resume AI utilities."""

from __future__ import annotations

import sys as _sys

from job_application_automation import resume_ai_client as _implementation

if __name__ == "__main__":
    raise SystemExit(0)

_sys.modules[__name__] = _implementation
