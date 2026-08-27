"""veya/oservi/gateway — oservi_api_gateway（统一极简指令入口）。

外部只发 5 种指令，一切复杂编排在 daemon 内部:
    POST   /api/v1/3o/tasks                启动任务 {user_input, tools?, system_prompt?}
    GET    /api/v1/3o/tasks/{task_id}      查询状态
    POST   /api/v1/3o/tasks/{task_id}/pause   挂起（HITL）
    POST   /api/v1/3o/tasks/{task_id}/resume  恢复 {input? 人类输入注入}
    GET    /api/v1/3o/tasks/{task_id}/stream  SSE 事件流（agent_loop.* 实时轨迹）

挂载: server/app.py include_router(gateway_router)（新增前缀，不替换现有路由；
现有 veya start / CLI 保持旧路径，切换由调用方决定——统一入口已就位）。

engine 为模块级懒单例（挂载时创建 DaemonEngine；测试可用 gateway_engine(new) 替换）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from veya.oservi.daemon_engine import DaemonEngine

router = APIRouter(prefix="/api/v1/3o", tags=["3o-gateway"])

_engine: DaemonEngine | None = None


def gateway_engine(engine: DaemonEngine | None = None) -> DaemonEngine:
    """获取/替换网关引擎单例（测试注入用）。"""
    global _engine
    if engine is not None:
        _engine = engine
    if _engine is None:
        _engine = DaemonEngine()
    return _engine


class CreateTaskBody(BaseModel):
    user_input: str = Field(min_length=1)
    system_prompt: str = ""


class ResumeBody(BaseModel):
    input: str | None = None


@router.post("/tasks", status_code=201)
async def create_task(body: CreateTaskBody) -> dict[str, Any]:
    """启动任务：后台 daemon 驱动独立 AgentLoop（工具由 daemon 侧 register_tool 注入）。"""
    engine = gateway_engine()
    state = await engine.create_task(body.user_input)
    return {"task_id": state.task_id, "status": state.status.value}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    """查询任务状态（pending/running/paused/completed/failed + 结果）。"""
    try:
        return await gateway_engine().status(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str) -> dict[str, Any]:
    """挂起任务（HITL：等待人类输入）。"""
    try:
        return await gateway_engine().pause(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, body: ResumeBody | None = None) -> dict[str, Any]:
    """恢复任务；body.input 非空时先注入人类输入。"""
    try:
        return await gateway_engine().resume(task_id, input_text=(body.input if body else None))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request) -> StreamingResponse:
    """SSE 事件流：agent_loop.start/round/tool_result/done 实时轨迹。"""
    try:
        engine = gateway_engine()
        await engine.status(task_id)  # 404 校验
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _events():
        async for event in engine.stream(task_id):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["gateway_engine", "router"]
