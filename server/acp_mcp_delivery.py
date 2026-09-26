"""ACP session <-> MCP capability delivery assembly (D5D, Veya project layer).

ONE session-attachment authority: binds ACP transport sessions to
session-scoped MCP capability *references* (ids/URIs/generations — never
copied definitions, never a second registry).

Boundaries (frozen):
- D3 decides resume-vs-fresh; here we only attach/rebind/detach on sessions
  the caller already owns. This module never creates transport sessions to
  implement a policy and never judges rejections.
- D9 owns selection; here we only answer "which allowed MCP
  capabilities are currently delivered to this session".
- obase owns transports/protocols; ToolRegistry owns tool exposure; the
  route ``_registered`` map stays a live-handle cache (see routes/mcp.py).

Delivery sessions are process-bound (ACP backends are in-process children),
so live attachment state is in-memory; every edge is also audited through
the injected ``emit`` callable into the canonical event model.
"""

from __future__ import annotations

import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ATTACHMENT_STATUSES",
    "AcpMcpDelivery",
    "AcpMcpSession",
    "McpAttachPolicy",
    "McpCapabilityRef",
    "McpSourceAttachment",
    "default_mcp_sources",
    "discover_via_client_registry",
    "discover_via_route_session",
    "get_acp_mcp_delivery",
    "reset_acp_mcp_delivery",
]


async def _default_discover(server: str) -> McpSourceDiscovery:
    """Canonical-authorities-first discovery (registry, then route cache)."""
    from veya.platform import load

    if load("obase").McpClientRegistry.has(server):
        return await discover_via_client_registry(server)
    return await discover_via_route_session(server)


ATTACHMENT_STATUSES = (
    "attached",
    "denied",
    "unsupported",
    "failed",
    "stale",
)


@dataclass(frozen=True)
class McpCapabilityRef:
    """One canonical capability reference (identity only, never a copy)."""

    kind: str  # "tool" | "resource" | "skill"
    ref: str
    server: str
    version: str | None = None


@dataclass(frozen=True)
class McpSourceDiscovery:
    """What one MCP source currently offers (transport observation)."""

    server: str
    transport: str
    tools: tuple[dict[str, Any], ...] = ()
    resources: tuple[dict[str, Any], ...] = ()
    skills_supported: bool = False
    version: str | None = None

    def tool_refs(self) -> tuple[str, ...]:
        out: list[str] = []
        for tool in self.tools:
            name = str(tool.get("name", "")) if isinstance(tool, dict) else ""
            if name:
                out.append(f"mcp/{self.server}/{name}")
        return tuple(sorted(set(out)))

    def resource_refs(self) -> tuple[str, ...]:
        out: list[str] = []
        for res in self.resources:
            uri = str(res.get("uri", "")) if isinstance(res, dict) else ""
            if uri:
                out.append(uri)
        return tuple(sorted(set(out)))


@dataclass(frozen=True)
class McpAttachPolicy:
    """Transport gate for attachment (fail-closed per source)."""

    allowed_transports: frozenset[str] = frozenset({"stdio", "http", "https", "route-session"})
    require_registered_spec: bool = False

    def check(self, transport: str, *, spec_known: bool) -> tuple[bool, str]:
        """Return (allowed, reason). Missing specs are recorded, not guessed."""
        if transport not in self.allowed_transports:
            return False, f"transport {transport!r} not in allow-list"
        if self.require_registered_spec and not spec_known:
            return False, "registered MCP spec required but unknown"
        return True, "allowed"


@dataclass
class McpSourceAttachment:
    """Session-scoped attachment to one MCP source (refs only)."""

    source: str
    transport: str
    generation: int = 1
    tool_refs: tuple[str, ...] = ()
    resource_refs: tuple[str, ...] = ()
    status: str = "attached"
    spec_verified: bool = False
    last_refresh_at: float | None = None
    last_refresh_status: str | None = None
    error: str | None = None
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "transport": self.transport,
            "generation": self.generation,
            "tool_refs": list(self.tool_refs),
            "resource_refs": list(self.resource_refs),
            "skill_refs": [],
            "skills_supported": False,
            "status": self.status,
            "spec_verified": self.spec_verified,
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_status": self.last_refresh_status,
            "error": self.error,
            "version": self.version,
        }

    def capability_refs(self) -> list[McpCapabilityRef]:
        """Typed view over the stored string refs (no copies)."""
        return [
            McpCapabilityRef(kind="tool", ref=name, server=self.source, version=self.version)
            for name in self.tool_refs
        ] + [
            McpCapabilityRef(kind="resource", ref=uri, server=self.source, version=self.version)
            for uri in self.resource_refs
        ]


