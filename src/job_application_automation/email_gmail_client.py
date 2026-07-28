#!/usr/bin/env python3
"""
Gmail OAuth Reader and Sender
=============================

This Gmail workflow can:
- Read recent Gmail messages.
- Search Gmail using Gmail query syntax.
- Export results to CSV or JSON.
- Send a plain-text email from the authenticated Gmail account.
- Require explicit confirmation before sending unless --yes is supplied.

REQUIREMENTS
------------
Python 3.9+

Install dependencies:

    python -m pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib

GOOGLE CLOUD SETUP
------------------
1. Enable the Gmail API in your Google Cloud project.
2. Create an OAuth client of type "Desktop app".
3. Download the client JSON.
4. Rename it to credentials.json.
5. Put credentials.json in the project's config directory.

IMPORTANT WHEN UPGRADING FROM READ-ONLY
---------------------------------------
This workflow requests both:
- gmail.readonly
- gmail.send

If you previously ran a read-only version and already have token.json, delete
token.json before running this version. Google must ask you to approve the new
send permission.

FIRST RUN
---------
    python src/job_automation.py gmail --max-results 10

A browser window will open for OAuth authorization. After approval, token.json
will be saved locally.

READ EXAMPLES
-------------
Read the 10 newest inbox messages:

    python src/job_automation.py gmail --max-results 10

Read unread messages:

    python src/job_automation.py gmail --unread --max-results 20

Search all mail:

    python src/job_automation.py gmail --all-mail --query "from:example.com newer_than:30d"

Export:

    python src/job_automation.py gmail --max-results 100 --csv output/messages.csv
    python src/job_automation.py gmail --max-results 100 --json output/messages.json

SEND EXAMPLES
-------------
Interactive confirmation:

    python src/job_automation.py gmail \
        --send-to recipient@example.com \
        --subject "Test email" \
        --body "Hello from my local Python script."

Skip confirmation deliberately:

    python src/job_automation.py gmail \
        --send-to recipient@example.com \
        --subject "Test email" \
        --body "Hello from my local Python script." \
        --yes

SECURITY
--------
- Never share credentials.json or token.json.
- Add both files to .gitignore.
- Rotate any OAuth client secret that has been exposed.
- The send operation is explicit and never runs unless --send-to is present.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Sequence

from job_application_automation.gmail_messages import (
    EmailRecord,
    VerificationCodeMatch,
    build_query as _build_query,
    classify_application_email as _classify_application_email,
    decode_base64url as _decode_base64url,
    extract_body as _extract_body,
    fetch_messages as _fetch_messages,
    header_map as _header_map,
    html_to_text as _html_to_text,
    poll_for_verification_code as _poll_for_verification_code,
)
from job_application_automation.gmail_persistence import (
    export_rows as _export_rows_impl,
    load_used_verification_message_ids as _load_used_verification_message_ids,
    record_used_verification_message as _record_used_verification_message,
    write_csv as _write_csv,
    write_json as _write_json,
)
from .paths import CONFIG_DIR
from .gmail_auth import (
    GMAIL_SCOPES,
    get_gmail_service as _get_gmail_service,
    import_google_dependencies as _import_google_dependencies,
)

# Keep the mutable list export expected by existing callers and CLI extensions.
SCOPES = list(GMAIL_SCOPES)

DEFAULT_CREDENTIALS_FILE = str(CONFIG_DIR / "credentials.json")
DEFAULT_TOKEN_FILE = str(CONFIG_DIR / "token.json")


def classify_application_email(record: EmailRecord) -> str:
    """Classify common application-mail outcomes without sending or modifying mail."""
    return _classify_application_email(record)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read and send Gmail locally through OAuth.")

    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--query", default="")
    parser.add_argument("--all-mail", action="store_true")
    parser.add_argument("--unread", action="store_true")
    parser.add_argument("--include-body", action="store_true")
    parser.add_argument("--classify", action="store_true")
    parser.add_argument(
        "--redact", action="store_true", help="Redact sender, body, and IDs in exports."
    )
    parser.add_argument("--csv", dest="csv_path")
    parser.add_argument("--json", dest="json_path")

    parser.add_argument("--send-to")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument("--html-body")
    parser.add_argument(
        "--draft", action="store_true", help="Create a Gmail draft instead of sending."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Send without an interactive confirmation prompt.",
    )

    parser.add_argument("--credentials", default=DEFAULT_CREDENTIALS_FILE)
    parser.add_argument("--token", default=DEFAULT_TOKEN_FILE)

    args = parser.parse_args(argv)

    if args.max_results < 1:
        parser.error("--max-results must be at least 1")

    sending = any([args.send_to, args.subject, args.body, args.html_body, args.draft])
    if (
        sending
        and not all([args.send_to, args.subject])
        or (sending and not (args.body or args.html_body))
    ):
        parser.error("--send-to, --subject, and --body or --html-body must be supplied together")

    return args


def import_google_dependencies() -> tuple[Any, Any, Any, Any, Any]:
    """Load the optional Google SDK through the injectable OAuth service."""
    return _import_google_dependencies()


def get_gmail_service(credentials_path: Path, token_path: Path) -> Any:
    """Return an authorized Gmail client, refreshing or re-running OAuth as needed."""
    return _get_gmail_service(
        credentials_path,
        token_path,
        scopes=SCOPES,
        dependencies=import_google_dependencies(),
    )


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Return case-normalized headers from a Gmail payload."""
    return _header_map(payload)


