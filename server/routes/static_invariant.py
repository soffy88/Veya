"""Static invariant route — 前瞻性与静态不变量校验引擎 API.

挂载位置: 策略代码交给协处理器回测**之前** (数学级法律, 不依赖 LLM).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.static_invariant import VeyaStaticInvariant

router = APIRouter(prefix="/invariants", tags=["invariants"])


class InvariantCheckRequest(BaseModel):
    strategy_code: str
    filename: str = "<strategy>"


@router.post("/check")
async def invariant_check(req: InvariantCheckRequest) -> dict[str, Any]:
    """AST 硬扫描策略源码.

    Returns:
        {verdict: pass|review|block|error, findings: [...], violations: [...],
         warnings: [...], summary: {...}}

    verdict=block 的代码不得进入回测/实盘 (L1 未来函数 / L2 未来行索引 / L3 np.roll 负偏移).
    """
    if not req.strategy_code.strip():
        raise HTTPException(status_code=422, detail="strategy_code 不能为空")
    return VeyaStaticInvariant.check(req.strategy_code, filename=req.filename)
