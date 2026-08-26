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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
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


# =========================================================================
# P3-01 Permission Profiles — 档位查询/切换 (供 P1-05 档位选择器)
# =========================================================================


class ProfileSetRequest(BaseModel):
    profile: str = Field(..., min_length=1, description="READ_ONLY | DEVELOPMENT | PRODUCTION")


@router.get("/profiles")
async def list_permission_profiles() -> dict[str, Any]:
    """列出全部权限档位及其矩阵摘要 (P1-05 选择器数据源)。"""
    from server.permission_profiles import list_profiles

    return {"profiles": list_profiles()}


@router.get("/profile")
async def get_current_profile(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """返回当前用户/请求生效档位。"""
    from server.permission_profiles import default_profile

    return {"profile": default_profile().value}


@router.post("/profile")
async def set_permission_profile(
    req: ProfileSetRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """切换权限档位。

    档位按用户存储在当前进程内，不再用全局环境变量污染其他用户；宿主可在
    登录态持久化配置。enforce 与否由 VEYA_PERMISSION_PROFILE_ENFORCE 决定。
    """
    from server.permission_profiles import ProfileName, set_user_profile

    try:
        profile = ProfileName(req.profile.strip().upper())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"invalid profile '{req.profile}'; must be one of "
            "READ_ONLY, DEVELOPMENT, PRODUCTION",
        )
    set_user_profile(profile, user_id=str(user.get("user_id") or "anonymous"))
    return {
        "profile": profile.value,
        "description": {
            "READ_ONLY": "只读：写入/执行/外发/破坏性一律拒绝",
            "DEVELOPMENT": "开发：本地写入与测试执行放行，外发/破坏性需确认",
            "PRODUCTION": "生产：写入/执行/外发需确认，破坏性拒绝",
        }[profile.value],
        "note": "档位已切换（进程内）。enforce 与否由 VEYA_PERMISSION_PROFILE_ENFORCE 控制（默认 observe）。",
    }
