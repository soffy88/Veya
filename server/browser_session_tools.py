"""Opt-in stateful browser session tool (flag ``VEYA_BROWSER_SESSION``).

The frozen mainline exposes ``browser_run`` (one URL, one action, fresh
browser).  This module adds the long-lived counterpart a Hark-style RPA flow
needs: one persistent Playwright context per session, an accessibility
snapshot with numbered element refs, and ref-addressed click/type/select.

Everything here is additive and opt-in:

- the flag is OFF by default; with it off ``register_tools`` registers nothing
  and the tool surface stays byte-identical;
- the tool is registered in the canonical registry but never in the resident
  set, so the model reaches it through ``search_tools`` only;
- credentials come from the existing Zero-Trust Vault, never from the model;
- logins and submit/purchase/payment clicks go through the existing
  ``server.privileged_action_gate`` before the physical click.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from server.feature_flags import enabled as _flag_enabled

logger = logging.getLogger("veya.browser_session")

FLAG_NAME = "VEYA_BROWSER_SESSION"
TOOL_NAME = "browser_session"
ROUND_ENV = "VEYA_BROWSER_SESSION_TIMEOUT_MS"
DEFAULT_TIMEOUT_MS = 30_000
MAX_TEXT_CHARS = 8_000

#: Remote Chromium DevTools endpoint (e.g. ``http://veya-browser:9222``).
#: When set the backend attaches to that browser; when unset it launches a
#: local Chromium exactly as before.
CDP_ENV = "VEYA_BROWSER_CDP_URL"

#: Element text/type markers that make a click an irreversible commitment.
SENSITIVE_CLICK_MARKERS = (
    "pay",
    "purchase",
    "buy",
    "checkout",
    "place order",
    "submit",
    "confirm",
    "subscribe",
    "donate",
    "支付",
    "付款",
    "购买",
    "下单",
    "提交",
    "确认",
)

#: URL fragments that mark a page as a checkout/payment surface.
SENSITIVE_URL_MARKERS = (
    "checkout",
    "payment",
    "pay.",
    "/pay",
    "billing",
    "order/confirm",
    "cart/checkout",
)

_SNAPSHOT_JS = """
() => {
  const selector = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="tab"]',
    '[contenteditable="true"]', 'summary', '[onclick]'
  ].join(',');
  const nodes = Array.from(document.querySelectorAll(selector));
  const out = [];
  let n = 0;
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    if (rect.width === 0 && rect.height === 0) continue;
    if (el.disabled) { /* keep: caller must see it as disabled */ }
    n += 1;
    const ref = 'e' + n;
    el.setAttribute('data-veya-ref', ref);
    const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
                  el.getAttribute('placeholder') || el.getAttribute('title') || '')
                 .replace(/\\s+/g, ' ').trim().slice(0, 120);
    out.push({
      ref,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      role: (el.getAttribute('role') || '').toLowerCase(),
      text,
      href: (el.getAttribute('href') || '').slice(0, 300),
      disabled: !!el.disabled,
      checked: el.checked === true,
    });
  }
  return out;
}
"""


class BrowserBackend(Protocol):
    """Physical driver contract; the Playwright implementation and test fakes."""

    async def open(
        self, key: str, url: str, *, storage_state_path: str | None, timeout_ms: int
    ) -> dict[str, Any]: ...

    async def snapshot(self, key: str) -> dict[str, Any]: ...

    async def click(self, key: str, ref: str, *, timeout_ms: int) -> dict[str, Any]: ...

    async def type_text(
        self, key: str, ref: str, text: str, *, timeout_ms: int, clear: bool
    ) -> dict[str, Any]: ...

    async def select(
        self, key: str, ref: str, value: str, *, timeout_ms: int
    ) -> dict[str, Any]: ...

    async def screenshot(self, key: str, path: str, *, full_page: bool) -> dict[str, Any]: ...

    async def wait(
        self, key: str, *, ms: int | None, selector: str | None, timeout_ms: int
    ) -> dict[str, Any]: ...

    async def extract(
        self, key: str, selector: str | None, *, max_chars: int
    ) -> dict[str, Any]: ...

    async def upload(self, key: str, ref: str, path: str, *, timeout_ms: int) -> dict[str, Any]: ...

    async def close(self, key: str) -> dict[str, Any]: ...


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "on", "yes"}


class PlaywrightBackend:
    """One shared Chromium process, one persistent context per browser session.

    Cookies/localStorage are written to ``storage_state_path`` when a session
    closes, so a later session with the same key resumes the login.

    With ``VEYA_BROWSER_CDP_URL`` set the shared browser is a remote Chromium
    reached over CDP (a sidecar container); otherwise a local Chromium is
    launched.  Either way each session gets its own isolated context.
    """

    def __init__(self, *, state_dir: str | Path | None = None) -> None:
        configured = os.environ.get("VEYA_BROWSER_STATE_DIR", "").strip()
        self.state_dir = Path(
            state_dir or configured or (Path.home() / ".veya" / "browser_sessions")
        ).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._playwright: Any = None
        self._browser: Any = None
        self._owns_browser = False
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def state_path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return str(self.state_dir / f"{digest}.json")

    @staticmethod
    def cdp_url() -> str:
        return os.environ.get(CDP_ENV, "").strip()

    async def _ensure_browser(self) -> Any:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._browser is not None:
            # A CDP sidecar restarted under us: drop the dead handles and
            # reconnect on the next session instead of failing forever.
            logger.info("browser disconnected; reconnecting")
            self._browser = None
            self._contexts.clear()
            self._pages.clear()
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        cdp_url = self.cdp_url()
        if cdp_url:
            # The remote browser outlives this process; never close it.
            self._browser = await self._playwright.chromium.connect_over_cdp(
                cdp_url,
                timeout=float(os.environ.get("VEYA_BROWSER_CDP_TIMEOUT_MS", "30000") or 30000),
            )
            self._owns_browser = False
            return self._browser
        launch: dict[str, Any] = {"headless": True}
        executable = os.environ.get("VEYA_BROWSER_EXECUTABLE", "").strip()
        if executable:
            launch["executable_path"] = executable
        if _truthy_env("VEYA_BROWSER_NO_SANDBOX"):
            launch["chromium_sandbox"] = False
            launch["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
        self._browser = await self._playwright.chromium.launch(**launch)
        self._owns_browser = True
        return self._browser

    async def _page(self, key: str, *, create: bool = True) -> Any:
        page = self._pages.get(key)
        if page is not None and self._browser is not None and self._browser.is_connected():
            return page
        if not create:
            raise KeyError(f"browser session not open: {key}")
        async with self._lock:
            page = self._pages.get(key)
            if page is not None and self._browser is not None and self._browser.is_connected():
                return page
            browser = await self._ensure_browser()
            state = self.state_path(key)
            kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 900}}
            if os.path.exists(state):
                kwargs["storage_state"] = state
            context = await browser.new_context(**kwargs)
            page = await context.new_page()
            self._contexts[key] = context
            self._pages[key] = page
            return page

    async def open(
        self, key: str, url: str, *, storage_state_path: str | None, timeout_ms: int
    ) -> dict[str, Any]:
        page = await self._page(key)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return {
            "url": page.url,
            "status": getattr(response, "status", None),
            "title": await page.title(),
        }

    async def snapshot(self, key: str) -> dict[str, Any]:
        page = await self._page(key)
        elements = await page.evaluate(_SNAPSHOT_JS)
        tree: Any = None
        formatted = ""
        try:
            accessibility = getattr(page, "accessibility", None)
            if accessibility is not None and hasattr(accessibility, "snapshot"):
                tree = await accessibility.snapshot(interesting_only=True)
                from veya.obase.a11y import format_a11y_tree

                formatted = format_a11y_tree(tree) if tree else ""
            elif hasattr(page, "locator"):
                formatted = await page.locator("body").aria_snapshot()
        except Exception as exc:  # a11y is best-effort; refs still work
            formatted = f"(accessibility tree unavailable: {type(exc).__name__})"
        return {
            "url": page.url,
            "title": await page.title(),
            "accessibility": formatted,
            "elements": elements,
        }

    async def click(self, key: str, ref: str, *, timeout_ms: int) -> dict[str, Any]:
        page = await self._page(key)
        locator = page.locator(f'[data-veya-ref="{ref}"]')
        await locator.click(timeout=timeout_ms)
        return {"url": page.url, "title": await page.title()}

    async def type_text(
        self, key: str, ref: str, text: str, *, timeout_ms: int, clear: bool
    ) -> dict[str, Any]:
        page = await self._page(key)
        locator = page.locator(f'[data-veya-ref="{ref}"]')
        if clear:
            await locator.fill(text, timeout=timeout_ms)
        else:
            await locator.click(timeout=timeout_ms)
            await page.keyboard.type(text)
        return {"url": page.url}

    async def select(self, key: str, ref: str, value: str, *, timeout_ms: int) -> dict[str, Any]:
        page = await self._page(key)
        locator = page.locator(f'[data-veya-ref="{ref}"]')
        await locator.select_option(value, timeout=timeout_ms)
        return {"url": page.url}

    async def screenshot(self, key: str, path: str, *, full_page: bool) -> dict[str, Any]:
        page = await self._page(key)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = await page.screenshot(path=path, full_page=full_page)
        return {"path": path, "bytes": len(data)}

    async def wait(
        self, key: str, *, ms: int | None, selector: str | None, timeout_ms: int
    ) -> dict[str, Any]:
        page = await self._page(key)
        if selector:
            await page.wait_for_selector(selector, timeout=timeout_ms)
        elif ms:
            await asyncio.sleep(min(max(ms, 0), timeout_ms) / 1000)
        return {"url": page.url}

    async def extract(self, key: str, selector: str | None, *, max_chars: int) -> dict[str, Any]:
        page = await self._page(key)
        if selector:
            text = await page.locator(selector).first.inner_text()
        else:
            text = await page.inner_text("body")
        return {"url": page.url, "text": text[:max_chars]}

    async def upload(self, key: str, ref: str, path: str, *, timeout_ms: int) -> dict[str, Any]:
        page = await self._page(key)
        locator = page.locator(f'[data-veya-ref="{ref}"]')
        await locator.set_input_files(path, timeout=timeout_ms)
        return {"url": page.url, "uploaded": Path(path).name}

    async def close(self, key: str) -> dict[str, Any]:
        context = self._contexts.pop(key, None)
        self._pages.pop(key, None)
        if context is None:
            return {"closed": False}
        state = self.state_path(key)
        try:
            await context.storage_state(path=state)
        except Exception:
            logger.debug("browser session state save failed for %s", key, exc_info=True)
        await context.close()
        return {"closed": True, "state_path": state}

    async def shutdown(self) -> None:
        for key in list(self._contexts):
            try:
                await self.close(key)
            except Exception:
                logger.debug("browser session close failed during shutdown", exc_info=True)
        if self._browser is not None and self._owns_browser:
            await self._browser.close()
        self._browser = None
        self._owns_browser = False
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


class BrowserSessionManager:
    """Track one backend session per (chat session, browser session) pair."""

    def __init__(self, backend_factory: Callable[[], BrowserBackend] | None = None) -> None:
        self._backend_factory = backend_factory or PlaywrightBackend
        self._backend: BrowserBackend | None = None
        self._refs: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_url: dict[str, str] = {}

    def backend(self) -> BrowserBackend:
        if self._backend is None:
            self._backend = self._backend_factory()
        return self._backend

    def ref_meta(self, key: str, ref: str) -> dict[str, Any]:
        return self._refs.get(key, {}).get(str(ref), {})

    def remember(self, key: str, snapshot: dict[str, Any]) -> None:
        self._refs[key] = {
            str(element.get("ref")): element for element in snapshot.get("elements") or []
        }
        if snapshot.get("url"):
            self._last_url[key] = str(snapshot["url"])

    def forget(self, key: str) -> None:
        self._refs.pop(key, None)
        self._last_url.pop(key, None)


_manager: BrowserSessionManager | None = None


def get_manager() -> BrowserSessionManager:
    global _manager
    if _manager is None:
        _manager = BrowserSessionManager()
    return _manager


def reset_state() -> None:
    """Test hook: drop the manager (and any fake backend) between cases."""
    global _manager
    _manager = None


def enabled() -> bool:
    try:
        return _flag_enabled(FLAG_NAME)
    except KeyError:
        return False


def _session_key(session_id: str, browser_id: str) -> str:
    return f"{session_id or 'default'}::{browser_id or 'default'}"


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Never put typed text or file paths into an approval record."""
    safe = {key: value for key, value in arguments.items() if key not in {"text", "path"}}
    if "text" in arguments:
        safe["text_chars"] = len(str(arguments["text"]))
    if "path" in arguments:
        safe["file_name"] = Path(str(arguments["path"])).name
    return safe


