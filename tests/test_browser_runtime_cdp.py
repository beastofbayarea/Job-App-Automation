from __future__ import annotations

# ruff: noqa: S101 - pytest assertions provide the clearest focused CDP contracts.

import io
import json
from typing import Any

import pytest

from job_application_automation.core.foundation import BrowserAutomationError
from job_application_automation.engines import browser_runtime


def _json_response(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _target_payload(
    target_id: str = "TARGET-1",
    *,
    url: str = "about:blank#job-automation-marker",
    target_type: str = "page",
    socket_target_id: str | None = None,
) -> list[dict[str, str]]:
    socket_id = socket_target_id or target_id
    return [
        {
            "id": target_id,
            "type": target_type,
            "url": url,
            "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{socket_id}",
        }
    ]


def test_validate_background_tab_requires_exact_target_and_marker() -> None:
    marker = "about:blank#job-automation-marker"

    def urlopen(url: str, *, timeout: int) -> io.BytesIO:
        assert url == "http://127.0.0.1:9222/json/list"
        assert timeout == 5
        return _json_response(_target_payload(url=marker))

    info = browser_runtime._cdp_target_info(
        "http://127.0.0.1:9222",
        "TARGET-1",
        urlopen=urlopen,
    )
    assert info["id"] == "TARGET-1"

    original_lookup = browser_runtime._cdp_target_info
    browser_runtime._cdp_target_info = lambda endpoint, target_id: info
    try:
        assert (
            browser_runtime.validate_background_tab(
                "http://127.0.0.1:9222",
                "TARGET-1",
                expected_marker=marker,
            )["url"]
            == marker
        )
        with pytest.raises(BrowserAutomationError, match="marker did not match"):
            browser_runtime.validate_background_tab(
                "http://127.0.0.1:9222",
                "TARGET-1",
                expected_marker="about:blank#other",
            )
    finally:
        browser_runtime._cdp_target_info = original_lookup


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "target is unavailable"),
        (_target_payload(target_type="iframe"), "target is not a page"),
        (
            _target_payload(socket_target_id="OTHER-TARGET"),
            "WebSocket did not match requested target",
        ),
    ],
)
def test_target_lookup_rejects_nonexact_targets(
    payload: object,
    message: str,
) -> None:
    with pytest.raises(BrowserAutomationError, match=message):
        browser_runtime._cdp_target_info(
            "http://127.0.0.1:9222",
            "TARGET-1",
            urlopen=lambda *_args, **_kwargs: _json_response(payload),
        )


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.replies = iter(
            (
                {"method": "Page.frameStartedLoading", "params": {}},
                {"id": 1, "result": {"loaderId": "loader-1"}},
            )
        )

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self, *, timeout: int) -> str:
        assert timeout == 5
        return json.dumps(next(self.replies))


def test_raw_target_reload_sends_one_nonactivating_command() -> None:
    socket = _FakeSocket()

    def connect_socket(
        url: str,
        *,
        open_timeout: int,
        close_timeout: int,
    ) -> _FakeSocket:
        assert url.endswith("/devtools/page/TARGET-1")
        assert (open_timeout, close_timeout) == (5, 2)
        return socket

    result = browser_runtime._raw_target_cdp_command(
        "ws://127.0.0.1:9222/devtools/page/TARGET-1",
        "Page.reload",
        {"ignoreCache": False},
        connect_socket=connect_socket,
    )

    assert result == {"loaderId": "loader-1"}
    assert socket.sent == [
        {
            "id": 1,
            "method": "Page.reload",
            "params": {"ignoreCache": False},
        }
    ]
    assert all(item["method"] != "Target.activateTarget" for item in socket.sent)


def test_reload_background_tab_validates_then_reloads_exact_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    marker = "about:blank#job-automation-marker"
    info = {
        "id": "TARGET-1",
        "type": "page",
        "url": "https://example.test/job/1",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/TARGET-1",
    }

    def validate(endpoint: str, target_id: str, *, expected_marker: str) -> object:
        calls.append(("validate", endpoint, target_id, expected_marker))
        return info

    def command(url: str, method: str, params: object) -> dict[str, str]:
        calls.append(("command", url, method, params))
        return {"loaderId": "loader-1"}

    monkeypatch.setattr(browser_runtime, "validate_background_tab", validate)
    monkeypatch.setattr(browser_runtime, "_raw_target_cdp_command", command)

    result = browser_runtime.reload_background_tab(
        "http://127.0.0.1:9222",
        "TARGET-1",
        expected_marker=marker,
    )

    assert result == {"loaderId": "loader-1"}
    assert calls == [
        ("validate", "http://127.0.0.1:9222", "TARGET-1", marker),
        (
            "command",
            info["webSocketDebuggerUrl"],
            "Page.reload",
            {"ignoreCache": False},
        ),
    ]


