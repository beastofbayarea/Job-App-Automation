"""Submit sitemaps and eligible page notifications to Google.

General site coverage uses the Search Console Sitemaps API. Direct Indexing API
notifications are deliberately limited to pages containing supported structured
data, as required by Google.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .foundation import read_json, write_json
from .foundation import PROJECT_ROOT
from .runtime_config import RUNTIME_CONFIG, resolve_runtime_path


DEFAULT_SEO_CONFIG = resolve_runtime_path(RUNTIME_CONFIG.application.seo_config_file)
DEFAULT_CLOUD_CONFIG = PROJECT_ROOT / "data" / "private" / "google_cloud_config.json"
INDEXING_SCOPE = "https://www.googleapis.com/auth/indexing"
SEARCH_CONSOLE_SCOPE = "https://www.googleapis.com/auth/webmasters"
INDEXING_PUBLISH_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"
INDEXING_METADATA_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications/metadata"
SEARCH_CONSOLE_SITEMAPS_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites"
SUPPORTED_NOTIFICATION_TYPES = frozenset({"URL_UPDATED", "URL_DELETED"})
SUPPORTED_STRUCTURED_DATA = frozenset({"JobPosting", "BroadcastEvent"})


class GoogleSubmissionError(RuntimeError):
    """A Google authentication, permission, or remote API failure."""


@dataclass(frozen=True)
class GoogleSubmissionConfig:
    """Validated settings assembled from the public SEO and private cloud configs."""

    domain: str
    search_console_property: str
    sitemap_url: str
    eligible_urls: tuple[str, ...]
    service_account_file: Path
    service_account_email: str
    project_id: str
    indexing_endpoint: str
    indexing_scopes: tuple[str, ...]
    daily_quota: int
    batch_size: int
    request_timeout_seconds: int
    report_file: Path


class _StructuredDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[object] = []
        self.robots_noindex = False
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        if tag.lower() == "script" and attributes.get("type", "").lower() == (
            "application/ld+json"
        ):
            self._in_json_ld = True
            self._json_ld_parts = []
        if tag.lower() == "meta" and attributes.get("name", "").lower() in {
            "robots",
            "googlebot",
        }:
            directives = {item.strip().lower() for item in attributes.get("content", "").split(",")}
            if "noindex" in directives:
                self.robots_noindex = True

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self._in_json_ld:
            return
        self._in_json_ld = False
        payload = "".join(self._json_ld_parts).strip()
        self._json_ld_parts = []
        if not payload:
            return
        try:
            self.documents.append(json.loads(payload))
        except json.JSONDecodeError:
            return


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _project_path(value: str, label: str) -> Path:
    candidate = Path(value).expanduser()
    resolved = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
    resolved = resolved.resolve()
    if not resolved.name:
        raise ValueError(f"{label} must name a file")
    return resolved


def _load_document(path: Path, label: str) -> Mapping[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file: {path}")
    try:
        return _mapping(read_json(path), label)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {label}: {path}") from exc


def _validate_domain_url(url: str, domain: str, label: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    normalized_domain = domain.lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    if host != normalized_domain and not host.endswith(f".{normalized_domain}"):
        raise ValueError(f"{label} must belong to {domain}")
    if parsed.fragment:
        raise ValueError(f"{label} cannot contain a fragment")
    return url


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{label} must be an array of non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} cannot contain duplicates")
    return normalized


def load_google_submission_config(
    seo_config_file: str | Path = DEFAULT_SEO_CONFIG,
    cloud_config_file: str | Path | None = None,
    *,
    report_override: str | Path | None = None,
) -> GoogleSubmissionConfig:
    """Load and cross-check site, endpoint, quota, identity, and key metadata."""

    seo_path = Path(seo_config_file).expanduser().resolve()
    seo = _load_document(seo_path, "SEO config")
    if seo.get("schema_version") != 1:
        raise ValueError("SEO config schema_version must be 1")

    domain = _text(seo.get("domain"), "SEO config domain").lower().rstrip(".")
    gsc = _mapping(seo.get("gsc"), "SEO config gsc")
    sitemap_url = _validate_domain_url(
        _text(gsc.get("sitemap_url"), "SEO config gsc.sitemap_url"),
        domain,
        "SEO config gsc.sitemap_url",
    )
    submission = _mapping(
        seo.get("google_submission"),
        "SEO config google_submission",
    )
    search_console_property = _text(
        submission.get("search_console_property"),
        "SEO config google_submission.search_console_property",
    )
    if search_console_property != f"sc-domain:{domain}":
        raise ValueError(
            "google_submission.search_console_property must match the configured domain"
        )
    configured_cloud_file = _text(
        submission.get("cloud_config_file", str(DEFAULT_CLOUD_CONFIG)),
        "SEO config google_submission.cloud_config_file",
    )
    cloud_path = (
        Path(cloud_config_file).expanduser().resolve()
        if cloud_config_file is not None
        else _project_path(configured_cloud_file, "cloud config file")
    )
    timeout = _positive_int(
        submission.get("request_timeout_seconds", 30),
        "SEO config google_submission.request_timeout_seconds",
    )
    eligible_urls = _string_list(
        submission.get("eligible_urls", []),
        "SEO config google_submission.eligible_urls",
    )
    eligible_urls = tuple(
        _validate_domain_url(url, domain, "configured eligible URL") for url in eligible_urls
    )
    report_value = (
        str(report_override)
        if report_override is not None
        else _text(
            submission.get(
                "report_file",
                "output/google_url_submission_report.json",
            ),
            "SEO config google_submission.report_file",
        )
    )
    report_file = _project_path(report_value, "Google submission report")

    cloud = _load_document(cloud_path, "private cloud config")
    if cloud.get("schema_version") != 1:
        raise ValueError("private cloud config schema_version must be 1")
    reference_sources = _mapping(
        cloud.get("reference_sources"),
        "private cloud config reference_sources",
    )
    google = _mapping(cloud.get("google"), "private cloud config google")
    service_accounts = _mapping(
        google.get("service_accounts"),
        "private cloud config google.service_accounts",
    )
    indexing_account = _mapping(
        service_accounts.get("search_console_indexing"),
        "private cloud config google.service_accounts.search_console_indexing",
    )
    role_key_file = _text(
        indexing_account.get("key_file"),
        "private cloud config search_console_indexing.key_file",
    )
    reference_key_file = _text(
        reference_sources.get("search_console_indexing_service_account"),
        "private cloud config reference_sources.search_console_indexing_service_account",
    )
    if _project_path(role_key_file, "search_console_indexing.key_file") != _project_path(
        reference_key_file,
        "reference search_console_indexing_service_account",
    ):
        raise ValueError("private cloud config indexing key-file references disagree")
    service_account_file = _project_path(role_key_file, "indexing service-account file")
    key = _load_document(service_account_file, "indexing service-account file")
    if key.get("type") != "service_account":
        raise ValueError("indexing credential must be a Google service-account key")

    project_id = _text(google.get("cloud_project_id"), "private cloud project ID")
    expected_email = _text(
        indexing_account.get("email"),
        "private cloud indexing service-account email",
    )
    expected_key_id = _text(
        indexing_account.get("key_id"),
        "private cloud indexing service-account key ID",
    )
    if key.get("project_id") != project_id:
        raise ValueError("indexing key project_id does not match private cloud config")
    if key.get("client_email") != expected_email:
        raise ValueError("indexing key client_email does not match private cloud config")
    if key.get("private_key_id") != expected_key_id:
        raise ValueError("indexing key private_key_id does not match private cloud config")
    if not isinstance(key.get("private_key"), str) or not key["private_key"].strip():
        raise ValueError("indexing service-account key is missing private_key")

    indexing = _mapping(
        google.get("indexing_api"),
        "private cloud config google.indexing_api",
    )
    endpoint = _text(indexing.get("endpoint"), "private cloud indexing endpoint")
    if endpoint != INDEXING_PUBLISH_ENDPOINT:
        raise ValueError("private cloud indexing endpoint is not Google's v3 publish endpoint")
    scopes = _string_list(indexing.get("scopes"), "private cloud indexing scopes")
    if INDEXING_SCOPE not in scopes:
        raise ValueError("private cloud indexing scopes must include the Indexing API scope")
    daily_quota = _positive_int(indexing.get("daily_quota"), "indexing daily quota")
    batch_size = _positive_int(indexing.get("batch_size"), "indexing batch size")
    if batch_size > 100 or batch_size > daily_quota:
        raise ValueError("indexing batch size cannot exceed 100 or the daily quota")

    return GoogleSubmissionConfig(
        domain=domain,
        search_console_property=search_console_property,
        sitemap_url=sitemap_url,
        eligible_urls=eligible_urls,
        service_account_file=service_account_file,
        service_account_email=expected_email,
        project_id=project_id,
        indexing_endpoint=endpoint,
        indexing_scopes=scopes,
        daily_quota=daily_quota,
        batch_size=batch_size,
        request_timeout_seconds=timeout,
        report_file=report_file,
    )


def _node_types(node: Mapping[str, object]) -> set[str]:
    value = node.get("@type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _contains_type(node: object, expected: str) -> bool:
    if isinstance(node, Mapping):
        if expected in _node_types(node):
            return True
        return any(_contains_type(value, expected) for value in node.values())
    if isinstance(node, list):
        return any(_contains_type(item, expected) for item in node)
    return False


def _eligible_structured_data(documents: Sequence[object]) -> str | None:
    if any(_contains_type(document, "JobPosting") for document in documents):
        return "JobPosting"

    def qualifying_broadcast(node: object) -> bool:
        if isinstance(node, Mapping):
            if "VideoObject" in _node_types(node) and any(
                _contains_type(value, "BroadcastEvent")
                for key, value in node.items()
                if key != "@type"
            ):
                return True
            return any(qualifying_broadcast(value) for value in node.values())
        if isinstance(node, list):
            return any(qualifying_broadcast(item) for item in node)
        return False

    if any(qualifying_broadcast(document) for document in documents):
        return "BroadcastEvent"
    return None


def validate_url_notification(
    config: GoogleSubmissionConfig,
    url: str,
    notification_type: str,
    *,
    fetcher: Callable[..., Any] = requests.get,
) -> dict[str, object]:
    """Validate ownership and Google's content/removal prerequisites."""

    normalized_type = notification_type.upper()
    if normalized_type not in SUPPORTED_NOTIFICATION_TYPES:
        raise ValueError(f"unsupported notification type: {notification_type}")
    normalized_url = _validate_domain_url(url, config.domain, "submission URL")
    try:
        response = fetcher(
            normalized_url,
            timeout=config.request_timeout_seconds,
            allow_redirects=True,
            headers={"User-Agent": "JobApplicationAutomation-GoogleIndexing/1.0"},
        )
    except requests.RequestException as exc:
        raise ValueError(f"Could not load submission URL: {normalized_url}") from exc

    final_url = str(getattr(response, "url", normalized_url) or normalized_url)
    _validate_domain_url(final_url, config.domain, "final submission URL")
    status_code = int(getattr(response, "status_code", 0))
    content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
    body = str(getattr(response, "text", ""))
    parser = _StructuredDataParser()
    if "html" in content_type or body.lstrip().startswith("<"):
        parser.feed(body)

    if normalized_type == "URL_DELETED":
        if status_code not in {404, 410} and not parser.robots_noindex:
            raise ValueError(
                "URL_DELETED requires a 404/410 response or a noindex robots directive"
            )
        return {
            "url": normalized_url,
            "notification_type": normalized_type,
            "http_status": status_code,
            "eligibility": "removed-or-noindex",
        }

    if status_code != 200:
        raise ValueError(f"URL_UPDATED requires HTTP 200; received {status_code}")
    structured_data_type = _eligible_structured_data(parser.documents)
    if structured_data_type not in SUPPORTED_STRUCTURED_DATA:
        raise ValueError(
            "URL_UPDATED requires JobPosting structured data or BroadcastEvent "
            "embedded in a VideoObject"
        )
    return {
        "url": normalized_url,
        "notification_type": normalized_type,
        "http_status": status_code,
        "eligibility": structured_data_type,
    }


