"""Hark parity Phase 0: stateful browser session tool (flag OFF by default)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server import browser_session_tools as bst
from server.tool_registry import MasterToolRegistry


class _FakeBackend:
    """In-memory driver: records calls and returns deterministic snapshots."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.snapshot_payload: dict[str, Any] = {
            "url": "https://example.com/",
            "title": "Example",
            "accessibility": '[document] "Example Domain"',
            "elements": [
                {"ref": "e1", "tag": "a", "type": "", "role": "", "text": "More information"},
                {
                    "ref": "e2",
                    "tag": "button",
                    "type": "submit",
                    "role": "",
                    "text": "Submit order",
                },
            ],
        }

    async def open(self, key, url, *, storage_state_path, timeout_ms):
        self.calls.append(("open", {"key": key, "url": url}))
        return {"url": url, "status": 200, "title": "Example"}

    async def snapshot(self, key):
        self.calls.append(("snapshot", {"key": key}))
        return dict(self.snapshot_payload)

    async def click(self, key, ref, *, timeout_ms):
        self.calls.append(("click", {"key": key, "ref": ref}))
        return {"url": "https://example.com/next", "title": "Next"}

    async def type_text(self, key, ref, text, *, timeout_ms, clear):
        self.calls.append(("type", {"key": key, "ref": ref, "text": text, "clear": clear}))
        return {"url": "https://example.com/"}

    async def select(self, key, ref, value, *, timeout_ms):
        self.calls.append(("select", {"key": key, "ref": ref, "value": value}))
        return {"url": "https://example.com/"}

    async def screenshot(self, key, path, *, full_page):
        self.calls.append(("screenshot", {"key": key, "path": path, "full_page": full_page}))
        return {"path": path, "bytes": 12}

    async def wait(self, key, *, ms, selector, timeout_ms):
        self.calls.append(("wait", {"key": key, "ms": ms, "selector": selector}))
        return {"url": "https://example.com/"}

    async def extract(self, key, selector, *, max_chars):
        self.calls.append(("extract", {"key": key, "selector": selector}))
        return {"url": "https://example.com/", "text": "Example Domain"}

    async def upload(self, key, ref, path, *, timeout_ms):
        self.calls.append(("upload", {"key": key, "ref": ref, "path": path}))
        return {"url": "https://example.com/", "uploaded": "file.txt"}

    async def close(self, key):
        self.calls.append(("close", {"key": key}))
        self.closed = True
        return {"closed": True}


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(bst.FLAG_NAME, raising=False)
    bst.reset_state()
    yield
    bst.reset_state()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeBackend:
    monkeypatch.setenv(bst.FLAG_NAME, "1")
    backend = _FakeBackend()
    manager = bst.BrowserSessionManager(lambda: backend)
    monkeypatch.setattr(bst, "get_manager", lambda: manager)
    return backend


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


# ── flag gating: the surface must be byte-identical when OFF ──────────────


def test_flag_defaults_off() -> None:
    assert bst.enabled() is False


def test_registration_is_noop_when_flag_off() -> None:
    registry = MasterToolRegistry()
    before = registry.get_all_schemas()
    assert bst.register_tools(registry) == 0
    assert registry.get_all_schemas() == before
    assert not registry.has(bst.TOOL_NAME)


def test_registration_adds_one_lazy_tool_when_flag_on(monkeypatch) -> None:
    from server import lazy_tools

    monkeypatch.setenv(bst.FLAG_NAME, "1")
    registry = MasterToolRegistry()
    assert bst.register_tools(registry) == 1
    assert registry.has(bst.TOOL_NAME)
    assert bst.register_tools(registry) == 0  # idempotent
    assert bst.TOOL_NAME not in lazy_tools.resident_tool_names()


@pytest.mark.asyncio
async def test_disabled_tool_returns_error_without_touching_backend(monkeypatch) -> None:
    backend = _FakeBackend()
    manager = bst.BrowserSessionManager(lambda: backend)
    monkeypatch.setattr(bst, "get_manager", lambda: manager)
    payload = _payload(await bst.browser_session("open", url="https://example.com"))
    assert payload["status"] == "failed"
    assert "disabled" in payload["error"]
    assert backend.calls == []


# ── actions ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_then_snapshot_exposes_numbered_refs(fake: _FakeBackend) -> None:
    opened = _payload(await bst.browser_session("open", url="https://example.com", session_id="s"))
    assert opened["status"] == "ok"
    assert opened["element_count"] == 2

    snap = _payload(await bst.browser_session("snapshot", session_id="s"))
    assert snap["status"] == "ok"
    assert [element["ref"] for element in snap["elements"]] == ["e1", "e2"]
    assert "accessibility" in snap


