"""Shared Playwright session, navigation, and browser evidence runtime.

Provider adapters own form semantics and submission policy. This module owns
only reusable browser-resource lifecycle and page-state primitives. Optional
hook arguments keep the historic ``core.engine_shared`` patch surface working
while allowing new callers to depend on this focused boundary directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, Playwright

from ..core.exceptions import BrowserAutomationError
from ..core.runtime_config import RUNTIME_CONFIG
from ..core.screenshots import active_screenshot_directory

logger = logging.getLogger("ATSEngineBrowserRuntime")

DEFAULT_CONFIRMATION_PHRASES = (
    "application submitted",
    "application has been submitted",
    "thank you for applying",
    "thank you so much for your interest",
    "thanks for applying",
    "thanks a lot for applying",
    "application received",
    "application has been received",
    "successfully submitted",
)
DEFAULT_FAILURE_PHRASES = (
    "flagged as possible spam",
    "flagged as potential bot traffic",
    "couldn't submit",
    "submission failed",
)


@dataclass
class PlaywrightBrowserSession:
    """Resources owned by one provider browser attempt."""

    browser: Browser
    page: Page
    close_browser_on_exit: bool
    close_page_on_exit: bool = False
    close_cdp_browser_on_exit: bool = False
    cdp_endpoint: str = ""
    owned_process: subprocess.Popen[Any] | None = None
    owned_profile_path: Path | None = None

    def close(self) -> None:
        """Release resources through the focused runtime boundary."""
        close_browser_session(self)


# Compatibility name used by the established engine facade.
BrowserSession = PlaywrightBrowserSession


def _safe_filename(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip()).strip("_")
    return cleaned or fallback


def capture_screenshot(
    page: Page,
    directory: Path,
    company: str,
    tag: str,
    *,
    filename_sanitizer: Callable[[str, str], str] = _safe_filename,
) -> str:
    """Capture full-page evidence, falling back to the visible viewport."""
    directory = active_screenshot_directory(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (
        f"{filename_sanitizer(company, 'ats')}_{filename_sanitizer(tag, 'capture')}.png"
    )
    try:
        page.screenshot(path=str(target), full_page=True, timeout=15_000)
        return str(target)
    except Exception:
        try:
            page.screenshot(path=str(target), full_page=False, timeout=10_000)
            return str(target)
        except Exception:
            return ""


def confirmation_visible(
    page: Page,
    *,
    success_phrases: Sequence[str] = DEFAULT_CONFIRMATION_PHRASES,
    failure_phrases: Sequence[str] = DEFAULT_FAILURE_PHRASES,
) -> bool:
    """Return whether the visible page contains unambiguous success evidence."""
    return text_confirms_submission(
        page.locator("body").inner_text().lower(),
        success_phrases=success_phrases,
        failure_phrases=failure_phrases,
    )


def text_confirms_submission(
    text: str,
    *,
    success_phrases: Sequence[str] = DEFAULT_CONFIRMATION_PHRASES,
    failure_phrases: Sequence[str] = DEFAULT_FAILURE_PHRASES,
) -> bool:
    """Require a success phrase and reject any simultaneous failure phrase."""
    normalized = text.lower()
    return any(phrase.lower() in normalized for phrase in success_phrases) and not any(
        phrase.lower() in normalized for phrase in failure_phrases
    )


def page_has_captcha(page: Page) -> bool:
    """Return whether a visible CAPTCHA is present without interacting with it."""
    inspection_failed = False
    try:
        challenge = page.locator(
            'iframe[src*="captcha" i]:visible, iframe[title*="captcha" i]:visible, '
            'iframe[src*="challenges.cloudflare.com" i]:visible, '
            'iframe[src*="turnstile" i]:visible, iframe[title*="challenge" i]:visible, '
            '[class*="captcha" i]:visible, [id*="captcha" i]:visible, '
            '[class*="turnstile" i]:visible, [id*="turnstile" i]:visible'
        )
        if challenge.count() > 0:
            return True
    except Exception:
        inspection_failed = True
    try:
        body = page.locator("body").inner_text()
        if re.search(
            r"\b(?:verify you are human|complete the security (?:check|challenge)|"
            r"cloudflare security challenge)\b",
            body,
            re.I,
        ):
            return True
    except Exception:
        inspection_failed = True
    if inspection_failed:
        logger.warning(
            "CAPTCHA inspection failed; blocking browser action because page state is uncertain"
        )
        return True
    return False


def _normalized_navigation_url(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def navigate_reusing_tab(
    page: Page,
    url: str,
    *,
    timeout: int,
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "domcontentloaded",
    captcha_checker: Callable[[Page], bool] = page_has_captcha,
) -> None:
    """Preserve a matching application tab; navigate only when it differs."""
    current = _normalized_navigation_url(page.url) if page.url not in ("", "about:blank") else ""
    target = _normalized_navigation_url(url)
    if current == target:
        if captcha_checker(page):
            raise RuntimeError("CAPTCHA_REQUIRED: existing tab was left open")
        return
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as exc:
            last_error = exc
            try:
                at_target = _normalized_navigation_url(page.url) == target
                body_text = page.locator("body").inner_text(timeout=2000).strip()
                network_error = re.search(
                    r"\b(?:this site can(?:not|'t) be reached|"
                    r"page (?:is not|isn't) working|err_[a-z_]+)\b",
                    body_text,
                    re.I,
                )
                if at_target and len(body_text) >= 40 and not network_error:
                    logger.info(
                        "Navigation timed out after usable page content loaded; continuing in-place"
                    )
                    return
            except Exception:
                pass
            if attempt == 0:
                page.wait_for_timeout(750)
    if last_error is not None:
        raise last_error


def _new_page(browser: Browser) -> Page:
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return context.new_page()


def _raw_browser_cdp_command(
    endpoint: str,
    method: str,
    params: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Send one browser-level CDP command without activating a Chrome target."""
    try:
        from websockets.sync.client import connect

        version_url = f"{endpoint.rstrip('/')}/json/version"
        with urllib.request.urlopen(version_url, timeout=5) as response:  # noqa: S310
            version = json.load(response)
        web_socket_url = str(version.get("webSocketDebuggerUrl", ""))
        if not web_socket_url:
            raise BrowserAutomationError(
                "Chrome CDP endpoint did not expose a browser WebSocket URL"
            )
        request_id = 1
        with connect(web_socket_url, open_timeout=5, close_timeout=2) as socket:
            socket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": method,
                        "params": dict(params),
                    }
                )
            )
            while True:
                message = json.loads(socket.recv(timeout=5))
                if message.get("id") != request_id:
                    continue
                if message.get("error"):
                    raise BrowserAutomationError(f"Chrome CDP command failed: {message['error']}")
                result = message.get("result", {})
                return result if isinstance(result, Mapping) else {}
    except BrowserAutomationError:
        raise
    except Exception as exc:
        raise BrowserAutomationError(
            f"Chrome CDP transport failed while executing {method}: {exc}"
        ) from exc


