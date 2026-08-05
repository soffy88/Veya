"""Master route — 主脑聊天端点。

前端只发文本、收 SSE 流;主脑负责意图路由与工具调度。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from server.coordinator_master import MasterCoordinator
from server.sse import get_or_create_queue

router = APIRouter(prefix="/master", tags=["master"])


class MasterChatRequest(BaseModel):
    text: str
    session_id: str | None = None
    user_api_key: str | None = None
    model: str | None = None
    provider: str | None = None
    endpoint: str | None = None
    max_rounds: int | None = None
    extra: dict[str, Any] = {}


@router.post("/chat")
async def master_chat(req: MasterChatRequest) -> dict[str, Any]:
    """主脑聊天: 意图路由 + 工具调用 + 最终回答(经 SSE 事件流推送)。"""
    sid = req.session_id
    on_step = None
    if sid:
        queue = get_or_create_queue(sid)
        on_step = queue.on_step

    coordinator = MasterCoordinator(
        user_api_key=req.user_api_key,
        model=req.model,
        provider=req.provider,
        endpoint=req.endpoint,
    )
    return await coordinator.chat_stream(
        req.text,
        session_id=sid,
        on_step=on_step,
        max_rounds=req.max_rounds,
    )


@router.get("/tools")
async def list_master_tools() -> dict[str, Any]:
    """暴露主脑可见的全部能力(调试/前端展示用)。"""
    from server.tool_registry import master_tools

    return master_tools.to_dict()