@dataclass
class AcpMcpSession:
    """One delivery session: ACP transport id + MCP attachment refs."""

    session_id: str
    backend_kind: str
    sources: dict[str, McpSourceAttachment] = field(default_factory=dict)
    capabilities: dict[str, bool] = field(
        default_factory=lambda: {
            "mcp_attachment": True,
            "mcp_refresh": True,
            "session_close": True,
        }
    )
    closed: bool = False
    created_at: float = field(default_factory=time.time)
    owner: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _on_close_transport: Any = field(default=None, repr=False, compare=False)
    _remote_close: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "backend_kind": self.backend_kind,
            "generation": max([item.generation for item in self.sources.values()] or [0]),
            "sources": {name: item.to_dict() for name, item in self.sources.items()},
            "capabilities": dict(self.capabilities),
            "closed": self.closed,
            "created_at": self.created_at,
            "owner": self.owner,
            "metadata": dict(self.metadata),
            "remote_close": dict(self._remote_close),
        }


async def discover_via_client_registry(server: str) -> McpSourceDiscovery:
    """Read one MCP source through the canonical obase client registry."""
    from veya.platform import load

    obase = load("obase")
    handle = obase.McpClientRegistry.get(server)
    transport = str(getattr(handle, "transport", "route-session") or "route-session")
    tools = await handle.list_tools() if hasattr(handle, "list_tools") else []
    resources: list[dict[str, Any]] = []
    if obase.supports_resources(handle):
        resources = await obase.list_resources(handle)
    return McpSourceDiscovery(
        server=server,
        transport=transport,
        tools=tuple(item for item in tools if isinstance(item, dict)),
        resources=tuple(item for item in resources if isinstance(item, dict)),
        skills_supported=False,
        version=None,
    )


async def discover_via_route_session(server: str) -> McpSourceDiscovery:
    """Read one MCP source through the route live-handle cache.

    The route map stays a handle cache: this reader borrows its live
    session object and returns observations. Truth about registration
    remains with the canonical registries.
    """
    from server.routes import mcp as mcp_routes

    entry = mcp_routes._registered.get(server)
    if entry is None:
        raise KeyError(f"no live route handle for MCP server {server!r}")
    session = entry.session
    list_tools = getattr(session, "list_tools", None)
    tools: list[dict[str, Any]] = []
    if callable(list_tools):
        import inspect as _inspect

        listed = list_tools()
        if _inspect.isawaitable(listed):
            listed = await listed
        tools = [item for item in listed or [] if isinstance(item, dict)]
    return McpSourceDiscovery(
        server=server,
        transport="route-session",
        tools=tuple(tools),
        resources=(),
        skills_supported=False,
        version=getattr(entry.spec, "version", None),
    )


