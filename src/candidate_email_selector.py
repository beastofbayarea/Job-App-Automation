#!/usr/bin/env python3
"""Select one or more random addresses from the candidate email pool."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Literal, Optional, Sequence, overload

from project_paths import CONFIG_DIR


def _resolve_email_pool(json_file_path: Path) -> Path:
    """Resolve the configured pool, retaining the legacy parent fallback."""
    if not json_file_path.exists():
        parent_path = json_file_path.parent.parent / json_file_path.name
        if parent_path.exists():
            return parent_path
        raise FileNotFoundError(f"Email pool JSON file not found at: {json_file_path}")
    return json_file_path


def _load_email_pool(json_file_path: Path) -> list[str]:
    resolved_path = _resolve_email_pool(json_file_path)
    try:
        with resolved_path.open("r", encoding="utf-8") as stream:
            raw_emails = json.load(stream)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Email pool contains invalid JSON: {resolved_path}") from exc

    if not isinstance(raw_emails, list) or not raw_emails:
        raise ValueError(f"No email addresses found in {resolved_path}")

    emails: list[str] = []
    for index, value in enumerate(raw_emails, start=1):
        if not isinstance(value, str) or "@" not in value:
            raise ValueError(f"Invalid email address at item {index} in {resolved_path}")
        normalized = value.strip()
        local, separator, domain = normalized.partition("@")
        if not separator or not local or not domain or "@" in domain:
            raise ValueError(f"Invalid email address at item {index} in {resolved_path}")
        emails.append(normalized)
    return emails


@overload
def get_random_email(json_file_path: Path, count: Literal[1] = 1) -> str:
    ...


@overload
def get_random_email(json_file_path: Path, count: int) -> str | list[str]:
    ...


def get_random_email(json_file_path: Path, count: int = 1) -> str | list[str]:
    """Return one random email, or a unique sample when ``count`` is greater than one."""
    if count < 1:
        raise ValueError("count must be greater than zero")
    emails = _load_email_pool(Path(json_file_path))

    if count == 1:
        return random.choice(emails)
    return random.sample(emails, min(count, len(emails)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pick a random candidate email address from candidate_email_pool.json"
    )
    parser.add_argument(
        "--file",
        type=str,
        default=str(CONFIG_DIR / "candidate_email_pool.json"),
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
