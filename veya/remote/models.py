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
    EMPTY_MODEL_RESPONSE = "EMPTY_MODEL_RESPONSE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    NOT_FOUND = "NOT_FOUND"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    REQUIRED_CAPABILITY_UNAVAILABLE = "REQUIRED_CAPABILITY_UNAVAILABLE"
    INVALID_APPROVAL = "INVALID_APPROVAL"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"


class ExecutorFailureClass(StrEnum):
    """Canonical failure taxonomy (P6)."""

    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    AUTH_FAILURE = "AUTH_FAILURE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    MODEL_FAILURE = "MODEL_FAILURE"
    WORKER_TIMEOUT = "WORKER_TIMEOUT"
    WORKER_CRASH = "WORKER_CRASH"
    WORKER_CANCELLED = "WORKER_CANCELLED"
    WORKTREE_FAILURE = "WORKTREE_FAILURE"
    SUBMODULE_FAILURE = "SUBMODULE_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    PROCESS_REAP_FAILURE = "PROCESS_REAP_FAILURE"

    # L1 executor-plane classes (spec P4.5). These answer "could this executor
    # be chosen, and did choosing it hold up" — deliberately not provider
    # concerns. A provider timeout or a provider outage belongs to L2, where
    # the provider runtime actually lives; reporting it as an executor fault
    # would send the operator looking at the wrong layer.
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"
    EXECUTOR_DISABLED = "EXECUTOR_DISABLED"
    EXECUTOR_CAPABILITY_MISMATCH = "EXECUTOR_CAPABILITY_MISMATCH"
    EXECUTOR_HEALTH_FAILURE = "EXECUTOR_HEALTH_FAILURE"
    EXECUTOR_CRASH = "EXECUTOR_CRASH"


class ExecutorHealth(StrEnum):
    """Runtime health classification states (P5)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


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


class RiskClass(StrEnum):
    """Human gate risk classification (spec §33)."""

    P1_PRIVILEGED_HOST = "P1_PRIVILEGED_HOST"
    P2_ROOT_MUTATION = "P2_ROOT_MUTATION"
    P3_CRITICAL_HOST = "P3_CRITICAL_HOST"


@dataclass
class ApprovalRecord:
    """Server-issued single-use approval record (spec §30-§32)."""

    approval_id: str
    principal: str
    capability_id: str
    normalized_operation: str
    operation_hash: str
    cwd: str
    workspace: str
    risk_class: RiskClass
    created_at: float
    expires_at: float
    used_at: float | None = None
    decision: str = "approved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "principal": self.principal,
            "capability_id": self.capability_id,
            "normalized_operation": self.normalized_operation,
            "operation_hash": self.operation_hash,
            "cwd": self.cwd,
            "workspace": self.workspace,
            "risk_class": str(self.risk_class),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "used_at": self.used_at,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRecord:
        return cls(
            approval_id=str(data["approval_id"]),
            principal=str(data["principal"]),
            capability_id=str(data["capability_id"]),
            normalized_operation=str(data["normalized_operation"]),
            operation_hash=str(data["operation_hash"]),
            cwd=str(data["cwd"]),
            workspace=str(data["workspace"]),
            risk_class=RiskClass(data["risk_class"]),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            used_at=float(data["used_at"]) if data.get("used_at") is not None else None,
            decision=str(data.get("decision", "approved")),
        )


@dataclass
class ManagedUserService:
    """Canonical registry entry for a managed user-level systemd service (spec §10)."""

    unit: str
    unit_path: str
    owner_project: str
    discovered_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "unit_path": self.unit_path,
            "owner_project": self.owner_project,
            "discovered_at": self.discovered_at,
        }


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
    service_control: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "read": self.read,
            "write": self.write,
            "shell": self.shell,
            "git": self.git,
            "network": self.network,
            "destructive": self.destructive,
            "service_control": self.service_control,
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
            service_control=bool(data.get("service_control", False)),
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
    # Explicit per-call workspace request (P0-A/J). When set, status/cancel must
    # not observe an execution whose workspace identity differs.
    explicit_workspace: str | None = None

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
        if self.result is not None:
            payload["result"] = self.result
        elif self.ok:
            payload["result"] = {}
        if self.execution_id is not None:
            payload["execution_id"] = self.execution_id
        # Durable dispatch handles are intentionally available at the MCP
        # envelope level, not only inside the human-readable result object.
        if isinstance(self.result, dict):
            for key in ("dispatch_id", "goal_run_id", "goal_task_id", "executor_id", "status"):
                if key in self.result:
                    payload[key] = self.result[key]
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