def _create_background_target(
    endpoint: str,
    *,
    raw_command: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] = (
        _raw_browser_cdp_command
    ),
) -> tuple[str, str]:
    marker = f"about:blank#job-automation-{uuid.uuid4().hex}"
    result = raw_command(endpoint, "Target.createTarget", {"url": marker, "background": True})
    target_id = str(result.get("targetId", ""))
    if not target_id:
        raise BrowserAutomationError("Chrome did not return an ID for the background target")
    return marker, target_id


def _close_background_target(
    endpoint: str,
    target_id: str,
    *,
    raw_command: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] = (
        _raw_browser_cdp_command
    ),
) -> None:
    try:
        raw_command(endpoint, "Target.closeTarget", {"targetId": target_id})
    except Exception:
        logger.debug("Could not close orphaned background Chrome target %s", target_id)


def _resolve_background_page(browser: Browser, marker: str) -> Page:
    for _ in range(30):
        for context in browser.contexts:
            for page in context.pages:
                if page.url == marker:
                    return page
        time.sleep(0.1)
    raise BrowserAutomationError(
        "Chrome created a background target but Playwright could not resolve it"
    )


def _reusable_page(
    browser: Browser,
    target_url: str,
    *,
    captcha_checker: Callable[[Page], bool] = page_has_captcha,
) -> Page | None:
    """Reuse an existing tab for the same application, excluding CAPTCHA tabs."""
    target = _normalized_navigation_url(target_url)
    blank: Page | None = None
    for context in browser.contexts:
        for page in context.pages:
            if page.is_closed():
                continue
            if page.url in ("", "about:blank", "chrome://newtab/"):
                blank = blank or page
                continue
            try:
                if _normalized_navigation_url(page.url) == target and not captcha_checker(page):
                    return page
            except Exception:
                continue
    return blank


