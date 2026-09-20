"""Thin HTTP transport for the remote MCP gateway (spec §5).

Transport only: authentication, sessions, permissions, adaptation and audit all
live in :mod:`veya.remote`. Mounted at ``POST /mcp`` (JSON-RPC) and
``GET /mcp/health`` (readiness, no secrets). The compatibility REST API at
``POST /mcp/connect`` / ``POST /mcp/call`` is unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from veya.remote.mcp_server import create_gateway

router = APIRouter(prefix="/mcp", tags=["remote-mcp"])

_gateway: Any = None


def get_gateway() -> Any:
    """Lazily build the process-wide gateway (credentials read at first use)."""

    global _gateway
    if _gateway is None:
        _gateway = create_gateway()
    return _gateway


@router.post("")
async def remote_mcp_jsonrpc(
    request: Request,
    authorization: str | None = Header(default=None),
    x_veya_session: str | None = Header(default=None),
    mcp_session_id: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()
    response = await get_gateway().handle_message(
        body,
        authorization=authorization,
        remote_addr=request.client.host if request.client else None,
        # Support the MCP-standard ``Mcp-Session-Id`` header used by official
        # clients, as well as the explicit ``X-Veya-Session`` header.
        session_header=mcp_session_id or x_veya_session,
    )
    if response is None:
        # JSON-RPC notification: no body, acknowledged.
        return JSONResponse(status_code=202, content={"status": "accepted"})
    headers: dict[str, str] = {}
    result = response.get("result")
    if isinstance(result, dict) and result.get("sessionId"):
        # Official MCP clients store this and echo it on later requests.
        headers["Mcp-Session-Id"] = str(result["sessionId"])
    return JSONResponse(content=response, headers=headers)


@router.delete("")
async def remote_mcp_terminate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_veya_session: str | None = Header(default=None),
    mcp_session_id: str | None = Header(default=None),
) -> Response:
    """MCP Streamable HTTP session termination (SDK sends DELETE on close)."""

    result = await get_gateway().handle_terminate(
        authorization=authorization,
        session_id=mcp_session_id or x_veya_session,
        remote_addr=request.client.host if request.client else None,
    )
    return Response(status_code=204 if result.get("ok") else 401)


@router.get("/health")
async def remote_mcp_health() -> dict[str, Any]:
    return get_gateway().health()
