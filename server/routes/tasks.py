"""server.routes.tasks — P1-03 Web Task Center API (docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §6)。

端点：
    GET  /api/v1/tasks                      列出 (支持 workspace/status/session 过滤)
    POST /api/v1/tasks                      显式创建一个任务记录 (低侵入接入点)
    GET  /api/v1/tasks/{task_id}            单个任务详情
    POST /api/v1/tasks/{task_id}/cancel     取消任务 (状态投影: cancelled)
    GET  /api/v1/tasks/{task_id}/events     任务事件列表 (Canonical Event Model)

设计约束 (A-04): Task 状态是 Projection 不是控制器。本路由只读写 task_store 的
记录, 不做任何执行决策; 任务如何被真实执行链路创建/更新, 由调用方 (chat 热路径
接入点) 显式调用 task_store, 不属于本路由范围。
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.task_store import TaskStore, task_store

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _get_store() -> TaskStore:
    return task_store


class TaskCreateRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    title: str = Field("", max_length=200)
    objective: str = Field(..., min_length=1)
    workspace_id: str | None = None
    task_id: str | None = None
    acceptance: list[dict[str, Any]] = Field(default_factory=list)


class TaskResumeRequest(BaseModel):
    text: str | None = Field(None, min_length=1)
    max_rounds: int | None = Field(None, ge=1, le=100)


@router.get("")
async def list_tasks(
    workspace: str | None = None,
    status: str | None = None,
    session: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """列出任务。过滤: workspace / status / session。"""
    store = _get_store()
    tasks = store.list(
        workspace_id=workspace,
        status=status,
        session_id=session,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    return {"tasks": [t.to_dict() for t in tasks], "count": len(tasks)}


@router.post("")
async def create_task(req: TaskCreateRequest) -> dict[str, Any]:
    """显式创建任务记录 (供前端/调用方手动登记; 真实执行链路的自动登记见
    coordinator_master 接入点)。"""
    store = _get_store()
    task = store.create(
        session_id=req.session_id,
        title=req.title or req.objective[:40],
        objective=req.objective,
        workspace_id=req.workspace_id,
        task_id=req.task_id,
        acceptance=getattr(req, "acceptance", []),
    )
    return {"status": "created", "task": task.to_dict()}


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    store = _get_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return {"task": task.to_dict()}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, Any]:
    """取消任务。状态投影: → cancelled。取消传播到真实执行由调用方负责
    (UI → Task Runtime → Master Turn → Active Tool)。"""
    store = _get_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    runtime: dict[str, Any] = {"cancelled": ["none"]}
    with contextlib.suppress(Exception):
        from server.coordinator_master import cancel_session

        runtime = await cancel_session(task.session_id)
    task = store.cancel(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return {"status": task.status, "task": task.to_dict(), "runtime": runtime}


@router.post("/{task_id}/resume")
async def resume_task(
    task_id: str,
    req: TaskResumeRequest | None = None,
) -> dict[str, Any]:
    """Resume the task's durable session through the single MasterAgent path."""
    store = _get_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    from server.events import append_canonical_event

    append_canonical_event(
        "resume.started",
        {"reason": "task_center_resume"},
        actor="user",
        session_id=task.session_id,
        trace_id=task.trace_id,
        task_id=task_id,
    )
    if not req or not req.text:
        append_canonical_event(
            "resume.completed",
            {"status": "ready", "checkpoint_id": task.latest_checkpoint_id},
            actor="system",
            session_id=task.session_id,
            trace_id=task.trace_id,
            task_id=task_id,
        )
        return {
            "task_id": task_id,
            "session_id": task.session_id,
            "status": "ready",
            "resumed": True,
        }
    from server.coordinator_master import master_coordinator

    try:
        result = await master_coordinator.chat_stream(
            req.text,
            session_id=task.session_id,
            task_id=task_id,
            max_rounds=req.max_rounds,
        )
    except Exception as exc:
        append_canonical_event(
            "resume.failed",
            {"error_type": type(exc).__name__},
            actor="system",
            session_id=task.session_id,
            trace_id=task.trace_id,
            task_id=task_id,
        )
        raise
    append_canonical_event(
        "resume.completed",
        {"status": result.get("status", "completed")},
        actor="system",
        session_id=task.session_id,
        trace_id=task.trace_id,
        task_id=task_id,
    )
    return {"task_id": task_id, "session_id": task.session_id, "resumed": True, "result": result}


@router.get("/{task_id}/events")
async def task_events(task_id: str) -> dict[str, Any]:
    """返回任务生命周期事件，按 EventStore 写入顺序排列。"""
    store = _get_store()
    if store.get(task_id) is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return {"task_id": task_id, "events": store.events(task_id)}