def _authorized_session(config: GoogleSubmissionConfig, scopes: Sequence[str]) -> Any:
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GoogleSubmissionError(
            "Google authentication dependencies are missing; install project requirements"
        ) from exc
    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(config.service_account_file),
            scopes=list(scopes),
        )
    except (OSError, ValueError) as exc:
        raise GoogleSubmissionError("Could not load Google service-account credentials") from exc
    if credentials.service_account_email != config.service_account_email:
        raise GoogleSubmissionError(
            "Loaded Google credential identity does not match configuration"
        )
    return AuthorizedSession(credentials)


def _response_payload(response: Any) -> object:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return {}


def _google_error(response: Any, action: str) -> GoogleSubmissionError:
    payload = _response_payload(response)
    message = ""
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            message = error["message"].strip()
    suffix = f": {message}" if message else ""
    return GoogleSubmissionError(
        f"{action} returned HTTP {getattr(response, 'status_code', 'unknown')}{suffix}"
    )


def submit_sitemap(
    config: GoogleSubmissionConfig,
    *,
    session: Any | None = None,
) -> dict[str, object]:
    """Submit the configured sitemap through the Search Console API."""

    active_session = session or _authorized_session(config, [SEARCH_CONSOLE_SCOPE])
    endpoint = (
        f"{SEARCH_CONSOLE_SITEMAPS_ENDPOINT}/"
        f"{quote(config.search_console_property, safe='')}/sitemaps/"
        f"{quote(config.sitemap_url, safe='')}"
    )
    response = active_session.put(endpoint, timeout=config.request_timeout_seconds)
    if not 200 <= int(response.status_code) < 300:
        raise _google_error(response, "Search Console sitemap submission")
    return {
        "property": config.search_console_property,
        "sitemap_url": config.sitemap_url,
        "http_status": int(response.status_code),
        "status": "submitted",
    }


