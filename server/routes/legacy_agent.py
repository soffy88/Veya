"""Legacy L4 gateway 协议兼容路由 — /api/v1/agent/* (旧前端/Caddy 入口)。

旧网关 (veya.server.app) 的协议被域名前端依赖:
  POST /api/v1/agent/run     one-shot agent run (text 新契约 / task 旧契约)
  POST /api/v1/agent/stream   SSE 流式 run (on_step → data frames)

本模块在新后端 (server.app) 上以**同路径同 schema** 注册,
内部委托新主脑 master_coordinator — 旧入口全部指向新代码。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.events import _to_envelope

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
    freeze_allow: str | None = Field(
        None,
        description="Session write freeze: relative subdir still writable; empty string clears freeze",
    )


class LegacyAgentRunResponse(BaseModel):
    session_id: str
    status: str
    result: Any = None
    cost_usd: float = 0.0
    user_ref: str | None = None
    plan: dict[str, Any] | None = None


def _new_session_id() -> str:
    from server.session_identity import new_session_id

    return new_session_id()


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
            freeze_allow=req.freeze_allow,
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
        freeze_allow=req.freeze_allow,
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
                yield f"data: {json.dumps(_to_envelope(evt), ensure_ascii=False)}\n\n"

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
            freeze_allow=req.freeze_allow,
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


@router.get("/api/v1/agent/stream_status")
async def legacy_agent_stream_status(session_id: str) -> dict:
    """会话对应的后台主脑任务是否仍在跑 (前端断流重连前先探活)。

    SSE 推流与后台任务解耦 (见 server/chat_stream.py) — 任务完成/取消后
    再重连 GET /stream/{sid} 只会拿到一个空队列, 永远等不到新事件, 白白
    挂起。前端靠这个先判断"值不值得重连", 不值得就直接发新消息。
    """
    from server.coordinator_master import _active_streams

    task = _active_streams.get(session_id)
    return {"active": task is not None and not task.done()}


class LegacyAgentSteerRequest(BaseModel):
    session_id: str
    # ``text`` is canonical; action/instruction preserve the older gateway
    # payload accepted by existing clients.
    text: str | None = Field(default=None, min_length=1)
    action: str = ""
    instruction: str = ""


@router.post("/api/v1/agent/steer")
async def legacy_agent_steer(
    req: LegacyAgentSteerRequest,
    user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """运行中注入一条 follow-up 消息, 不用等当前轮次跑完 (Harness steering)。

    效果出现在该 session 已经开着的那条 SSE 流里——本端点本身不开新流, 只是把
    消息排进正在跑的那一轮 (`MasterCoordinator.enqueue_steering_message`)。
    """
    from server.coordinator_master import master_coordinator

    owner = master_coordinator._history_owners.get(req.session_id)
    if owner is not None and owner != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作该会话")
    text = (req.text or req.instruction).strip()
    if not text:
        return {"status": "ok", "queued": False, "reason": "missing_instruction"}
    queued = master_coordinator.enqueue_steering_message(req.session_id, text)
    if not queued:
        return {
            "status": "ok",
            "queued": False,
            "reason": "no_active_turn_or_queue_full",
            "hint": "该会话当前没有正在跑的轮次 (或排队已满), 请改用 /api/v1/agent/run 或 /stream",
        }
    return {"status": "ok", "queued": True}


class AgentApprovalRequest(BaseModel):
    request_id: str
    approved: bool


@router.post("/api/v1/agent/approval")
async def agent_approval(req: AgentApprovalRequest) -> dict:
    """Web 聊天: 批准或拒绝一次高影响工具。"""
    from server.user_control import resolve_approval

    ok = resolve_approval(req.request_id, req.approved)
    return {"ok": ok, "request_id": req.request_id, "approved": req.approved}


class AgentAnswerRequest(BaseModel):
    request_id: str
    answer: str


@router.post("/api/v1/agent/answer")
async def agent_answer(req: AgentAnswerRequest) -> dict:
    """Web 聊天: 回答 bot 执行中的提问 (提问卡片回填, OpenMausBot 内化)。"""
    from server.user_control import resolve_answer

    ok = resolve_answer(req.request_id, req.answer)
    return {"ok": ok, "request_id": req.request_id}


@router.get("/api/v1/agent/sessions")
async def list_user_sessions(
    user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """列出当前用户的会话 (多端同步: 手机建的会话, 电脑端可见可续)。

    数据源 = veya.history_store (SqliteHistoryStore) —— 主脑唯一主链
    (MasterAgent ReAct) 权威写入的持久层 (见 coordinator_master._persist_history);
    按 user_id 分区, 天然只读到本人会话。
    """
    from veya.history_store import default_history_store

    sessions = await default_history_store().list_sessions(user_id=user["user_id"], limit=50)
    return {"sessions": sessions, "user_id": user["user_id"]}


@router.get("/api/v1/agent/history/{sid}")
async def get_session_history(
    sid: str,
    user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """返回当前用户的指定会话消息 (跨端恢复历史; 仅本人数据)。

    history_store 按 (user_id, sid) 联合主键存取, 查询本身已按 user_id 分区
    (不存在跨用户 owner 校验的必要), 且只存非 system 消息, 无需额外过滤。
    """
    from veya.history_store import default_history_store

    messages = await default_history_store().load(sid, user_id=user["user_id"])
    return {"session_id": sid, "messages": messages}


@router.post("/api/v1/agent/sessions/{sid}/attach")
async def attach_session(
    sid: str,
    user: dict = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §5: 接管一个已存在会话 —— 不新建、

    不改动会话本身, 只是把「当前完整历史 + 是否有轮次正在跑」一次性交给客户端,
    让它决定要不要接着开 GET /stream/{sid} 看实时事件 (`active=true` 时才值得连,
    跟 `/api/v1/agent/stream_status` 同一份 `_active_streams` 判断逻辑,
    这里只是把它跟历史拼在一起, 省一次往返)。复用 `get_session_history` 完全
    一样的鉴权/数据源 (history_store 按 (user_id, sid) 分区), 不引入新状态。
    """
    from server.coordinator_master import _active_streams
    from veya.history_store import default_history_store

    messages = await default_history_store().load(sid, user_id=user["user_id"])
    task = _active_streams.get(sid)
    return {
        "session_id": sid,
        "messages": messages,
        "active": task is not None and not task.done(),
    }
