"""Veya Remote Machine Interface — governed MCP gateway over the existing runtime.

This package is a *transport, authentication, permission, session and tool
adaptation* layer. It never executes work itself: every tool call is mapped to
an existing canonical Veya tool and dispatched through ``MasterToolRegistry``
(``server.tool_registry``) / Hicode. No new shell, filesystem, git, artifact or
execution runtime is introduced.

See ``docs/remote/REMOTE_INTERFACE.md`` for the design record.
"""

from __future__ import annotations

from .audit import RemoteAudit
from .auth import RemoteAuth, RemoteAuthError, RemoteToken
from .mcp_server import RemoteMCPGateway, create_gateway
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
from .workspace_policy import WorkspacePolicy, WorkspacePolicyError

__all__ = [
    "AuditRecord",
    "EffectClass",
    "JobState",
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
    "ToolBinding",
    "WorkspacePolicy",
    "WorkspacePolicyError",
    "create_gateway",
    "default_tool_adapter",
]
