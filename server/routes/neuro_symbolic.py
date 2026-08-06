"""Neuro-symbolic route — O1 神经符号规划器 API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.neuro_symbolic import VeyaNeuroSymbolic

router = APIRouter(prefix="/neurosymbolic", tags=["neurosymbolic"])


class PlanRequest(BaseModel):
    ir: dict[str, Any]                          # Plan IR JSON (LLM 产出, 机器判定)
    seed: int = 0
    strict_diff: bool = True
    deterministic_tiebreak: bool = True


@router.post("/plan")
async def neuro_symbolic_plan(req: PlanRequest) -> dict[str, Any]:
    """O1 四道闸门: 校验 → 回译 diff → Z3 可行性+MUS → MaxSMT 唯一最优解。

    ok=False 时返回 repair(喂回 LLM 的确定性反馈: 矛盾核心/回译证据/修复 hint)。
    """
    if not req.ir:
        raise HTTPException(status_code=422, detail="ir 不能为空")
    return VeyaNeuroSymbolic.plan(
        req.ir,
        seed=req.seed,
        strict_diff=req.strict_diff,
        deterministic_tiebreak=req.deterministic_tiebreak,
    )
