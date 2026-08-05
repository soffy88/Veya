"""
veya/oprim/browser.py — Atomic browser operations (Layer 1).

Stateless, pure-function browser primitives built on Playwright.
Each operation is a single, independent browser action.

Requires: playwright (pip install playwright && playwright install chromium)
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class BrowserElement:
    """A located browser element."""

    selector: str
    tag_name: str = ""
    text: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    bounding_box: dict[str, float] | None = None
    is_visible: bool = True
    is_enabled: bool = True


@dataclass
class BrowserPage:
    """Snapshot of a browser page."""

    url: str
    title: str
    html: str = ""
    text_content: str = ""
    elements: list[BrowserElement] = field(default_factory=list)
    screenshot_base64: str = ""


@dataclass
class BrowserAction:
    """A single browser action to execute."""

    action: Literal[
        "navigate", "click", "type", "press", "screenshot",
        "extract_text", "extract_html", "scroll", "wait",
        "select", "hover", "focus", "evaluate",
    ]
    selector: str | None = None
    value: str | None = None
    timeout_ms: int = 30000
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "networkidle"


@dataclass
class BrowserActionResult:
    """Result of a single browser action."""

    success: bool
    action: str
    page_url: str = ""
    page_title: str = ""
    text: str = ""
    html: str = ""
    screenshot_base64: str = ""
    elements: list[BrowserElement] = field(default_factory=list)
    error: str = ""
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Browser session management (stateless factory)
# ---------------------------------------------------------------------------


def create_browser_context(
    *,
    headless: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 720,
    user_agent: str | None = None,
    locale: str = "en-US",
    timezone_id: str = "America/New_York",
    proxy: dict[str, str] | None = None,
    storage_state_path: str | None = None,
    accept_downloads: bool = False,
    extra_http_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a browser context configuration dict (stateless).

    This returns a config dict; actual browser instantiation happens
    in the oskill layer (browser pipeline).

    Args:
        headless: Run browser in headless mode.
        viewport_width: Browser viewport width.
        viewport_height: Browser viewport height.
        user_agent: Custom User-Agent string.
        locale: Browser locale.
        timezone_id: Browser timezone.
        proxy: Proxy config {"server": "http://proxy:8080"}.
        storage_state_path: Path to saved auth state (cookies, localStorage).
        accept_downloads: Accept file downloads.
        extra_http_headers: Additional HTTP headers.

    Returns:
        Config dict ready for browser pipeline.
    """
    return {
        "headless": headless,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "user_agent": user_agent,
        "locale": locale,
        "timezone_id": timezone_id,
        "proxy": proxy,
        "storage_state_path": storage_state_path,
        "accept_downloads": accept_downloads,
        "extra_http_headers": extra_http_headers or {},
    }


# ---------------------------------------------------------------------------
# Element selector builders (stateless)
# ---------------------------------------------------------------------------


def build_selector(
    *,
    css: str | None = None,
    text: str | None = None,
    xpath: str | None = None,
    role: str | None = None,
    placeholder: str | None = None,
    label: str | None = None,
    test_id: str | None = None,
    nth: int = 0,
) -> str:
    """Build a Playwright-compatible selector string from semantic parts.

    Example:
        >>> build_selector(role="button", text="Submit")
        'role=button[name="Submit"]'
        >>> build_selector(css=".login-form input[type=email]")
        '.login-form input[type=email]'
        >>> build_selector(placeholder="Search...")
        '[placeholder="Search..."]'
    """
    if css:
        return css
    if xpath:
        return f"xpath={xpath}"

    parts: list[str] = []
    if role:
        parts.append(f"role={role}")
    if text:
        parts.append(f'has-text="{text}"')
    if placeholder:
        parts.append(f'[placeholder="{placeholder}"]')
    if label:
        parts.append(f'[aria-label="{label}"]')
    if test_id:
        parts.append(f'[data-testid="{test_id}"]')

    selector = " ".join(parts) if parts else "*"
    if nth > 0:
        selector = f"{selector} >> nth={nth}"
    return selector


# ---------------------------------------------------------------------------
# Action builders (stateless — produce BrowserAction specs)
# ---------------------------------------------------------------------------


def action_navigate(url: str, wait_until: str = "networkidle") -> BrowserAction:
    """Build a navigate action."""
    return BrowserAction(action="navigate", value=url, wait_until=wait_until)


def action_click(selector: str, timeout_ms: int = 30000) -> BrowserAction:
    """Build a click action."""
    return BrowserAction(action="click", selector=selector, timeout_ms=timeout_ms)


def action_type(selector: str, text: str, timeout_ms: int = 30000) -> BrowserAction:
    """Build a type action."""
    return BrowserAction(action="type", selector=selector, value=text, timeout_ms=timeout_ms)


def action_press(key: str) -> BrowserAction:
    """Build a key press action (e.g., 'Enter', 'Escape', 'Tab')."""
    return BrowserAction(action="press", value=key)


def action_screenshot(selector: str | None = None) -> BrowserAction:
    """Build a screenshot action (full page or element)."""
    return BrowserAction(action="screenshot", selector=selector)


def action_extract_text(selector: str | None = None) -> BrowserAction:
    """Build a text extraction action."""
    return BrowserAction(action="extract_text", selector=selector)


