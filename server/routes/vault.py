"""Zero-Trust Vault routes — 人类审批悬浮窗的 Approve/Reject 端点。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.zero_trust_vault import global_vault

router = APIRouter(prefix="/vault", tags=["vault"])


class ApprovalRequest(BaseModel):
    approved: bool = True


class SecretRequest(BaseModel):
    vault_id: str
    secret: str


@router.post("/tasks/{task_id}/approve")
async def handle_human_approval(task_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    """前端 Svelte 悬浮窗点击按钮后请求这个接口, 唤醒挂起的协程。"""
    resolved = global_vault.resolve_approval(task_id, payload.approved)
    if not resolved:
        raise HTTPException(status_code=404, detail="Task ID not found or expired")
    return {"status": "Resolved", "approved": payload.approved}


@router.post("/secrets")
async def set_secret(req: SecretRequest) -> dict[str, Any]:
    """注册密钥(运维/受信通道, 生产环境需额外鉴权)。"""
    msg = global_vault.set_secret(req.vault_id, req.secret)
    return {"status": "ok", "message": msg}


@router.get("/secrets")
async def list_secrets() -> dict[str, Any]:
    """只返回密钥 ID(绝不返回明文)。"""
    return {"vault_ids": global_vault.list_secret_ids()}


@router.get("/pending")
async def list_pending() -> dict[str, Any]:
    return {"pending": global_vault.get_pending()}


# 兼容脚手架路径: /api/v1/tasks/{task_id}/approve
compat_router = APIRouter(tags=["vault"])


@compat_router.post("/api/v1/tasks/{task_id}/approve")
async def handle_human_approval_compat(task_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    return await handle_human_approval(task_id, payload)
