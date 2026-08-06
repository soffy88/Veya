"""
veya/oskill/browser.py — Browser Automation Pipeline (Layer 2).

Composite skill built on oprim browser ops + Playwright.
Manages browser sessions, executes action sequences, and captures results.

Requires: playwright
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.oprim.browser import (
    BrowserAction,
    BrowserActionResult,
    BrowserElement,
    BrowserPage,
    build_selector,
    create_browser_context,
    screenshot_to_base64,
    screenshot_to_data_uri,
)


# ---------------------------------------------------------------------------
# Browser Session (stateful — manages one Playwright browser)
# ---------------------------------------------------------------------------


class BrowserSession:
    """Manages a single Playwright browser session.

    Handles browser lifecycle, page management, and action execution.

    Example:
        >>> session = BrowserSession(headless=True)
        >>> await session.start()
        >>> result = await session.navigate("https://example.com")
        >>> result = await session.click("text=More information")
        >>> await session.stop()
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        user_agent: str | None = None,
        browser_type: str = "chromium",
        storage_state_path: str | None = None,
        proxy: dict[str, str] | None = None,
    ):
        self._config = create_browser_context(
            headless=headless,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            user_agent=user_agent,
            proxy=proxy,
            storage_state_path=storage_state_path,
        )
        self._browser_type = browser_type
        self._storage_state_path = storage_state_path

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._started = False
        self._action_history: list[BrowserActionResult] = []

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def current_url(self) -> str:
        return self._page.url if self._page else ""

    @property
    def history(self) -> list[BrowserActionResult]:
        return self._action_history

    async def start(self) -> None:
        """Start the browser session."""
        if self._started:
            return

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright not installed. Run: pip install playwright && playwright install chromium"
            )

        self._playwright = await async_playwright().start()

        browser_launcher = getattr(self._playwright, self._browser_type)
        launch_kwargs: dict[str, Any] = {"headless": self._config["headless"]}
        # 容器/打包/CI 环境 (无 user namespace 或 /dev/shm 受限):
        # Chromium 自身 sandbox 会报 "Failed to move to new namespace" /
        # "cannot write to /dev/shm" → 显式关闭并禁用 dev-shm
        if os.environ.get("VEYA_BROWSER_NO_SANDBOX"):
            launch_kwargs["chromium_sandbox"] = False
            launch_kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
        self._browser = await browser_launcher.launch(**launch_kwargs)

        context_kwargs = {
            "viewport": self._config["viewport"],
            "locale": self._config["locale"],
            "timezone_id": self._config["timezone_id"],
        }
        if self._config["user_agent"]:
            context_kwargs["user_agent"] = self._config["user_agent"]
        if self._config["proxy"]:
            context_kwargs["proxy"] = self._config["proxy"]
        if self._storage_state_path and os.path.exists(self._storage_state_path):
            context_kwargs["storage_state"] = self._storage_state_path

        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        self._started = True

    async def stop(self) -> None:
        """Stop the browser session."""
        if self._storage_state_path and self._context:
            try:
                state = await self._context.storage_state()
                os.makedirs(os.path.dirname(self._storage_state_path) or ".", exist_ok=True)
                with open(self._storage_state_path, "w") as f:
                    json.dump(state, f)
            except Exception:
                pass

        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._started = False

    # ── Core actions ────────────────────────────────────────────────────

    async def execute_action(self, action: BrowserAction) -> BrowserActionResult:
        """Execute a single browser action."""
        t0 = time.time()
        try:
            result = await self._do_action(action)
            result.duration_ms = (time.time() - t0) * 1000
            self._action_history.append(result)
            return result
        except Exception as e:
            err_result = BrowserActionResult(
                success=False,
                action=action.action,
                error=str(e),
                duration_ms=(time.time() - t0) * 1000,
            )
            self._action_history.append(err_result)
            return err_result

    async def execute_sequence(self, actions: list[BrowserAction]) -> list[BrowserActionResult]:
        """Execute a sequence of browser actions."""
        results = []
        for action in actions:
            result = await self.execute_action(action)
            results.append(result)
            if not result.success:
                break
        return results

    async def _do_action(self, action: BrowserAction) -> BrowserActionResult:
        """Internal action dispatcher."""
        page = self._page
        if page is None:
            raise RuntimeError("Browser not started")

        if action.action == "navigate":
            await page.goto(action.value or "about:blank", wait_until=action.wait_until, timeout=action.timeout_ms)
            return self._page_snapshot(action)

        elif action.action == "click":
            el = await page.wait_for_selector(action.selector or "body", timeout=action.timeout_ms)
            await el.click(timeout=action.timeout_ms)
            await page.wait_for_load_state("networkidle")
            return self._page_snapshot(action)

        elif action.action == "type":
            el = await page.wait_for_selector(action.selector or "body", timeout=action.timeout_ms)
            await el.fill(action.value or "")
            return BrowserActionResult(success=True, action="type", page_url=page.url)

        elif action.action == "press":
            await page.keyboard.press(action.value or "Enter")
            return BrowserActionResult(success=True, action="press", page_url=page.url)

        elif action.action == "screenshot":
            if action.selector:
                el = await page.wait_for_selector(action.selector, timeout=action.timeout_ms)
                screenshot_bytes = await el.screenshot()
            else:
                screenshot_bytes = await page.screenshot(full_page=True)
            return BrowserActionResult(
                success=True,
                action="screenshot",
                page_url=page.url,
                screenshot_base64=screenshot_to_base64(screenshot_bytes),
            )

        elif action.action == "extract_text":
            if action.selector:
                el = await page.wait_for_selector(action.selector, timeout=action.timeout_ms)
                text = await el.inner_text()
            else:
                text = await page.inner_text("body")
            return BrowserActionResult(success=True, action="extract_text", text=text, page_url=page.url)

        elif action.action == "extract_html":
            if action.selector:
                el = await page.wait_for_selector(action.selector, timeout=action.timeout_ms)
                html = await el.inner_html()
            else:
                html = await page.content()
            return BrowserActionResult(success=True, action="extract_html", html=html, page_url=page.url)

        elif action.action == "scroll":
            parts = (action.value or "down:500").split(":", 1)
            direction = parts[0]
            amount = int(parts[1]) if len(parts) > 1 else 500
            if direction == "down":
                await page.evaluate(f"window.scrollBy(0, {amount})")
            elif direction == "up":
                await page.evaluate(f"window.scrollBy(0, -{amount})")
            return BrowserActionResult(success=True, action="scroll", page_url=page.url)

        elif action.action == "wait":
            ms = int(action.value or "1000")
            await asyncio.sleep(ms / 1000)
            return BrowserActionResult(success=True, action="wait", page_url=page.url)

        elif action.action == "select":
            el = await page.wait_for_selector(action.selector or "select", timeout=action.timeout_ms)
            await el.select_option(action.value or "")
            return BrowserActionResult(success=True, action="select", page_url=page.url)

        elif action.action == "hover":
            el = await page.wait_for_selector(action.selector or "body", timeout=action.timeout_ms)
            await el.hover()
            return BrowserActionResult(success=True, action="hover", page_url=page.url)

        elif action.action == "focus":
            el = await page.wait_for_selector(action.selector or "body", timeout=action.timeout_ms)
            await el.focus()
            return BrowserActionResult(success=True, action="focus", page_url=page.url)

        elif action.action == "evaluate":
            result = await page.evaluate(action.value or "")
            return BrowserActionResult(
                success=True,
                action="evaluate",
                text=str(result),
                page_url=page.url,
            )

        return BrowserActionResult(success=False, action=action.action, error=f"Unknown action: {action.action}")

    def _page_snapshot(self, action: BrowserAction) -> BrowserActionResult:
        """Create a text snapshot of the current page."""
        return BrowserActionResult(
            success=True,
            action=action.action,
            page_url=self._page.url if self._page else "",
            page_title="",
        )

    # ── High-level convenience methods ──────────────────────────────────

    async def navigate(self, url: str) -> BrowserActionResult:
        return await self.execute_action(BrowserAction(action="navigate", value=url))

    async def click(self, selector: str) -> BrowserActionResult:
        return await self.execute_action(BrowserAction(action="click", selector=selector))

    async def type_text(self, selector: str, text: str) -> BrowserActionResult:
        return await self.execute_action(BrowserAction(action="type", selector=selector, value=text))

    async def screenshot(self, selector: str | None = None) -> BrowserActionResult:
        return await self.execute_action(BrowserAction(action="screenshot", selector=selector))

    async def get_text(self, selector: str | None = None) -> str:
        result = await self.execute_action(BrowserAction(action="extract_text", selector=selector))
        return result.text

    async def get_html(self, selector: str | None = None) -> str:
        result = await self.execute_action(BrowserAction(action="extract_html", selector=selector))
        return result.html

    async def get_page_state(self) -> BrowserPage:
        """Get a complete snapshot of the current page."""
        html = await self.get_html()
        text = await self.get_text()
        screenshot_result = await self.screenshot()
        return BrowserPage(
            url=self.current_url,
            title="",
            html=html,
            text_content=text,
            screenshot_base64=screenshot_result.screenshot_base64,
        )


