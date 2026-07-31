"""Pure Gmail message parsing, classification, retrieval, and OTP polling.

The module intentionally accepts Gmail's duck-typed service object so it can
be exercised with fakes and does not import Google's optional SDK packages.
"""

from __future__ import annotations

import base64
import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Protocol


class GmailQueryArguments(Protocol):
    """The subset of CLI arguments used to construct a Gmail query."""

    all_mail: bool
    unread: bool
    query: str


@dataclass
class EmailRecord:
    message_id: str
    thread_id: str
    sender: str
    recipient: str
    subject: str
    date: str
    labels: list[str]
    snippet: str
    body: str = ""


@dataclass(frozen=True)
class VerificationCodeMatch:
    message_id: str
    thread_id: str
    sender: str
    code: str


def classify_application_email(record: EmailRecord) -> str:
    """Classify common application-mail outcomes without modifying mail."""
    text = f"{record.subject}\n{record.snippet}\n{record.body}".lower()
    if re.search(r"security code|verification code|one[- ]time code|\botp\b", text):
        return "verification_code"
    if re.search(r"application (?:has been )?(?:received|submitted)|thank you for applying", text):
        return "application_confirmation"
    if re.search(r"interview|schedule time|speak with", text):
        return "interview_or_recruiter"
    if re.search(r"not moving forward|regret to inform|unfortunately", text):
        return "rejection"
    return "unknown"


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Return case-normalized header names from a Gmail message payload."""
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in payload.get("headers", [])
        if item.get("name")
    }


def decode_base64url(data: str) -> str:
    """Decode Gmail's URL-safe base64 text payloads."""
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode((data + padding).encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def html_to_text(value: str) -> str:
    """Produce the historic lightweight text rendering for HTML message parts."""
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", value).strip()


def extract_body(payload: dict[str, Any]) -> str:
    """Collect text parts recursively, preferring text/plain over HTML."""
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


def build_query(args: GmailQueryArguments) -> str:
    """Build the existing Gmail query from CLI read options."""
    clauses: list[str] = []
    if not args.all_mail:
        clauses.append("in:inbox")
    if args.unread:
        clauses.append("is:unread")
    if args.query.strip():
        clauses.append(args.query.strip())
    return " ".join(clauses)


def record_from_gmail_message(message: dict[str, Any], include_body: bool) -> EmailRecord:
    """Translate a Gmail API full/metadata message into an exportable record."""
    payload = message.get("payload", {})
    headers = header_map(payload)
    return EmailRecord(
        message_id=message.get("id", ""),
        thread_id=message.get("threadId", ""),
        sender=headers.get("from", ""),
        recipient=headers.get("delivered-to", "") or headers.get("to", ""),
        subject=headers.get("subject", ""),
        date=headers.get("date", ""),
        labels=list(message.get("labelIds", [])),
        snippet=message.get("snippet", ""),
        body=extract_body(payload) if include_body else "",
    )


def fetch_messages(
    service: Any,
    query: str,
    max_results: int,
    include_body: bool,
) -> list[EmailRecord]:
    """Fetch Gmail records using only the Gmail API surface used by the CLI."""
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
                    else ["From", "To", "Delivered-To", "Subject", "Date"],
                )
                .execute()
            )
            records.append(record_from_gmail_message(message, include_body))

            if len(records) >= max_results:
                break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return records


FetchMessages = Callable[[Any, str, int, bool], list[EmailRecord]]


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
    fetcher: FetchMessages = fetch_messages,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> VerificationCodeMatch | None:
    """Wait for a fresh verification email and return its first matching code."""
    deadline = monotonic() + timeout_seconds
    compiled = re.compile(pattern, re.I)
    excluded_message_ids = excluded_message_ids or set()
    expected_recipient = parseaddr(expected_recipient)[1].lower()
    while True:
        records = fetcher(service, query, max_results=10, include_body=True)
        for record in records:
            if record.message_id in excluded_message_ids:
                continue
            sender_address = parseaddr(record.sender)[1].lower()
            if sender_domains and not any(
                sender_address.endswith(f"@{domain.lower()}") for domain in sender_domains
            ):
                continue
            if expected_recipient and parseaddr(record.recipient)[1].lower() != expected_recipient:
                continue
            match = compiled.search(f"{record.subject}\n{record.body}")
            if match:
                return VerificationCodeMatch(
                    message_id=record.message_id,
                    thread_id=record.thread_id,
                    sender=record.sender,
                    code=match.group(1) if match.groups() else match.group(0),
                )
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            return None
        sleep(min(poll_interval_seconds, remaining_seconds))
