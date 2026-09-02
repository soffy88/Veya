"""Targeted test for MCP sidecar readiness.

Ensures that the MCP server infrastructure is properly initialized
and accessible at /api/v1/mcp/health.
This is one of the two production release blockers (alongside GoalRun lease fencing).
"""

from __future__ import annotations

import pytest

from server.routes.cindy_compat import mcp_health
from veya.mcp_server import MCPServer, create_mcp_server


@pytest.mark.asyncio
async def test_mcp_health_handler_returns_ok() -> None:
    """Verify that mcp_health async handler returns healthy status and tool counts."""
    result = await mcp_health()
    assert result["status"] == "ok"
    assert "server" in result
    assert "version" in result
    assert "tools_count" in result
    assert isinstance(result["tools_count"], int)


@pytest.mark.asyncio
async def test_mcp_server_instantiation_and_tool_registration() -> None:
    """Verify create_mcp_server creates a functional MCPServer instance."""
    server = create_mcp_server(name="test-mcp-sidecar")
    assert isinstance(server, MCPServer)
    assert server.name == "test-mcp-sidecar"

    tools = server.tools.list_tools()
    assert isinstance(tools, list)
    assert len(tools) > 0

    # Ensure essential MCP methods respond cleanly
    tools_list_resp = await server.handle_tools_list()
    assert "tools" in tools_list_resp
    assert len(tools_list_resp["tools"]) == len(tools)


@pytest.mark.asyncio
async def test_mcp_server_initialize_handshake() -> None:
    """Verify MCP protocol initialize handshake."""
    server = create_mcp_server()
    init_resp = await server.handle_initialize({"clientInfo": {"name": "test-client", "version": "1.0"}})

    assert init_resp["protocolVersion"] == "2024-11-05"
    assert "serverInfo" in init_resp
    assert init_resp["serverInfo"]["name"] == server.name
