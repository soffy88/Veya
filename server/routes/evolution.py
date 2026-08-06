"""Evolution route — 达尔文算子自进化闭环 API.

生命周期: register (ACTIVE) → record_shadow (影子测试) → evolve (突变+回测择优,
PRD 升级申请经通知中心 HITL_REQUIRED 推送) → promote (审批替换, 谱系可回滚).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.darwin_evolution import darwin_evolution

router = APIRouter(prefix="/evolution", tags=["evolution"])


class RegisterOperatorRequest(BaseModel):
    code: str
    name: str | None = None


class ShadowObservationRequest(BaseModel):
    slippage: float
    accuracy: float
    extra: dict[str, Any] | None = None


@router.get("/operators")
async def list_operators() -> dict[str, Any]:
    """算子种群: 状态/影子指标/衰减判定/候选."""
    return {"operators": darwin_evolution.list_operators()}


@router.post("/operators")
async def register_operator(req: RegisterOperatorRequest) -> dict[str, Any]:
    """登记一个 ACTIVE 算子 (Genesis 锻造产物 / 人工提交)."""
    if not req.code.strip():
        raise HTTPException(status_code=422, detail="code 不能为空")
    op_id = darwin_evolution.register_operator(req.code, req.name)
    return {"operator_id": op_id, "status": "ACTIVE"}


@router.get("/operators/{op_id}")
async def get_operator(op_id: str) -> dict[str, Any]:
    op = darwin_evolution.get_operator(op_id)
    if op is None:
        raise HTTPException(status_code=404, detail=f"operator {op_id} not found")
    return op


@router.post("/operators/{op_id}/shadow")
async def record_shadow(op_id: str, req: ShadowObservationRequest) -> dict[str, Any]:
    """影子测试观测: 后台静默记录滑点/预测准确率 (不接管真实资金)."""
    try:
        return darwin_evolution.record_shadow(
            op_id, slippage=req.slippage, accuracy=req.accuracy, extra=req.extra
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/operators/{op_id}/evolve")
async def evolve_operator(op_id: str, force: bool = False) -> dict[str, Any]:
    """触发一轮达尔文进化: 突变 3 变种 → 并发回测 → 择优 → PRD 申请."""
    try:
        return await darwin_evolution.evolve(op_id, force=force)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/operators/{op_id}/promote")
async def promote_operator(op_id: str) -> dict[str, Any]:
    """批准 PRD → 候选算子替换 ACTIVE (旧代码进谱系 lineage, 可回滚)."""
    try:
        return darwin_evolution.promote(op_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/operators/{op_id}/prd")
async def get_prd(op_id: str) -> dict[str, Any]:
    """PRD 升级申请文档 (markdown)."""
    prd = darwin_evolution.get_prd(op_id)
    if prd is None:
        raise HTTPException(status_code=404, detail=f"operator {op_id} has no pending PRD")
    return {"operator_id": op_id, "prd": prd}


@router.get("/health")
async def evolution_health() -> dict[str, Any]:
    return darwin_evolution.health()