@pytest.mark.asyncio
async def test_click_by_ref_and_session_isolation(fake: _FakeBackend) -> None:
    await bst.browser_session("open", url="https://example.com", session_id="s")
    await bst.browser_session("snapshot", session_id="s")
    result = _payload(await bst.browser_session("click", ref="e1", session_id="s"))
    assert result["status"] == "ok"
    assert result["gated"] is False
    assert ("click", {"key": "s::default", "ref": "e1"}) in fake.calls

    # A different browser_id keeps its own context key.
    await bst.browser_session("open", url="https://example.com", browser_id="other", session_id="s")
    assert ("open", {"key": "s::other", "url": "https://example.com"}) in fake.calls


@pytest.mark.asyncio
async def test_type_select_extract_screenshot_upload_wait(fake: _FakeBackend, tmp_path) -> None:
    await bst.browser_session("open", url="https://example.com", session_id="s")
    typed = _payload(await bst.browser_session("type", ref="e1", text="hello", session_id="s"))
    assert typed["status"] == "ok" and typed["chars"] == 5
    selected = _payload(await bst.browser_session("select", ref="e1", value="v", session_id="s"))
    assert selected["status"] == "ok"
    extracted = _payload(await bst.browser_session("extract", selector="body", session_id="s"))
    assert extracted["text"] == "Example Domain"
    shot_path = tmp_path / "shot.png"
    shot = _payload(await bst.browser_session("screenshot", path=str(shot_path), session_id="s"))
    assert shot["path"] == str(shot_path)
    upload_file = tmp_path / "file.txt"
    upload_file.write_text("x")
    uploaded = _payload(
        await bst.browser_session("upload", ref="e1", path=str(upload_file), session_id="s")
    )
    assert uploaded["uploaded"] == "file.txt"
    waited = _payload(await bst.browser_session("wait", wait_ms=10, session_id="s"))
    assert waited["status"] == "ok"


@pytest.mark.asyncio
async def test_upload_rejects_missing_file(fake: _FakeBackend, tmp_path) -> None:
    payload = _payload(
        await bst.browser_session("upload", ref="e1", path=str(tmp_path / "nope"), session_id="s")
    )
    assert payload["status"] == "failed"
    assert "not found" in payload["error"]


@pytest.mark.asyncio
async def test_close_releases_session(fake: _FakeBackend) -> None:
    await bst.browser_session("open", url="https://example.com", session_id="s")
    closed = _payload(await bst.browser_session("close", session_id="s"))
    assert closed["closed"] is True
    assert fake.closed is True


@pytest.mark.asyncio
async def test_unknown_action_is_reported(fake: _FakeBackend) -> None:
    payload = _payload(await bst.browser_session("teleport", session_id="s"))
    assert payload["status"] == "failed"
    assert "unknown browser action" in payload["error"]


# ── approval gate for irreversible clicks ────────────────────────────────


@pytest.mark.asyncio
async def test_sensitive_click_requires_approval(fake: _FakeBackend, monkeypatch) -> None:
    captured: list[dict[str, Any]] = []

    class _Outcome:
        approved = False
        reason = "approval required"
        request_id = "veya-privileged:test"

    async def _fake_request(action, arguments, url):
        captured.append({"action": action, "arguments": arguments, "url": url})
        return _Outcome()

    monkeypatch.setattr(bst, "_request_approval", _fake_request)
    await bst.browser_session("open", url="https://example.com", session_id="s")
    await bst.browser_session("snapshot", session_id="s")
    payload = _payload(await bst.browser_session("click", ref="e2", session_id="s"))
    assert payload["status"] == "approval_required"
    assert captured and captured[0]["action"] == "browser_click"
    assert not any(call[0] == "click" for call in fake.calls)


@pytest.mark.asyncio
async def test_approved_sensitive_click_executes(fake: _FakeBackend, monkeypatch) -> None:
    recorded: list[dict[str, Any]] = []

    class _Outcome:
        approved = True
        reason = "ok"
        request_id = "veya-privileged:ok"

    async def _fake_request(action, arguments, url):
        return _Outcome()

    monkeypatch.setattr(bst, "_request_approval", _fake_request)
    monkeypatch.setattr(
        "server.privileged_action_gate.record_privileged_action_approval",
        lambda **kwargs: recorded.append(kwargs) or True,
    )
    await bst.browser_session("open", url="https://example.com", session_id="s")
    await bst.browser_session("snapshot", session_id="s")
    payload = _payload(await bst.browser_session("click", ref="e2", session_id="s"))
    assert payload["status"] == "ok"
    assert payload["gated"] is True
    assert recorded and recorded[0]["decision"] == "approved"


