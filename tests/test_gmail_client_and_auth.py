from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from job_application_automation.mail.gmail_auth import (
    GMAIL_SCOPES,
    get_gmail_service,
)
from job_application_automation.mail.gmail_client import (
    confirm_send,
    parse_args,
    send_email,
)
from job_application_automation.mail.pool_select import get_random_email, main as pool_select_main


def test_get_gmail_service_with_existing_token(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    cred_path = tmp_path / "credentials.json"
    token_path.write_text('{"token": "fake_token"}', encoding="utf-8")
    cred_path.write_text('{"installed": {}}', encoding="utf-8")

    mock_credentials_cls = MagicMock()
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_credentials_cls.from_authorized_user_file.return_value = mock_creds

    mock_build = MagicMock(return_value="gmail_service")

    deps = (MagicMock(), mock_credentials_cls, MagicMock(), mock_build, MagicMock())

    service = get_gmail_service(
        cred_path,
        token_path,
        dependencies=deps,
    )
    assert service == "gmail_service"
    mock_credentials_cls.from_authorized_user_file.assert_called_once_with(
        str(token_path), list(GMAIL_SCOPES)
    )


def test_get_gmail_service_expired_token_refresh(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    cred_path = tmp_path / "credentials.json"
    token_path.write_text('{"token": "fake"}', encoding="utf-8")

    mock_credentials_cls = MagicMock()
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "refresh_tok"
    mock_creds.to_json.return_value = '{"refreshed": "ok"}'
    mock_credentials_cls.from_authorized_user_file.return_value = mock_creds

    mock_build = MagicMock(return_value="refreshed_service")
    mock_writer = MagicMock()

    deps = (MagicMock(), mock_credentials_cls, MagicMock(), mock_build, MagicMock())

    service = get_gmail_service(
        cred_path,
        token_path,
        dependencies=deps,
        token_writer=mock_writer,
    )
    assert service == "refreshed_service"
    mock_creds.refresh.assert_called_once()
    mock_writer.assert_called_once()


def test_get_gmail_service_missing_credentials_file(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    cred_path = tmp_path / "missing_credentials.json"

    deps = (MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    deps[1].from_authorized_user_file.side_effect = Exception("No file")

    with pytest.raises(FileNotFoundError, match="OAuth client file not found"):
        get_gmail_service(cred_path, token_path, dependencies=deps)


def test_gmail_parse_args_validation() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--max-results", "0"])

    with pytest.raises(SystemExit):
        parse_args(["--send-to", "a@example.com"])  # missing subject and body


def test_gmail_send_email_and_draft() -> None:
    mock_service = MagicMock()
    send_email(mock_service, "recip@example.com", "Subj", "Body", html_body="<p>Body</p>")
    mock_service.users().messages().send.assert_called_once()

    mock_service_draft = MagicMock()
    send_email(mock_service_draft, "recip@example.com", "Subj", "Body", draft=True)
    mock_service_draft.users().drafts().create.assert_called_once()


def test_confirm_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert confirm_send("a@b.com", "Subj", "Body") is True

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert confirm_send("a@b.com", "Subj", "Body") is False


def test_pool_select_invalid_count(tmp_path: Path) -> None:
    pool_file = tmp_path / "pool.json"
    pool_file.write_text('["a@b.com"]', encoding="utf-8")
    with pytest.raises(ValueError, match="count must be greater than zero"):
        get_random_email(pool_file, count=0)


def test_pool_select_main_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pool_file = tmp_path / "candidate_email_pool.json"
    pool_file.write_text('["user@example.com"]', encoding="utf-8")

    res = pool_select_main(["--file", str(pool_file), "--count", "1"])
    assert res == 0
    captured = capsys.readouterr()
    assert "user@example.com" in captured.out


def test_pool_select_main_error(capsys: pytest.CaptureFixture[str]) -> None:
    res = pool_select_main(["--file", "nonexistent_pool_file.json"])
    assert res == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err
