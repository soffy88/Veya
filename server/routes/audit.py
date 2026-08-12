"""Audit route — 决策审计回放 API (取证/追责/回放)。"""

from __future__ import annotations

from fastapi import Depends, APIRouter, HTTPException
from server import auth as auth_mod

from server.audit import VeyaAudit

router = APIRouter(prefix="/audit", tags=["audit"],
              dependencies=[Depends(auth_mod.require_user)])

_audit = VeyaAudit()


@router.get("/traces")
async def list_traces(limit: int = 50) -> dict:
    """最近决策链路清单 (trace_id + 事件类型序列)。"""
    return _audit.traces(limit=limit)


@router.get("/{trace_id}")
async def replay_trace(trace_id: str) -> dict:
    """回放一次故障处理链路的完整审计记录 (diagnose→plan→decide→execute→learn)。"""
    result = _audit.replay(trace_id)
    if result["event_count"] == 0:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} 不存在")
    return result
