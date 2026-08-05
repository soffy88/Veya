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

    No blocking-wait consumer yet — nothing in this codebase currently gates a
    background action on this decision. Once one exists, it can await a resolution
    registered here instead of just logging.
    """
    logger.info(
        "[notifications] %s: %s", notification_id, "approved" if req.approved else "rejected"
    )
    global_notifier.dismiss(notification_id)
    return {"status": "ok", "notification_id": notification_id, "approved": req.approved}
