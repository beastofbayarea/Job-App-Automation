"""Unit tests for search_job_boards_async."""

import asyncio
from unittest.mock import patch

from job_application_automation.search.job_boards import search_job_boards_async


def test_search_job_boards_async_mock(socket_enabled):
    urls = ["https://example.test/available", "https://example.test/missing"]

    class Response:
        def __init__(self, status_code: int, text: str = "") -> None:
            self.status_code = status_code
            self.text = text

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str) -> Response:
            if url.endswith("/available"):
                return Response(200, "available role")
            return Response(404)

    assert callable(search_job_boards_async)
    with patch("httpx.AsyncClient", return_value=Client()):
        results = asyncio.run(search_job_boards_async(urls, timeout=1.0))

    assert results[0] == {
        "url": "https://example.test/available",
        "status": 200,
        "text": "available role",
    }
    assert results[1]["url"] == "https://example.test/missing"
    assert results[1]["status"] == 404
    assert results[1]["error"] == "HTTP 404"