def _find_chrome_executable() -> Path | None:
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    )
    return next((path for path in candidates if path.is_file()), None)


def _read_owned_cdp_endpoint(profile: Path) -> str:
    """Read the exclusive loopback endpoint Chrome assigned to an owned profile."""
    try:
        lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        if 1 <= port <= 65_535:
            return f"http://127.0.0.1:{port}"
    except (OSError, ValueError, IndexError):
        pass
    return ""


def _owned_cdp_endpoint_is_live(endpoint: str) -> bool:
    if not endpoint:
        return False
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"{endpoint.rstrip('/')}/json/version",
            timeout=0.5,
        ):
            return True
    except Exception:
        return False


def _cleanup_owned_profile(profile: Path) -> None:
    """Remove only an exact temporary Chrome profile created by this runtime."""
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved = profile.resolve()
        if resolved.parent == temp_root and resolved != temp_root:
            for _ in range(3):
                shutil.rmtree(resolved, ignore_errors=True)
                if not resolved.exists():
                    return
                time.sleep(0.2)
            logger.warning("Owned Chrome profile remains after cleanup: %s", resolved)
    except Exception:
        logger.debug("Could not remove owned Chrome profile %s", profile, exc_info=True)


def _cleanup_stale_owned_profiles(
    temp_root: Path,
    profile_name: str,
    *,
    max_age_seconds: int = 3600,
    read_endpoint: Callable[[Path], str] = _read_owned_cdp_endpoint,
    endpoint_is_live: Callable[[str], bool] = _owned_cdp_endpoint_is_live,
    cleanup_profile: Callable[[Path], None] = _cleanup_owned_profile,
) -> None:
    """Remove only aged, inactive unique profiles left by a force-killed engine."""
    prefix = f"{_safe_filename(profile_name, 'ats-profile')}-"
    try:
        candidates = list(temp_root.iterdir())
    except OSError:
        return
    now = time.time()
    for candidate in candidates:
        try:
            if (
                candidate.is_symlink()
                or not candidate.is_dir()
                or not candidate.name.startswith(prefix)
                or now - candidate.stat().st_mtime < max_age_seconds
            ):
                continue
            endpoint = read_endpoint(candidate)
            if endpoint and endpoint_is_live(endpoint):
                continue
            cleanup_profile(candidate)
        except OSError:
            logger.debug("Could not inspect stale owned profile %s", candidate, exc_info=True)


def _stop_owned_chrome_process(process: subprocess.Popen[Any]) -> None:
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        logger.debug("Could not wait for owned Chrome to exit", exc_info=True)
    try:
        process.terminate()
    except Exception:
        logger.debug("Could not terminate owned Chrome", exc_info=True)
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        logger.debug("Could not wait for terminated owned Chrome", exc_info=True)
    try:
        process.kill()
    except Exception:
        logger.debug("Could not kill owned Chrome", exc_info=True)
    try:
        process.wait(timeout=5)
    except Exception:
        logger.debug("Owned Chrome did not exit after kill", exc_info=True)