@pytest.mark.asyncio
async def test_checkout_url_forces_approval_for_plain_link(fake: _FakeBackend, monkeypatch) -> None:
    asked: list[str] = []

    class _Outcome:
        approved = False
        reason = "checkout"
        request_id = "rid"

    async def _fake_request(action, arguments, url):
        asked.append(url)
        return _Outcome()

    monkeypatch.setattr(bst, "_request_approval", _fake_request)
    fake.snapshot_payload = {**fake.snapshot_payload, "url": "https://shop.test/checkout"}
    await bst.browser_session("open", url="https://shop.test/checkout", session_id="s")
    await bst.browser_session("snapshot", session_id="s")
    payload = _payload(await bst.browser_session("click", ref="e1", session_id="s"))
    assert payload["status"] == "approval_required"
    assert asked


# ── credentials only through the Vault ───────────────────────────────────


@pytest.mark.asyncio
async def test_login_reads_secret_from_vault(fake: _FakeBackend, monkeypatch) -> None:
    class _Outcome:
        approved = True
        reason = "ok"
        request_id = "rid-login"

    async def _fake_request(action, arguments, url):
        assert action == "browser_login"
        assert "s3cret" not in json.dumps(arguments)
        return _Outcome()

    async def _fake_vault(vault_id, intent, physical):
        assert vault_id == "site-login"
        return await physical(_injected_secret="alice:s3cret", **intent)

    monkeypatch.setattr(bst, "_request_approval", _fake_request)
    monkeypatch.setattr(bst, "_vault_login", _fake_vault)
    monkeypatch.setattr(
        "server.privileged_action_gate.record_privileged_action_approval",
        lambda **kwargs: True,
    )
    await bst.browser_session("open", url="https://example.com", session_id="s")
    payload = _payload(
        await bst.browser_session(
            "login",
            vault_id="site-login",
            username_ref="e1",
            password_ref="e2",
            session_id="s",
        )
    )
    assert payload["status"] == "ok"
    typed = [call for call in fake.calls if call[0] == "type"]
    assert [call[1]["text"] for call in typed] == ["alice", "s3cret"]


@pytest.mark.asyncio
async def test_login_requires_vault_arguments(fake: _FakeBackend) -> None:
    payload = _payload(await bst.browser_session("login", session_id="s"))
    assert payload["status"] == "failed"
    assert "vault_id" in payload["error"]


def test_redacted_arguments_never_carry_text_or_password() -> None:
    safe = bst._redact_arguments({"ref": "e1", "text": "hunter2", "path": "/tmp/a/b.txt"})
    assert "hunter2" not in json.dumps(safe)
    assert safe["text_chars"] == 7
    assert safe["file_name"] == "b.txt"


def test_canonical_registry_has_no_browser_session_when_flag_off() -> None:
    """The process-global surface is unchanged with the flag OFF."""
    from server.tool_registry import master_tools

    assert not master_tools.has(bst.TOOL_NAME)
    names = [schema["function"]["name"] for schema in master_tools.get_all_schemas()]
    assert bst.TOOL_NAME not in names


def test_browser_computer_adapter_constructs_without_optional_design_capture(
    monkeypatch, tmp_path
) -> None:
    """Regression: binding a missing 3O op made every adapter construction fail.

    ``oprim`` exposes no ``browser_design_capture``; Design Mode capture is
    served by server/routes/design.py. The adapter must construct without it
    and report the action as unknown rather than raising AttributeError.
    """
    from server.browser_computer_adapter import BrowserComputerAdapter

    monkeypatch.setenv("VEYA_OUTPUT_DIR", str(tmp_path))
    adapter = BrowserComputerAdapter(output_dir=tmp_path)
    assert "design_capture" not in adapter._browser_actions
    assert {"navigate", "snapshot", "click", "type", "screenshot"} <= set(adapter._browser_actions)


# ── CDP sidecar: attach when configured, launch locally otherwise ─────────


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[Any] = []
        self.closed = False
        self.storage_state_writes: list[str] = []

    async def new_page(self) -> Any:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def storage_state(self, *, path: str) -> None:
        self.storage_state_writes.append(path)
        Path(path).write_text("{}")

    async def close(self) -> None:
        self.closed = True


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"

    async def goto(self, url: str, **_kwargs: Any) -> Any:
        self.url = url
        return type("Response", (), {"status": 200})()

    async def title(self) -> str:
        return "Example Domain"

    async def evaluate(self, _script: str) -> list[dict[str, Any]]:
        return [{"ref": "e1", "tag": "a", "text": "More information"}]

    async def inner_text(self, _selector: str) -> str:
        return "Example Domain"


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []
        self.closed = False

    async def new_context(self, **_kwargs: Any) -> _FakeContext:
        context = _FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self) -> None:
        self.launch_calls: list[dict[str, Any]] = []
        self.cdp_calls: list[tuple[str, dict[str, Any]]] = []
        self.browser = _FakeBrowser()

    async def launch(self, **kwargs: Any) -> _FakeBrowser:
        self.launch_calls.append(kwargs)
        return self.browser

    async def connect_over_cdp(self, url: str, **kwargs: Any) -> _FakeBrowser:
        self.cdp_calls.append((url, kwargs))
        return self.browser


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakePlaywrightManager:
    def __init__(self) -> None:
        self.instance = _FakePlaywright()

    async def start(self) -> _FakePlaywright:
        return self.instance


