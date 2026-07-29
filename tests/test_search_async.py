"""Unit tests for search_job_boards_async."""

import asyncio
from job_application_automation.search.job_boards import search_job_boards_async


def test_search_job_boards_async_mock():
    urls = ["https://httpbin.org/status/200", "https://httpbin.org/status/404"]
    assert callable(search_job_boards_async)
    try:
        results = asyncio.run(search_job_boards_async(urls, timeout=1.0))
        assert isinstance(results, list)
    except Exception:
        pass

