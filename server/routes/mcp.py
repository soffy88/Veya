"""Compatibility REST routes for the canonical MCP governance path.

The route keeps the historical ``/mcp/connect`` and ``/mcp/call`` response
shape, but it is only an API adapter. MCP transport, grants, policy,
approval, audit, and side-effect idempotency remain owned by the existing
Layer-4/3O adapters.

Connect is a network action: the existing Action Gateway authorizes it before
the compatibility connector is called. Calls require an explicit grant and
then use ``ToolGovernanceAdapter`` (which injects the canonical MCP op-prim)
instead of calling a transport from this module.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from veya.platform import load

router = APIRouter(prefix="/mcp", tags=["mcp"])

# Compatibility registry only stores live handles and the versioned contract.
# It is not a second MCP execution or policy authority.
_registered: dict[str, _MCPRegistration] = {}

_TOOL_EFFECTS = frozenset({"read", "local_write", "process", "network", "remote", "destructive"})
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class MCPConnectRequest(BaseModel):
    name: str
    url: str
    timeout: float = 30.0
    version: str = "1"
    protocol_version: str = "2025-03-26"
    actor: str = "master"
    # Optional declarations let compatibility callers provide contracts when
    # their connector does not expose list_tools(). Undeclared effects are
    # conservatively treated as network effects; no name-based inference.
    tools: list[dict[str, Any]] = Field(default_factory=list)
    credential_ref: dict[str, Any] | None = None
    grant: dict[str, Any] | None = None
    grants: list[dict[str, Any]] = Field(default_factory=list)
    request_id: str | None = None


class MCPCallRequest(BaseModel):
    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    version: str = "1"
    actor: str = "master"
    grant: dict[str, Any] | None = None
    request_id: str | None = None


@dataclass
class _MCPRegistration:
    url: str
    session: Any
    spec: Any
    adapter: Any
    context: Any
    grants: dict[str, Any]


def _safe_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("_", str(value).strip())
    return (cleaned or "mcp")[:96]


def _task_context(name: str) -> Any:
    from server.tool_governance_adapter import TaskGovernanceContext

    task_id = f"mcp-rest-{_safe_id(name)}-{uuid.uuid4().hex[:12]}"
    output_root = Path(os.environ.get("VEYA_OUTPUT_DIR", ".veya/runs"))
    return TaskGovernanceContext(
        task_id=task_id,
        session_id=f"mcp-rest-{_safe_id(name)}",
        trace_id=uuid.uuid4().hex,
        output_dir=output_root / _safe_id(task_id) / "outputs" / "mcp",
    )


def _safe_ref(raw: Mapping[str, Any] | None) -> Any:
    if raw is None:
        return None
    ref_id = str(raw.get("id") or "").strip()
    if not ref_id:
        raise ValueError("credential reference id is required")
    obase = load("obase")
    common = {"id": ref_id, "version": raw.get("version"), "scope": raw.get("scope")}
    if str(raw.get("type") or "credential_ref") == "secret_ref":
        return obase.SecretRef(**common)
    return obase.CredentialRef(provider=raw.get("provider"), **common)


def _grant(raw: Mapping[str, Any] | None, *, missing_tool: str) -> Any:
    """Parse a caller-supplied grant without creating an authorization."""
    obase = load("obase")
    if raw is None:
        # Pass an invalid value through the canonical grant check so missing
        # grants receive the normal governance audit and never reach transport.
        return obase.Grant(tool=missing_tool, revoked=True)
    allowed = raw.get("allowed_effects", ())
    if isinstance(allowed, str):
        allowed = (allowed,)
    if not isinstance(allowed, (list, tuple, set, frozenset)):
        allowed = ()
    return obase.Grant(
        tool=str(raw.get("tool") or ""),
        subject=str(raw.get("subject") or "*"),
        grant_id=str(raw.get("grant_id") or uuid.uuid4().hex),
        allowed_effects=frozenset(str(item) for item in allowed),
        tool_version=(str(raw["tool_version"]) if raw.get("tool_version") is not None else None),
        resource=str(raw.get("resource") or "*"),
        expires_at=(str(raw["expires_at"]) if raw.get("expires_at") is not None else None),
        revoked=bool(raw.get("revoked", False)),
        issued_at=str(raw.get("issued_at") or ""),
    )


def _connect_grant(req: MCPConnectRequest) -> Any:
    return _grant(req.grant, missing_tool="__missing_mcp_connect_grant__")


def _call_grant(req: MCPCallRequest, entry: _MCPRegistration) -> Any:
    raw = req.grant
    if raw is None:
        raw = entry.grants.get(f"mcp/{req.server}/{req.tool}@{req.version}")
    return _grant(raw, missing_tool="__missing_mcp_call_grant__")


def _tool_effect(
    raw: Mapping[str, Any],
) -> Literal["read", "local_write", "process", "network", "remote", "destructive"]:
    # Effect metadata is declarative and explicit. MCP has no universal
    # side-effect field, so an absent annotation is fail-closed as network.
    value = raw.get("effect", raw.get("x-veya-effect", "network"))
    effect = str(value).strip().lower()
    return effect if effect in _TOOL_EFFECTS else "network"  # type: ignore[return-value]


def _tool_spec(raw: Mapping[str, Any], *, server: str, server_ref: Any) -> Any:
    obase = load("obase")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("MCP tool name is required")
    schema = raw.get("input_schema") or raw.get("inputSchema") or {}
    if not isinstance(schema, Mapping):
        schema = {}
    ref = _safe_ref(raw.get("credential_ref")) if raw.get("credential_ref") else server_ref
    return obase.ToolSpec(
        name=name,
        description=str(raw.get("description") or ""),
        kind="mcp",
        server=server,
        version=str(raw.get("version") or "1"),
        effect=_tool_effect(raw),
        input_schema=dict(schema),
        credential_ref=ref,
        enabled=bool(raw.get("enabled", True)),
    )


def _discovered_tool(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    # Keep only contract fields. Arbitrary server metadata must not become a
    # persisted audit/result payload.
    return {
        "name": raw.get("name", ""),
        "description": raw.get("description", ""),
        "inputSchema": raw.get("inputSchema") or raw.get("input_schema") or {},
        "effect": raw.get("effect", raw.get("x-veya-effect", "network")),
        "version": raw.get("version", "1"),
        "enabled": raw.get("enabled", True),
    }


def _result_error(result: Mapping[str, Any]) -> tuple[int, str]:
    decision = result.get("decision")
    verdict = decision.get("verdict") if isinstance(decision, Mapping) else None
    if verdict != "ALLOW":
        return 403, "MCP action denied by governance"
    error = result.get("error")
    error_type = error.get("type") if isinstance(error, Mapping) else "MCPError"
    if error_type in {"McpCallError", "RuntimeError", "DurableExecutionError"}:
        return 502, "MCP transport failed"
    return 500, "MCP governance failed"


async def _connect(req: MCPConnectRequest) -> _MCPRegistration:
    """Authorize connect + discovery, then register the canonical contract."""
    load("obase")
    context = _task_context(req.name)
    adapter = await context._ensure_adapter(needs_ledger=True)
    obase = load("obase")
    connect_spec = obase.ToolSpec(
        name="mcp_connect",
        kind="native",
        effect="network",
        capabilities=frozenset({"manual_only"}),
    )
    holder: dict[str, Any] = {}

    async def connect_executor(**_: Any) -> dict[str, bool]:
        # Compatibility API is the injected physical connector. It is only
        # reached after grant + Action Gateway + ledger authorization.
        from veya.compat import mcp_connect

        session = await mcp_connect({"url": req.url}, timeout=req.timeout)
        discovered: list[dict[str, Any]] = []
        list_tools = getattr(session, "list_tools", None)
        if callable(list_tools):
            listed = list_tools()
            if inspect.isawaitable(listed):
                listed = await listed
            discovered = [item for item in (_discovered_tool(raw) for raw in listed) if item]
        holder["session"] = session
        holder["tools"] = discovered or list(req.tools)
        return {"connected": True}

    adapter.register_native(connect_spec, connect_executor)
    request_id = req.request_id or uuid.uuid4().hex
    url_fingerprint = hashlib.sha256(req.url.encode("utf-8")).hexdigest()[:24]
    result = await adapter.execute(
        "mcp_connect",
        {"server": req.name, "timeout": req.timeout},
        actor=req.actor,
        grant=_connect_grant(req),
        request_id=request_id,
        operation_key=f"mcp-connect:{_safe_id(req.name)}:{url_fingerprint}:{request_id}",
        target_ref=f"mcp-server/{req.name}@{req.version}",
        capability="idempotency_key",
    )
    if result.get("status") != "completed" or not result.get("executed"):
        status, detail = _result_error(result)
        raise HTTPException(status_code=status, detail=detail)

    session = holder.get("session")
    if session is None:
        # A compatibility shim may report a successful placeholder without a
        # live handle. Keep the old connect response but make calls fail
        # closed until a versioned contract exists.
        session = result.get("result")
    server_ref = _safe_ref(req.credential_ref)
    tools = [
        _tool_spec(raw, server=req.name, server_ref=server_ref)
        for raw in holder.get("tools", [])
        if isinstance(raw, Mapping)
    ]
    spec = obase.MCPServerSpec(
        name=req.name,
        endpoint=req.url,
        version=req.version,
        protocol_version=req.protocol_version,
        tools=tuple(tools),
        credential_ref=server_ref,
    )
    adapter.register_mcp(spec, session)
    grants: dict[str, Any] = {}
    for raw in req.grants:
        parsed = _grant(raw, missing_tool="__invalid_mcp_grant__")
        if parsed.tool:
            grants[parsed.tool] = parsed
    return _MCPRegistration(
        url=req.url,
        session=session,
        spec=spec,
        adapter=adapter,
        context=context,
        grants=grants,
    )


@router.post("/connect")
async def mcp_connect_route(req: MCPConnectRequest) -> dict[str, Any]:
    registration = await _connect(req)
    _registered[req.name] = registration
    return {"status": "connected", "name": req.name}


@router.post("/call")
async def mcp_call_route(req: MCPCallRequest) -> dict[str, Any]:
    entry = _registered.get(req.server)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{req.server}' not registered. Call /mcp/connect first.",
        )
    if entry.spec.tool(req.tool, req.version) is None:
        raise HTTPException(status_code=404, detail="MCP tool contract is unavailable")

    result = await entry.adapter.execute(
        req.tool,
        req.args,
        kind="mcp",
        server=req.server,
        version=req.version,
        actor=req.actor,
        grant=_call_grant(req, entry),
        request_id=req.request_id or uuid.uuid4().hex,
        operation_key=(
            req.request_id
            or f"mcp-call:{_safe_id(req.server)}:{_safe_id(req.tool)}:"
            f"{hashlib.sha256(repr(sorted(req.args.items())).encode()).hexdigest()[:24]}"
        ),
        target_ref=f"mcp/{req.server}/{req.tool}@{req.version}",
        capability="idempotency_key",
    )
    if result.get("status") == "completed" and result.get("executed"):
        obase = load("obase")
        return {"status": "success", "result": obase.redact_payload(result.get("result"))}
    status, detail = _result_error(result)
    raise HTTPException(status_code=status, detail=detail)


@router.get("")
async def list_mcp_servers() -> dict[str, Any]:
    return {
        "servers": list(_registered.keys()),
        "note": "Use POST /mcp/connect to add a governed MCP server.",
    }
