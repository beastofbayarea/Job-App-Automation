"""Persistent, guarded Lever application worker."""

from __future__ import annotations

from typing import Sequence

from .continuous_ats import main as run_continuous_worker


def main(argv: Sequence[str] | None = None) -> int:
    return run_continuous_worker(argv, ats_platform="lever")


if __name__ == "__main__":
    raise SystemExit(main())
