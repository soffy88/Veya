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
from .events import VeyaEvent, VeyaEventType, create_veya_event
from .execution import (
    DurableJobManager,
    ExecutionBlocked,
    ExecutionPhase,
    ExecutionStatus,
    ExecutionStore,
    ExecutionType,
)
from .executor_health import (
    DEFAULT_EXECUTOR_PREFERENCE,
    ExecutorHealthRegistry,
    SubstitutionEvidence,
    classify_executor_failure,
    resolve_executor,
)
from .mcp_server import RemoteMCPGateway, create_gateway
from .metrics import LatencyMetrics
from .models import (
    AuditRecord,
    EffectClass,
    ExecutorFailureClass,
    ExecutorHealth,
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
    "DEFAULT_EXECUTOR_PREFERENCE",
    "AuditRecord",
    "DirectCommandResult",
    "DurableJobManager",
    "EffectClass",
    "ExecutionBlocked",
    "ExecutionPhase",
    "ExecutionStatus",
    "ExecutionStore",
    "ExecutionType",
    "ExecutorFailureClass",
    "ExecutorHealth",
    "ExecutorHealthRegistry",
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
    "SubstitutionEvidence",
    "ToolBinding",
    "VeyaEvent",
    "VeyaEventType",
    "WorkspaceBinding",
    "WorkspaceBindingError",
    "WorkspacePolicy",
    "WorkspacePolicyError",
    "classify_executor_failure",
    "create_gateway",
    "create_veya_event",
    "default_tool_adapter",
    "direct_sync_window_s",
    "resolve_executor",
    "resolve_repo_target",
    "resolve_requested_workspace",
    "run_direct_command",
    "verify_worktree_repo_identity",
]
