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
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException

from server import auth as auth_mod
from pydantic import BaseModel, Field

from server.tool_governance_adapter import default_veya_output_root
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
    # Deprecated compatibility fields. They are ignored for authorization;
    # only the server-side governance engine issues the execution grant.
    grant: dict[str, Any] | None = None
    grants: list[dict[str, Any]] = Field(default_factory=list)
    request_id: str | None = None


class MCPCallRequest(BaseModel):
    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    version: str = "1"
    actor: str = "master"
    # Deprecated compatibility field. Caller-asserted grants are never trusted.
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
    owner_id: str


def _safe_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("_", str(value).strip())
    return (cleaned or "mcp")[:96]


def _canonically_registered(server: str) -> bool:
    """Whether canonical authorities know this MCP server (any live handle or not).

    Consults the obase client registry (handles + specs) without touching
    the route-local cache. Never raises: unreadable authorities count as
    unknown, and the caller keeps its fail-closed default.
    """
    try:
        obase = load("obase")
    except Exception:
        return False
    try:
        if obase.McpClientRegistry.has(server):
            return True
    except Exception:
        pass
    try:
        obase.McpClientRegistry.spec(server)
        return True
    except (KeyError, AttributeError):
        return False


def _task_context(name: str) -> Any:
    from server.tool_governance_adapter import TaskGovernanceContext

    task_id = f"mcp-rest-{_safe_id(name)}-{uuid.uuid4().hex[:12]}"
    output_root = default_veya_output_root()
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


async def _connect(req: MCPConnectRequest, *, authenticated_subject: str) -> _MCPRegistration:
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
        actor="master",
        # Always let ToolGovernanceAdapter create its own grant from the
        # registered contract. req.grant/req.grants are untrusted legacy fields.
        grant=None,
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
    # Legacy req.grants cannot mint authority. Call-time grants are generated
    # by the canonical ToolGovernanceAdapter from the versioned server contract.
    grants: dict[str, Any] = {}
    return _MCPRegistration(
        url=req.url,
        session=session,
        spec=spec,
        adapter=adapter,
        context=context,
        grants=grants,
        owner_id=authenticated_subject,
    )


@router.post("/connect")
async def mcp_connect_route(
    req: MCPConnectRequest,
    user: dict[str, Any] = Depends(auth_mod.require_owner),
) -> dict[str, Any]:
    authenticated_subject = str(user.get("user_id") or "")
    if not authenticated_subject:
        raise HTTPException(status_code=401, detail="authenticated owner identity is missing")
    existing = _registered.get(req.name)
    if existing is not None and existing.owner_id != authenticated_subject:
        raise HTTPException(status_code=409, detail="MCP server name is already registered")
    registration = await _connect(req, authenticated_subject=authenticated_subject)
    _registered[req.name] = registration
    return {"status": "connected", "name": req.name}


@router.post("/call")
async def mcp_call_route(
    req: MCPCallRequest,
    user: dict[str, Any] = Depends(auth_mod.require_owner),
) -> dict[str, Any]:
    authenticated_subject = str(user.get("user_id") or "")
    if not authenticated_subject:
        raise HTTPException(status_code=401, detail="authenticated owner identity is missing")
    entry = _registered.get(req.server)
    if entry is not None and entry.owner_id != authenticated_subject:
        raise HTTPException(status_code=404, detail="MCP server not registered")
    if entry is None:
        # The route map is a live-handle cache, not the registration truth:
        # a canonically known server without a live handle is a reconnect
        # case (409), only a canonically unknown server is 404.
        if _canonically_registered(req.server):
            raise HTTPException(
                status_code=409,
                detail=f"MCP server '{req.server}' is registered but not connected. "
                "Call /mcp/connect first.",
            )
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
        actor="master",
        # Ignore req.grant; the adapter constructs a grant from the server-owned
        # ToolSpec and the authenticated actor for every invocation.
        grant=None,
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
async def list_mcp_servers(user: dict[str, Any] = Depends(auth_mod.require_user)) -> dict[str, Any]:
    return {
        "servers": list(_registered.keys()),
        "note": "Use POST /mcp/connect to add a governed MCP server.",
    }
