"""Unit tests for pool_select.py email selection."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from job_application_automation.mail.pool_select import (
    _load_email_pool,
    _resolve_email_pool,
    get_random_email,
    main,
)


def test_get_random_email_single_and_multiple(tmp_path: Path) -> None:
    pool_file = tmp_path / "email_pool.json"
    pool_file.write_text('["user1@example.com", "user2@example.com", "user3@example.com"]', encoding="utf-8")

    single = get_random_email(pool_file, count=1)
    assert isinstance(single, str)
    assert "@example.com" in single

    multiple = get_random_email(pool_file, count=2)
    assert isinstance(multiple, list)
    assert len(multiple) == 2

    with pytest.raises(ValueError, match="count must be greater than zero"):
        get_random_email(pool_file, count=0)


def test_resolve_and_load_email_pool(tmp_path: Path) -> None:
    pool_file = tmp_path / "email_pool.json"
    pool_file.write_text('["test@example.com"]', encoding="utf-8")

    resolved = _resolve_email_pool(pool_file)
    assert resolved.exists()

    emails = _load_email_pool(pool_file)
    assert emails == ["test@example.com"]


def test_pool_select_main_help() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
