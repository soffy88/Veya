"""PR-11 Browser Computer + Human Takeover contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from server.browser_computer_adapter import BrowserComputerAdapter
from veya.platform import load

obase = load("obase")
oprim = load("oprim")
oskill = load("oskill")
omodul = load("omodul")
oservi = load("oservi")


class FakeBrowserAdapter:
    def __init__(self) -> None:
        self.handles: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sequence = 0

    @staticmethod
    def _result(handle: Any, operation: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "ok": True,
            "operation": operation,
            "handle": handle.to_dict(),
            "browser": handle.to_dict(),
            "status": handle.state,
        }
        payload.update(extra)
        return payload

    def create(self, profile: Any) -> dict[str, Any]:
        self.sequence += 1
        handle = obase.BrowserSessionHandle(
            session_id=f"browser-test-{self.sequence}",
            profile_id=profile.id,
            computer_id=profile.computer_id,
        )
        self.handles[handle.session_id] = handle
        return self._result(handle, "create")

    def _handle(self, value: Any) -> Any:
        session_id = value.session_id if hasattr(value, "session_id") else value["session_id"]
        return self.handles[session_id]

    def _state(self, value: Any, state: str, *, attached: bool = False) -> dict[str, Any]:
        handle = self._handle(value)
        handle = obase.BrowserSessionHandle(
            **{**handle.to_dict(), "state": state, "attached": attached}
        )
        self.handles[handle.session_id] = handle
        return self._result(handle, "state")

    def start(self, handle: Any) -> dict[str, Any]:
        return self._state(handle, "running")

    def status(self, handle: Any) -> dict[str, Any]:
        return self._result(self._handle(handle), "status")

    def attach(self, handle: Any) -> dict[str, Any]:
        return self._state(handle, "attached", attached=True)

    def stop(self, handle: Any) -> dict[str, Any]:
        return self._state(handle, "stopped")

    def reset(self, handle: Any) -> dict[str, Any]:
        return self._state(handle, "running")

    def set_control_state(self, handle: Any, state: str) -> dict[str, Any]:
        current = self._handle(handle)
        updated = obase.BrowserSessionHandle(**{**current.to_dict(), "control_state": state})
        self.handles[updated.session_id] = updated
        return self._result(updated, "set_control_state")

    def _action(self, handle: Any, operation: str, **kwargs: Any) -> dict[str, Any]:
        current = self._handle(handle)
        self.calls.append((operation, kwargs))
        return self._result(current, operation, **kwargs)

    def navigate(self, handle: Any, url: str, **kwargs: Any) -> dict[str, Any]:
        return self._action(handle, "navigate", url=url, **kwargs)

    def snapshot(self, handle: Any, **kwargs: Any) -> dict[str, Any]:
        return self._action(handle, "snapshot", **kwargs)

    def click(self, handle: Any, selector: str, **kwargs: Any) -> dict[str, Any]:
        return self._action(handle, "click", selector=selector, **kwargs)

    def type(self, handle: Any, selector: str, text: str, **kwargs: Any) -> dict[str, Any]:
        return self._action(handle, "type", selector=selector, text=text, **kwargs)

    def download(self, handle: Any, selector: str, **kwargs: Any) -> dict[str, Any]:
        return self._action(handle, "download", selector=selector, **kwargs)

    def upload(self, handle: Any, selector: str, file_paths: Any, **kwargs: Any) -> dict[str, Any]:
        return self._action(handle, "upload", selector=selector, file_paths=file_paths, **kwargs)

    def screenshot(self, handle: Any, **kwargs: Any) -> dict[str, Any]:
        return self._action(handle, "screenshot", **kwargs)


async def _atomic(atomic: Any, *args: Any, adapter: Any) -> dict[str, Any]:
    return await atomic(*args, adapter=adapter)


@pytest.mark.asyncio
async def test_browser_session_is_bound_to_existing_computer_lifecycle(tmp_path: Path) -> None:
    browser = FakeBrowserAdapter()

    async def prepare_computer(profile: Any, **_: Any) -> dict[str, Any]:
        return {
            "status": "completed",
            "computer": {
                "computer_id": "computer-test",
                "state": "running",
                "workspace": profile.workspace,
            },
        }

    engine = oservi.BrowserComputerEngine(
        computer_prepare=prepare_computer,
        browser_create=lambda profile: _atomic(oprim.browser_create, profile, adapter=browser),
        browser_start=lambda handle: _atomic(oprim.browser_start, handle, adapter=browser),
        browser_status=lambda handle: _atomic(oprim.browser_status, handle, adapter=browser),
        browser_attach=lambda handle: _atomic(oprim.browser_attach, handle, adapter=browser),
        browser_stop=lambda handle: _atomic(oprim.browser_stop, handle, adapter=browser),
        browser_reset=lambda handle: _atomic(oprim.browser_reset, handle, adapter=browser),
        browser_set_control_state=lambda handle, *, state: _atomic(
            oprim.browser_set_control_state, handle, state=state, adapter=browser
        ),
        prepare_browser_session=omodul.prepare_browser_session,
        trigger={"on_demand": True},
        config={"output_dir": str(tmp_path)},
        name="test-browser-computer",
    )
    engine.run()

    result = await engine.prepare(
        obase.ComputerProfile(id="computer-test", workspace=str(tmp_path)),
        obase.BrowserProfile(id="browser-test"),
        attach=True,
        output_dir=tmp_path,
    )

    assert result["status"] == "completed"
    assert result["prepared"] is True
    assert result["browser"]["computer_id"] == "computer-test"
    assert result["browser"]["state"] == "attached"
    assert engine.health()["details"]["remote_worker"] is False


@pytest.mark.asyncio
async def test_browser_atomic_surface_is_single_step_and_complete() -> None:
    browser = FakeBrowserAdapter()
    profile = obase.BrowserProfile(id="browser-test", computer_id="computer-test")
    created = await oprim.browser_create(profile, adapter=browser)
    started = await oprim.browser_start(created["handle"], adapter=browser)
    handle = started["handle"]

    await oprim.browser_navigate(handle, url="https://example.com", adapter=browser)
    await oprim.browser_snapshot(handle, selector="body", adapter=browser)
    await oprim.browser_click(handle, selector="#go", adapter=browser)
    await oprim.browser_type(handle, selector="#name", text="Ada", adapter=browser)
    await oprim.browser_download(handle, selector="#download", adapter=browser)
    await oprim.browser_upload(
        handle, selector="input[type=file]", file_paths=["/tmp/a.txt"], adapter=browser
    )
    await oprim.browser_screenshot(handle, adapter=browser)

    assert [name for name, _ in browser.calls] == [
        "navigate",
        "snapshot",
        "click",
        "type",
        "download",
        "upload",
        "screenshot",
    ]


def test_browser_takeover_policy_is_stateless_and_fail_closed() -> None:
    assert oskill.classify_browser_action_effect("snapshot") == "read"
    assert oskill.classify_browser_action_effect("browser_upload") == "network"
    assert oskill.review_browser_takeover_need("login")["verdict"] == "REQUIRE_HUMAN_CONTROL"
    assert (
        oskill.review_browser_takeover_need("click", control_state="HUMAN_CONTROL")["verdict"]
        == "DENY"
    )
    assert (
        oskill.review_browser_takeover_need("snapshot", control_state="HUMAN_CONTROL")["verdict"]
        == "ALLOW_AGENT"
    )
    assert (
        oskill.review_browser_takeover_need("click", context={"two_factor": True})["verdict"]
        == "REQUIRE_HUMAN_CONTROL"
    )
    assert (
        oskill.review_browser_takeover_need(
            "browser-confirm-payment", context={"sensitive_confirmation": True}
        )["verdict"]
        == "REQUIRE_HUMAN_CONTROL"
    )


class _RecordingLedger:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(str(kwargs["operation_key"]))
        return await kwargs["provider"]()


@pytest.mark.asyncio
async def test_browser_writes_use_existing_action_gateway_and_human_control() -> None:
    browser = FakeBrowserAdapter()
    ledger = _RecordingLedger()
    audits: list[dict[str, Any]] = []
    adapter = BrowserComputerAdapter(
        browser_adapter=browser,
        ledger=ledger,
        approval_resolver=lambda _request: True,
        audit_writer=lambda record: audits.append(record.to_dict()),
        policy_profile="DEVELOPMENT",
    )
    profile = obase.BrowserProfile(id="browser-test", computer_id="computer-test")
    created = await oprim.browser_create(profile, adapter=browser)
    started = await oprim.browser_start(created["handle"], adapter=browser)
    handle = started["handle"]

    allowed = await adapter.action(handle, "click", selector="#go")
    assert allowed["status"] == "completed"
    assert allowed["executed"] is True
    assert [name for name, _ in browser.calls] == ["click"]
    assert len(ledger.calls) == 1
    assert len(audits) >= 3

    human = await adapter.take_control(handle)
    assert human["handle"]["control_state"] == "HUMAN_CONTROL"
    denied = await adapter.action(handle, "click", selector="#go")
    assert denied["status"] == "failed"
    assert denied["verdict"] == "DENY"
    assert [name for name, _ in browser.calls] == ["click"]

    spoofed = await adapter.action(
        handle,
        "click",
        selector="#go",
        context={"control_state": "AGENT_CONTROL"},
    )
    assert spoofed["status"] == "failed"
    assert spoofed["verdict"] == "DENY"
    assert [name for name, _ in browser.calls] == ["click"]

    await adapter.return_control(handle)
    sensitive = await adapter.action(
        handle,
        "navigate",
        url="https://example.com/login",
        context={"authentication": True},
    )
    assert sensitive["status"] == "failed"
    assert sensitive["verdict"] == "REQUIRE_HUMAN_CONTROL"
    assert [name for name, _ in browser.calls] == ["click"]


def test_browser_3o_injection_and_single_source_contract() -> None:
    assert obase.BrowserProfile.__module__ == "obase.browser"
    assert oprim.browser_click.__module__ == "oprim.browser"
    assert oskill.review_browser_takeover_need.__module__ == "oskill.browser_takeover"
    assert omodul.prepare_browser_session.__module__ == "omodul.browser_session"
    points = oservi.BrowserComputerEngine.injection_points
    assert points["computer_prepare"].kind == "layer4"
    for name in (
        "browser_create",
        "browser_start",
        "browser_status",
        "browser_attach",
        "browser_stop",
        "browser_reset",
        "browser_set_control_state",
    ):
        assert points[name].kind == "oprim"
    assert points["prepare_browser_session"].kind == "omodul"
    assert "browser_computer" in oservi.list_skeletons()
