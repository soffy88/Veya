"""
Permission API — 交互式权限确认（G5）

规则自动裁决 ALLOW/DENY；PENDING（ask: 规则或无匹配）挂起为可批准/拒绝的请求：
    GET  /api/v1/permission/pending       列出待确认请求
    POST /api/v1/permission/evaluate      评估（wait=false → 返回 request_id 挂起）
    POST /api/v1/permission/{id}/approve  人工批准
    POST /api/v1/permission/{id}/deny     人工拒绝
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from veya.obase.authz import InteractivePermissionGate

router = APIRouter(prefix="/permission", tags=["permission"])

# 进程内单例 gate（服务层装配；CLI 可另建实例）
_gate = InteractivePermissionGate()


def get_gate() -> InteractivePermissionGate:
    return _gate


class EvaluateRequest(BaseModel):
    action: str
    resource: str | None = None
    persona: str = Field(default="build")
    context: dict[str, Any] = Field(default_factory=dict)


class DecisionResponse(BaseModel):
    decision: str
    action: str
    resource: str | None = None
    persona: str
    status: str
    request_id: str | None = None
    note: str = ""
    error: str | None = None


@router.get("/pending")
async def pending() -> list[dict[str, Any]]:
    """列出全部待确认请求。"""
    return [
        {
            "request_id": r.request_id,
            "action": r.action,
            "resource": r.resource,
            "persona": r.persona,
            "context": r.context,
        }
        for r in _gate.pending_requests()
    ]


@router.post("/evaluate", response_model=DecisionResponse)
async def evaluate(req: EvaluateRequest) -> DecisionResponse:
    """评估权限；PENDING 时挂起并返回 request_id（wait=false）。"""
    result = await _gate.evaluate(
        req.action,
        resource=req.resource,
        persona=req.persona,
        context=req.context,
        wait=False,
    )
    return DecisionResponse(
        decision=str(result["decision"]),
        action=result["action"],
        resource=result.get("resource"),
        persona=result["persona"],
        status=result["status"],
        request_id=result.get("request_id"),
        error=result.get("error"),
    )


@router.post("/{request_id}/approve", response_model=DecisionResponse)
async def approve(request_id: str, note: str = "approved via API") -> DecisionResponse:
    """人工批准一个挂起请求。"""
    gate = get_gate()
    request = gate.get_request(request_id)
    if not gate.approve(request_id, note=note):
        raise HTTPException(status_code=404, detail=f"unknown or resolved request: {request_id}")
    return _to_response(request, note)


@router.post("/{request_id}/deny", response_model=DecisionResponse)
async def deny(request_id: str, note: str = "denied via API") -> DecisionResponse:
    """人工拒绝一个挂起请求。"""
    gate = get_gate()
    request = gate.get_request(request_id)
    if not gate.deny(request_id, note=note):
        raise HTTPException(status_code=404, detail=f"unknown or resolved request: {request_id}")
    return _to_response(request, note)


def _to_response(request, note: str) -> DecisionResponse:
    return DecisionResponse(
        decision=str(request.decision),
        action=request.action,
        resource=request.resource,
        persona=request.persona,
        status="decided",
        request_id=request.request_id,
        note=note or request.note,
    )
