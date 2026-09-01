"""Inbound MCP protocol calls must use an injected governed executor."""

from __future__ import annotations

import json
from typing import Any

import pytest

from veya.oservi.mcp_server import MCPServer, MCPTool


def _call_payload(name: str, *, grant: dict[str, Any] | None = None) -> str:
    params: dict[str, Any] = {"name": name, "arguments": {}}
    if grant is not None:
        params["grant"] = grant
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})


@pytest.mark.asyncio
async def test_mcp_server_without_governed_executor_has_zero_side_effects() -> None:
    calls: list[str] = []
    server = MCPServer()
    server.tools.register(
        MCPTool(name="fixture_write", description="fixture", handler=lambda: calls.append("ran"))
    )

    response = json.loads(await server.handle_request(_call_payload("fixture_write")))

    assert response["result"]["isError"] is True
    assert calls == []


@pytest.mark.asyncio
async def test_mcp_server_requires_grant_before_injected_executor() -> None:
    calls: list[dict[str, Any]] = []

    async def execute(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True, "token": "must-be-redacted-by-layer4"}

    server = MCPServer(tool_executor=execute)
    server.tools.register(MCPTool(name="fixture_read", description="fixture", effect="read"))

    response = json.loads(await server.handle_request(_call_payload("fixture_read")))

    assert response["result"]["isError"] is True
    assert "grant" in response["result"]["content"][0]["text"]
    assert calls == []


@pytest.mark.asyncio
async def test_mcp_server_delegates_call_and_never_leaks_executor_exception() -> None:
    seen: list[dict[str, Any]] = []
    secret = "fixture-secret-value"

    async def execute(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        raise RuntimeError(secret)

    server = MCPServer(tool_executor=execute)
    server.tools.register(MCPTool(name="fixture_read", description="fixture", effect="read"))
    grant = {
        "tool": "native/fixture_read@1",
        "subject": "mcp-client",
        "allowed_effects": ["read"],
        "tool_version": "1",
    }

    response = json.loads(await server.handle_request(_call_payload("fixture_read", grant=grant)))

    assert seen and seen[0]["grant"] == grant
    assert response["result"]["isError"] is True
    serialized = json.dumps(response)
    assert secret not in serialized
