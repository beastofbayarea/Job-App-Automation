"""Ashby entrypoint for the continuous guarded ATS worker."""

from __future__ import annotations

from collections.abc import Sequence

from .continuous_ats import main as _continuous_main


def main(argv: Sequence[str] | None = None) -> int:
    return _continuous_main(argv, ats_platform="ashby")


if __name__ == "__main__":
    raise SystemExit(main())
