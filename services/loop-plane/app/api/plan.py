"""loop-plane api.plan — Causal API（SPEC §4.3）：/plan/goal、/plan/diagnose。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.domain.causal.service import audit_and_report, diagnose, plan_for_goal
from app.schemas import DiagnoseBody, PlanGoalBody

router = APIRouter(prefix="/v1/loop/plan", tags=["causal"])


def _trace(request: Request) -> str:
    return request.headers.get("X-Trace-Id", "")


@router.post("/goal")
async def plan_goal(body: PlanGoalBody, request: Request) -> dict[str, Any]:
    """目标规划 → GoalPlanReport（默认 execute=false）。"""
    trace_id = _trace(request)
    try:
        report = plan_for_goal(
            body.goal,
            body.criteria,
            execute=body.execute,
            trace_id=trace_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 审计（plan 节点）
    from app.deps import get_audit

    audit_and_report(
        get_audit(),
        phase="plan",
        trace_id=report["trace_id"],
        decision_made={"goal": body.goal, "actions": len(report["ranked_actions"])},
        context_snapshot={"criteria": body.criteria},
    )
    return report


@router.post("/diagnose")
async def plan_diagnose(body: DiagnoseBody, request: Request) -> dict[str, Any]:
    """故障诊断 → DiagnosisReport。"""
    trace_id = _trace(request)
    try:
        report = diagnose(body.symptom, body.context, trace_id=trace_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    from app.deps import get_audit

    audit_and_report(
        get_audit(),
        phase="diagnose",
        trace_id=report["trace_id"],
        decision_made={"symptom": body.symptom, "root_causes": len(report["root_causes"])},
        context_snapshot=body.context,
    )
    return report


__all__ = ["router"]