def _sensitive_click(meta: dict[str, Any], url: str, explicit: bool) -> tuple[bool, str]:
    if explicit:
        return True, "caller marked the click as irreversible"
    haystack = " ".join(
        [str(meta.get("text") or ""), str(meta.get("type") or ""), str(meta.get("role") or "")]
    ).lower()
    for marker in SENSITIVE_CLICK_MARKERS:
        if marker in haystack:
            return True, f"click target looks like '{marker}'"
    lowered_url = str(url or "").lower()
    for marker in SENSITIVE_URL_MARKERS:
        if marker in lowered_url:
            return True, f"page looks like a checkout surface ('{marker}')"
    return False, ""


async def _request_approval(action: str, arguments: dict[str, Any], url: str) -> Any:
    from server.privileged_action_gate import request_privileged_action

    return request_privileged_action(
        action=action,
        arguments=_redact_arguments(arguments),
        resource=url or "browser",
        effect="destructive",
    )


async def _vault_login(
    vault_id: str,
    intent: dict[str, Any],
    physical: Callable[..., Any],
) -> str:
    """Inject the credential through the existing HITL-gated vault path."""
    from server.zero_trust_vault import global_vault

    return await global_vault.execute_secure_tool(
        tool_name="browser_login",
        intent_args=intent,
        required_vault_id=vault_id,
        physical_tool_callback=physical,
    )


