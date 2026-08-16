"""User-held plan mode + high-impact tool approval.

Plan mode and approvals are *user* decisions (toggle / approve button), not
keyword routing. Default for tests/CLI: agent mode, no approval wait.
Web chat sends require_approval=true so write/execute tools pause for the user.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import uuid
from typing import Any

from server.events import fire_step

_mode: contextvars.ContextVar[str] = contextvars.ContextVar("veya_mode", default="agent")
_require_approval: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "veya_require_approval", default=False
)
_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("veya_uc_sid", default="")

PLAN_ALLOW = frozenset(
    {
        "fetch_url",
        "browser_run",
        "grep",
        "list_files",
        "read_file_ast",
        "long_read",
        "assemble_code_context",
        "create_plan",
        "plan_status",
        "update_todo",
        "hicode_status",
        "hicode_sessions",
        "hicode_tasks",
        "system_workspace_search",
        "system_workspace_reindex",
        "system_list_automations",
        "list_skills",
        "mcp_codebase",
        "mcp_stratum",
        "get_market_data_schema",
        "search_genesis_ledger",
    }
)

HIGH_IMPACT = frozenset(
    {
        "write_file",
        "run_in_sandbox",
        "hicode_run",
        "evolve_solution",
        "delegate_to_genesis",
        "system_spawn_swarm",
        "system_graph_cycle",
        "hicode_rollback",
        "hicode_stop",
        "produce_wechat_article",
    }
)

_POLICY = "user_control"
_APPROVAL_TIMEOUT_S = float(os.environ.get("VEYA_APPROVAL_TIMEOUT_S", "120") or 120)


class _Pending:
    def __init__(self, tool: str, args: dict[str, Any], sid: str) -> None:
        self.event = asyncio.Event()
        self.approved: bool | None = None
        self.tool = tool
        self.args = args
        self.sid = sid


_pending: dict[str, _Pending] = {}


def activate(
    *, mode: str = "agent", require_approval: bool = False, session_id: str = ""
) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    """Bind this request's control flags. Pair with deactivate() in finally."""
    m = "plan" if str(mode).strip().lower() == "plan" else "agent"
    return (
        _mode.set(m),
        _require_approval.set(bool(require_approval)),
        _session_id.set(session_id or ""),
    )


def deactivate(tokens: tuple[contextvars.Token, contextvars.Token, contextvars.Token]) -> None:
    _mode.reset(tokens[0])
    _require_approval.reset(tokens[1])
    _session_id.reset(tokens[2])


def current_mode() -> str:
    return _mode.get()


def require_approval() -> bool:
    return _require_approval.get()


def _safe_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (kwargs or {}).items():
        if str(k).startswith("_"):
            continue
        s = str(v)
        out[k] = s[:200] + ("…" if len(s) > 200 else "")
    return out


def resolve_approval(request_id: str, approved: bool) -> bool:
    pending = _pending.get(request_id)
    if pending is None:
        return False
    pending.approved = bool(approved)
    pending.event.set()
    return True


async def _wait_approval(tool: str, kwargs: dict[str, Any]) -> str | None:
    """Return a deny reason, or None to allow."""
    rid = uuid.uuid4().hex[:12]
    sid = _session_id.get()
    pending = _Pending(tool, _safe_args(kwargs), sid)
    _pending[rid] = pending
    fire_step(
        {
            "type": "permission_request",
            "request_id": rid,
            "tool_name": tool,
            "tool_args": pending.args,
            "session_id": sid,
            "reason": f"「{tool}」需要你批准后才会执行",
        }
    )
    try:
        await asyncio.wait_for(pending.event.wait(), timeout=_APPROVAL_TIMEOUT_S)
    except TimeoutError:
        _pending.pop(rid, None)
        return f"approval timed out for '{tool}' (waited {_APPROVAL_TIMEOUT_S:.0f}s)"
    _pending.pop(rid, None)
    if pending.approved:
        return None
    return f"user denied '{tool}'"


async def user_control_policy(name: str, kwargs: dict, source: str) -> str | None:
    """Plan mode = read-only allowlist; agent + require_approval = HITL on high-impact."""
    if current_mode() == "plan" and name not in PLAN_ALLOW:
        return (
            f"plan mode: '{name}' writes or executes. Stay read-only, draft a plan "
            f"(create_plan), and wait for the user to switch to agent mode."
        )
    if require_approval() and name in HIGH_IMPACT:
        return await _wait_approval(name, kwargs)
    return None


def install_user_control_policy(guard: Any | None = None) -> None:
    """Idempotent. Always enforce — this is the user's steering wheel."""
    from server.tool_guard import global_tool_guard

    guard = guard or global_tool_guard
    if guard.has_policy(_POLICY):
        return
    guard.register_policy(_POLICY, user_control_policy, enforce=True)
