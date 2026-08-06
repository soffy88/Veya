"""Legacy L4 gateway 协议兼容路由 — /api/v1/agent/* (旧前端/Caddy 入口)。

旧网关 (veya.server.app) 的协议被域名前端依赖:
  POST /api/v1/agent/run     one-shot agent run (text 新契约 / task 旧契约)
  POST /api/v1/agent/stream   SSE 流式 run (on_step → data frames)

本模块在新后端 (server.app) 上以**同路径同 schema** 注册,
内部委托新主脑 master_coordinator — 旧入口全部指向新代码。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(tags=["legacy-agent"])


class LegacyAgentRunRequest(BaseModel):
    task: str | None = Field(None, description="Task description (legacy contract)")
    text: str | None = Field(None, description="Chat prompt (new Agent OS contract)")
    provider: str | None = None
    model: str | None = None
    student_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["run", "dry_run"] = "run"
    engine: str = Field("master", description="执行引擎: master|claude|codex|pi")


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
async def legacy_agent_run(req: LegacyAgentRunRequest) -> LegacyAgentRunResponse:
    """旧协议入口 → 新主脑 (同进程委托, 无网络跳转)。"""
    from server.coordinator_master import master_coordinator

    if req.engine != "master":
        from server.engine_runner import run_engine

        res = await run_engine(req.engine, req.text or req.task or "",
                               model=req.model, timeout_s=600.0)
        return LegacyAgentRunResponse(
            session_id=req.session_id or _new_session_id(),
            status="success" if res["ok"] else "failed",
            result=res.get("output") or res.get("error") or "",
            cost_usd=0.0,
        )

    if req.text is not None:
        result = await master_coordinator.chat_stream(
            req.text,
            session_id=req.session_id or None,
            max_rounds=8,
            config=req.config or None,
            provider=req.provider,
            model=req.model,
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
            user_ref = f"u_{abs(hash(raw_uid)) % 10 ** 8:08d}"
    if req.mode == "dry_run":
        return LegacyAgentRunResponse(
            session_id=session_id,
            status="dry_run",
            plan={"name": "Agent OS master brain", "skeleton": "master_agent"},
            user_ref=user_ref,
        )

    result = await master_coordinator.chat_stream(
        req.task or "",
        session_id=session_id,
        max_rounds=8,
        config=req.config or None,
        provider=req.provider,
        model=req.model,
    )
    return LegacyAgentRunResponse(
        session_id=result.get("session_id") or session_id,
        status=result.get("status", "failed"),
        result=result.get("final_answer") or result.get("error", ""),
        cost_usd=result.get("cost_usd", 0.0),
    )


@router.post("/api/v1/agent/stream")
async def legacy_agent_stream(req: LegacyAgentRunRequest, request: Request) -> StreamingResponse:
    """旧协议 SSE 流 → 新主脑事件流 (text_delta / tool_call / master_done)。"""
    from server.chat_stream import new_agent_stream_events

    if req.engine != "master":
        from fastapi.responses import StreamingResponse
        from server.engine_runner import stream_engine

        async def _engine_events():
            async for evt in stream_engine(req.engine, req.text or req.task or "",
                                           model=req.model, timeout_s=600.0):
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
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