class _FakePage:
    def __init__(self, url: str = "https://example.test/job/1") -> None:
        self.url = url
        self.reload_calls = 0
        self.goto_calls = 0

    def is_closed(self) -> bool:
        return False

    def reload(self, **_kwargs: object) -> None:
        self.reload_calls += 1

    def goto(self, *_args: object, **_kwargs: object) -> None:
        self.goto_calls += 1


class _FakeCdpSession:
    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        self.detached = False

    def send(self, method: str) -> dict[str, object]:
        assert method == "Target.getTargetInfo"
        return {"targetInfo": {"targetId": self.target_id}}

    def detach(self) -> None:
        self.detached = True


class _FakeContext:
    def __init__(self, pages: list[_FakePage], ids: list[str]) -> None:
        self.pages = pages
        self.sessions = {
            id(page): _FakeCdpSession(target_id) for page, target_id in zip(pages, ids, strict=True)
        }

    def new_cdp_session(self, page: _FakePage) -> _FakeCdpSession:
        return self.sessions[id(page)]


def test_resolve_target_page_matches_id_and_detaches_probe_sessions() -> None:
    first = _FakePage("https://example.test/job/1")
    second = _FakePage("https://example.test/job/2")
    context = _FakeContext([first, second], ["TARGET-1", "TARGET-2"])
    browser = type("FakeBrowser", (), {"contexts": [context]})()

    resolved = browser_runtime._resolve_target_page(browser, "TARGET-2")

    assert resolved is second
    assert all(session.detached for session in context.sessions.values())


def test_resolve_target_page_uses_unique_marker_fast_path() -> None:
    marker = "about:blank#job-automation-marker"
    target = _FakePage(marker)
    unrelated = _FakePage("https://example.test/job/other")
    context = _FakeContext([unrelated, target], ["OTHER", "TARGET-1"])
    browser = type("FakeBrowser", (), {"contexts": [context]})()

    resolved = browser_runtime._resolve_target_page(
        browser,
        "TARGET-1",
        target_marker=marker,
        target_url="https://example.test/job/1",
    )

    assert resolved is target
    assert context.sessions[id(target)].detached is True
    assert context.sessions[id(unrelated)].detached is False


def test_resolve_target_page_uses_unique_canonical_url_fast_path() -> None:
    target = _FakePage("https://example.test/job/1?utm_source=test")
    unrelated = _FakePage("https://example.test/job/other")
    context = _FakeContext([unrelated, target], ["OTHER", "TARGET-1"])
    browser = type("FakeBrowser", (), {"contexts": [context]})()

    resolved = browser_runtime._resolve_target_page(
        browser,
        "TARGET-1",
        target_url="https://example.test/job/1",
    )

    assert resolved is target
    assert context.sessions[id(target)].detached is True
    assert context.sessions[id(unrelated)].detached is False


def test_unique_target_metadata_with_wrong_id_fails_closed() -> None:
    marker = "about:blank#job-automation-marker"
    target = _FakePage(marker)
    context = _FakeContext([target], ["OTHER"])
    browser = type("FakeBrowser", (), {"contexts": [context]})()

    with pytest.raises(BrowserAutomationError, match="metadata did not match"):
        browser_runtime._resolve_target_page(
            browser,
            "TARGET-1",
            target_marker=marker,
        )


def test_ambiguous_canonical_url_falls_back_to_exact_target_id() -> None:
    first = _FakePage("https://example.test/job/1?utm_source=first")
    second = _FakePage("https://example.test/job/1?utm_source=second")
    context = _FakeContext([first, second], ["OTHER", "TARGET-1"])
    browser = type("FakeBrowser", (), {"contexts": [context]})()

    resolved = browser_runtime._resolve_target_page(
        browser,
        "TARGET-1",
        target_url="https://example.test/job/1",
    )

    assert resolved is second
    assert all(session.detached for session in context.sessions.values())


