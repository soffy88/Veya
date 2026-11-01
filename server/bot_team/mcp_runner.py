"""MCP bridge: run a bot's turn on an external agent CLI via an MCP server.

An OpenMausBot method, adapted to veya's runner contract. A runner with
``kind="mcp"`` is an MCP Streamable-HTTP endpoint, not another veya
server: the hub initializes, then calls the ``veya_turn`` tool with
``{bot_id, text, context, trace_id, workspace_id}``. The tool returns MCP
content blocks; text blocks join into the reply, and an optional
``structuredContent`` may carry ``task_id``/``session_id``/``status``.

The endpoint is stateless from the hub's side: no session id is stored
or replayed, because the existing transport drops response headers. An
endpoint that requires a session id is not supported; the reference
server in ``apps/web/scripts/mcp-turn-server.py`` is stateless.

Security mirrors the veya runner contract: the runner's token, resolved
from its token_ref, travels as a Bearer token on every call, and the hub
never sees the external agent's own credentials — the CLI on the other
side uses its own login. Fail-closed: any transport failure, bad
JSON-RPC, or tool error is a failed turn, never a silent empty reply.
One retry on transport failure or 5xx, mirroring ``RunnerClient``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from server.bot_team.runner_client import (
    ForwardOutcome,
    RunnerTransport,
    TransportResponse,
    _timeout_from_env,
    redact_secret,
)
from server.bot_team.runners import RunnerRecord

logger = logging.getLogger(__name__)

MCP_TURN_TOOL = "veya_turn"
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_CLIENT_NAME = "veya-hub"


class McpError(RuntimeError):
    """The MCP endpoint answered badly, or not at all."""


@dataclass(frozen=True)
class McpTurnResult:
    reply: str
    task_id: str = ""
    session_id: str = ""
    status: str = "completed"


def _parse_jsonrpc(body: Any, *, request_id: int) -> dict[str, Any]:
    """One JSON-RPC response out of a decoded body or an SSE text body."""

    candidate: Any = None
    if isinstance(body, dict):
        candidate = body
    elif isinstance(body, str):
        text = body.strip()
        if text:
            try:
                candidate = json.loads(text)
            except ValueError:
                candidate = None
            if candidate is None:
                # text/event-stream: take the last data: line that parses.
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        try:
                            parsed = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if isinstance(parsed, dict):
                            candidate = parsed
    if not isinstance(candidate, dict):
        raise McpError("MCP endpoint returned an unreadable response")
    if candidate.get("jsonrpc") != "2.0":
        raise McpError("MCP endpoint returned a non-JSON-RPC response")
    if "id" in candidate and candidate.get("id") != request_id:
        raise McpError("MCP response id does not match the request")
    if "error" in candidate and isinstance(candidate.get("error"), dict):
        error = candidate["error"]
        raise McpError(
            f"MCP error {error.get('code')}: {error.get('message') or 'unknown'}"
        )
    result = candidate.get("result")
    if not isinstance(result, dict):
        raise McpError("MCP response has no result object")
    return result


def _result_to_turn(result: dict[str, Any]) -> McpTurnResult:
    if result.get("isError"):
        texts = [
            str(block.get("text") or "")
            for block in result.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        raise McpError("MCP tool failed: " + (" ".join(texts)[:300] or "unknown error"))
    texts = [
        str(block.get("text") or "")
        for block in result.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        structured = {}
    reply = "\n".join(text for text in texts if text).strip()
    if not reply:
        reply = str(structured.get("reply") or "").strip()
    if not reply:
        raise McpError("MCP tool returned no text")
    return McpTurnResult(
        reply=reply,
        task_id=str(structured.get("task_id") or ""),
        session_id=str(structured.get("session_id") or ""),
        status=str(structured.get("status") or "completed"),
    )


class McpRunnerClient:
    """JSON-RPC client for one MCP turn endpoint."""

    def __init__(
        self,
        transport: RunnerTransport | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        from server.bot_team.runner_client import HttpxTransport

        self._transport = transport if transport is not None else HttpxTransport()
        self.timeout = (
            float(timeout)
            if timeout is not None
            else _timeout_from_env("VEYA_MCP_TURN_TIMEOUT", 300.0)
        )

    async def _post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> TransportResponse:
        return await self._transport.post(
            url, payload=payload, timeout=self.timeout, headers=headers
        )

    async def call_turn(
        self,
        record: RunnerRecord,
        *,
        bot_id: str,
        text: str,
        context: str = "",
        trace_id: str = "",
        workspace_id: str | None = None,
        token: str,
    ) -> McpTurnResult:
        """Initialize, notify, call ``veya_turn``; retry once on transport failure."""

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        last_error = ""
        for attempt in range(2):
            try:
                return await self._call_once(
                    record.base_url,
                    headers=headers,
                    bot_id=bot_id,
                    text=text,
                    context=context,
                    trace_id=trace_id,
                    workspace_id=workspace_id,
                    token=token,
                )
            except McpError as exc:
                message = str(exc)
                if message.startswith("MCP transport:"):
                    last_error = message
                    logger.info(
                        "mcp runner %s attempt %s failed (%s); retrying once",
                        record.runner_id,
                        attempt + 1,
                        redact_secret(message, token)[:200],
                    )
                    continue
                raise
        raise McpError(last_error or "MCP endpoint did not answer")

    async def _call_once(
        self,
        url: str,
        *,
        headers: dict[str, str],
        bot_id: str,
        text: str,
        context: str,
        trace_id: str,
        workspace_id: str | None,
        token: str,
    ) -> McpTurnResult:
        init = await self._post(
            url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": MCP_CLIENT_NAME, "version": "0.1.0"},
                },
            },
            headers,
        )
        if init.status_code == 0:
            raise McpError(f"MCP transport: {redact_secret(init.error, token)}")
        if init.status_code >= 500:
            raise McpError(f"MCP transport: HTTP {init.status_code}")
        if init.status_code != 200:
            raise McpError(f"MCP endpoint refused initialize: HTTP {init.status_code}")
        _parse_jsonrpc(init.body, request_id=1)

        # Best-effort: a server that rejects notifications may still serve
        # tools/call; the call result is the authoritative check.
        try:
            await self._post(
                url,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers,
            )
        except Exception:
            pass

        call = await self._post(
            url,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": MCP_TURN_TOOL,
                    "arguments": {
                        "bot_id": bot_id,
                        "text": text,
                        "context": context,
                        "trace_id": trace_id,
                        "workspace_id": workspace_id,
                    },
                },
            },
            headers,
        )
        if call.status_code == 0:
            raise McpError(f"MCP transport: {redact_secret(call.error, token)}")
        if call.status_code >= 500:
            raise McpError(f"MCP transport: HTTP {call.status_code}")
        if call.status_code != 200:
            raise McpError(f"MCP endpoint refused tools/call: HTTP {call.status_code}")
        return _result_to_turn(_parse_jsonrpc(call.body, request_id=2))


async def forward_mcp_turn(
    record: RunnerRecord,
    *,
    bot_id: str,
    text: str,
    context: str = "",
    trace_id: str = "",
    workspace_id: str | None = None,
    token: str,
    client: McpRunnerClient | None = None,
) -> ForwardOutcome:
    """One MCP turn, in the shape ``forward_bot_turn`` already handles."""

    client = client if client is not None else McpRunnerClient()
    try:
        result = await client.call_turn(
            record,
            bot_id=bot_id,
            text=text,
            context=context,
            trace_id=trace_id,
            workspace_id=workspace_id,
            token=token,
        )
    except (McpError, RuntimeError) as exc:
        return ForwardOutcome(ok=False, error=redact_secret(str(exc), token))
    return ForwardOutcome(
        ok=True,
        status_code=200,
        turn={
            "reply": result.reply,
            "task_id": result.task_id,
            "session_id": result.session_id,
            "status": result.status,
            "trace_id": trace_id,
        },
    )


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCP_TURN_TOOL",
    "McpError",
    "McpRunnerClient",
    "McpTurnResult",
    "forward_mcp_turn",
]