def _output_dir() -> Path:
    configured = os.environ.get("VEYA_OUTPUT_DIR", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".veya" / "runs"
    target = base / "browser_sessions"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(action: str, message: str, **extra: Any) -> str:
    return _json({"status": "failed", "action": action, "error": message, **extra})


async def browser_session(
    action: str,
    *,
    session_id: str = "",
    browser_id: str = "default",
    url: str | None = None,
    ref: str | None = None,
    text: str | None = None,
    value: str | None = None,
    selector: str | None = None,
    path: str | None = None,
    timeout_ms: int | None = None,
    wait_ms: int | None = None,
    full_page: bool = False,
    clear: bool = True,
    vault_id: str | None = None,
    username_ref: str | None = None,
    password_ref: str | None = None,
    sensitive: bool = False,
) -> str:
    """Run one stateful browser action. Returns a JSON string for the model."""
    if not enabled():
        return _error(action, f"browser session disabled ({FLAG_NAME} is OFF)")

    manager = get_manager()
    backend = manager.backend()
    key = _session_key(session_id, browser_id)
    timeout = int(timeout_ms or os.environ.get(ROUND_ENV, DEFAULT_TIMEOUT_MS) or DEFAULT_TIMEOUT_MS)
    timeout = max(1_000, min(timeout, 120_000))
    normalized = str(action or "").strip().lower()

    try:
        if normalized in {"open", "goto", "navigate"}:
            if not url:
                return _error(action, "open requires url")
            result = await backend.open(key, url, storage_state_path=None, timeout_ms=timeout)
            snapshot = await backend.snapshot(key)
            manager.remember(key, snapshot)
            return _json(
                {
                    "status": "ok",
                    "action": "open",
                    "browser_id": browser_id,
                    "page": result,
                    "element_count": len(snapshot.get("elements") or []),
                }
            )

        if normalized in {"snapshot", "observe"}:
            snapshot = await backend.snapshot(key)
            manager.remember(key, snapshot)
            return _json(
                {
                    "status": "ok",
                    "action": "snapshot",
                    "browser_id": browser_id,
                    "url": snapshot.get("url"),
                    "title": snapshot.get("title"),
                    "accessibility": snapshot.get("accessibility", "")[:MAX_TEXT_CHARS],
                    "elements": snapshot.get("elements") or [],
                }
            )

        if normalized == "click":
            if not ref:
                return _error(action, "click requires ref (from snapshot)")
            meta = manager.ref_meta(key, ref)
            page_url = manager._last_url.get(key, "")
            is_sensitive, reason = _sensitive_click(meta, page_url, bool(sensitive))
            if is_sensitive:
                outcome = await _request_approval(
                    "browser_click",
                    {"ref": ref, "element": meta, "reason": reason},
                    page_url,
                )
                if not outcome.approved:
                    return _json(
                        {
                            "status": "approval_required",
                            "action": "click",
                            "reason": outcome.reason or reason,
                            "request_id": outcome.request_id,
                        }
                    )
                from server.privileged_action_gate import record_privileged_action_approval

                record_privileged_action_approval(
                    action="browser_click",
                    arguments={"ref": ref, "element": meta, "reason": reason},
                    session_id=session_id,
                    decision="approved",
                    approval_id=outcome.request_id,
                )
            result = await backend.click(key, ref, timeout_ms=timeout)
            snapshot = await backend.snapshot(key)
            manager.remember(key, snapshot)
            return _json(
                {
                    "status": "ok",
                    "action": "click",
                    "ref": ref,
                    "gated": is_sensitive,
                    **result,
                }
            )

        if normalized == "type":
            if not ref:
                return _error(action, "type requires ref")
            result = await backend.type_text(
                key, ref, text or "", timeout_ms=timeout, clear=bool(clear)
            )
            return _json(
                {"status": "ok", "action": "type", "ref": ref, "chars": len(text or ""), **result}
            )

        if normalized == "select":
            if not ref:
                return _error(action, "select requires ref")
            result = await backend.select(key, ref, value or "", timeout_ms=timeout)
            return _json({"status": "ok", "action": "select", "ref": ref, **result})

        if normalized == "screenshot":
            target = Path(path) if path else _output_dir() / f"shot-{int(time.time() * 1000)}.png"
            result = await backend.screenshot(key, str(target), full_page=bool(full_page))
            return _json({"status": "ok", "action": "screenshot", **result})

        if normalized == "wait":
            result = await backend.wait(key, ms=wait_ms, selector=selector, timeout_ms=timeout)
            return _json({"status": "ok", "action": "wait", **result})

        if normalized in {"extract", "extract_text"}:
            result = await backend.extract(key, selector, max_chars=MAX_TEXT_CHARS)
            return _json({"status": "ok", "action": "extract", **result})

        if normalized == "upload":
            if not ref or not path:
                return _error(action, "upload requires ref and path")
            upload_path = Path(path).expanduser()
            if not upload_path.is_file():
                return _error(action, f"upload file not found: {upload_path.name}")
            result = await backend.upload(key, ref, str(upload_path), timeout_ms=timeout)
            return _json({"status": "ok", "action": "upload", "ref": ref, **result})

        if normalized == "login":
            if not (vault_id and username_ref and password_ref):
                return _error(action, "login requires vault_id, username_ref, password_ref")
            outcome = await _request_approval(
                "browser_login",
                {"vault_id": vault_id, "username_ref": username_ref, "password_ref": password_ref},
                manager._last_url.get(key, ""),
            )
            if not outcome.approved:
                return _json(
                    {
                        "status": "approval_required",
                        "action": "login",
                        "reason": outcome.reason or "login requires human approval",
                        "request_id": outcome.request_id,
                    }
                )
            from server.privileged_action_gate import record_privileged_action_approval

            record_privileged_action_approval(
                action="browser_login",
                arguments={"vault_id": vault_id, "username_ref": username_ref},
                session_id=session_id,
                decision="approved",
                approval_id=outcome.request_id,
            )

            async def fill(
                *,
                _injected_secret: str = "",
                user_ref: str = "",
                pass_ref: str = "",
            ) -> str:
                username, _, password = str(_injected_secret).partition(":")
                await backend.type_text(key, user_ref, username, timeout_ms=timeout, clear=True)
                await backend.type_text(key, pass_ref, password, timeout_ms=timeout, clear=True)
                return "credentials entered"

            await _vault_login(
                vault_id,
                {"user_ref": username_ref, "pass_ref": password_ref},
                fill,
            )
            return _json(
                {
                    "status": "ok",
                    "action": "login",
                    "vault_id": vault_id,
                    "gated": True,
                }
            )

        if normalized == "close":
            result = await backend.close(key)
            manager.forget(key)
            return _json({"status": "ok", "action": "close", **result})

        return _error(
            action,
            "unknown browser action; use open/snapshot/click/type/select/"
            "screenshot/wait/extract/upload/login/close",
        )
    except Exception as exc:
        logger.warning("browser_session %s failed: %s", normalized, type(exc).__name__)
        return _error(action, f"{type(exc).__name__}: {exc}")


def _tool_browser_session(**kwargs: Any) -> Any:
    """Registry entry point; pulls the chat session from the tool contextvar."""
    from server.tool_registry import _current_master_session

    session_id = _current_master_session.get() or ""
    action = str(kwargs.pop("action", "") or "")
    return browser_session(action, session_id=session_id, **kwargs)


_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": (
                "open | snapshot | click | type | select | screenshot | wait | "
                "extract | upload | login | close"
            ),
        },
        "browser_id": {
            "type": "string",
            "description": "Stable id for this browser session (default 'default').",
        },
        "url": {"type": "string", "description": "URL for open."},
        "ref": {
            "type": "string",
            "description": "Element ref from snapshot, e.g. 'e3'.",
        },
        "text": {"type": "string", "description": "Text for type."},
        "value": {"type": "string", "description": "Option value for select."},
        "selector": {
            "type": "string",
            "description": "CSS selector for wait/extract (optional).",
        },
        "path": {"type": "string", "description": "Screenshot or upload file path."},
        "timeout_ms": {"type": "integer", "description": "Action timeout (ms)."},
        "wait_ms": {"type": "integer", "description": "Milliseconds for wait."},
        "full_page": {"type": "boolean", "description": "Full-page screenshot."},
        "clear": {"type": "boolean", "description": "Replace field content on type."},
        "vault_id": {
            "type": "string",
            "description": "Vault id holding 'username:password' for login.",
        },
        "username_ref": {"type": "string", "description": "Ref of the username field."},
        "password_ref": {"type": "string", "description": "Ref of the password field."},
        "sensitive": {
            "type": "boolean",
            "description": "Mark this click as irreversible (forces approval).",
        },
    },
    "required": ["action"],
}


def register_tools(registry: Any) -> int:
    """Register the lazy-only tool when the flag is ON; otherwise no-op."""
    if not enabled():
        return 0
    if registry.has(TOOL_NAME):
        return 0
    from server.tool_registry import SideEffect

    registry.register(
        name=TOOL_NAME,
        description=(
            "Stateful headless-browser session for multi-step web tasks. Call action='open' "
            "then action='snapshot' to get an accessibility tree plus numbered interactive "
            "elements (e1, e2, ...); address them by ref for click/type/select. Logins use "
            "vault_id (never inline passwords). Payment/submit clicks require human approval."
        ),
        parameters=_PARAMETERS,
        func=_tool_browser_session,
        max_result_chars=20_000,
        side_effect=SideEffect.NETWORK_WRITE,
    )
    return 1


__all__ = [
    "FLAG_NAME",
    "TOOL_NAME",
    "BrowserSessionManager",
    "PlaywrightBackend",
    "browser_session",
    "enabled",
    "get_manager",
    "register_tools",
    "reset_state",
]
