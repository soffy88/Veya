"""Plan mode + high-impact approval (user-held steering)."""

from __future__ import annotations

import asyncio

import pytest

from server import user_control as uc
from server.tool_guard import ToolDenied, ToolGuard


@pytest.fixture
def isolated_guard():
    g = ToolGuard()
    uc.install_user_control_policy(g)
    return g


def test_plan_mode_blocks_hicode_allows_assemble(isolated_guard):
    tokens = uc.activate(mode="plan", require_approval=False, session_id="s1")
    try:
        with pytest.raises(ToolDenied, match="plan mode"):
            asyncio.run(isolated_guard.acheck("hicode_run", {"task": "x"}, source="test"))
        asyncio.run(isolated_guard.acheck("assemble_code_context", {"query": "x"}, source="test"))
        asyncio.run(isolated_guard.acheck("grep", {"pattern": "foo"}, source="test"))
    finally:
        uc.deactivate(tokens)


def test_agent_mode_without_approval_allows_high_impact(isolated_guard):
    tokens = uc.activate(mode="agent", require_approval=False, session_id="s1")
    try:
        asyncio.run(isolated_guard.acheck("hicode_run", {"task": "x"}, source="test"))
        asyncio.run(isolated_guard.acheck("write_file", {"path": "a.py"}, source="test"))
    finally:
        uc.deactivate(tokens)


def test_resolve_approval_unblocks(isolated_guard):
    tokens = uc.activate(mode="agent", require_approval=True, session_id="s1")

    async def _run() -> None:
        task = asyncio.create_task(
            isolated_guard.acheck("hicode_run", {"task": "x"}, source="test")
        )
        await asyncio.sleep(0.05)
        assert uc._pending, "should have parked an approval"
        rid = next(iter(uc._pending))
        assert uc.resolve_approval(rid, True) is True
        await task

    try:
        asyncio.run(_run())
    finally:
        uc.deactivate(tokens)
        uc._pending.clear()


def test_luna_core_keeps_hicode():
    from veya.obase._llm_protocol import _core_tool_schemas

    tools = [
        {"function": {"name": "hicode_run"}},
        {"function": {"name": "fetch_url"}},
        {"function": {"name": "mcp_hevi"}},
    ]
    names = {(s.get("function") or {}).get("name") for s in (_core_tool_schemas(tools) or [])}
    assert "hicode_run" in names
    assert "fetch_url" in names
    assert "mcp_hevi" not in names
