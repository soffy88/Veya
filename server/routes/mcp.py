"""MCP bridge routes: connect external MCP servers and call their tools.

NOTE: ④ 待外部環境 — requires a live MCP server (HTTP/SSE or stdio).
      Connect+list+call chain is implemented; test once a real MCP server is available.
      Interface: oprim.mcp_connect(server_url, *, timeout) → McpSession
                 oprim.mcp_call_tool(name, *, arguments, client) → dict
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/mcp", tags=["mcp"])

# Registered MCP sessions: name → {url, session_handle}
_registered: dict[str, dict] = {}


class MCPConnectRequest(BaseModel):
    name: str
    url: str
    timeout: float = 30.0


class MCPCallRequest(BaseModel):
    server: str
    tool: str
    args: dict[str, Any] = {}


@router.post("/connect")
async def mcp_connect_route(req: MCPConnectRequest) -> dict[str, Any]:
    from veya.compat import mcp_connect

    try:
        session = await mcp_connect(req.url, timeout=req.timeout)
        _registered[req.name] = {"url": req.url, "session": session}
        return {"status": "connected", "name": req.name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MCP connect failed: {exc}")


@router.post("/call")
async def mcp_call_route(req: MCPCallRequest) -> dict[str, Any]:
    from veya.compat import mcp_call_tool

    entry = _registered.get(req.server)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{req.server}' not registered. Call /mcp/connect first.",
        )
    try:
        result = await mcp_call_tool(
            req.tool,
            arguments=req.args,
            client=entry["session"],
        )
        return {"status": "success", "result": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MCP call failed: {exc}")


@router.get("")
async def list_mcp_servers() -> dict[str, Any]:
    return {
        "servers": list(_registered.keys()),
        "note": "待外部環境 — no MCP server registered yet. Use POST /mcp/connect to add one.",
    }
