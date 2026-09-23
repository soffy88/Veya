"""Veya Remote Machine Interface — governed MCP gateway over the host.

Transport, authentication, permission, session, tool adaptation and durable
execution control. Two execution families share one workspace contract:

* a **direct fast path** for metadata/file/git primitives and host commands
  (streaming stdout/stderr, no Hicode / MasterAgent / agent loop / LLM), and
* a durable **LLM coding path** (``hicode.execute``) over the same substrate.

See ``docs/remote/REMOTE_INTERFACE.md`` for the design record.
"""

from __future__ import annotations

from .audit import RemoteAudit
from .auth import RemoteAuth, RemoteAuthError, RemoteToken
from .direct_exec import DirectCommandResult, direct_sync_window_s, run_direct_command
from .execution import (
    DurableJobManager,
    ExecutionBlocked,
    ExecutionPhase,
    ExecutionStatus,
    ExecutionStore,
    ExecutionType,
)
from .mcp_server import RemoteMCPGateway, create_gateway
from .metrics import LatencyMetrics
from .models import (
    AuditRecord,
    EffectClass,
    JobState,
    RemoteCallResult,
    RemoteErrorCode,
    RemotePermissions,
    RemoteSession,
    ToolBinding,
)
from .session import RemoteSessionError, RemoteSessionManager
from .tool_adapter import RemoteToolAdapter, default_tool_adapter
from .workspace_binding import (
    RepoResolution,
    WorkspaceBinding,
    WorkspaceBindingError,
    resolve_repo_target,
    resolve_requested_workspace,
    verify_worktree_repo_identity,
)
from .workspace_policy import WorkspacePolicy, WorkspacePolicyError

__all__ = [
    "AuditRecord",
    "DirectCommandResult",
    "DurableJobManager",
    "EffectClass",
    "ExecutionBlocked",
    "ExecutionPhase",
    "ExecutionStatus",
    "ExecutionStore",
    "ExecutionType",
    "JobState",
    "LatencyMetrics",
    "RemoteAudit",
    "RemoteAuth",
    "RemoteAuthError",
    "RemoteCallResult",
    "RemoteErrorCode",
    "RemoteMCPGateway",
    "RemotePermissions",
    "RemoteSession",
    "RemoteSessionError",
    "RemoteSessionManager",
    "RemoteToken",
    "RemoteToolAdapter",
    "RepoResolution",
    "ToolBinding",
    "WorkspaceBinding",
    "WorkspaceBindingError",
    "WorkspacePolicy",
    "WorkspacePolicyError",
    "create_gateway",
    "default_tool_adapter",
    "direct_sync_window_s",
    "resolve_repo_target",
    "resolve_requested_workspace",
    "run_direct_command",
    "verify_worktree_repo_identity",
]
