from __future__ import annotations

import base64
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.mail import gmail_client as gmail  # noqa: E402
from job_application_automation.mail import pool_select as email_pool_cli  # noqa: E402
from job_application_automation.mail import gmail_auth, gmail_messages, gmail_persistence  # noqa: E402


def encode_body(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, object]:
        return self.payload


class FakeGmailService:
    def __init__(self, messages: dict[str, dict[str, object]]) -> None:
        self.messages_by_id = messages
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def users(self) -> FakeGmailService:
        return self

    def messages(self) -> FakeGmailService:
        return self

    def list(self, **kwargs: object) -> _Response:
        self.list_calls.append(kwargs)
        return _Response({"messages": [{"id": message_id} for message_id in self.messages_by_id]})

    def get(self, **kwargs: object) -> _Response:
        self.get_calls.append(kwargs)
        message_id = str(kwargs["id"])
        return _Response(self.messages_by_id[message_id])


class _FakeRequest:
    pass


class _FakeCredentials:
    instance: _FakeCredentials | None = None

    def __init__(self) -> None:
        self.valid = False
        self.expired = True
        self.refresh_token = "refresh-token"
        self.refreshed = False

    @classmethod
    def from_authorized_user_file(cls, _path: str, _scopes: list[str]) -> _FakeCredentials:
        cls.instance = cls()
        return cls.instance

    def refresh(self, _request: object) -> None:
        self.refreshed = True
        self.valid = True

    def to_json(self) -> str:
        return '{"token":"fresh"}'


class _UnusedFlow:
    @classmethod
    def from_client_secrets_file(cls, *_args: object, **_kwargs: object) -> _UnusedFlow:
        raise AssertionError("refreshable credentials must not launch OAuth")


class _FailingRefreshCredentials(_FakeCredentials):
    def refresh(self, _request: object) -> None:
        raise RuntimeError("invalid_scope: Invalid OAuth scope or ID token audience provided.")


class _FakeFreshCredentials:
    def to_json(self) -> str:
        return '{"token":"reauthorized"}'


class _ReauthFlow:
    ran_local_server = False

    @classmethod
    def from_client_secrets_file(cls, *_args: object, **_kwargs: object) -> _ReauthFlow:
        return cls()

    def run_local_server(self, *, port: int) -> _FakeFreshCredentials:
        type(self).ran_local_server = True
        return _FakeFreshCredentials()


