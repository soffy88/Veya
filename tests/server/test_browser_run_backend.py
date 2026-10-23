"""P2-2: `browser_run` must reuse the CDP backend the deployment already runs.

Acceptance found `browser_run` failing with
``Executable doesn't exist at /opt/ms-playwright/chromium_headless_shell-1248``
while `browser_session` reached the same pages over ``VEYA_BROWSER_CDP_URL``.
The two tools must share one browser backend: when a CDP endpoint is
configured, `browser_run` attaches to it instead of launching a local
Chromium the container does not ship.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from veya.oprim.browser import action_extract_text
from veya.oskill.browser import BrowserSession


class _FakePlaywright:
    def __init__(self) -> None:
        self.attached: list[tuple[str, float]] = []
        self.launched: list[dict[str, Any]] = []
        self.stopped = False
        self.chromium = _FakeChromium(self)

    async def stop(self) -> None:
        self.stopped = True


class _FakeChromium:
    def __init__(self, owner: _FakePlaywright) -> None:
        self._owner = owner

    async def connect_over_cdp(self, url: str, *, timeout: float) -> Any:
        self._owner.attached.append((url, timeout))
        return _FakeBrowser(connected=True)

    async def launch(self, **kwargs: Any) -> Any:
        self._owner.launched.append(kwargs)
        return _FakeBrowser(connected=True)


class _FakeBrowser:
    def __init__(self, *, connected: bool) -> None:
        self._connected = connected
        self.closed = False
        self.contexts: list[Any] = []

    def is_connected(self) -> bool:
        return self._connected

    async def new_context(self, **kwargs: Any) -> Any:
        return _FakeContext(self)

    async def close(self) -> None:
        self.closed = True


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com/"

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.url = url
        return type("Resp", (), {"status": 200})()

    async def title(self) -> str:
        return "Example Domain"

    async def inner_text(self, selector: str) -> str:
        return "This domain is for use in documentation examples."


class _FakeContext:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    async def new_page(self) -> Any:
        return _FakePage()

    async def close(self) -> None:
        return None


class _FakeAsyncPlaywright:
    """Stands in for ``async_playwright()`` inside the module under test."""

    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    def __call__(self) -> _FakeAsyncPlaywright:
        return self

    async def start(self) -> _FakePlaywright:
        return self._playwright


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> _FakePlaywright:
    """Install a fake ``playwright.async_api`` (playwright is not a test dep)."""
    import sys
    import types

    fake = _FakePlaywright()
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = _FakeAsyncPlaywright(fake)  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.async_api = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    return fake


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VEYA_BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("VEYA_BROWSER_NO_SANDBOX", raising=False)
    monkeypatch.delenv("VEYA_BROWSER_CDP_TIMEOUT_MS", raising=False)


@pytest.mark.asyncio
async def test_cdp_endpoint_is_preferred_over_a_local_launch(
    fake_playwright: _FakePlaywright, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEYA_BROWSER_CDP_URL", "http://sidecar:9222")
    session = BrowserSession(headless=True)

    await session.start()

    assert fake_playwright.attached == [("http://sidecar:9222", 30000.0)]
    assert fake_playwright.launched == []
    assert session._owns_browser is False
    await session.stop()


@pytest.mark.asyncio
async def test_cdp_attached_browser_is_not_closed_on_stop(
    fake_playwright: _FakePlaywright, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar outlives this process; closing it would break other clients."""
    monkeypatch.setenv("VEYA_BROWSER_CDP_URL", "http://sidecar:9222")
    session = BrowserSession(headless=True)
    await session.start()
    browser = session._browser

    await session.stop()

    assert browser.closed is False


@pytest.mark.asyncio
async def test_local_launch_uses_an_installed_binary(
    fake_playwright: _FakePlaywright, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "ms-playwright"
    binary = root / "chromium-1234" / "chrome-linux64" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(root))

    session = BrowserSession(headless=True)
    await session.start()

    assert fake_playwright.launched[0]["executable_path"] == str(binary)
    assert session._owns_browser is True
    await session.stop()


@pytest.mark.asyncio
async def test_missing_local_browser_names_the_alternative(
    fake_playwright: _FakePlaywright, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No local Chromium and no CDP: fail with the actionable alternative."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))
    session = BrowserSession(headless=True)

    with pytest.raises(RuntimeError) as excinfo:
        await session.start()

    message = str(excinfo.value)
    assert "VEYA_BROWSER_CDP_URL" in message
    assert "chromium" in message.lower()


def test_installed_executable_finds_a_bundled_chromium(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "ms-playwright"
    binary = root / "chromium-1248" / "chrome-linux64" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(root))

    assert BrowserSession._installed_executable() == binary


def test_installed_executable_is_none_without_a_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "empty"))
    assert BrowserSession._installed_executable() is None


@pytest.mark.asyncio
async def test_navigate_snapshot_reports_the_page_title(
    fake_playwright: _FakePlaywright, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`browser_run` returns `last.page_title`; navigate must actually fill it.

    With an empty snapshot title the tool reported ``"title": ""`` for every
    page, so a model asked for a page title had nothing to answer with and
    fell back to re-trying other actions.
    """
    monkeypatch.setenv("VEYA_BROWSER_CDP_URL", "http://sidecar:9222")
    session = BrowserSession(headless=True)
    await session.start()

    result = await session.navigate("https://example.com")

    assert result.success is True
    assert result.page_title == "Example Domain"

    # browser_run reports the last action's result, so text extraction must
    # carry the title too.
    extracted = await session.execute_action(action_extract_text())
    assert extracted.text.startswith("This domain is for use")
    assert extracted.page_title == "Example Domain"
    await session.stop()
