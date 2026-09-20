"""JSON-RPC 2.0 MCP gateway over the existing Veya runtime (spec §1/§5/§7).

This module is transport + protocol only. Every ``tools/call`` is authenticated,
bound to a workspace session, permission-checked, adapted and audited; the
actual work happens in :mod:`veya.remote.tool_adapter` via canonical Veya tools.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .audit import RemoteAudit
from .auth import RemoteAuth, RemoteAuthError
from .models import RemoteCallResult, RemoteErrorCode
from .session import RemoteSessionError, RemoteSessionManager
from .tool_adapter import RemoteToolAdapter

PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")

_JSONRPC_ERROR = {
    "PARSE": -32700,
    "INVALID_REQUEST": -32600,
    "METHOD_NOT_FOUND": -32601,
    "INVALID_PARAMS": -32602,
    "INTERNAL": -32603,
    "AUTH": -32001,
}


@dataclass
class RemoteMCPGateway:
    auth: RemoteAuth
    sessions: RemoteSessionManager
    audit: RemoteAudit
    adapter: RemoteToolAdapter
    server_name: str = "veya-remote"
    version: str = "1.0.0"
    _started_at: float = field(default_factory=time.time)

    # ── readiness ───────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "server": self.server_name,
            "version": self.version,
            "protocol_version": PROTOCOL_VERSION,
            "auth_configured": self.auth.configured,
            "tools": len(self.adapter.list_tools()),
            "active_sessions": self.sessions.active_count(),
            "uptime_s": round(time.time() - self._started_at, 3),
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return self.adapter.list_tools()

    # ── JSON-RPC entry point ────────────────────────────────────────
    async def handle_message(
        self,
        raw: str | bytes | dict[str, Any],
        *,
        authorization: str | None = None,
        remote_addr: str | None = None,
        session_header: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return _rpc_error(None, _JSONRPC_ERROR["PARSE"], "Parse error")
        if not isinstance(payload, dict):
            return _rpc_error(None, _JSONRPC_ERROR["INVALID_REQUEST"], "Invalid request")

        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(
                request_id, _JSONRPC_ERROR["INVALID_PARAMS"], "params must be an object"
            )

        if method == "initialize":
            return await self._initialize(request_id, params, authorization, remote_addr)
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return _rpc_result(request_id, {})
        if method == "tools/list":
            return self._tools_list(request_id, params, authorization, session_header)
        if method == "tools/call":
            return await self._tools_call(
                request_id, params, authorization, remote_addr, session_header
            )
        return _rpc_error(
            request_id, _JSONRPC_ERROR["METHOD_NOT_FOUND"], f"Method not found: {method}"
        )

    # ── methods ─────────────────────────────────────────────────────
    async def _initialize(
        self,
        request_id: Any,
        params: dict[str, Any],
        authorization: str | None,
        remote_addr: str | None,
    ) -> dict[str, Any]:
        try:
            token = self.auth.verify(authorization)
        except RemoteAuthError as exc:
            self.audit.record(
                tool="initialize",
                status="auth_denied",
                error_code=RemoteErrorCode.AUTH_DENIED,
                remote_addr=remote_addr,
            )
            return _rpc_error(
                request_id, _JSONRPC_ERROR["AUTH"], exc.message, error_code="AUTH_DENIED"
            )
        requested_protocol = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        protocol = (
            requested_protocol
            if requested_protocol in SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION
        )
        try:
            session = self.sessions.create(
                token,
                workspace=params.get("workspace"),
                client_info=params.get("clientInfo") or {},
            )
        except RemoteSessionError as exc:
            return _rpc_error(request_id, _JSONRPC_ERROR["AUTH"], exc.message, error_code=exc.code)
        self.audit.record(
            tool="initialize",
            status="session_created",
            session_id=session.session_id,
            principal=session.principal,
            token_id=session.token_id,
            workspace=session.active_workspace,
            remote_addr=remote_addr,
        )
        return _rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.server_name, "version": self.version},
                "sessionId": session.session_id,
                "workspace": session.active_workspace,
                "permissions": session.permissions.to_dict(),
            },
        )

    def _tools_list(
        self,
        request_id: Any,
        params: dict[str, Any],
        authorization: str | None,
        session_header: str | None,
    ) -> dict[str, Any]:
        try:
            self._require_session(authorization, params, session_header)
        except _GatewayDenied as denied:
            return denied.to_rpc(request_id)
        return _rpc_result(request_id, {"tools": self.adapter.list_tools()})

    async def _tools_call(
        self,
        request_id: Any,
        params: dict[str, Any],
        authorization: str | None,
        remote_addr: str | None,
        session_header: str | None,
    ) -> dict[str, Any]:
        try:
            session = self._require_session(authorization, params, session_header)
        except _GatewayDenied as denied:
            self.audit.record(
                tool=str(params.get("name") or "unknown"),
                status="auth_denied",
                error_code=RemoteErrorCode.AUTH_DENIED,
                remote_addr=remote_addr,
            )
            return denied.to_rpc(request_id)

        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(
                request_id, _JSONRPC_ERROR["INVALID_PARAMS"], "arguments must be an object"
            )

        # An explicit, authorized workspace switch is allowed per call.
        requested_workspace = params.get("workspace") or arguments.pop("workspace", None)
        if requested_workspace:
            try:
                self.sessions.bind_workspace(session, str(requested_workspace))
            except RemoteSessionError as exc:
                return _tools_call_error(
                    request_id,
                    name,
                    RemoteCallResult(
                        ok=False,
                        tool=name,
                        session_id=session.session_id,
                        error_code=RemoteErrorCode(exc.code),
                        message=exc.message,
                    ),
                )

        binding = self.adapter.binding(name)
        effect = str(binding.effect) if binding else None
        self.audit.record(
            tool=name,
            status="requested",
            session_id=session.session_id,
            principal=session.principal,
            token_id=session.token_id,
            veya_tool=binding.veya_tool if binding else None,
            effect=effect,
            workspace=session.active_workspace,
            args=arguments,
            remote_addr=remote_addr,
        )

        started = time.time()
        result = await self.adapter.call(session, name, arguments)
        self.audit.record(
            tool=name,
            status="completed" if result.ok else (result.error_code or "failed"),
            session_id=session.session_id,
            principal=session.principal,
            token_id=session.token_id,
            veya_tool=binding.veya_tool if binding else None,
            effect=effect,
            workspace=result.workspace or session.active_workspace,
            args=arguments,
            error_code=result.error_code,
            duration_ms=(time.time() - started) * 1000,
            execution_id=result.execution_id,
            remote_addr=remote_addr,
        )
        return _tools_call_error(request_id, name, result)

    # ── session termination (MCP Streamable HTTP DELETE) ─────────────
    async def handle_terminate(
        self,
        *,
        authorization: str | None,
        session_id: str | None,
        remote_addr: str | None = None,
    ) -> dict[str, Any]:
        try:
            session = self._require_session(authorization, {"session_id": session_id}, session_id)
        except _GatewayDenied as denied:
            self.audit.record(
                tool="session/terminate",
                status="auth_denied",
                error_code=RemoteErrorCode.AUTH_DENIED,
                remote_addr=remote_addr,
            )
            return {"ok": False, "error_code": denied.code}
        self.sessions.close(session.session_id)
        self.audit.record(
            tool="session/terminate",
            status="closed",
            session_id=session.session_id,
            principal=session.principal,
            token_id=session.token_id,
            workspace=session.active_workspace,
            remote_addr=remote_addr,
        )
        return {"ok": True, "session_id": session.session_id}

    # ── helpers ─────────────────────────────────────────────────────
    def _require_session(
        self,
        authorization: str | None,
        params: dict[str, Any],
        session_header: str | None,
    ):
        try:
            token = self.auth.verify(authorization)
        except RemoteAuthError as exc:
            raise _GatewayDenied("AUTH_DENIED", exc.message) from exc
        session_id = (
            params.get("session_id")
            or params.get("sessionId")
            or session_header
            or (params.get("_meta") or {}).get("session_id")
        )
        try:
            session = self.sessions.require(str(session_id) if session_id else None)
        except RemoteSessionError as exc:
            raise _GatewayDenied(exc.code, exc.message) from exc
        if session.token_id != token.token_id:
            raise _GatewayDenied("AUTH_DENIED", "session does not belong to this token")
        return session


class _GatewayDenied(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_rpc(self, request_id: Any) -> dict[str, Any]:
        rpc_code = (
            _JSONRPC_ERROR["AUTH"]
            if self.code == "AUTH_DENIED"
            else _JSONRPC_ERROR["INVALID_PARAMS"]
        )
        return _rpc_error(request_id, rpc_code, self.message, error_code=self.code)


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: Any,
    rpc_code: int,
    message: str,
    *,
    error_code: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": rpc_code, "message": message}
    if error_code:
        payload["data"] = {"error_code": error_code, **(data or {})}
    elif data:
        payload["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": payload}


def _tools_call_error(request_id: Any, name: str, result: RemoteCallResult) -> dict[str, Any]:
    envelope = result.to_dict()
    text = json.dumps(envelope, ensure_ascii=False, default=str)
    return _rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": envelope,
            "isError": not result.ok,
        },
    )


def create_gateway(
    *,
    auth: RemoteAuth | None = None,
    sessions: RemoteSessionManager | None = None,
    audit: RemoteAudit | None = None,
    adapter: RemoteToolAdapter | None = None,
    server_name: str = "veya-remote",
    version: str = "1.0.0",
) -> RemoteMCPGateway:
    import os

    auth = auth if auth is not None else RemoteAuth.from_env()
    if audit is None:
        audit = RemoteAudit(os.environ.get("VEYA_REMOTE_AUDIT_LOG") or None)
    adapter = adapter if adapter is not None else RemoteToolAdapter(redact=audit.redact)
    if sessions is None:
        sessions = RemoteSessionManager(
            ttl_s=float(os.environ.get("VEYA_REMOTE_SESSION_TTL_S", "3600")),
            max_sessions=int(os.environ.get("VEYA_REMOTE_MAX_SESSIONS", "8")),
        )
    return RemoteMCPGateway(
        auth=auth,
        sessions=sessions,
        audit=audit,
        adapter=adapter,
        server_name=server_name,
        version=version,
    )
