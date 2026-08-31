"""Product hardening: legacy product entry points use PR-13 governance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server import events as events_module
from server.events import EventStore
from server.skill_hub import VeyaSkillHub
from server.tool_governance_adapter import (
    bind_task_governance,
    current_task_governance,
    reset_task_governance,
)
from server.tool_registry import (
    _INTERNALLY_GOVERNED_TOOLS,
    MasterToolRegistry,
    SideEffect,
)


def test_product_bridge_does_not_double_govern_browser_run() -> None:
    """The compatibility entry keeps its existing internal gateway owner."""

    assert "browser_run" in _INTERNALLY_GOVERNED_TOOLS


@pytest.mark.asyncio
async def test_internal_browser_entry_is_not_wrapped_by_product_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events_module, "event_store", event_store)
    registry = MasterToolRegistry()
    registry.register(
        "browser_run",
        "already governed browser compatibility entry",
        {"type": "object", "properties": {}},
        lambda: "browser-result",
        side_effect=SideEffect.PURE_READ,
    )
    token = bind_task_governance(
        task_id="hardening-browser-bridge",
        session_id="hardening-session",
        trace_id="hardening-trace",
        output_dir=tmp_path / "outputs",
    )
    try:
        governance = current_task_governance()
        assert governance is not None

        async def unexpected_outer_governance(**_: Any) -> str:
            raise AssertionError("browser_run was wrapped by the product bridge")

        monkeypatch.setattr(governance, "execute_native", unexpected_outer_governance)
        assert await registry.execute("browser_run", {}) == "browser-result"
    finally:
        reset_task_governance(token)


@pytest.mark.asyncio
async def test_product_static_registry_uses_action_gateway_and_replays_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events_module, "event_store", event_store)
    monkeypatch.setenv("VEYA_EXECUTION_SQLITE_PATH", str(tmp_path / "execution.sqlite3"))

    registry = MasterToolRegistry()
    physical_calls: list[str] = []
    registry.register(
        "write_file",
        "write a local fixture",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        lambda value: physical_calls.append(value) or {"saved": value},
        side_effect=SideEffect.LOCAL_WRITE,
        effect_capability="idempotency_key",
    )

    token = bind_task_governance(
        task_id="hardening-static",
        session_id="hardening-session",
        trace_id="hardening-trace",
        output_dir=tmp_path / "outputs",
    )
    try:
        first = await registry.execute("write_file", {"value": "safe"})
        second = await registry.execute("write_file", {"value": "safe"})
    finally:
        reset_task_governance(token)

    assert json.loads(first)["saved"] == "safe"
    assert second == first
    assert physical_calls == ["safe"]
    audit_events = event_store.read_all(task_id="hardening-static", topics={"action_gateway.audit"})
    assert audit_events
    assert all(event["task_id"] == "hardening-static" for event in audit_events)


@pytest.mark.asyncio
async def test_product_nested_registry_calls_keep_one_governance_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events_module, "event_store", event_store)
    monkeypatch.setenv("VEYA_EXECUTION_SQLITE_PATH", str(tmp_path / "execution.sqlite3"))
    registry = MasterToolRegistry()

    registry.register(
        "hardening_inner_read",
        "read a fixture",
        {"type": "object", "properties": {}},
        lambda: "inner",
        side_effect=SideEffect.PURE_READ,
    )

    async def outer() -> str:
        return await registry.execute("hardening_inner_read", {})

    registry.register(
        "hardening_outer_read",
        "read through another registered callable",
        {"type": "object", "properties": {}},
        outer,
        side_effect=SideEffect.PURE_READ,
    )
    token = bind_task_governance(
        task_id="hardening-nested",
        session_id="hardening-session",
        trace_id="hardening-trace",
        output_dir=tmp_path / "outputs",
    )
    try:
        result = await registry.execute("hardening_outer_read", {})
    finally:
        reset_task_governance(token)

    assert result == "inner"
    audits = event_store.read_all(task_id="hardening-nested", topics={"action_gateway.audit"})
    assert len(audits) >= 2


@pytest.mark.asyncio
async def test_product_skillhub_dispatch_uses_existing_governance_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr(events_module, "event_store", event_store)
    monkeypatch.setenv("VEYA_EXECUTION_SQLITE_PATH", str(tmp_path / "execution.sqlite3"))
    skill_dir = tmp_path / "safe_echo"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "safe_echo",
                "description": "echo one goal",
                "parameters": {
                    "type": "object",
                    "properties": {"goal": {"type": "string"}},
                    "required": ["goal"],
                },
                "entrypoint": "run.py",
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "run.py").write_text(
        "def main(goal: str) -> str:\n    return goal\n", encoding="utf-8"
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)
    token = bind_task_governance(
        task_id="hardening-skill",
        session_id="hardening-session",
        trace_id="hardening-trace",
        output_dir=tmp_path / "outputs",
    )
    try:
        result = await hub.execute(
            "run_skill", {"skill_name": "safe_echo", "args": {"goal": "safe"}}
        )
    finally:
        reset_task_governance(token)

    assert result == "safe"
    assert event_store.read_all(task_id="hardening-skill", topics={"action_gateway.audit"})