# ---------------------------------------------------------------------------
# Browser pipeline — multi-session management
# ---------------------------------------------------------------------------


class BrowserPipeline:
    """Manages multiple browser sessions for concurrent or sequential tasks.

    Example:
        >>> pipeline = BrowserPipeline()
        >>> session = await pipeline.create_session(headless=True)
        >>> await session.navigate("https://example.com")
        >>> await pipeline.close_all()
    """

    def __init__(self, max_sessions: int = 4):
        self._sessions: dict[str, BrowserSession] = {}
        self._max_sessions = max_sessions

    async def create_session(
        self,
        session_id: str | None = None,
        **kwargs,
    ) -> BrowserSession:
        """Create a new browser session."""
        import uuid

        sid = session_id or str(uuid.uuid4())[:8]

        # Cleanup old sessions if at capacity
        if len(self._sessions) >= self._max_sessions:
            oldest = next(iter(self._sessions))
            await self._sessions[oldest].stop()
            del self._sessions[oldest]

        session = BrowserSession(**kwargs)
        await session.start()
        self._sessions[sid] = session
        return session

    async def get_session(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str):
        session = self._sessions.pop(session_id, None)
        if session:
            await session.stop()

    async def close_all(self):
        for session in list(self._sessions.values()):
            await session.stop()
        self._sessions.clear()

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)