def decode_base64url(data: str) -> str:
    """Decode Gmail's URL-safe base64 text payloads."""
    return _decode_base64url(data)


def html_to_text(value: str) -> str:
    """Render HTML body parts as the CLI's plain-text output."""
    return _html_to_text(value)


def extract_body(payload: dict[str, Any]) -> str:
    """Recursively collect a message's text parts, preferring plain text."""
    return _extract_body(payload)


def build_query(args: argparse.Namespace) -> str:
    """Build the Gmail search query from the CLI arguments."""
    return _build_query(args)


def fetch_messages(
    service: Any,
    query: str,
    max_results: int,
    include_body: bool,
) -> list[EmailRecord]:
    """Fetch Gmail messages through the reusable parser/service adapter."""
    return _fetch_messages(service, query, max_results, include_body)


def poll_for_verification_code(
    service: Any,
    query: str,
    pattern: str,
    *,
    timeout_seconds: int = 60,
    poll_interval_seconds: float = 3,
    sender_domains: tuple[str, ...] = (),
    expected_recipient: str = "",
    excluded_message_ids: set[str] | None = None,
) -> VerificationCodeMatch | None:
    """Wait for a fresh verification email and return its first matching code."""
    return _poll_for_verification_code(
        service,
        query,
        pattern,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sender_domains=sender_domains,
        expected_recipient=expected_recipient,
        excluded_message_ids=excluded_message_ids,
        fetcher=fetch_messages,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


def load_used_verification_message_ids(path: Path) -> set[str]:
    """Load locally recorded OTP message IDs through the persistence adapter."""
    return _load_used_verification_message_ids(path)


def record_used_verification_message(path: Path, match: VerificationCodeMatch) -> None:
    """Record a consumed OTP without retaining its verification code."""
    _record_used_verification_message(path, match, clock=time.time)


def send_email(
    service: Any,
    recipient: str,
    subject: str,
    body: str,
    html_body: str = "",
    draft: bool = False,
) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    resource = service.users().drafts() if draft else service.users().messages()
    action = (
        resource.create(userId="me", body={"message": {"raw": encoded}})
        if draft
        else resource.send(userId="me", body={"raw": encoded})
    )
    return action.execute()


def confirm_send(
    recipient: str, subject: str, body: str, html_body: str = "", draft: bool = False
) -> bool:
    print("\nAbout to send:")
    print(f"To: {recipient}")
    print(f"Subject: {subject}")
    print("Body:")
    print(body)
    if html_body:
        print("HTML body supplied.")
    answer = input(f"\n{'Create draft' if draft else 'Send'} this email? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def _export_rows(records: list[EmailRecord], redact: bool) -> list[dict[str, Any]]:
    """Return export rows in the historic schema used by CSV and JSON output."""
    return _export_rows_impl(records, redact)


def write_csv(path: Path, records: list[EmailRecord], redact: bool = False) -> None:
    """Write a Gmail CSV export through the reusable atomic persistence layer."""
    _write_csv(path, records, redact)


def write_json(path: Path, records: list[EmailRecord], redact: bool = False) -> None:
    """Write a Gmail JSON export through the reusable atomic persistence layer."""
    _write_json(path, records, redact)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        _, _, _, _, HttpError = import_google_dependencies()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 4

    try:
        service = get_gmail_service(
            Path(args.credentials),
            Path(args.token),
        )

        if args.send_to:
            if not args.yes and not confirm_send(
                args.send_to,
                args.subject,
                args.body or "",
                args.html_body or "",
                args.draft,
            ):
                print("Send cancelled.")
                return 0

            result = send_email(
                service,
                args.send_to,
                args.subject,
                args.body or "",
                args.html_body or "",
                args.draft,
            )
            print("Draft created successfully." if args.draft else "Email sent successfully.")
            print(f"Message ID: {result.get('id', '')}")
            print(f"Thread ID: {result.get('threadId', '')}")
            return 0

        query = build_query(args)
        records = fetch_messages(
            service,
            query=query,
            max_results=args.max_results,
            include_body=args.include_body,
        )

        if not records:
            print("No matching messages found.")
            return 0

        for index, record in enumerate(records, start=1):
            print(f"\n[{index}] {record.subject or '(No subject)'}")
            print(f"From: {record.sender}")
            print(f"Date: {record.date}")
            print(f"Labels: {', '.join(record.labels)}")
            print(f"Snippet: {record.snippet}")
            if record.body:
                print("Body:")
                print(record.body)
            if args.classify:
                print(f"Classification: {classify_application_email(record)}")

        if args.csv_path:
            write_csv(Path(args.csv_path), records, args.redact)
            print(f"\nCSV written to {args.csv_path}")

        if args.json_path:
            write_json(Path(args.json_path), records, args.redact)
            print(f"\nJSON written to {args.json_path}")

        return 0

    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except HttpError as exc:
        print(f"Gmail API error: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