class AcpMcpDelivery:
    """Session-scoped MCP delivery coordinator (single attachment authority).

    Holds attachment *references* per ACP session plus a
    source -> sessions usage projection for shared-connection safety.
    Never copies tool/provider definitions, never ranks capabilities,
    never decides resume-vs-fresh.
    """

    def __init__(
        self,
        *,
        emit: Callable[[str, dict[str, Any]], Any] | None = None,
        discover: Callable[[str], Awaitable[McpSourceDiscovery]] | None = None,
    ) -> None:
        self._sessions: dict[str, AcpMcpSession] = {}
        self._usage: dict[str, set[str]] = {}
        self._emit = emit
        self._discover = discover or _default_discover

    # -- events ---------------------------------------------------------
    def _event(self, topic: str, payload: Mapping[str, Any]) -> None:
        if self._emit is None:
            return
        self._emit(topic, dict(payload))

    # -- open (new session path) ----------------------------------------
    async def open_session(
        self,
        session_id: str,
        *,
        backend_kind: str = "acp",
        sources: list[str] | None = None,
        policy: McpAttachPolicy | None = None,
        owner: str | None = None,
        on_close_transport: Any = None,
        reuse_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AcpMcpSession:
        """Open (or rebind) a delivery session and attach MCP sources.

        With ``reuse_key``, an already-open session for the same backend
        identity is revalidated instead of duplicated (resume/reconnect
        path). Unknown/unsupported sources are recorded honestly — never
        faked, never fatal to the ACP session itself.
        """
        active_policy = policy or McpAttachPolicy()
        if reuse_key is not None:
            for session in self._sessions.values():
                if not session.closed and session.metadata.get("reuse_key") == reuse_key:
                    if on_close_transport is not None:
                        session._on_close_transport = on_close_transport
                    return await self.reconnect(session.session_id)
        if session_id in self._sessions and not self._sessions[session_id].closed:
            raise ValueError(f"delivery session already open: {session_id!r}")
        session = AcpMcpSession(
            session_id=session_id,
            backend_kind=backend_kind,
            owner=owner,
            metadata={**(metadata or {}), **({"reuse_key": reuse_key} if reuse_key else {})},
        )
        session._on_close_transport = on_close_transport
        self._sessions[session_id] = session
        for source in sources or []:
            await self._attach_source(session, source, active_policy, owner=owner)
        return session

    async def _attach_source(
        self,
        session: AcpMcpSession,
        source: str,
        policy: McpAttachPolicy,
        *,
        owner: str | None = None,
    ) -> McpSourceAttachment:
        from veya.platform import load

        obase = load("obase")
        try:
            discovery = await self._discover(source)
        except KeyError as exc:
            attachment = McpSourceAttachment(
                source=source,
                transport="unknown",
                status="unsupported",
                error=f"source unknown: {exc}",
            )
        except Exception as exc:
            attachment = McpSourceAttachment(
                source=source,
                transport="unknown",
                status="failed",
                error=f"discovery failed: {exc}",
            )
        else:
            try:
                spec_known = self._spec_known(source, obase)
            except Exception:
                spec_known = False
            allowed, reason = policy.check(discovery.transport, spec_known=spec_known)
            if not allowed:
                attachment = McpSourceAttachment(
                    source=source,
                    transport=discovery.transport,
                    status="denied",
                    spec_verified=spec_known,
                    error=reason,
                    version=discovery.version,
                )
            else:
                attachment = McpSourceAttachment(
                    source=source,
                    transport=discovery.transport,
                    tool_refs=discovery.tool_refs(),
                    resource_refs=discovery.resource_refs(),
                    status="attached",
                    spec_verified=spec_known,
                    last_refresh_at=time.time(),
                    last_refresh_status="attached",
                    version=discovery.version,
                )
        session.sources[source] = attachment
        if attachment.status == "attached":
            self._usage.setdefault(source, set()).add(session.session_id)
        self._event(
            "acp.mcp.attached",
            {
                "session_id": session.session_id,
                "generation": attachment.generation,
                "source": source,
                "status": attachment.status,
                "tool_refs": list(attachment.tool_refs),
                "resource_refs": list(attachment.resource_refs),
                "reason": attachment.error or "attached",
            },
        )
        return attachment

    @staticmethod
    def _spec_known(source: str, obase: Any) -> bool:
        try:
            obase.McpClientRegistry.spec(source)
            return True
        except (KeyError, AttributeError):
            return False

    # -- reconnect / refresh --------------------------------------------
    async def reconnect(self, session_id: str) -> AcpMcpSession:
        """Revalidate an existing open session (no duplicate attachments).

        Transport liveness stays the caller's (D3) business; here we only
        re-read MCP sources and rebind or refresh by generation diff.
        """
        session = self._require_open(session_id)
        for source in list(session.sources):
            current = session.sources[source]
            if current.status != "attached":
                continue
            try:
                discovery = await self._discover(source)
            except Exception as exc:
                current.status = "stale"
                current.error = f"revalidate failed, last good kept: {exc}"
                continue
            fresh_tools, fresh_resources = discovery.tool_refs(), discovery.resource_refs()
            if (fresh_tools, fresh_resources) == (current.tool_refs, current.resource_refs):
                current.last_refresh_at = time.time()
                current.last_refresh_status = "revalidated"
            else:
                await self.refresh(session_id, source)
        return session

    async def refresh(self, session_id: str, source: str | None = None) -> dict[str, Any]:
        """Refresh attachments; generation advances only on real change.

        Failures preserve the last known good attachment (never emptied).
        """
        session = self._require_open(session_id)
        targets = (
            [source]
            if source
            else [
                name
                for name, item in session.sources.items()
                if item.status in ("attached", "stale")
            ]
        )
        report: dict[str, Any] = {"session_id": session_id, "refreshed": [], "failed": []}
        for name in targets:
            current = session.sources.get(name)
            if current is None:
                report["failed"].append({"source": name, "reason": "not attached"})
                continue
            try:
                discovery = await self._discover(name)
            except Exception as exc:
                current.status = "stale" if current.status == "attached" else current.status
                current.error = f"refresh failed, last good kept: {exc}"
                current.last_refresh_status = "failed"
                report["failed"].append({"source": name, "reason": str(exc)[:200]})
                self._event(
                    "acp.mcp.refresh_failed",
                    {
                        "session_id": session_id,
                        "generation": current.generation,
                        "source": name,
                        "reason": str(exc)[:500],
                        "status": "last-good-preserved",
                    },
                )
                continue
            added = sorted(set(discovery.tool_refs()) - set(current.tool_refs))
            removed = sorted(set(current.tool_refs) - set(discovery.tool_refs()))
            added_res = sorted(set(discovery.resource_refs()) - set(current.resource_refs))
            removed_res = sorted(set(current.resource_refs) - set(discovery.resource_refs()))
            if added or removed or added_res or removed_res:
                current.tool_refs = discovery.tool_refs()
                current.resource_refs = discovery.resource_refs()
                current.generation += 1
                current.version = discovery.version
                current.error = None
            current.last_refresh_at = time.time()
            current.last_refresh_status = "refreshed"
            report["refreshed"].append(
                {
                    "source": name,
                    "generation": current.generation,
                    "added": added,
                    "removed": removed,
                    "added_resources": added_res,
                    "removed_resources": removed_res,
                }
            )
            self._event(
                "acp.mcp.refreshed",
                {
                    "session_id": session_id,
                    "generation": current.generation,
                    "source": name,
                    "added": added,
                    "removed": removed,
                    "added_resources": added_res,
                    "removed_resources": removed_res,
                    "reason": "refreshed",
                },
            )
        return report

    # -- close / shutdown -------------------------------------------------
    async def close_session(self, session_id: str) -> dict[str, Any]:
        """Detach all session refs; idempotent; never drops global defs.

        Shared sources stay alive while other sessions use them; only the
        closing session's usage is withdrawn. Transport close is attempted
        through the recorded hook; an unsupported remote close is recorded
        honestly while local detach always completes.
        """
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            return {
                "session_id": session_id,
                "closed": True,
                "already": True,
                "detached_sources": [],
                "remote_close": {"supported": False, "note": "already closed"},
            }
        detached: list[str] = []
        for source in list(session.sources):
            users = self._usage.get(source, set())
            users.discard(session_id)
            if not users:
                self._usage.pop(source, None)
            detached.append(source)
        remote_close: dict[str, Any] = {"supported": False, "note": "no transport hook"}
        hook = session._on_close_transport
        if hook is not None:
            try:
                result = hook()
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, dict) and "remote_close_supported" in result:
                    remote_close = {
                        "supported": bool(result["remote_close_supported"]),
                        "detail": result,
                    }
                else:
                    remote_close = {"supported": True, "detail": result}
            except Exception as exc:
                remote_close = {"supported": False, "error": str(exc)[:500]}
        session._remote_close = remote_close
        session.closed = True
        self._event(
            "acp.mcp.detached",
            {
                "session_id": session_id,
                "generation": max([item.generation for item in session.sources.values()] or [0]),
                "source_refs": detached,
                "added": [],
                "removed": detached,
                "reason": "session closed",
                "remote_close": remote_close,
            },
        )
        return {
            "session_id": session_id,
            "closed": True,
            "already": False,
            "detached_sources": detached,
            "remote_close": remote_close,
        }

    async def close_all(self) -> dict[str, Any]:
        """Shutdown path: deterministically detach every open session."""
        report: dict[str, Any] = {"closed": [], "failed": []}
        for session_id in [sid for sid, item in self._sessions.items() if not item.closed]:
            try:
                await self.close_session(session_id)
                report["closed"].append(session_id)
            except Exception as exc:
                report["failed"].append({"session_id": session_id, "error": str(exc)[:300]})
        return report

    # -- inspection -------------------------------------------------------
    def get_session(self, session_id: str) -> AcpMcpSession | None:
        return self._sessions.get(session_id)

    def open_sessions(self) -> list[str]:
        return [sid for sid, item in self._sessions.items() if not item.closed]

    def source_users(self, source: str) -> list[str]:
        return sorted(self._usage.get(source, set()))

    def _require_open(self, session_id: str) -> AcpMcpSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown delivery session: {session_id!r}")
        if session.closed:
            raise ValueError(f"delivery session already closed: {session_id!r}")
        return session


