"""Backward-compatible facade for Gmail OAuth tools."""

from __future__ import annotations

import sys as _sys

from job_application_automation import email_gmail_client as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
