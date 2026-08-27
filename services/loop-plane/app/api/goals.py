"""loop-plane api.goals — State API（SPEC §4.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.domain.state.service import GoalService
from app.schemas import (
    ClaimBody,
    CreateGoalBody,
    GateCheckBody,
    SpendBody,
    TerminalCheckBody,
    TodoUpdateBody,
)

router = APIRouter(prefix="/v1/loop", tags=["state"])


def _service(request: Request) -> GoalService:
    svc: GoalService | None = getattr(request.app.state, "goal_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="state service 未初始化")
    return svc


def _trace(request: Request) -> str:
    return request.headers.get("X-Trace-Id", "")


@router.post("/goals", status_code=201)
async def create_goal(
    body: CreateGoalBody, request: Request, svc: GoalService = Depends(_service)
) -> dict[str, Any]:
    """创建 Goal + todos（≡ create_plan）。"""
    goal = svc.create_goal(
        body.objective,
        [t.model_dump() for t in body.todos],
        trace_id=body.trace_id or _trace(request),
    )
    return goal


@router.get("/goals")
async def list_goals(request: Request, svc: GoalService = Depends(_service)) -> dict[str, Any]:
    """Goal 列表（未完成优先）。"""
    goals = svc.list_goals()
    return {"goals": goals, "count": len(goals)}


@router.get("/goals/{goal_id}")
async def get_goal(goal_id: str, svc: GoalService = Depends(_service)) -> dict[str, Any]:
    try:
        return svc.get_goal(goal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/goals/{goal_id}/todos/{todo_id}")
async def update_todo(
    goal_id: str,
    todo_id: str,
    body: TodoUpdateBody,
    request: Request,
    svc: GoalService = Depends(_service),
) -> dict[str, Any]:
    """更新 todo status + evidence（≡ update_todo）。"""
    try:
        return svc.update_todo(
            goal_id, todo_id, body.status, body.evidence, trace_id=_trace(request)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/goals/{goal_id}/todos/{todo_id}/claim")
async def claim_todo(
    goal_id: str, todo_id: str, body: ClaimBody | None = None, svc: GoalService = Depends(_service)
) -> dict[str, Any]:
    """claim + lease（未过期再 claim → 409）。"""
    try:
        return svc.claim(goal_id, todo_id, lease_min=body.lease_min if body else 45)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/goals/{goal_id}/quota/should_run")
async def should_run(goal_id: str, svc: GoalService = Depends(_service)) -> dict[str, Any]:
    """该不该动（≡ quota_should_run）。"""
    try:
        return svc.should_run(goal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/goals/{goal_id}/quota/spend")
async def spend(
    goal_id: str, body: SpendBody, request: Request, svc: GoalService = Depends(_service)
) -> dict[str, Any]:
    """quota 记账（≡ quota_spend_slot）。"""
    try:
        return svc.spend(goal_id, body.todo_id, body.slots, trace_id=_trace(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/goals/{goal_id}/gates/check")
async def gate_check(
    goal_id: str, body: GateCheckBody, svc: GoalService = Depends(_service)
) -> dict[str, Any]:
    """scoped 决策检查（≡ gate_check）。"""
    try:
        return svc.gate_check(goal_id, body.gate_scope)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/goals/{goal_id}/terminal_check")
async def terminal_check(
    goal_id: str, body: TerminalCheckBody, svc: GoalService = Depends(_service)
) -> dict[str, Any]:
    """terminal 动作：只返回「需审批」建议（≡ terminal_gate_check）。"""
    return svc.terminal_check(body.action, goal_id)


__all__ = ["router"]
