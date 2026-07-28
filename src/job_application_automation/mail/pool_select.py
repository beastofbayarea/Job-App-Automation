#!/usr/bin/env python3
"""Select one or more random addresses from the candidate email pool."""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path
from typing import Literal, Optional, Sequence, overload

from job_application_automation.mail.pool import (
    load_email_pool as _load_email_pool_impl,
    resolve_email_pool as _resolve_email_pool_impl,
    select_emails,
)
from ..core.paths import CONFIG_DIR
from ..core.runtime_config import RUNTIME_CONFIG, resolve_runtime_path


def _resolve_email_pool(json_file_path: Path) -> Path:
    """Resolve the configured pool, retaining the legacy parent fallback."""
    return _resolve_email_pool_impl(json_file_path)


def _load_email_pool(json_file_path: Path) -> list[str]:
    """Load the configured pool using the reusable validator."""
    return _load_email_pool_impl(json_file_path)


@overload
def get_random_email(json_file_path: Path, count: Literal[1] = 1) -> str: ...


@overload
def get_random_email(json_file_path: Path, count: int) -> str | list[str]: ...


def get_random_email(json_file_path: Path, count: int = 1) -> str | list[str]:
    """Return one random email, or a unique sample when ``count`` is greater than one."""
    if count < 1:
        raise ValueError("count must be greater than zero")
    return select_emails(
        _load_email_pool(Path(json_file_path)),
        count,
        choice=random.choice,
        sample=random.sample,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pick a random candidate email address from candidate_email_pool.json"
    )
    parser.add_argument(
        "--file",
        type=str,
        default=str(resolve_runtime_path(RUNTIME_CONFIG.application["candidate_email_pool_file"])),
        help="Path to candidate_email_pool.json",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of random emails to pick (default: 1)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    file_path = Path(args.file).expanduser()
    if not file_path.is_absolute():
        file_path = CONFIG_DIR / file_path

    try:
        result = get_random_email(file_path, count=args.count)
        if isinstance(result, list):
            for email in result:
                print(email)
        else:
            print(result)
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