def publish_url_notification(
    config: GoogleSubmissionConfig,
    validation: Mapping[str, object],
    *,
    session: Any | None = None,
) -> dict[str, object]:
    """Publish one prevalidated update or deletion notification."""

    active_session = session or _authorized_session(config, config.indexing_scopes)
    body = {
        "url": validation["url"],
        "type": validation["notification_type"],
    }
    response = active_session.post(
        config.indexing_endpoint,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=config.request_timeout_seconds,
    )
    if int(response.status_code) != 200:
        raise _google_error(response, "Google Indexing API submission")
    return {
        **dict(validation),
        "http_status": int(response.status_code),
        "status": "submitted",
        "google": _response_payload(response),
    }


def get_notification_status(
    config: GoogleSubmissionConfig,
    url: str,
    *,
    session: Any | None = None,
) -> dict[str, object]:
    """Read the last notification metadata Google received for one owned URL."""

    normalized_url = _validate_domain_url(url, config.domain, "status URL")
    active_session = session or _authorized_session(config, config.indexing_scopes)
    response = active_session.get(
        INDEXING_METADATA_ENDPOINT,
        params={"url": normalized_url},
        timeout=config.request_timeout_seconds,
    )
    if int(response.status_code) == 404:
        return {
            "url": normalized_url,
            "status": "not-found",
            "http_status": 404,
            "google": _response_payload(response),
        }
    if int(response.status_code) != 200:
        raise _google_error(response, "Google Indexing API status")
    return {
        "url": normalized_url,
        "status": "found",
        "http_status": 200,
        "google": _response_payload(response),
    }


