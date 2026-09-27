"""User-held plan mode, freeze, and high-impact tool approval.

Plan mode, freeze, and approvals are *user* decisions (request flags), not
keyword routing or slash commands. Default for tests/CLI: agent mode, no freeze,
no approval wait. Web chat defaults to autonomous execution; set
require_approval=true when an interactive approval gate is wanted. Sandbox-backed
execution remains enforced by the tool implementation. freeze_allow locks writes
to one subdirectory for the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import time
import uuid
from pathlib import Path
from typing import Any

from server.events import (
    append_canonical_event,
    append_observability_event,
    current_task_id,
    fire_step,
)
from server.governance_store import ApprovalRecord, SessionGovernanceState, governance_store

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
        "github_pr_fetch",
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
        "github_pr_post_review",
        "github_pr_create_draft",
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


_pending_questions: dict[str, _PendingQuestion] = {}

# We still use contextvars for synchronous getters when needed,
# but the durable state is stored in governance_store.
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


async def set_freeze(session_id: str, *, allow: str, root: str | None = None) -> None:
    """Lock writes to ``root/allow`` for this session. Empty allow = deny all freeze-write tools."""
    sid = (session_id or "").strip()
    if not sid:
        return
    base = Path(root).expanduser().resolve() if root else _default_freeze_root()
    allow_rel = (allow or "").strip().replace("\\", "/", -1).lstrip("/")
    if allow_rel in {".", "./"}:
        allow_rel = ""

    state = await governance_store.get_state(sid)
    if state:
        state.freeze_root = str(base)
        state.freeze_allow = allow_rel
        await governance_store.set_state(state)
    else:
        state = SessionGovernanceState(
            session_id=sid,
            mode="agent",
            require_approval=False,
            freeze_root=str(base),
            freeze_allow=allow_rel,
            revision=0,
            updated_at=time.time(),
        )
        await governance_store.set_state(state)


async def clear_freeze(session_id: str) -> None:
    sid = (session_id or "").strip()
    if not sid:
        return
    state = await governance_store.get_state(sid)
    if state:
        state.freeze_root = None
        state.freeze_allow = None
        await governance_store.set_state(state)


async def current_freeze() -> tuple[str, str] | None:
    sid = _session_id.get()
    if not sid:
        return None
    state = await governance_store.get_state(sid)
    if state and state.freeze_root is not None:
        return (state.freeze_root, state.freeze_allow or "")
    return None


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


async def resolve_approval(request_id: str, approved: bool) -> bool:
    await governance_store.resolve_approval(request_id, "approved" if approved else "denied")
    # if it's currently waiting in process memory, let it proceed
    pending = _pending_waiters.get(request_id)
    if pending:
        pending.set()
    return True


def resolve_answer(request_id: str, answer: str) -> bool:
    """回填一次 bot 提问的回答 (POST /api/v1/agent/answer)。"""
    q = _pending_questions.get(request_id)
    if q is None:
        return False
    q.answer = answer
    q.event.set()
    return True


_pending_waiters: dict[str, asyncio.Event] = {}


async def _wait_approval(tool: str, kwargs: dict[str, Any]) -> str | None:
    """Return a deny reason, or None to allow."""
    import hashlib
    import json

    sid = _session_id.get()
    args_json = json.dumps(_safe_args(kwargs), sort_keys=True)
    req_hash = hashlib.sha256(f"{sid}:{tool}:{args_json}".encode()).hexdigest()

    # Check if we already have an approval for this exact call
    existing = await governance_store.get_approval_by_hash(sid, req_hash)
    if existing and existing.status != "pending":
        if existing.status == "approved":
            return None
        return f"user denied '{tool}'"

    if existing and existing.status == "pending":
        rid = existing.request_id
    else:
        rid = uuid.uuid4().hex[:12]
        record = ApprovalRecord(
            request_id=rid,
            session_id=sid,
            tool=tool,
            tool_args=_safe_args(kwargs),
            status="pending",
            decision_reason=None,
            request_hash=req_hash,
            created_at=time.time(),
            updated_at=time.time(),
        )
        await governance_store.create_approval(record)

    event = asyncio.Event()
    _pending_waiters[rid] = event

    fire_step(
        {
            "type": "permission_request",
            "topic": "tool.approval_required",
            "request_id": rid,
            "tool_name": tool,
            "tool_args": record.tool_args,
            "session_id": sid,
            "reason": f"「{tool}」需要你批准后才会执行",
        }
    )
    with contextlib.suppress(Exception):
        append_canonical_event(
            "tool.approval_required",
            {"request_id": rid, "tool_name": tool, "tool_args": record.tool_args},
            actor="system",
            session_id=sid or None,
            task_id=current_task_id(),
        )
        append_observability_event(
            "approval.suspended",
            payload={
                "request_id": rid,
                "tool_name": tool,
                "reason": f"Approval required for '{tool}'",
            },
            actor="system",
            session_id=sid or None,
            task_id=current_task_id(),
            tool=tool,
            action=tool,
            status="suspended",
        )
    try:
        start_t = time.time()
        while time.time() - start_t < _APPROVAL_TIMEOUT_S:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=5.0)

            # check db
            current = await governance_store.get_approval(rid)
            if current and current.status != "pending":
                if current.status == "approved":
                    with contextlib.suppress(Exception):
                        append_canonical_event(
                            "tool.approved",
                            {"request_id": rid, "tool_name": tool},
                            actor="user",
                            session_id=sid or None,
                            task_id=current_task_id(),
                        )
                        append_observability_event(
                            "approval.resumed",
                            payload={
                                "request_id": rid,
                                "tool_name": tool,
                                "decision": "approved",
                            },
                            actor="user",
                            session_id=sid or None,
                            task_id=current_task_id(),
                            tool=tool,
                            action=tool,
                            status="resumed",
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
                else:
                    with contextlib.suppress(Exception):
                        append_canonical_event(
                            "tool.denied",
                            {"request_id": rid, "tool_name": tool},
                            actor="user",
                            session_id=sid or None,
                            task_id=current_task_id(),
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

        return f"approval timed out for '{tool}' (waited {_APPROVAL_TIMEOUT_S:.0f}s)"
    finally:
        _pending_waiters.pop(rid, None)


async def request_approval(tool: str, kwargs: dict[str, Any]) -> bool:
    """Request approval through the existing user-control pending store."""
    return await _wait_approval(tool, kwargs) is None


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

    async def _check_freeze() -> str | None:
        frozen = await current_freeze()
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
        return None

    freeze_err = await _check_freeze()
    if freeze_err:
        return freeze_err

    if require_approval() and name in HIGH_IMPACT:
        wait_err = await _wait_approval(name, kwargs)
        if wait_err:
            return wait_err

        # Revalidate freeze after wait block
        freeze_err_after = await _check_freeze()
        if freeze_err_after:
            return f"{freeze_err_after} (freeze applied during approval wait)"

    return None


def install_user_control_policy(guard: Any | None = None) -> None:
    """Idempotent. Always enforce — this is the user's steering wheel."""
    from server.tool_guard import global_tool_guard

    guard = guard or global_tool_guard
    if guard.has_policy(_POLICY):
        return
    guard.register_policy(_POLICY, user_control_policy, enforce=True)
