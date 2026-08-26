"""User-held plan mode, freeze, and high-impact tool approval.

Plan mode, freeze, and approvals are *user* decisions (request flags), not
keyword routing or slash commands. Default for tests/CLI: agent mode, no freeze,
no approval wait. Web chat sends require_approval=true so write/execute tools
pause for the user. freeze_allow locks writes to one subdirectory for the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import uuid
from pathlib import Path
from typing import Any

from server.events import append_canonical_event, fire_step

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
        "read_hashline",
        "runtime_calls_query",
        "ast_grep_search",
        "code_blast_radius",
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
        "edit_hashline",
        "ast_grep_rewrite",
        "run_in_sandbox",
        "hicode_run",
        "evolve_solution",
        "delegate_to_genesis",
        "system_spawn_swarm",
        "system_create_automation",
        "system_remove_automation",
        "system_dispatch_omni_channel",
        "system_reload_skills",
        "system_workspace_reindex",
        "system_graph_cycle",
        "hicode_rollback",
        "hicode_stop",
        "produce_wechat_article",
    }
)

_POLICY = "user_control"
_APPROVAL_TIMEOUT_S = float(os.environ.get("VEYA_APPROVAL_TIMEOUT_S", "120") or 120)
_QUESTION_TIMEOUT_S = float(os.environ.get("VEYA_QUESTION_TIMEOUT_S", "300") or 300)


class _Pending:
    def __init__(self, tool: str, args: dict[str, Any], sid: str) -> None:
        self.event = asyncio.Event()
        self.approved: bool | None = None
        self.tool = tool
        self.args = args
        self.sid = sid


class _PendingQuestion:
    """OpenMausBot 提问卡片内化: bot 执行中向用户提问, 文字回答回填。"""

    def __init__(self, question: str, options: list[str], sid: str) -> None:
        self.event = asyncio.Event()
        self.answer: str | None = None
        self.question = question
        self.options = options
        self.sid = sid


_pending: dict[str, _Pending] = {}
_pending_questions: dict[str, _PendingQuestion] = {}

# Session-sticky freeze: writes locked to one subdirectory of the workspace root.
# Not a slash command — user sets it on the request / session (same class as plan mode).
_freeze: dict[str, tuple[str, str]] = {}  # session_id -> (root, allow_rel)
_FREEZE_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_hashline",
        "ast_grep_rewrite",
        "hicode_run",
        "hicode_rollback",
        "evolve_solution",
    }
)
_PATH_KEYS = ("filepath", "path", "workspace")


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


def _default_freeze_root() -> Path:
    raw = os.environ.get("VEYA_WORKSPACE") or str(Path.home() / ".veya" / "work")
    return Path(raw).expanduser().resolve()


def set_freeze(session_id: str, *, allow: str, root: str | None = None) -> None:
    """Lock writes to ``root/allow`` for this session. Empty allow = deny all freeze-write tools."""
    sid = (session_id or "").strip()
    if not sid:
        return
    base = Path(root).expanduser().resolve() if root else _default_freeze_root()
    allow_rel = (allow or "").strip().replace("\\", "/").lstrip("/")
    if allow_rel in {".", "./"}:
        allow_rel = ""
    _freeze[sid] = (str(base), allow_rel)


def clear_freeze(session_id: str) -> None:
    _freeze.pop((session_id or "").strip(), None)


def current_freeze() -> tuple[str, str] | None:
    sid = _session_id.get()
    if not sid:
        return None
    return _freeze.get(sid)


def _path_in_allow(root: str, allow_rel: str, raw_path: str) -> bool:
    if not allow_rel:
        return False
    base = Path(root).resolve()
    allow_dir = (base / allow_rel).resolve()
    try:
        allow_dir.relative_to(base)
    except ValueError:
        return False
    p = Path(raw_path).expanduser()
    target = p.resolve() if p.is_absolute() else (base / p).resolve()
    if target == allow_dir:
        return True
    try:
        target.relative_to(allow_dir)
        return True
    except ValueError:
        return False


def _freeze_target(name: str, kwargs: dict[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        val = (kwargs or {}).get(key)
        if val:
            return str(val)
    return None


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


def resolve_answer(request_id: str, answer: str) -> bool:
    """回填一次 bot 提问的回答 (POST /api/v1/agent/answer)。"""
    q = _pending_questions.get(request_id)
    if q is None:
        return False
    q.answer = answer
    q.event.set()
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
            "topic": "tool.approval_required",
            "request_id": rid,
            "tool_name": tool,
            "tool_args": pending.args,
            "session_id": sid,
            "reason": f"「{tool}」需要你批准后才会执行",
        }
    )
    with contextlib.suppress(Exception):
        append_canonical_event(
            "tool.approval_required",
            {"request_id": rid, "tool_name": tool, "tool_args": pending.args},
            actor="system",
            session_id=sid or None,
        )
    try:
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=_APPROVAL_TIMEOUT_S)
        except TimeoutError:
            return f"approval timed out for '{tool}' (waited {_APPROVAL_TIMEOUT_S:.0f}s)"
        if pending.approved:
            with contextlib.suppress(Exception):
                append_canonical_event(
                    "tool.approved",
                    {"request_id": rid, "tool_name": tool},
                    actor="user",
                    session_id=sid or None,
                )
            fire_step(
                {
                    "type": "tool.approved",
                    "tool_name": tool,
                    "request_id": rid,
                    "session_id": sid,
                }
            )
            return None
        with contextlib.suppress(Exception):
            append_canonical_event(
                "tool.denied",
                {"request_id": rid, "tool_name": tool},
                actor="user",
                session_id=sid or None,
            )
        fire_step(
            {
                "type": "tool.denied",
                "tool_name": tool,
                "request_id": rid,
                "session_id": sid,
            }
        )
        return f"user denied '{tool}'"
    finally:
        # Stop/断连取消等待时也必须释放请求，避免悬挂审批泄漏。
        _pending.pop(rid, None)


async def ask_question(question: str, options: list[str] | None = None) -> str:
    """OpenMausBot 提问卡片内化 (2026-08-16): bot 执行中向用户提问并等待文字回答。

    返回用户回答; 超时/无会话 → 明确提示 (模型自主决定放弃或继续), 不阻断。
    事件 agent_question 由前端渲染为提问卡片, 回答经 /api/v1/agent/answer 回填。
    """
    rid = uuid.uuid4().hex[:12]
    sid = _session_id.get()
    opts = [str(o)[:200] for o in (options or [])][:6]
    q = _PendingQuestion(str(question)[:2000], opts, sid)
    _pending_questions[rid] = q
    fire_step(
        {
            "type": "agent_question",
            "request_id": rid,
            "question": q.question,
            "options": opts,
            "session_id": sid,
        }
    )
    try:
        try:
            await asyncio.wait_for(q.event.wait(), timeout=_QUESTION_TIMEOUT_S)
        except TimeoutError:
            return (
                f"[user did not answer within {_QUESTION_TIMEOUT_S:.0f}s] "
                "用合理的默认假设继续, 不要重复提问。"
            )
        if q.answer is None or not str(q.answer).strip():
            return "[user dismissed the question] 用合理默认假设继续。"
        return str(q.answer)
    finally:
        _pending_questions.pop(rid, None)


async def user_control_policy(name: str, kwargs: dict, source: str) -> str | None:
    """Plan mode = read-only allowlist; freeze = writes locked to one subdir; HITL on high-impact."""
    if current_mode() == "plan" and name not in PLAN_ALLOW:
        return (
            f"plan mode: '{name}' writes or executes. Stay read-only, draft a plan "
            f"(create_plan), and wait for the user to switch to agent mode."
        )
    frozen = current_freeze()
    if frozen and name in _FREEZE_WRITE_TOOLS:
        root, allow_rel = frozen
        target = _freeze_target(name, kwargs)
        if not allow_rel:
            return (
                f"freeze: writes locked; no allow-dir set. "
                f"Cannot run '{name}' until the user unfreezes or names a subdirectory."
            )
        if target is None:
            return (
                f"freeze: '{name}' needs an explicit path under {allow_rel}/ "
                f"(session writes locked outside that directory)."
            )
        if not _path_in_allow(root, allow_rel, target):
            return (
                f"freeze: writes locked to '{allow_rel}/' under {root}; "
                f"'{target}' is outside the allow-dir."
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
