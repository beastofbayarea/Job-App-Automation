#!/usr/bin/env python3
"""
Gmail OAuth Reader and Sender
=============================

This single-file script can:
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
This script requests both:
- gmail.readonly
- gmail.send

If you previously ran a read-only version and already have token.json, delete
token.json before running this version. Google must ask you to approve the new
send permission.

FIRST RUN
---------
    python src/email_gmail_client.py --max-results 10

A browser window will open for OAuth authorization. After approval, token.json
will be saved locally.

READ EXAMPLES
-------------
Read the 10 newest inbox messages:

    python src/email_gmail_client.py --max-results 10

Read unread messages:

    python src/email_gmail_client.py --unread --max-results 20

Search all mail:

    python src/email_gmail_client.py --all-mail --query "from:example.com newer_than:30d"

Export:

    python src/email_gmail_client.py --max-results 100 --csv output/messages.csv
    python src/email_gmail_client.py --max-results 100 --json output/messages.json

SEND EXAMPLES
-------------
Interactive confirmation:

    python email_gmail_client.py \
        --send-to recipient@example.com \
        --subject "Test email" \
        --body "Hello from my local Python script."

Skip confirmation deliberately:

    python email_gmail_client.py \
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
import csv
import html
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from paths import CONFIG_DIR

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

DEFAULT_CREDENTIALS_FILE = str(CONFIG_DIR / "credentials.json")
DEFAULT_TOKEN_FILE = str(CONFIG_DIR / "token.json")


@dataclass
class EmailRecord:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    labels: list[str]
    snippet: str
    body: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read and send Gmail locally through OAuth."
    )

    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--query", default="")
    parser.add_argument("--all-mail", action="store_true")
    parser.add_argument("--unread", action="store_true")
    parser.add_argument("--include-body", action="store_true")
    parser.add_argument("--csv", dest="csv_path")
    parser.add_argument("--json", dest="json_path")

    parser.add_argument("--send-to")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Send without an interactive confirmation prompt.",
    )

    parser.add_argument("--credentials", default=DEFAULT_CREDENTIALS_FILE)
    parser.add_argument("--token", default=DEFAULT_TOKEN_FILE)

    args = parser.parse_args()

    if args.max_results < 1:
        parser.error("--max-results must be at least 1")

    sending = any([args.send_to, args.subject, args.body])
    if sending and not all([args.send_to, args.subject, args.body]):
        parser.error("--send-to, --subject, and --body must be supplied together")

    return args


def import_google_dependencies() -> tuple[Any, Any, Any, Any, Any]:
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


def get_gmail_service(credentials_path: Path, token_path: Path) -> Any:
    """Return an authorized Gmail client, refreshing or re-running OAuth as needed."""
    Request, Credentials, InstalledAppFlow, build, _ = import_google_dependencies()
    credentials = None

    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"OAuth client file not found: {credentials_path}"
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path),
                SCOPES,
            )
            credentials = flow.run_local_server(port=0)

        token_path.write_text(credentials.to_json(), encoding="utf-8")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in payload.get("headers", [])
        if item.get("name")
    }


def decode_base64url(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode((data + padding).encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", value).strip()


def extract_body(payload: dict[str, Any]) -> str:
    """Recursively collect a message's text parts, preferring plain text over HTML."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType", ""))
        encoded = (part.get("body") or {}).get("data", "")

        if encoded:
            decoded = decode_base64url(encoded)
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)

        for child in part.get("parts", []) or []:
            visit(child)

    visit(payload)

    if plain_parts:
        return "\n\n".join(part.strip() for part in plain_parts if part.strip())
    if html_parts:
        return html_to_text("\n\n".join(html_parts))
    return ""


def build_query(args: argparse.Namespace) -> str:
    clauses: list[str] = []
    if not args.all_mail:
        clauses.append("in:inbox")
    if args.unread:
        clauses.append("is:unread")
    if args.query.strip():
        clauses.append(args.query.strip())
    return " ".join(clauses)


def fetch_messages(
    service: Any,
    query: str,
    max_results: int,
    include_body: bool,
) -> list[EmailRecord]:
    records: list[EmailRecord] = []
    page_token = None

    while len(records) < max_results:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query or None,
                maxResults=min(100, max_results - len(records)),
                pageToken=page_token,
            )
            .execute()
        )

        for item in response.get("messages", []):
            message = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=item["id"],
                    format="full" if include_body else "metadata",
                    metadataHeaders=None
                    if include_body
                    else ["From", "Subject", "Date"],
                )
                .execute()
            )

            headers = header_map(message.get("payload", {}))
            records.append(
                EmailRecord(
                    message_id=message.get("id", ""),
                    thread_id=message.get("threadId", ""),
                    sender=headers.get("from", ""),
                    subject=headers.get("subject", ""),
                    date=headers.get("date", ""),
                    labels=list(message.get("labelIds", [])),
                    snippet=message.get("snippet", ""),
                    body=extract_body(message.get("payload", {}))
                    if include_body
                    else "",
                )
            )

            if len(records) >= max_results:
                break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return records


def send_email(
    service: Any,
    recipient: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    return (
        service.users()
        .messages()
        .send(userId="me", body={"raw": encoded})
        .execute()
    )


def confirm_send(recipient: str, subject: str, body: str) -> bool:
    print("\nAbout to send:")
    print(f"To: {recipient}")
    print(f"Subject: {subject}")
    print("Body:")
    print(body)
    answer = input("\nSend this email? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def write_csv(path: Path, records: list[EmailRecord]) -> None:
    rows = [asdict(record) for record in records]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [
            "message_id", "thread_id", "sender", "subject", "date",
            "labels", "snippet", "body"
        ])
        writer.writeheader()
        for row in rows:
            row["labels"] = ";".join(row["labels"])
            writer.writerow(row)


def write_json(path: Path, records: list[EmailRecord]) -> None:
    path.write_text(
        json.dumps([asdict(record) for record in records], indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()

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
                args.body,
            ):
                print("Send cancelled.")
                return 0

            result = send_email(
                service,
                args.send_to,
                args.subject,
                args.body,
            )
            print("Email sent successfully.")
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

        if args.csv_path:
            write_csv(Path(args.csv_path), records)
            print(f"\nCSV written to {args.csv_path}")

        if args.json_path:
            write_json(Path(args.json_path), records)
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
