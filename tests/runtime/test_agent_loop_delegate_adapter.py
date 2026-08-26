from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_agent_loop_tool_returns_unified_delegate_result(monkeypatch):
    import server.agent_loop_bridge as bridge
    from server.goal_run.wire import wire_master_tools
    from server.tool_registry import master_tools

    async def fake_run(*_args, **kwargs):
        return {
            "status": "success",
            "final_answer": "child result",
            "stop_kind": "completed",
            "tool_calls": [],
            "cost_usd": 0.01,
            "session_id": kwargs.get("session_id"),
        }

    monkeypatch.setattr(bridge, "run_strict_chat", fake_run)
    wire_master_tools()
    raw = await master_tools._functions["agent_loop_run"](task="inspect branch")
    result = json.loads(raw)

    assert result["status"] == "complete"
    assert result["stop_reason"] == "completed"
    assert result["summary"] == "child result"
    assert result["delegate_id"].startswith("agent-loop-tool-")
