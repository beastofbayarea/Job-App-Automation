"""Deterministic discovery planning and one-hop career-page extraction.

Network access remains owned by the compatible CLI module.  The routines here
accept transport and model callbacks so discovery waves can be tested with
simple fakes and never need to instantiate DDGS in unit tests.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urljoin, urlparse


class LinkExtractor(HTMLParser):
    """Collect link-like HTML attributes without crawling beyond one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src", "data-url"} and value:
                self.urls.append(value)


def iter_discovery_requests(
    queries: Sequence[Any],
    site_hosts: Sequence[str],
    regions: Sequence[str],
    backends: Sequence[str],
) -> Iterator[tuple[Any, str, str, str]]:
    """Yield a fair diagonal ordering of query/host/region/backend requests."""
    variants = [
        (site_host, region, backend)
        for site_host in site_hosts
        for region in regions
        for backend in backends
    ]
    if not variants:
        return
    for offset in range(len(variants)):
        for query_index, discovery_query in enumerate(queries):
            site_host, region, backend = variants[(query_index + offset) % len(variants)]
            yield discovery_query, site_host, region, backend


def extract_ats_urls_from_html(
    html_text: str,
    *,
    base_url: str,
    discovery_url_key: Callable[[str], str],
) -> list[str]:
    """Return deduplicated HTTP(S) ATS links from static page markup."""
    decoded = html.unescape(html_text).replace(r"\/", "/")
    url_pattern = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
    urls: list[str] = []
    seen: set[str] = set()

    parser = LinkExtractor()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception:
        # A raw absolute-url scan still finds useful links in malformed markup.
        parser.urls = []

    raw_urls = [match.group(0) for match in url_pattern.finditer(decoded)]
    raw_urls.extend(parser.urls)
    for raw_url in raw_urls:
        url = urljoin(base_url, raw_url).rstrip('.,;:)]}"')
        if urlparse(url).scheme.lower() not in {"http", "https"}:
            continue
        key = discovery_url_key(url)
        if key and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def discover_boards(
    *,
    queries: Sequence[Any],
    site_hosts: Sequence[str],
    allowed_platforms: set[str],
    regions: Sequence[str],
    timelimit: str | None,
    results_per_query: int,
    backends: Sequence[str],
    timeout: float,
    delay: float,
    max_queries: int,
    search_retries: int,
    stats: Any,
    now_text: str,
    search_text: Callable[..., list[dict[str, Any]]],
    unwrap_url: Callable[[str], str],
    board_from_url: Callable[[str], Any | None],
    looks_like_job_url: Callable[[str], bool],
    make_candidate: Callable[..., Any],
    add_candidate: Callable[[dict[str, list[Any]], Any], bool],
    clean_text: Callable[[Any], str],
    sleep: Callable[[float], None],
    logger: Any,
    on_progress: Callable[[set[Any], dict[str, list[Any]], Any], None] | None = None,
) -> tuple[set[Any], dict[str, list[Any]], Any]:
    """Run discovery using injected I/O while retaining original cache semantics."""
    boards: set[Any] = set()
    candidates_by_board: dict[str, list[Any]] = {}
    stats.queries_planned = len(queries) * len(site_hosts) * len(regions) * len(backends)

    for discovery_query, site_host, region, backend in iter_discovery_requests(
        queries,
        site_hosts,
        regions,
        backends,
    ):
        if max_queries > 0 and stats.queries_attempted >= max_queries:
            logger.warning(
                "Discovery query budget reached (%d of %d planned). Use "
                "--max-discovery-queries 0 to run every planned query.",
                stats.queries_attempted,
                stats.queries_planned,
            )
            return boards, candidates_by_board, stats
        query = f"{site_host} {discovery_query.text}"
        logger.info("Searching [%s/%s]: %s", backend, region, query)
        stats.queries_attempted += 1
        try:
            results = search_text(
                query,
                region=region,
                timelimit=timelimit,
                max_results=results_per_query,
                backend=backend,
                timeout=timeout,
                retries=search_retries,
                retry_delay=max(delay, 0.25),
            )
        except Exception as exc:
            stats.query_failures += 1
            stats.add_query(
                query=query,
                family=discovery_query.family,
                backend=backend,
                region=region,
                status=f"error: {clean_text(exc)}",
            )
            logger.warning("Search failed for %r: %s", query, exc)
            if on_progress is not None:
                on_progress(boards, candidates_by_board, stats)
            continue

        stats.add_query(
            query=query,
            family=discovery_query.family,
            backend=backend,
            region=region,
            status=f"ok:{len(results)}",
        )
        stats.results_seen += len(results)
        provenance = f"{discovery_query.family}|{backend}|{region}|{site_host}"
        for result in results:
            if not isinstance(result, dict):
                continue
            raw_url = str(result.get("href") or result.get("url") or "").strip()
            if not raw_url:
                continue
            url = unwrap_url(raw_url)
            board = board_from_url(url)
            if board is None or board.platform not in allowed_platforms:
                continue
            before = len(boards)
            boards.add(board)
            stats.boards_discovered += int(len(boards) > before)
            if not looks_like_job_url(url):
                continue
            added = add_candidate(
                candidates_by_board,
                make_candidate(
                    url=url,
                    title=clean_text(result.get("title", "")),
                    snippet=clean_text(result.get("body", "")),
                    board=board,
                    provenance=[provenance],
                    first_seen_at=now_text,
                    last_seen_at=now_text,
                ),
            )
            stats.candidates_discovered += int(added)

        if delay > 0:
            sleep(delay)
        if on_progress is not None:
            on_progress(boards, candidates_by_board, stats)

    return boards, candidates_by_board, stats


def discover_boards_from_career_pages(
    session: Any,
    page_urls: Sequence[str],
    *,
    allowed_platforms: set[str],
    timeout: float,
    delay: float,
    max_pages: int,
    now_text: str,
    extract_urls: Callable[..., list[str]],
    board_from_url: Callable[[str], Any | None],
    looks_like_job_url: Callable[[str], bool],
    make_candidate: Callable[..., Any],
    add_candidate: Callable[[dict[str, list[Any]], Any], bool],
    sleep: Callable[[float], None],
    logger: Any,
) -> tuple[set[Any], dict[str, list[Any]]]:
    """Discover boards from explicitly supplied career pages with injectable I/O."""
    boards: set[Any] = set()
    candidates_by_board: dict[str, list[Any]] = {}
    checked = 0
    for raw_url in page_urls:
        if max_pages > 0 and checked >= max_pages:
            break
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"}:
            logger.warning("Skipping non-HTTP career page: %s", raw_url)
            continue
        checked += 1
        try:
            response = session.get(
                raw_url,
                timeout=timeout,
                headers={"Accept": "text/html,*/*;q=0.8"},
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Career-page fetch failed for %s: %s", raw_url, exc)
            continue

        for url in [
            response.url,
            *extract_urls(response.text, base_url=response.url),
        ]:
            board = board_from_url(url)
            if board is None or board.platform not in allowed_platforms:
                continue
            boards.add(board)
            if looks_like_job_url(url):
                add_candidate(
                    candidates_by_board,
                    make_candidate(
                        url=url,
                        board=board,
                        provenance=[f"career_page|{raw_url}"],
                        first_seen_at=now_text,
                        last_seen_at=now_text,
                    ),
                )
        if delay > 0:
            sleep(delay)
    return boards, candidates_by_board
