"""Global notification routes: SSE broadcast stream + HITL approve/reject."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.notification_center import global_notifier

logger = logging.getLogger("notifications")

router = APIRouter(prefix="/notifications", tags=["notifications"])


class ApproveRequest(BaseModel):
    approved: bool


@router.get("/stream")
async def notifications_stream() -> StreamingResponse:
    return StreamingResponse(
        global_notifier.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{notification_id}/approve")
async def approve_notification(notification_id: str, req: ApproveRequest) -> dict[str, Any]:
    """Record the HITL decision and tell every connected tab to drop the toast.

    Zero-trust vault integration: when the toast carries a vault task reference
    (payload.task_id — see server/zero_trust_vault.py's vault_hitl bridge), the
    human verdict is forwarded to `global_vault.resolve_approval()`, which wakes
    the suspended coroutine; it then decrypts and injects the secret into the
    physical layer. This closes the HITL loop: Approve/Reject button → resolve.
    """
    message = global_notifier.get(notification_id)
    vault_task_resolved: bool | None = None
    if message is not None:
        task_id = (message.get("payload") or {}).get("task_id")
        if task_id:
            # 惰性 import: server.zero_trust_vault 启动早期被加载, 避免 import 环
            from server.zero_trust_vault import global_vault

            vault_task_resolved = global_vault.resolve_approval(task_id, req.approved)
            logger.info(
                "[notifications] vault task %s: %s (resolved=%s)",
                task_id,
                "approved" if req.approved else "rejected",
                vault_task_resolved,
            )
    logger.info(
        "[notifications] %s: %s", notification_id, "approved" if req.approved else "rejected"
    )
    global_notifier.dismiss(notification_id)
    return {
        "status": "ok",
        "notification_id": notification_id,
        "approved": req.approved,
        "vault_task_resolved": vault_task_resolved,
    }