def _base_report(
    config: GoogleSubmissionConfig,
    operation: str,
    *,
    dry_run: bool,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "dry_run": dry_run,
        "project_id": config.project_id,
        "service_account": config.service_account_email,
        "domain": config.domain,
    }


def _write_and_print(config: GoogleSubmissionConfig, payload: Mapping[str, object]) -> None:
    write_json(config.report_file, payload, indent=2, sort_keys=True)
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--seo-config",
        default=str(DEFAULT_SEO_CONFIG),
        help="Tracked site/SEO configuration JSON",
    )
    parser.add_argument(
        "--cloud-config",
        default="",
        help="Ignored cloud inventory JSON; defaults to google_submission.cloud_config_file",
    )
    parser.add_argument(
        "--report",
        default="",
        help="Override the atomic JSON report path",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Submit the configured sitemap to Search Console or notify Google's "
            "Indexing API about eligible job/livestream pages."
        )
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    sitemap = subparsers.add_parser(
        "sitemap",
        help="Submit the configured sitemap through the Search Console API",
    )
    _add_common_arguments(sitemap)
    sitemap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration without calling Google",
    )

    submit = subparsers.add_parser(
        "submit",
        help="Submit eligible URL_UPDATED or URL_DELETED notifications",
    )
    _add_common_arguments(submit)
    submit.add_argument(
        "--url",
        action="append",
        default=[],
        help="Owned HTTPS URL; repeat as needed (defaults to configured eligible_urls)",
    )
    submit.add_argument(
        "--type",
        choices=sorted(SUPPORTED_NOTIFICATION_TYPES),
        default="URL_UPDATED",
        dest="notification_type",
    )
    submit.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate pages without sending Google notifications",
    )

    status = subparsers.add_parser(
        "status",
        help="Read the last Indexing API notification metadata for a URL",
    )
    _add_common_arguments(status)
    status.add_argument("--url", required=True, help="Owned HTTPS URL")
    return parser