_delivery: AcpMcpDelivery | None = None


def get_acp_mcp_delivery(
    *, emit: Callable[[str, dict[str, Any]], Any] | None = None
) -> AcpMcpDelivery:
    """Process-global delivery assembly (mirrors get_backend_registry).

    An explicit ``emit`` (re)binds the assembly's audit emitter so production
    edges are audited; tests construct ``AcpMcpDelivery`` directly instead.
    """
    global _delivery
    if _delivery is None:
        _delivery = AcpMcpDelivery(emit=emit)
    elif emit is not None:
        _delivery._emit = emit
    return _delivery


def reset_acp_mcp_delivery() -> None:
    """Test hook: drop the process-global assembly."""
    global _delivery
    _delivery = None


def default_mcp_sources() -> list[str]:
    """Currently connected MCP source names from canonical authorities.

    Union of the obase client registry and the route live-handle cache
    (both read-only here). Origins that cannot be read are skipped —
    attachment of each source still passes its own gate.
    """
    names: list[str] = []
    with contextlib.suppress(Exception):
        from veya.platform import load

        names.extend(load("obase").McpClientRegistry.list_clients())
    with contextlib.suppress(Exception):
        from server.routes import mcp as mcp_routes

        names.extend(mcp_routes._registered.keys())
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered
