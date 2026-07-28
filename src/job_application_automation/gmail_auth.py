"""Injectable OAuth boundary for the Gmail CLI.

Google SDK imports remain lazy so parsing, export, and automated tests do not
need credentials or a browser.  The public CLI retains its established helper
functions as compatibility wrappers around this service.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_text


GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
)
GoogleDependencies = tuple[Any, Any, Any, Any, Any]
TokenWriter = Callable[[Path, str], Path]


def import_google_dependencies() -> GoogleDependencies:
    """Load optional Google SDK dependencies only for an OAuth operation."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise RuntimeError(
            "Missing Google packages. Install with:\n"
            "python -m pip install --upgrade google-api-python-client "
            "google-auth-httplib2 google-auth-oauthlib"
        ) from exc
    return Request, Credentials, InstalledAppFlow, build, HttpError


def _write_token(path: Path, value: str) -> Path:
    return atomic_write_text(path, value)


def get_gmail_service(
    credentials_path: Path,
    token_path: Path,
    *,
    scopes: Sequence[str] = GMAIL_SCOPES,
    dependencies: GoogleDependencies | None = None,
    token_writer: TokenWriter = _write_token,
    chmod: Callable[[Path, int], None] = os.chmod,
) -> Any:
    """Return an authorized Gmail service using refresh or local OAuth flow.

    All externally effectful operations are injected or lazily loaded, so unit
    tests can assert token and service behavior without opening a browser.
    """
    Request, Credentials, InstalledAppFlow, build, _ = dependencies or import_google_dependencies()
    credentials = None

    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), list(scopes))

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(f"OAuth client file not found: {credentials_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), list(scopes))
            credentials = flow.run_local_server(port=0)

        token_writer(token_path, credentials.to_json())
        try:
            chmod(token_path, 0o600)
        except OSError:
            pass

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)