class GmailOAuthTests(unittest.TestCase):
    def test_oauth_service_refreshes_and_persists_token_through_injected_sdk(self) -> None:
        built: dict[str, object] = {}

        def build(name: str, version: str, **kwargs: object) -> object:
            built.update({"name": name, "version": version, **kwargs})
            return object()

        dependencies = (_FakeRequest, _FakeCredentials, _UnusedFlow, build, object)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials_path = root / "credentials.json"
            token_path = root / "token.json"
            token_path.write_text('{"token":"old"}', encoding="utf-8")

            service = gmail_auth.get_gmail_service(
                credentials_path,
                token_path,
                dependencies=dependencies,
            )

            self.assertIsNotNone(service)
            self.assertTrue(_FakeCredentials.instance and _FakeCredentials.instance.refreshed)
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"token":"fresh"}')
            self.assertEqual(built["name"], "gmail")
            self.assertEqual(built["version"], "v1")
            self.assertFalse(bool(built["cache_discovery"]))

    def test_oauth_service_falls_back_to_local_flow_when_refresh_fails(self) -> None:
        dependencies = (
            _FakeRequest,
            _FailingRefreshCredentials,
            _ReauthFlow,
            lambda *_a, **_k: object(),
            object,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials_path = root / "credentials.json"
            credentials_path.write_text("{}", encoding="utf-8")
            token_path = root / "token.json"
            token_path.write_text('{"token":"stale"}', encoding="utf-8")

            service = gmail_auth.get_gmail_service(
                credentials_path,
                token_path,
                dependencies=dependencies,
            )

            self.assertIsNotNone(service)
            self.assertTrue(_ReauthFlow.ran_local_server)
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"token":"reauthorized"}')

    def test_google_dependency_patch_seam_remains_available(self) -> None:
        dependencies = (
            _FakeRequest,
            _FakeCredentials,
            _UnusedFlow,
            lambda *_args, **_kwargs: object(),
            object,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token.json"
            token_path.write_text('{"token":"old"}', encoding="utf-8")
            with patch.object(gmail, "import_google_dependencies", return_value=dependencies):
                self.assertIsNotNone(gmail.get_gmail_service(root / "credentials.json", token_path))

    def test_read_service_requests_only_the_readonly_scope(self) -> None:
        with patch.object(gmail, "_get_gmail_service", return_value=object()) as get_service:
            with patch.object(gmail, "import_google_dependencies", return_value=("deps",)):
                service = gmail.get_gmail_read_service(
                    Path("credentials.json"),
                    Path("token.json"),
                )

        self.assertIsNotNone(service)
        self.assertEqual(
            get_service.call_args.kwargs["scopes"],
            ["https://www.googleapis.com/auth/gmail.readonly"],
        )


class EmailPoolTests(unittest.TestCase):
    def test_pool_normalizes_addresses_and_uses_legacy_parent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requested = root / "config" / "candidate_email_pool.json"
            root.joinpath("candidate_email_pool.json").write_text(
                json.dumps([" first@example.test ", "second@example.test"]),
                encoding="utf-8",
            )

            self.assertEqual(
                email_pool_cli._load_email_pool(requested),
                ["first@example.test", "second@example.test"],
            )
            with patch.object(
                email_pool_cli.random, "sample", return_value=["second@example.test"]
            ) as sample:
                self.assertEqual(
                    email_pool_cli.get_random_email(requested, count=5),
                    ["second@example.test"],
                )
            sample.assert_called_once_with(
                ["first@example.test", "second@example.test"],
                2,
            )

    def test_pool_rejects_invalid_addresses_and_non_positive_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate_email_pool.json"
            path.write_text(json.dumps(["not-an-address"]), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid email address at item 1"):
                email_pool_cli.get_random_email(path)
            with self.assertRaisesRegex(ValueError, "count must be greater than zero"):
                email_pool_cli.get_random_email(path, count=0)


class GmailMessageTests(unittest.TestCase):
    def test_extract_body_prefers_plain_parts_and_classifies_message(self) -> None:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": encode_body("<p>HTML body</p>")}},
                {
                    "mimeType": "text/plain",
                    "body": {"data": encode_body("Your verification code is 123456")},
                },
            ],
        }
        record = gmail.EmailRecord(
            message_id="message-1",
            thread_id="thread-1",
            sender="Recruiter <recruiter@example.test>",
            recipient="candidate@example.test",
            subject="Security code",
            date="",
            labels=[],
            snippet="",
            body=gmail.extract_body(payload),
        )

        self.assertEqual(record.body, "Your verification code is 123456")
        self.assertEqual(gmail.classify_application_email(record), "verification_code")
        self.assertEqual(
            gmail.html_to_text("<style>x</style><p>Hello<br>world</p>"), "Hello\nworld"
        )

    def test_fetch_messages_preserves_gmail_api_shape_and_metadata_mode(self) -> None:
        message = {
            "id": "message-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX", "UNREAD"],
            "snippet": "A snippet",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Sender <sender@example.test>"},
                    {"name": "Delivered-To", "value": "candidate@example.test"},
                    {"name": "Subject", "value": "Hello"},
                    {"name": "Date", "value": "Tue, 1 Jan 2030 00:00:00 +0000"},
                ],
                "body": {"data": encode_body("not requested")},
            },
        }
        service = FakeGmailService({"message-1": message})

        records = gmail.fetch_messages(service, "in:inbox", 5, include_body=False)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].sender, "Sender <sender@example.test>")
        self.assertEqual(records[0].recipient, "candidate@example.test")
        self.assertEqual(records[0].body, "")
        self.assertEqual(service.list_calls[0]["q"], "in:inbox")
        self.assertEqual(service.get_calls[0]["format"], "metadata")
        self.assertEqual(
            service.get_calls[0]["metadataHeaders"],
            ["From", "To", "Delivered-To", "Subject", "Date"],
        )

    def test_poll_filters_prior_messages_and_returns_matching_code(self) -> None:
        records = [
            gmail.EmailRecord(
                "old",
                "thread-old",
                "Mailer <noreply@ats.test>",
                "candidate@example.test",
                "Code 000000",
                "",
                [],
                "",
                "Code 000000",
            ),
            gmail.EmailRecord(
                "fresh",
                "thread-fresh",
                "Mailer <noreply@ats.test>",
                "candidate@example.test",
                "Code 654321",
                "",
                [],
                "",
                "Code 654321",
            ),
        ]
        monotonic_values = iter([10.0, 10.0])

        match = gmail_messages.poll_for_verification_code(
            object(),
            "newer_than:1d",
            r"Code (\d{6})",
            excluded_message_ids={"old"},
            sender_domains=("ats.test",),
            expected_recipient="Candidate <candidate@example.test>",
            fetcher=lambda *_args, **_kwargs: records,
            monotonic=lambda: next(monotonic_values),
            sleep=lambda _seconds: self.fail("matched message should not sleep"),
        )

        self.assertEqual(
            match,
            gmail.VerificationCodeMatch(
                "fresh", "thread-fresh", "Mailer <noreply@ats.test>", "654321"
            ),
        )

    def test_poll_retains_fetch_and_clock_patch_seams(self) -> None:
        record = gmail.EmailRecord(
            "fresh", "thread", "sender@example.test", "", "OTP 42", "", [], "", "OTP 42"
        )
        service = object()
        with (
            patch.object(gmail, "fetch_messages", return_value=[record]) as fetch,
            patch.object(gmail.time, "monotonic", side_effect=[0.0, 0.0]),
            patch.object(gmail.time, "sleep") as sleep,
        ):
            match = gmail.poll_for_verification_code(service, "", r"OTP (\d+)")

        self.assertEqual(match.code, "42")
        fetch.assert_called_once_with(service, "", max_results=10, include_body=True)
        sleep.assert_not_called()


