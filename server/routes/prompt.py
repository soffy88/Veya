from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from server.coordinator_master import master_coordinator
from server.session_events import durable_session_store

router = APIRouter()


class PromptRequest(BaseModel):
    text: str
    session_id: str | None = None
    persona: str = "build"
    model: str | None = None
    provider: str | None = None
    extra: dict[str, Any] = {}


@router.post("/prompt")
async def handle_prompt(req: PromptRequest) -> dict[str, Any]:
    # 提取 session_id,若无则由 coordinator 生成
    sid = req.session_id

    # 绑定 SSE 队列回调
    on_step = None
    if sid:
        on_step = lambda event: durable_session_store.publish_sync(sid, event)  # noqa: E731

    # /prompt is a semantic ingress.  The only semantic authority is the
    # canonical MasterCoordinator; legacy fields remain typed request context.
    extra = dict(req.extra or {})
    config = extra.get("config") if isinstance(extra.get("config"), dict) else None
    system_context = extra.get("system_context")
    if not system_context and req.persona:
        system_context = f"Requested persona: {req.persona}"
    result = await master_coordinator.chat_stream(
        req.text,
        session_id=sid,
        on_step=on_step,
        model=req.model,
        provider=req.provider,
        config=config,
        system_context=system_context,
    )
    return result
