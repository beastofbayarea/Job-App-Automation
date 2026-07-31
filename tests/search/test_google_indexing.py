from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core import google_indexing as indexing  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        payload: object | None = None,
        text: str = "",
        url: str = "https://example.com/jobs/1",
        content_type: str = "text/html",
    ) -> None:
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = text
        self.url = url
        self.headers = {"Content-Type": content_type}

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def put(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("PUT", url, dict(kwargs)))
        return self.response

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("POST", url, dict(kwargs)))
        return self.response

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("GET", url, dict(kwargs)))
        return self.response


def sample_config(directory: Path) -> indexing.GoogleSubmissionConfig:
    return indexing.GoogleSubmissionConfig(
        domain="example.com",
        search_console_property="sc-domain:example.com",
        sitemap_url="https://example.com/sitemap.xml",
        eligible_urls=(),
        service_account_file=directory / "key.json",
        service_account_email="indexer@example-project.iam.gserviceaccount.com",
        project_id="example-project",
        indexing_endpoint=indexing.INDEXING_PUBLISH_ENDPOINT,
        indexing_scopes=(indexing.INDEXING_SCOPE,),
        daily_quota=200,
        batch_size=100,
        request_timeout_seconds=30,
        report_file=directory / "report.json",
    )


def write_config_set(directory: Path) -> tuple[Path, Path, Path]:
    key_path = directory / "key.json"
    key_path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "example-project",
                "private_key_id": "key-id",
                "private_key": "test-private-key",
                "client_email": "indexer@example-project.iam.gserviceaccount.com",
            }
        ),
        encoding="utf-8",
    )
    cloud_path = directory / "cloud.json"
    cloud_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reference_sources": {
                    "search_console_indexing_service_account": str(key_path),
                },
                "google": {
                    "cloud_project_id": "example-project",
                    "service_accounts": {
                        "search_console_indexing": {
                            "email": "indexer@example-project.iam.gserviceaccount.com",
                            "key_id": "key-id",
                            "key_file": str(key_path),
                        }
                    },
                    "indexing_api": {
                        "endpoint": indexing.INDEXING_PUBLISH_ENDPOINT,
                        "scopes": [indexing.INDEXING_SCOPE],
                        "daily_quota": 200,
                        "batch_size": 100,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    seo_path = directory / "seo.json"
    seo_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "domain": "example.com",
                "gsc": {"sitemap_url": "https://example.com/sitemap.xml"},
                "google_submission": {
                    "cloud_config_file": str(cloud_path),
                    "search_console_property": "sc-domain:example.com",
                    "eligible_urls": [],
                    "request_timeout_seconds": 30,
                    "report_file": str(directory / "report.json"),
                },
            }
        ),
        encoding="utf-8",
    )
    return seo_path, cloud_path, key_path


class GoogleSubmissionConfigTests(unittest.TestCase):
    def test_loads_and_cross_checks_public_and_private_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            seo_path, cloud_path, key_path = write_config_set(directory)

            config = indexing.load_google_submission_config(seo_path, cloud_path)

            self.assertEqual("example.com", config.domain)
            self.assertEqual("sc-domain:example.com", config.search_console_property)
            self.assertEqual(key_path, config.service_account_file)
            self.assertEqual("example-project", config.project_id)
            self.assertEqual((indexing.INDEXING_SCOPE,), config.indexing_scopes)

    def test_rejects_a_key_identity_that_disagrees_with_role_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            seo_path, cloud_path, key_path = write_config_set(directory)
            key = json.loads(key_path.read_text(encoding="utf-8"))
            key["client_email"] = "wrong@example-project.iam.gserviceaccount.com"
            key_path.write_text(json.dumps(key), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "client_email"):
                indexing.load_google_submission_config(seo_path, cloud_path)

    def test_rejects_a_non_google_publish_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            seo_path, cloud_path, _key_path = write_config_set(directory)
            cloud = json.loads(cloud_path.read_text(encoding="utf-8"))
            cloud["google"]["indexing_api"]["endpoint"] = "https://example.com/collect"
            cloud_path.write_text(json.dumps(cloud), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "v3 publish endpoint"):
                indexing.load_google_submission_config(seo_path, cloud_path)


