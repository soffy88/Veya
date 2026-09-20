"""Typed data model for the remote MCP interface.

Everything the gateway needs to make a decision is explicit here so that no
part of the code has to infer permission or side-effect class from a tool name
at runtime (a name-based policy is the exact anti-pattern the frozen
architecture forbids).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RemoteErrorCode(StrEnum):
    """Stable failure taxonomy returned to remote clients (spec §7)."""

    AUTH_DENIED = "AUTH_DENIED"
    WORKSPACE_DENIED = "WORKSPACE_DENIED"
    TOOL_DENIED = "TOOL_DENIED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    NOT_FOUND = "NOT_FOUND"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class EffectClass(StrEnum):
    """Side-effect class of a remote tool (spec §4)."""

    READ = "READ"
    WRITE = "WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


class JobState(StrEnum):
    """Lifecycle of a long-running remote execution (spec §8)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass
class RemotePermissions:
    """Explicit capability grant carried by a token and copied into a session.

    Defaults are fail-closed: read only. Every escalation is an explicit,
    audited decision (spec §3).
    """

    read: bool = True
    write: bool = False
    shell: bool = False
    git: bool = False
    network: bool = False
    destructive: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "read": self.read,
            "write": self.write,
            "shell": self.shell,
            "git": self.git,
            "network": self.network,
            "destructive": self.destructive,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RemotePermissions:
        data = data or {}
        return cls(
            read=bool(data.get("read", True)),
            write=bool(data.get("write", False)),
            shell=bool(data.get("shell", False)),
            git=bool(data.get("git", False)),
            network=bool(data.get("network", False)),
            destructive=bool(data.get("destructive", False)),
        )


@dataclass(frozen=True)
class ToolBinding:
    """A remote MCP tool bound to one canonical Veya tool.

    ``veya_tool`` must be a real registered tool name. ``needs_shell`` /
    ``needs_git`` refine the permission check. ``long_running`` marks tools
    that are dispatched through the job manager instead of inline.
    """

    name: str
    # None marks adapter-owned control tools (process.status / process.cancel)
    # that do not dispatch a canonical Veya tool themselves.
    veya_tool: str | None
    effect: EffectClass
    description: str
    schema: dict[str, Any]
    long_running: bool = False
    needs_shell: bool = False
    needs_git: bool = False
    destructive_when: tuple[str, ...] = ()


@dataclass
class RemoteSession:
    """A remote client bound to a principal, token and workspace (spec §3)."""

    session_id: str
    principal: str
    token_id: str
    workspaces: tuple[str, ...]
    active_workspace: str
    permissions: RemotePermissions
    created_at: float
    expires_at: float
    client_info: dict[str, Any] = field(default_factory=dict)
    # canonical workspace -> isolated worktree path, when created
    worktrees: dict[str, str] = field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "principal": self.principal,
            "workspaces": list(self.workspaces),
            "active_workspace": self.active_workspace,
            "permissions": self.permissions.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass
class RemoteCallResult:
    """Uniform response envelope (spec §7). Never reports false success."""

    ok: bool
    tool: str
    session_id: str | None = None
    workspace: str | None = None
    result: Any = None
    execution_id: str | None = None
    duration_ms: float | None = None
    error_code: RemoteErrorCode | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "tool": self.tool}
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.workspace is not None:
            payload["workspace"] = self.workspace
        if self.ok:
            payload["result"] = self.result if self.result is not None else {}
        if self.execution_id is not None:
            payload["execution_id"] = self.execution_id
        if self.duration_ms is not None:
            payload["duration_ms"] = round(self.duration_ms, 3)
        if not self.ok:
            payload["error_code"] = str(self.error_code or RemoteErrorCode.EXECUTION_FAILED)
            payload["message"] = self.message or ""
        return payload


@dataclass
class AuditRecord:
    """Immutable audit entry (spec §1 audit / §9 AUDIT_TRAIL)."""

    ts: float
    session_id: str | None
    principal: str | None
    token_id: str | None
    tool: str
    veya_tool: str | None
    effect: str | None
    workspace: str | None
    args: dict[str, Any]
    status: str
    error_code: str | None
    duration_ms: float | None
    execution_id: str | None
    remote_addr: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "session_id": self.session_id,
            "principal": self.principal,
            "token_id": self.token_id,
            "tool": self.tool,
            "veya_tool": self.veya_tool,
            "effect": self.effect,
            "workspace": self.workspace,
            "args": self.args,
            "status": self.status,
            "error_code": self.error_code,
            "duration_ms": round(self.duration_ms, 3) if self.duration_ms is not None else None,
            "execution_id": self.execution_id,
            "remote_addr": self.remote_addr,
        }