def _start_hidden_background_chrome(
    endpoint: str,
    profile_name: str,
    *,
    find_chrome: Callable[[], Path | None] = _find_chrome_executable,
    cleanup_stale_profiles: Callable[[Path, str], None] = _cleanup_stale_owned_profiles,
    cleanup_profile: Callable[[Path], None] = _cleanup_owned_profile,
    read_endpoint: Callable[[Path], str] = _read_owned_cdp_endpoint,
    stop_process: Callable[[subprocess.Popen[Any]], None] = _stop_owned_chrome_process,
) -> tuple[subprocess.Popen[Any], Path, str] | None:
    """Start an owned Chrome without activating or exposing its window."""
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.hostname not in {"127.0.0.1", "localhost"}:
        return None
    chrome = find_chrome()
    if chrome is None:
        return None

    temp_root = Path(tempfile.gettempdir())
    cleanup_stale_profiles(temp_root, profile_name)
    profile: Path | None = None
    try:
        profile = Path(
            tempfile.mkdtemp(
                prefix=f"{_safe_filename(profile_name, 'ats-profile')}-",
                dir=temp_root,
            )
        )
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        process = subprocess.Popen(
            [
                str(chrome),
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--disable-background-mode",
                "--start-minimized",
                "--window-position=-32000,-32000",
                "--window-size=800,600",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception as exc:
        if profile is not None:
            cleanup_profile(profile)
        logger.info("Could not launch owned hidden Chrome: %s", exc)
        return None

    for _ in range(20):
        if process.poll() is not None:
            cleanup_profile(profile)
            return None
        owned_endpoint = read_endpoint(profile)
        if not owned_endpoint:
            time.sleep(0.3)
            continue
        try:
            version_url = f"{owned_endpoint}/json/version"
            with urllib.request.urlopen(version_url, timeout=1):  # noqa: S310
                return process, profile, owned_endpoint
        except Exception:
            time.sleep(0.3)
    stop_process(process)
    cleanup_profile(profile)
    return None


def close_browser_session(
    session: PlaywrightBrowserSession,
    *,
    raw_command: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] = (
        _raw_browser_cdp_command
    ),
    stop_process: Callable[[subprocess.Popen[Any]], None] = _stop_owned_chrome_process,
    cleanup_profile: Callable[[Path], None] = _cleanup_owned_profile,
) -> None:
    """Release a session, including an owned hidden Chrome when applicable."""
    if getattr(session, "close_page_on_exit", False):
        try:
            session.page.close()
        except Exception:
            logger.debug("Could not close the browser session page", exc_info=True)
    if getattr(session, "close_browser_on_exit", False):
        try:
            session.browser.close()
        except Exception:
            logger.debug("Could not close the Playwright browser", exc_info=True)
    if not getattr(session, "close_cdp_browser_on_exit", False):
        return

    endpoint = getattr(session, "cdp_endpoint", "")
    if endpoint:
        try:
            raw_command(endpoint, "Browser.close", {})
        except Exception:
            logger.debug("Could not close the owned Chrome over CDP", exc_info=True)
    process = getattr(session, "owned_process", None)
    if process is not None:
        try:
            stop_process(process)
        except Exception:
            logger.debug("Could not stop the owned Chrome process", exc_info=True)
    profile = getattr(session, "owned_profile_path", None)
    if profile is not None:
        try:
            cleanup_profile(profile)
        except Exception:
            logger.debug("Could not clean the owned Chrome profile", exc_info=True)


def open_chrome_session(
    playwright: Playwright,
    *,
    cdp_url: str | None = None,
    profile_name: str = "ats-cdp-profile",
    target_url: str = "",
    headless: bool = False,
    background: bool = False,
    create_background_target: Callable[[str], tuple[str, str]] = _create_background_target,
    close_background_target: Callable[[str, str], None] = _close_background_target,
    resolve_background_page: Callable[[Browser, str], Page] = _resolve_background_page,
    start_hidden_chrome: Callable[
        [str, str], tuple[subprocess.Popen[Any], Path, str] | None
    ] = _start_hidden_background_chrome,
    raw_command: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] = (
        _raw_browser_cdp_command
    ),
    stop_process: Callable[[subprocess.Popen[Any]], None] = _stop_owned_chrome_process,
    cleanup_profile: Callable[[Path], None] = _cleanup_owned_profile,
    find_chrome: Callable[[], Path | None] = _find_chrome_executable,
    reusable_page: Callable[[Browser, str], Page | None] = _reusable_page,
    new_page: Callable[[Browser], Page] = _new_page,
) -> PlaywrightBrowserSession:
    """Open a reusable, background, or isolated Playwright browser session."""
    endpoint = cdp_url or str(RUNTIME_CONFIG.browser["cdp_endpoint"])
    if background:
        target_id = ""
        try:
            marker, target_id = create_background_target(endpoint)
            browser = playwright.chromium.connect_over_cdp(endpoint)
            return PlaywrightBrowserSession(
                browser,
                resolve_background_page(browser, marker),
                False,
                True,
            )
        except Exception as exc:
            if target_id:
                close_background_target(endpoint, target_id)
            logger.info("Existing background Chrome session unavailable on %s: %s", endpoint, exc)
        owned_chrome = start_hidden_chrome(endpoint, profile_name)
        if owned_chrome is not None:
            owned_process, owned_profile, owned_endpoint = owned_chrome
            target_id = ""
            try:
                marker, target_id = create_background_target(owned_endpoint)
                browser = playwright.chromium.connect_over_cdp(owned_endpoint)
                return PlaywrightBrowserSession(
                    browser=browser,
                    page=resolve_background_page(browser, marker),
                    close_browser_on_exit=False,
                    close_page_on_exit=True,
                    close_cdp_browser_on_exit=True,
                    cdp_endpoint=owned_endpoint,
                    owned_process=owned_process,
                    owned_profile_path=owned_profile,
                )
            except Exception as exc:
                if target_id:
                    close_background_target(owned_endpoint, target_id)
                try:
                    raw_command(owned_endpoint, "Browser.close", {})
                except Exception:
                    logger.debug(
                        "Could not close the failed owned Chrome session over CDP",
                        exc_info=True,
                    )
                stop_process(owned_process)
                cleanup_profile(owned_profile)
                logger.info(
                    "Owned hidden Chrome session unavailable on %s; "
                    "using isolated headless browser: %s",
                    owned_endpoint,
                    exc,
                )
    if headless:
        browser = playwright.chromium.launch(headless=True)
        return PlaywrightBrowserSession(browser, new_page(browser), True)

    force_fresh = os.environ.get("JOB_APP_FRESH_BROWSER") == "1"
    if not force_fresh:
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            page = reusable_page(browser, target_url) if target_url else None
            return PlaywrightBrowserSession(browser, page or new_page(browser), False)
        except Exception as exc:
            logger.debug("Could not connect to existing CDP endpoint %s: %s", endpoint, exc)

    if force_fresh:
        logger.info("JOB_APP_FRESH_BROWSER requested; launching fresh Chromium instance")
        browser = playwright.chromium.launch(headless=False)
        return PlaywrightBrowserSession(browser, new_page(browser), True)

    chrome = find_chrome()
    if chrome:
        profile = Path(os.environ.get("TEMP", str(Path.cwd()))) / profile_name
        subprocess.Popen(
            [
                str(chrome),
                f"--remote-debugging-port={urlparse(endpoint).port or 9222}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        for _ in range(15):
            time.sleep(0.4)
            try:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                page = reusable_page(browser, target_url) if target_url else None
                return PlaywrightBrowserSession(browser, page or new_page(browser), False)
            except Exception:
                continue
        logger.info(
            "Chrome process started but CDP on %s did not become ready; falling back", endpoint
        )

    logger.info("CDP connection unavailable on %s; launching fresh Chromium instance", endpoint)
    browser = playwright.chromium.launch(headless=False)
    page = reusable_page(browser, target_url) if target_url else None
    return PlaywrightBrowserSession(browser, page or new_page(browser), True)
