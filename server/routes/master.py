"""Master route — 主脑聊天端点。

前端只发文本、收 SSE 流;主脑负责意图路由与工具调度。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import auth as auth_mod
from server.coordinator_master import MasterCoordinator
from server.session_events import durable_session_store

router = APIRouter(prefix="/master", tags=["master"])


class MasterChatRequest(BaseModel):
    text: str
    session_id: str | None = None
    user_api_key: str | None = None
    model: str | None = None
    provider: str | None = None
    endpoint: str | None = None
    extra: dict[str, Any] = {}


@router.post("/chat")
async def master_chat(
    req: MasterChatRequest, user: dict = Depends(auth_mod.require_user)
) -> dict[str, Any]:
    """主脑聊天: 意图路由 + 工具调用 + 最终回答(经 SSE 事件流推送)。

    SEC C4: 请求级 ``endpoint`` 只在调用方同时提供**自己的** ``user_api_key``
    时才生效 —— 否则 ``get_api_key`` 会回落到服务端环境变量里的 key, 并把服务端
    key 发往攻击者指定的 endpoint。无自有 key 时忽略 endpoint, 走服务端配置。
    """
    endpoint = req.endpoint
    if endpoint and not req.user_api_key:
        endpoint = None
    sid = req.session_id
    on_step = None
    if sid:
        on_step = lambda event: durable_session_store.publish_sync(sid, event)  # noqa: E731

    coordinator = MasterCoordinator(
        user_api_key=req.user_api_key,
        model=req.model,
        provider=req.provider,
        endpoint=endpoint,
    )
    return await coordinator.chat_stream(
        req.text,
        session_id=sid,
        on_step=on_step,
    )


@router.get("/tools")
async def list_master_tools(user: dict = Depends(auth_mod.require_user)) -> dict[str, Any]:
    """暴露主脑可见的全部能力(调试/前端展示用)。"""
    from server.tool_registry import master_tools

    return master_tools.to_dict()