def _install_fake_playwright(monkeypatch) -> _FakePlaywrightManager:
    """Install a stub ``playwright.async_api``; the package is optional locally."""
    import sys
    import types

    manager = _FakePlaywrightManager()
    package = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: manager  # type: ignore[attr-defined]
    package.async_api = async_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    return manager


@pytest.mark.asyncio
async def test_cdp_env_attaches_instead_of_launching(monkeypatch, tmp_path) -> None:
    fake = _install_fake_playwright(monkeypatch)
    monkeypatch.setenv(bst.CDP_ENV, "http://veya-browser:9222")
    backend = bst.PlaywrightBackend(state_dir=tmp_path)

    opened = await backend.open(
        "s::default", "https://example.com", storage_state_path=None, timeout_ms=1000
    )

    assert opened["title"] == "Example Domain"
    assert fake.instance.chromium.cdp_calls == [("http://veya-browser:9222", {"timeout": 30000.0})]
    assert fake.instance.chromium.launch_calls == []
    # Each session still gets its own isolated context inside the remote browser.
    assert len(fake.instance.chromium.browser.contexts) == 1
    await backend.open("s::other", "https://example.com", storage_state_path=None, timeout_ms=1000)
    assert len(fake.instance.chromium.browser.contexts) == 2


@pytest.mark.asyncio
async def test_unset_cdp_env_launches_local_chromium(monkeypatch, tmp_path) -> None:
    fake = _install_fake_playwright(monkeypatch)
    monkeypatch.delenv(bst.CDP_ENV, raising=False)
    backend = bst.PlaywrightBackend(state_dir=tmp_path)

    await backend.open(
        "s::default", "https://example.com", storage_state_path=None, timeout_ms=1000
    )

    assert fake.instance.chromium.cdp_calls == []
    assert fake.instance.chromium.launch_calls == [{"headless": True}]


@pytest.mark.asyncio
async def test_cdp_browser_is_not_closed_on_shutdown(monkeypatch, tmp_path) -> None:
    """The sidecar owns its browser process; the backend only detaches."""
    fake = _install_fake_playwright(monkeypatch)
    monkeypatch.setenv(bst.CDP_ENV, "http://veya-browser:9222")
    backend = bst.PlaywrightBackend(state_dir=tmp_path)
    await backend.open(
        "s::default", "https://example.com", storage_state_path=None, timeout_ms=1000
    )

    await backend.shutdown()

    assert fake.instance.chromium.browser.closed is False
    assert fake.instance.chromium.browser.contexts[0].closed is True
    assert fake.instance.stopped is True


@pytest.mark.asyncio
async def test_local_browser_is_closed_on_shutdown(monkeypatch, tmp_path) -> None:
    fake = _install_fake_playwright(monkeypatch)
    monkeypatch.delenv(bst.CDP_ENV, raising=False)
    backend = bst.PlaywrightBackend(state_dir=tmp_path)
    await backend.open(
        "s::default", "https://example.com", storage_state_path=None, timeout_ms=1000
    )

    await backend.shutdown()

    assert fake.instance.chromium.browser.closed is True


@pytest.mark.asyncio
async def test_cdp_timeout_is_configurable(monkeypatch, tmp_path) -> None:
    fake = _install_fake_playwright(monkeypatch)
    monkeypatch.setenv(bst.CDP_ENV, "http://veya-browser:9222")
    monkeypatch.setenv("VEYA_BROWSER_CDP_TIMEOUT_MS", "5000")
    backend = bst.PlaywrightBackend(state_dir=tmp_path)

    await backend.open(
        "s::default", "https://example.com", storage_state_path=None, timeout_ms=1000
    )

    assert fake.instance.chromium.cdp_calls == [("http://veya-browser:9222", {"timeout": 5000.0})]


def test_cdp_url_defaults_empty(monkeypatch) -> None:
    monkeypatch.delenv(bst.CDP_ENV, raising=False)
    assert bst.PlaywrightBackend.cdp_url() == ""
    monkeypatch.setenv(bst.CDP_ENV, "  http://veya-browser:9222  ")
    assert bst.PlaywrightBackend.cdp_url() == "http://veya-browser:9222"
