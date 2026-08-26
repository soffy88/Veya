"""Canonical tool facts are emitted at the single physical execution boundary."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_tool_execution_emits_canonical_lifecycle(tmp_path, monkeypatch):
    from server.events import EventStore, bind_event_context, reset_event_context
    from server.tool_guard import global_tool_guard
    from server.tool_registry import MasterToolRegistry

    store = EventStore(tmp_path / "events.jsonl")
    monkeypatch.setattr("server.events.event_store", store)
    monkeypatch.setattr(global_tool_guard, "_policies", [])

    registry = MasterToolRegistry()

    async def echo(value: str) -> str:
        return value

    registry.register(
        "echo",
        "echo a value",
        {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        echo,
    )
    tokens = bind_event_context(session_id="sess-tool", trace_id="trace-tool", turn_id="turn-tool")
    try:
        assert await registry.execute("echo", {"value": "ok"}) == "ok"
    finally:
        reset_event_context(tokens)

    events = store.read_all(session_id="sess-tool")
    assert [event["topic"] for event in events] == [
        "tool.requested",
        "tool.started",
        "tool.completed",
    ]
    assert all(event["trace_id"] == "trace-tool" for event in events)


def test_master_registry_never_hides_schemas(monkeypatch):
    from server.tool_registry import MasterToolRegistry

    monkeypatch.setenv("VEYA_MASTER_LITE_TOOLS", "1")
    registry = MasterToolRegistry()
    registry.register("a", "a", {"type": "object", "properties": {}}, lambda: "a")
    registry.register("b", "b", {"type": "object", "properties": {}}, lambda: "b")
    assert {item["function"]["name"] for item in registry.get_all_schemas()} == {"a", "b"}
