"""Observer route — O3 沙箱推演 API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.observer import VeyaObserver

router = APIRouter(prefix="/observer", tags=["observer"])

# 网关级单例(推演快照库持久化)
_observer = VeyaObserver(snapshot_dir="~/.veya/observer")


class ActionModel(BaseModel):
    id: str
    kind: str
    payload: dict[str, Any] = {}
    reversibility: str = "reversible"
    compensation: dict[str, Any] | None = None
    description: str = ""


class PlanModel(BaseModel):
    id: str
    actions: list[ActionModel]
    prior: float = 1.0
    rationale: str = ""


class DivergenceModel(BaseModel):
    kind: str
    detail: str
    severity: str = "medium"


class LookaheadRequest(BaseModel):
    plans: list[PlanModel]
    base_dir: str
    min_reward: float = 0.999
    stability_check: bool = False
    max_parallel: int = 4
    divergences: list[DivergenceModel] = []


@router.post("/lookahead")
async def observer_lookahead(req: LookaheadRequest) -> dict[str, Any]:
    """对候选方案做单步沙箱推演(可逆性闸门 → 并行 rollout → 稠密打分 → 裁决)。

    不可逆动作直接升级; 低于 min_reward 不输出 least-bad, 一律交给人。
    """
    if not req.plans:
        raise HTTPException(status_code=422, detail="plans 不能为空")
    base = Path(req.base_dir).expanduser()
    if not base.is_dir():
        raise HTTPException(status_code=404, detail=f"base_dir 不存在: {base}")
    return await _observer.lookahead(
        [p.model_dump() for p in req.plans],
        base,
        min_reward=req.min_reward,
        stability_check=req.stability_check,
        max_parallel=req.max_parallel,
        divergences=[d.model_dump() for d in req.divergences],
    )
