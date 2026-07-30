"""Unit tests for discovery.py page extraction and request planning."""

from job_application_automation.search.discovery import (
    LinkExtractor,
    extract_ats_urls_from_html,
    iter_discovery_requests,
)


def test_link_extractor() -> None:
    extractor = LinkExtractor()
    extractor.feed('<a href="https://example.com/careers">Careers</a><img src="/logo.png" />')
    extractor.close()
    assert "https://example.com/careers" in extractor.urls
    assert "/logo.png" in extractor.urls


def test_iter_discovery_requests_fair_ordering() -> None:
    queries = ["python engineer", "backend dev"]
    site_hosts = ["boards.greenhouse.io"]
    regions = ["us-en"]
    backends = ["ddgs"]

    requests = list(iter_discovery_requests(queries, site_hosts, regions, backends))
    assert len(requests) == 2
    assert requests[0][0] == "python engineer"
    assert requests[0][1] == "boards.greenhouse.io"


def test_extract_ats_urls_from_html() -> None:
    html_markup = """
    <div>
        <a href="https://boards.greenhouse.io/acme/jobs/12345">Apply Here</a>
        <a href="https://jobs.lever.co/acme/67890">Lever Job</a>
        <a href="/invalid-link">Invalid</a>
    </div>
    """

    def dummy_url_key(url: str) -> str:
        if "greenhouse" in url or "lever" in url:
            return url
        return ""

    urls = extract_ats_urls_from_html(
        html_markup,
        base_url="https://example.com",
        discovery_url_key=dummy_url_key,
    )
    assert len(urls) == 2
    assert "https://boards.greenhouse.io/acme/jobs/12345" in urls
    assert "https://jobs.lever.co/acme/67890" in urls