class GoogleEligibilityTests(unittest.TestCase):
    def test_accepts_jobposting_json_ld(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            html = """
                <html><head><script type="application/ld+json">
                {"@context":"https://schema.org","@type":"JobPosting","title":"Engineer"}
                </script></head></html>
            """

            result = indexing.validate_url_notification(
                config,
                "https://example.com/jobs/1",
                "URL_UPDATED",
                fetcher=lambda *args, **kwargs: FakeResponse(200, text=html),
            )

            self.assertEqual("JobPosting", result["eligibility"])

    def test_accepts_broadcast_event_only_when_nested_in_video_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            html = """
                <script type="application/ld+json">
                {"@type":"VideoObject","publication":{"@type":"BroadcastEvent"}}
                </script>
            """
            result = indexing.validate_url_notification(
                config,
                "https://example.com/live/1",
                "URL_UPDATED",
                fetcher=lambda *args, **kwargs: FakeResponse(
                    200,
                    text=html,
                    url="https://example.com/live/1",
                ),
            )
            self.assertEqual("BroadcastEvent", result["eligibility"])

            standalone = '<script type="application/ld+json">{"@type":"BroadcastEvent"}</script>'
            with self.assertRaisesRegex(ValueError, "embedded in a VideoObject"):
                indexing.validate_url_notification(
                    config,
                    "https://example.com/live/2",
                    "URL_UPDATED",
                    fetcher=lambda *args, **kwargs: FakeResponse(
                        200,
                        text=standalone,
                        url="https://example.com/live/2",
                    ),
                )

    def test_rejects_general_pages_and_foreign_redirects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            html = '<script type="application/ld+json">{"@type":"WebSite"}</script>'
            with self.assertRaisesRegex(ValueError, "requires JobPosting"):
                indexing.validate_url_notification(
                    config,
                    "https://example.com/",
                    "URL_UPDATED",
                    fetcher=lambda *args, **kwargs: FakeResponse(200, text=html),
                )
            with self.assertRaisesRegex(ValueError, "must belong"):
                indexing.validate_url_notification(
                    config,
                    "https://example.com/jobs/1",
                    "URL_UPDATED",
                    fetcher=lambda *args, **kwargs: FakeResponse(
                        200,
                        text=html,
                        url="https://outside.test/jobs/1",
                    ),
                )

    def test_delete_requires_removed_or_noindex_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            removed = indexing.validate_url_notification(
                config,
                "https://example.com/jobs/gone",
                "URL_DELETED",
                fetcher=lambda *args, **kwargs: FakeResponse(
                    410,
                    url="https://example.com/jobs/gone",
                ),
            )
            self.assertEqual("removed-or-noindex", removed["eligibility"])

            noindex_html = '<meta name="robots" content="noindex, nofollow">'
            noindex = indexing.validate_url_notification(
                config,
                "https://example.com/jobs/hidden",
                "URL_DELETED",
                fetcher=lambda *args, **kwargs: FakeResponse(
                    200,
                    text=noindex_html,
                    url="https://example.com/jobs/hidden",
                ),
            )
            self.assertEqual("removed-or-noindex", noindex["eligibility"])

            with self.assertRaisesRegex(ValueError, "404/410"):
                indexing.validate_url_notification(
                    config,
                    "https://example.com/jobs/live",
                    "URL_DELETED",
                    fetcher=lambda *args, **kwargs: FakeResponse(
                        200,
                        text="<html></html>",
                        url="https://example.com/jobs/live",
                    ),
                )


class GoogleApiRequestTests(unittest.TestCase):
    def test_submits_encoded_sitemap_without_a_request_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            session = FakeSession(FakeResponse(204))

            result = indexing.submit_sitemap(config, session=session)

            self.assertEqual("submitted", result["status"])
            method, endpoint, arguments = session.calls[0]
            self.assertEqual("PUT", method)
            self.assertIn("sc-domain%3Aexample.com", endpoint)
            self.assertIn("https%3A%2F%2Fexample.com%2Fsitemap.xml", endpoint)
            self.assertNotIn("json", arguments)

    def test_publishes_exact_notification_payload_and_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            session = FakeSession(
                FakeResponse(
                    200,
                    payload={
                        "urlNotificationMetadata": {
                            "url": "https://example.com/jobs/1",
                        }
                    },
                    content_type="application/json",
                )
            )
            validation = {
                "url": "https://example.com/jobs/1",
                "notification_type": "URL_UPDATED",
                "http_status": 200,
                "eligibility": "JobPosting",
            }

            result = indexing.publish_url_notification(
                config,
                validation,
                session=session,
            )

            self.assertEqual("submitted", result["status"])
            method, endpoint, arguments = session.calls[0]
            self.assertEqual("POST", method)
            self.assertEqual(indexing.INDEXING_PUBLISH_ENDPOINT, endpoint)
            self.assertEqual(
                {
                    "url": "https://example.com/jobs/1",
                    "type": "URL_UPDATED",
                },
                arguments["json"],
            )
            self.assertEqual("application/json", arguments["headers"]["Content-Type"])

    def test_reports_google_error_without_echoing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            session = FakeSession(
                FakeResponse(
                    403,
                    payload={"error": {"message": "Permission denied"}},
                    content_type="application/json",
                )
            )
            with self.assertRaisesRegex(
                indexing.GoogleSubmissionError,
                "HTTP 403: Permission denied",
            ):
                indexing.submit_sitemap(config, session=session)

    def test_status_treats_missing_metadata_as_a_readable_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = sample_config(Path(temporary))
            session = FakeSession(
                FakeResponse(
                    404,
                    payload={"error": {"message": "No notification found"}},
                    content_type="application/json",
                )
            )

            result = indexing.get_notification_status(
                config,
                "https://example.com/jobs/1",
                session=session,
            )

            self.assertEqual("not-found", result["status"])


class GoogleIndexingCliTests(unittest.TestCase):
    def test_sitemap_dry_run_writes_an_atomic_report_without_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            seo_path, cloud_path, _key_path = write_config_set(directory)
            report = directory / "custom-report.json"
            output = StringIO()

            with patch("sys.stdout", output):
                exit_code = indexing.main(
                    [
                        "sitemap",
                        "--seo-config",
                        str(seo_path),
                        "--cloud-config",
                        str(cloud_path),
                        "--report",
                        str(report),
                        "--dry-run",
                    ]
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(payload["dry_run"])
            self.assertEqual("validated", payload["result"]["status"])
            self.assertEqual(payload, json.loads(output.getvalue()))

    def test_submit_without_urls_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            seo_path, cloud_path, _key_path = write_config_set(directory)
            errors = StringIO()

            with patch("sys.stderr", errors):
                exit_code = indexing.main(
                    [
                        "submit",
                        "--seo-config",
                        str(seo_path),
                        "--cloud-config",
                        str(cloud_path),
                        "--dry-run",
                    ]
                )

            self.assertEqual(2, exit_code)
            self.assertIn("No eligible URLs configured", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