# ---------------------------------------------------------------------------
# Convenience: single-run browser task
# ---------------------------------------------------------------------------


async def run_browser_task(
    url: str,
    actions: list[BrowserAction],
    *,
    headless: bool = True,
    screenshot: bool = True,
    extract_text: bool = True,
    timeout_ms: int = 60000,
) -> dict[str, Any]:
    """Run a complete browser task: navigate → execute actions → capture results.

    Args:
        url: Starting URL.
        actions: List of browser actions to execute.
        headless: Run headless.
        screenshot: Capture screenshot at end.
        extract_text: Extract page text at end.
        timeout_ms: Per-action timeout.

    Returns:
        Dict with: success, page_url, text, screenshot_base64, action_results.
    """
    session = BrowserSession(headless=headless)
    try:
        await session.start()
        await session.navigate(url)

        action_results = []
        for action in actions:
            action.timeout_ms = timeout_ms
            result = await session.execute_action(action)
            action_results.append(result)
            if not result.success:
                break

        output: dict[str, Any] = {
            "success": all(r.success for r in action_results),
            "page_url": session.current_url,
            "action_results": [
                {"action": r.action, "success": r.success, "text": r.text[:200], "error": r.error}
                for r in action_results
            ],
        }

        if screenshot:
            ss = await session.screenshot()
            output["screenshot_base64"] = ss.screenshot_base64
        if extract_text:
            output["text"] = await session.get_text()

        return output
    finally:
        await session.stop()
