"""Operator route — O2 确定性调度中心 API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.operator_center import VeyaOperatorCenter

router = APIRouter(prefix="/operator", tags=["operator"])


class DispatchRequest(BaseModel):
    problem: dict[str, Any]                     # {tasks, workers, bids, unassigned_penalty}
    mode: str = "auto"                          # auto | one_to_one | capacity
    payment_rule: str | None = None          # None | first_price | second_price | vcg
    resource_ranking: list[str] = []
    balance_weight: float = 0.0


@router.post("/dispatch")
async def operator_dispatch(req: DispatchRequest) -> dict[str, Any]:
    """组合最优化分配(匈牙利/MILP) → VCG 支付 → 资源全序租约计划 → 可重放账本。"""
    if not req.problem.get("tasks") or not req.problem.get("workers"):
        raise HTTPException(status_code=422, detail="problem 需要 tasks 与 workers")
    return VeyaOperatorCenter.dispatch(
        req.problem,
        mode=req.mode,
        payment_rule=req.payment_rule,
        resource_ranking=req.resource_ranking,
        balance_weight=req.balance_weight,
    )