def test_matching_navigation_reloads_once_without_goto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage()
    monkeypatch.setenv("JOB_APP_RELOAD_TAB", "1")

    browser_runtime.navigate_reusing_tab(
        page,  # type: ignore[arg-type]
        page.url,
        timeout=1_000,
        captcha_checker=lambda _page: False,
    )

    assert page.reload_calls == 1
    assert page.goto_calls == 0


class _FakeChromium:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def connect_over_cdp(self, endpoint: str, **kwargs: object) -> object:
        self.calls.append((endpoint, kwargs))
        return object()


def test_cdp_attach_timeout_override_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chromium = _FakeChromium()
    playwright = type("FakePlaywright", (), {"chromium": chromium})()
    endpoint = "http://127.0.0.1:9222"

    monkeypatch.delenv("JOB_APP_CDP_ATTACH_TIMEOUT_MS", raising=False)
    browser_runtime._connect_over_cdp(playwright, endpoint)  # type: ignore[arg-type]
    monkeypatch.setenv("JOB_APP_CDP_ATTACH_TIMEOUT_MS", "90000")
    browser_runtime._connect_over_cdp(playwright, endpoint)  # type: ignore[arg-type]

    assert chromium.calls == [
        (endpoint, {}),
        (endpoint, {"timeout": 90_000}),
    ]


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_cdp_attach_timeout_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    chromium = _FakeChromium()
    playwright = type("FakePlaywright", (), {"chromium": chromium})()
    monkeypatch.setenv("JOB_APP_CDP_ATTACH_TIMEOUT_MS", value)

    with pytest.raises(BrowserAutomationError, match="must be a positive integer"):
        browser_runtime._connect_over_cdp(  # type: ignore[arg-type]
            playwright,
            "http://127.0.0.1:9222",
        )

    assert chromium.calls == []


def test_requested_target_forwards_marker_and_url_fast_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage()
    browser = object()
    resolved: list[tuple[object, ...]] = []
    monkeypatch.setenv("JOB_APP_TARGET_ID", "TARGET-1")
    monkeypatch.setenv("JOB_APP_TARGET_MARKER", "about:blank#marker")
    monkeypatch.setenv("JOB_APP_TARGET_URL", "https://example.test/job/1")
    monkeypatch.setenv("JOB_APP_REQUIRE_SHARED_CDP", "1")
    monkeypatch.setattr(browser_runtime, "_connect_over_cdp", lambda *_args: browser)

    def resolve(
        active_browser: object,
        target_id: str,
        *,
        target_marker: str,
        target_url: str,
    ) -> _FakePage:
        resolved.append((active_browser, target_id, target_marker, target_url))
        return page

    monkeypatch.setattr(browser_runtime, "_resolve_target_page", resolve)
    playwright = type("FakePlaywright", (), {"chromium": object()})()

    session = browser_runtime.open_chrome_session(playwright)  # type: ignore[arg-type]

    assert session.browser is browser
    assert session.page is page
    assert session.close_browser_on_exit is False
    assert session.close_page_on_exit is False
    assert resolved == [
        (
            browser,
            "TARGET-1",
            "about:blank#marker",
            "https://example.test/job/1",
        )
    ]


def test_required_background_cdp_failure_never_starts_owned_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_APP_BACKGROUND_TABS", "1")
    monkeypatch.setenv("JOB_APP_REQUIRE_SHARED_CDP", "1")
    monkeypatch.delenv("JOB_APP_TARGET_ID", raising=False)
    started = False

    def create_target(_endpoint: str) -> tuple[str, str]:
        raise RuntimeError("CDP unavailable")

    def start_hidden(_endpoint: str, _profile: str) -> None:
        nonlocal started
        started = True

    playwright = type("FakePlaywright", (), {"chromium": object()})()
    with pytest.raises(BrowserAutomationError, match="Required shared Chrome CDP"):
        browser_runtime.open_chrome_session(
            playwright,  # type: ignore[arg-type]
            create_background_target=create_target,
            start_hidden_chrome=start_hidden,  # type: ignore[arg-type]
        )

    assert started is False