def action_extract_html(selector: str | None = None) -> BrowserAction:
    """Build an HTML extraction action."""
    return BrowserAction(action="extract_html", selector=selector)


def action_scroll(direction: str = "down", amount: int = 500) -> BrowserAction:
    """Build a scroll action."""
    return BrowserAction(action="scroll", value=f"{direction}:{amount}")


def action_wait(ms: int = 1000) -> BrowserAction:
    """Build a wait action."""
    return BrowserAction(action="wait", value=str(ms))


def action_select(selector: str, value: str) -> BrowserAction:
    """Build a select (dropdown) action."""
    return BrowserAction(action="select", selector=selector, value=value)


def action_evaluate(js_code: str) -> BrowserAction:
    """Build a JavaScript evaluation action."""
    return BrowserAction(action="evaluate", value=js_code)


def action_fill_form(fields: dict[str, str]) -> list[BrowserAction]:
    """Build a sequence of type actions for a form.

    Args:
        fields: Dict of {selector: value} for each form field.

    Returns:
        List of BrowserAction for each field + a submit action.
    """
    actions = []
    for selector, value in fields.items():
        actions.append(action_type(selector, value))
    actions.append(action_press("Enter"))
    return actions


# ---------------------------------------------------------------------------
# BrowserGym compatibility — action space translation
# ---------------------------------------------------------------------------


_BROWSERGYM_ACTION_MAP: dict[str, str] = {
    "goto": "navigate",
    "go_back": "press:GoBack",
    "go_forward": "press:GoForward",
    "click": "click",
    "hover": "hover",
    "type": "type",
    "press": "press",
    "scroll": "scroll",
    "select_option": "select",
    "fill": "type",
    "check": "click",  # checkbox
    "uncheck": "click",
    "focus": "focus",
    "clear": "type:",
    "upload_file": "type",  # simplified
    "noop": "wait:0",
    "send_msg_to_user": "evaluate",
}


def browsergym_to_browser_action(
    action_name: str,
    element_bid: str | None = None,
    value: str | None = None,
) -> BrowserAction | None:
    """Translate a BrowserGym action to a BrowserAction.

    Args:
        action_name: BrowserGym action name (e.g., 'click', 'goto', 'type').
        element_bid: BrowserGym element backend ID (converted to selector).
        value: Action value (URL for goto, text for type, etc.).

    Returns:
        BrowserAction or None if unrecognized.
    """
    mapped = _BROWSERGYM_ACTION_MAP.get(action_name)
    if mapped is None:
        return None

    # Handle compound mappings (press:GoBack)
    if ":" in mapped:
        parts = mapped.split(":", 1)
        base_action = parts[0]
        fixed_value = parts[1] if len(parts) > 1 else value
        return BrowserAction(
            action=base_action,
            selector=f"[data-bid='{element_bid}']" if element_bid else None,
            value=fixed_value or value,
        )

    return BrowserAction(
        action=mapped,
        selector=f"[data-bid='{element_bid}']" if element_bid else None,
        value=value,
    )


def browsergym_actions_to_plan(
    actions: list[dict[str, Any]],
) -> list[BrowserAction]:
    """Convert a BrowserGym action plan to BrowserAction list.

    Args:
        actions: List of {"action": "click", "bid": "12", "value": ""} dicts.

    Returns:
        List of BrowserAction objects.
    """
    plan: list[BrowserAction] = []
    for a in actions:
        ba = browsergym_to_browser_action(
            a.get("action", ""),
            element_bid=a.get("bid"),
            value=a.get("value"),
        )
        if ba:
            plan.append(ba)
    return plan


# ---------------------------------------------------------------------------
# Screenshot utilities (stateless)
# ---------------------------------------------------------------------------


def screenshot_to_base64(screenshot_bytes: bytes) -> str:
    """Convert screenshot bytes to base64 string."""
    return base64.b64encode(screenshot_bytes).decode("utf-8")


def screenshot_to_data_uri(screenshot_bytes: bytes, fmt: str = "png") -> str:
    """Convert screenshot bytes to data URI for LLM consumption."""
    b64 = screenshot_to_base64(screenshot_bytes)
    return f"data:image/{fmt};base64,{b64}"


def screenshot_to_file(screenshot_bytes: bytes, path: str) -> str:
    """Save screenshot bytes to a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(screenshot_bytes)
    return path


# ---------------------------------------------------------------------------
# Page content extraction helpers (stateless)
# ---------------------------------------------------------------------------


def extract_interactive_elements(html: str) -> list[BrowserElement]:
    """Extract interactive elements from HTML string using simple parsing.

    For production use, the oskill pipeline uses Playwright's real DOM.
    This is a stateless fallback for offline inspection.
    """
    import re

    elements: list[BrowserElement] = []

    # Extract buttons
    for match in re.finditer(
        r'<button[^>]*>(.*?)</button>|<input[^>]*type="submit"[^>]*>|<a[^>]*href="[^"]*"[^>]*>(.*?)</a>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        text = (match.group(1) or match.group(2) or "").strip()
        tag = "button" if match.group(0).startswith("<button") else ("input" if "input" in match.group(0) else "a")
        if text:
            elements.append(BrowserElement(
                selector=f"text={text[:50]}",
                tag_name=tag,
                text=text[:100],
            ))

    return elements[:50]  # Cap at 50 elements
