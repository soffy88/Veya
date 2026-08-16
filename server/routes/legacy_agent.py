"""Legacy L4 gateway 协议兼容路由 — /api/v1/agent/* (旧前端/Caddy 入口)。

旧网关 (veya.server.app) 的协议被域名前端依赖:
  POST /api/v1/agent/run     one-shot agent run (text 新契约 / task 旧契约)
  POST /api/v1/agent/stream   SSE 流式 run (on_step → data frames)

本模块在新后端 (server.app) 上以**同路径同 schema** 注册,
内部委托新主脑 master_coordinator — 旧入口全部指向新代码。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server import auth as auth_mod

router = APIRouter(tags=["legacy-agent"])


@router.get("/api/v1/engines")
async def list_engines() -> dict:
    """本机可用的执行引擎 (master 恒可用; 其余需对应 CLI 已安装)。

    前端据此禁用不可用引擎, 避免选到后 520/500。
    """
    from server.engine_runner import available_engines

    return {"engines": available_engines()}


class LegacyAgentRunRequest(BaseModel):
    task: str | None = Field(None, description="Task description (legacy contract)")
    text: str | None = Field(None, description="Chat prompt (new Agent OS contract)")
    images: list[str] = Field(default_factory=list, description="附件图片 (base64 data URI 列表)")
    provider: str | None = None
    model: str | None = None
    student_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["run", "dry_run"] = "run"
    engine: str = Field("master", description="执行引擎: master|claude|codex|pi")
    agent_mode: Literal["agent", "plan"] = Field(
        "agent", description="agent=可写可执行; plan=只读规划"
    )
    require_approval: bool = Field(False, description="Web 聊天设 true: 高影响工具等用户批准")


class LegacyAgentRunResponse(BaseModel):
    session_id: str
    status: str
    result: Any = None
    cost_usd: float = 0.0
    user_ref: str | None = None
    plan: dict[str, Any] | None = None


def _new_session_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


@router.post("/api/v1/agent/run", response_model=LegacyAgentRunResponse)
async def legacy_agent_run(
    req: LegacyAgentRunRequest,
    user: dict = Depends(auth_mod.get_current_user),
) -> LegacyAgentRunResponse:
    """旧协议入口 → 新主脑 (同进程委托, 无网络跳转)。

    此前没有鉴权依赖: 带 token 也不会被解析, 请求一律落进共享的 anonymous
    历史/记忆桶。与已有鉴权的 /stream 端点保持一致的宽松策略 (get_current_user
    未登录回落 anonymous, 不强制 401) —— 不强制登录, 只是不再把已登录用户的
    请求错误地当匿名处理 (同步顺带修好按 user_id 隔离历史/记忆的前提)。
    """
    from server.coordinator_master import master_coordinator

    _ = user  # Depends 已把 auth.current_user() contextvar 设好, 下游按此隔离

    if req.engine != "master":
        from server.engine_runner import run_engine

        res = await run_engine(
            req.engine, req.text or req.task or "", model=req.model, timeout_s=600.0
        )
        return LegacyAgentRunResponse(
            session_id=req.session_id or _new_session_id(),
            status="success" if res["ok"] else "failed",
            result=res.get("output") or res.get("error") or "",
            cost_usd=0.0,
        )

    if req.text is not None:
        from server.coordinator_master import DEFAULT_MAX_ROUNDS

        result = await master_coordinator.chat_stream(
            req.text,
            session_id=req.session_id or None,
            max_rounds=DEFAULT_MAX_ROUNDS,
            config=req.config or None,
            provider=req.provider,
            model=req.model,
            mode=req.agent_mode,
            require_approval=req.require_approval,
        )
        return LegacyAgentRunResponse(
            session_id=result.get("session_id") or req.session_id or _new_session_id(),
            status=result.get("status", "failed"),
            result=result.get("final_answer") or result.get("error", ""),
            cost_usd=result.get("cost_usd", 0.0),
        )

    session_id = req.session_id or _new_session_id()
    user_ref = None
    raw_uid = req.student_id or req.user_id
    if raw_uid:
        try:
            from veya.im.pseudo import anonymize_user_id

            user_ref = anonymize_user_id(raw_uid)
        except Exception:
            user_ref = f"u_{abs(hash(raw_uid)) % 10**8:08d}"
    if req.mode == "dry_run":
        return LegacyAgentRunResponse(
            session_id=session_id,
            status="dry_run",
            plan={"name": "Agent OS master brain", "skeleton": "master_agent"},
            user_ref=user_ref,
        )

    from server.coordinator_master import DEFAULT_MAX_ROUNDS

    result = await master_coordinator.chat_stream(
        req.task or "",
        session_id=session_id,
        max_rounds=DEFAULT_MAX_ROUNDS,
        config=req.config or None,
        provider=req.provider,
        model=req.model,
        mode=req.agent_mode,
        require_approval=req.require_approval,
    )
    return LegacyAgentRunResponse(
        session_id=result.get("session_id") or session_id,
        status=result.get("status", "failed"),
        result=result.get("final_answer") or result.get("error", ""),
        cost_usd=result.get("cost_usd", 0.0),
    )


@router.post("/api/v1/agent/stream")
async def legacy_agent_stream(
    req: LegacyAgentRunRequest,
    request: Request,
    user: dict = Depends(auth_mod.get_current_user),
) -> StreamingResponse:
    """旧协议 SSE 流 → 新主脑事件流 (text_delta / tool_call / master_done)。"""
    from server.chat_stream import new_agent_stream_events

    if req.engine != "master":
        from server.engine_runner import stream_engine

        async def _engine_events():
            async for evt in stream_engine(
                req.engine, req.text or req.task or "", model=req.model, timeout_s=600.0
            ):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            _engine_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    prompt = req.text if req.text is not None else (req.task or "")
    session_id = req.session_id or _new_session_id()
    return StreamingResponse(
        new_agent_stream_events(
            prompt,
            session_id,
            config=req.config or None,
            provider=req.provider,
            model=req.model,
            user=user,
            images=req.images or None,
            request=request,
            mode=req.agent_mode,
            require_approval=req.require_approval,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


class LegacyAgentStopRequest(BaseModel):
    session_id: str | None = Field(None, description="SSE 会话 id (stream 请求的 session_id)")


@router.post("/api/v1/agent/stop")
async def legacy_agent_stop(req: LegacyAgentStopRequest) -> dict:
    """真正停止一个进行中的流式会话 (前端 Stop 按钮)。

    - 正在运行的 Hicode 任务 → serve POST /cancel 真正中断 turn (不只断 SSE);
    - 排队中的任务 → 直接取消;
    - 主脑 chat_task → cancel (SSE 结束)。
    """
    from server.coordinator_master import cancel_session

    if not req.session_id:
        return {"cancelled": "none", "error": "session_id required"}
    return await cancel_session(req.session_id)


class AgentApprovalRequest(BaseModel):
    request_id: str
    approved: bool


@router.post("/api/v1/agent/approval")
async def agent_approval(req: AgentApprovalRequest) -> dict:
    """Web 聊天: 批准或拒绝一次高影响工具。"""
    from server.user_control import resolve_approval

    ok = resolve_approval(req.request_id, req.approved)
    return {"ok": ok, "request_id": req.request_id, "approved": req.approved}


@router.get("/api/v1/agent/sessions")
async def list_user_sessions(
    user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """列出当前用户的会话 (多端同步: 手机建的会话, 电脑端可见可续)。

    2026-08-16 修复: 数据源从 veya.history_store (SqliteHistoryStore) 切到
    session_tree.db (SessionTreeMgr) —— 生产实际路径 (VEYA_AGENT_LOOP=strict)
    的对话只写进后者, history_store 从未被新主链写入过 (旧路径专用), 导致
    这个"多端同步"接口此前对所有新对话都读到空列表。
    """
    from server.agent_loop_bridge import _session_kv
    from veya.omodul.session_tree import SessionTreeMgr

    def _load() -> list[dict[str, Any]]:
        # kv 连接必须和使用它的代码在同一线程创建 (sqlite3 限制) —— 不能把
        # 构造挪到 to_thread 外面, 那样连接在事件循环线程创建、却在线程池
        # 线程里被访问, sqlite3 会直接报 ProgrammingError。
        tree = SessionTreeMgr(kv=_session_kv())
        return tree.list_sessions(owner=user["user_id"], limit=50)

    sessions = await asyncio.to_thread(_load)
    return {"sessions": sessions, "user_id": user["user_id"]}


@router.get("/api/v1/agent/history/{sid}")
async def get_session_history(
    sid: str,
    user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """返回当前用户的指定会话消息 (跨端恢复历史; 仅本人数据)。

    2026-08-16: 同上, 切到 session_tree.db；owner 记录且不匹配当前用户 →
    直接返回空 (不 404, 与 /sessions 列表"看不到即视为不存在"的语义一致，
    调用方是内部前端逻辑不是需要精确错误码的公开 API)。owner=None (旧数据,
    早于归属校验修复) 视为无主放行, 与仓库里其它归属校验点口径一致。
    """
    from server.agent_loop_bridge import _session_kv
    from veya.omodul.session_tree import SessionTreeMgr

    def _load() -> list[dict[str, Any]]:
        tree = SessionTreeMgr(kv=_session_kv())
        owner = tree.owner_of(sid)
        if owner is not None and owner != user["user_id"]:
            return []
        return [m for m in tree.messages(sid) if m.get("role") != "system"]

    messages = await asyncio.to_thread(_load)
    return {"session_id": sid, "messages": messages}