class GmailPersistenceTests(unittest.TestCase):
    def test_otp_history_is_idempotent_and_does_not_store_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "used_otp_messages.json"
            match = gmail.VerificationCodeMatch(
                "message-1", "thread-1", "sender@example.test", "123456"
            )

            gmail_persistence.record_used_verification_message(
                path, match, clock=lambda: 1_700_000_000
            )
            gmail_persistence.record_used_verification_message(
                path, match, clock=lambda: 1_700_000_001
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                gmail_persistence.load_used_verification_message_ids(path), {"message-1"}
            )
            self.assertEqual(
                payload["used_messages"],
                [
                    {
                        "message_id": "message-1",
                        "thread_id": "thread-1",
                        "sender": "sender@example.test",
                        "recorded_at": 1_700_000_000,
                    }
                ],
            )
            self.assertNotIn("123456", path.read_text(encoding="utf-8"))

    def test_exports_preserve_columns_classification_and_redaction(self) -> None:
        record = gmail.EmailRecord(
            "message-1",
            "thread-1",
            "sender@example.test",
            "candidate@example.test",
            "Thank you for applying",
            "Tue",
            ["INBOX", "UNREAD"],
            "Received",
            "Body text",
        )
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "nested" / "messages.csv"
            json_path = Path(directory) / "nested" / "messages.json"

            gmail.write_csv(csv_path, [record], redact=True)
            gmail.write_json(json_path, [record], redact=True)

            csv_rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
            json_rows = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(csv_rows[0]["labels"], "INBOX;UNREAD")
            self.assertEqual(csv_rows[0]["classification"], "application_confirmation")
            self.assertEqual(csv_rows[0]["sender"], "[redacted]")
            self.assertEqual(json_rows[0]["body"], "[redacted]")
            self.assertEqual(json_rows[0]["classification"], "application_confirmation")


if __name__ == "__main__":
    unittest.main()
