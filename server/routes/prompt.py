from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from server.coordinator import coordinator
from server.sse import get_or_create_queue

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
        queue = get_or_create_queue(sid)
        on_step = queue.on_step

    # 构造命令,包含 model 和 provider 参数
    command = {
        "text": req.text,
        "persona": req.persona,
        "model": req.model,
        "provider": req.provider,
        **req.extra,
    }

    # 执行(coordinator 会 fire_step 触发 SSE)
    result = await coordinator.handle(command, session_id=sid, on_step=on_step)
    return result