def _config_from_args(args: argparse.Namespace) -> GoogleSubmissionConfig:
    return load_google_submission_config(
        args.seo_config,
        args.cloud_config or None,
        report_override=args.report or None,
    )


def _run_sitemap(args: argparse.Namespace, config: GoogleSubmissionConfig) -> int:
    payload = _base_report(config, "sitemap", dry_run=args.dry_run)
    if args.dry_run:
        payload["result"] = {
            "property": config.search_console_property,
            "sitemap_url": config.sitemap_url,
            "status": "validated",
        }
    else:
        payload["result"] = submit_sitemap(config)
    _write_and_print(config, payload)
    return 0


def _run_submit(args: argparse.Namespace, config: GoogleSubmissionConfig) -> int:
    urls = tuple(args.url) if args.url else config.eligible_urls
    if not urls:
        raise ValueError(
            "No eligible URLs configured; pass --url or add google_submission.eligible_urls"
        )
    if len(urls) > config.batch_size:
        raise ValueError(
            f"requested {len(urls)} URLs, exceeding configured batch_size {config.batch_size}"
        )
    if len(set(urls)) != len(urls):
        raise ValueError("submission URLs cannot contain duplicates")

    validations = [validate_url_notification(config, url, args.notification_type) for url in urls]
    payload = _base_report(config, "submit", dry_run=args.dry_run)
    if args.dry_run:
        payload["results"] = [{**validation, "status": "validated"} for validation in validations]
        _write_and_print(config, payload)
        return 0

    session = _authorized_session(config, config.indexing_scopes)
    results: list[dict[str, object]] = []
    failures = 0
    for validation in validations:
        try:
            results.append(publish_url_notification(config, validation, session=session))
        except GoogleSubmissionError as exc:
            failures += 1
            results.append(
                {
                    **validation,
                    "status": "failed",
                    "error": str(exc),
                }
            )
    payload["results"] = results
    payload["status"] = "success" if failures == 0 else "partial-or-failed"
    _write_and_print(config, payload)
    return 0 if failures == 0 else 1


def _run_status(args: argparse.Namespace, config: GoogleSubmissionConfig) -> int:
    payload = _base_report(config, "status", dry_run=False)
    payload["result"] = get_notification_status(config, args.url)
    _write_and_print(config, payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Google sitemap and eligible-URL submission CLI."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
        if args.operation == "sitemap":
            return _run_sitemap(args, config)
        if args.operation == "submit":
            return _run_submit(args, config)
        if args.operation == "status":
            return _run_status(args, config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Invalid Google submission input: {exc}", file=sys.stderr)
        return 2
    except (GoogleSubmissionError, OSError, requests.RequestException) as exc:
        print(f"Google submission failed: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown Google submission operation: {args.operation}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
