"""Validation and selection helpers for the candidate email-address pool.

The public CLI keeps its historic private helper names, while this module owns
the reusable parsing and sampling behavior.  Callers can inject samplers when
they need deterministic selections in tests.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, TypeVar, overload


EmailSampler = Callable[[Sequence[str]], str]
EmailSample = Callable[[Sequence[str], int], list[str]]
_T = TypeVar("_T")


def resolve_email_pool(json_file_path: Path) -> Path:
    """Resolve a pool path, retaining the historic parent-directory fallback."""
    if not json_file_path.exists():
        parent_path = json_file_path.parent.parent / json_file_path.name
        if parent_path.exists():
            return parent_path
        raise FileNotFoundError(f"Email pool JSON file not found at: {json_file_path}")
    return json_file_path


def load_email_pool(json_file_path: Path) -> list[str]:
    """Load and validate a non-empty JSON list of candidate email addresses."""
    resolved_path = resolve_email_pool(json_file_path)
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
def select_emails(emails: Sequence[str], count: Literal[1] = 1) -> str: ...


@overload
def select_emails(emails: Sequence[str], count: int) -> str | list[str]: ...


def select_emails(
    emails: Sequence[str],
    count: int = 1,
    *,
    choice: EmailSampler = random.choice,
    sample: EmailSample = random.sample,
) -> str | list[str]:
    """Choose one address or a unique sample, preserving legacy count behavior."""
    if count < 1:
        raise ValueError("count must be greater than zero")
    if count == 1:
        return choice(emails)
    return sample(emails, min(count, len(emails)))


@overload
def get_random_email(json_file_path: Path, count: Literal[1] = 1) -> str: ...


@overload
def get_random_email(json_file_path: Path, count: int) -> str | list[str]: ...


def get_random_email(
    json_file_path: Path,
    count: int = 1,
    *,
    loader: Callable[[Path], Sequence[str]] = load_email_pool,
    choice: EmailSampler = random.choice,
    sample: EmailSample = random.sample,
) -> str | list[str]:
    """Load a pool then choose addresses, with injectable dependencies for tests."""
    if count < 1:
        raise ValueError("count must be greater than zero")
    return select_emails(loader(Path(json_file_path)), count, choice=choice, sample=sample)
