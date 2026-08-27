"""loop-plane api.exec — Exec API（SPEC §4.4）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.domain.exec.service import ExecService
from app.schemas import DispatchBody

router = APIRouter(prefix="/v1/loop/exec", tags=["exec"])


def _service(request: Request) -> ExecService:
    svc: ExecService | None = getattr(request.app.state, "exec_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="exec service 未初始化")
    return svc


def _trace(request: Request) -> str:
    return request.headers.get("X-Trace-Id", "")


@router.post("/dispatch")
async def dispatch(
    body: DispatchBody, request: Request, svc: ExecService = Depends(_service)
) -> dict[str, Any]:
    """硬化执行分发（mode 服务端收缩，白名单限制）。"""
    from app.deps import get_audit, get_store

    return svc.dispatch(
        mode=body.mode,
        tool_name=body.tool_name,
        args=body.args,
        trace_id=_trace(request),
        audit=get_audit(),
        store=get_store(),
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, svc: ExecService = Depends(_service)) -> dict[str, Any]:
    try:
        return svc.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/adapters")
async def list_adapters(svc: ExecService = Depends(_service)) -> dict[str, Any]:
    """白名单列表。"""
    return {"adapters": svc.registry.list()}


__all__ = ["router"]
