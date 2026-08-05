"""Veya chat streaming — master-brain SSE event pump (single source).

Produces the OpenAI-style SSE frame stream for a chat request:
  text_delta / tool_call / master_round / master_done → data: {...} → [DONE]

Used by both the Agent OS backend (server.app) and the unified gateway
(veya.server.app, systemd :8767) so the two never drift on stream semantics.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from server.coordinator_master import master_coordinator
from server.sse import get_or_create_queue

# 后台任务引用集(防 GC 回收进行中的流式任务)
_stream_tasks: set[asyncio.Task] = set()


async def new_agent_stream_events(
    text: str,
    session_id: str | None = None,
    *,
    config: dict | None = None,
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
) -> AsyncIterator[str]:
    """主脑 SSE 事件泵: 消费事件队列 → SSE 帧。

    text_delta / tool_call / master_done 事件流实时推送, 末尾 [DONE]。
    config/provider/model/endpoint 为请求级 LLM 覆盖(前端传入的 user key)。
    """
    sid = session_id or "chat_stream"
    queue = get_or_create_queue(sid)
    from server.events import _on_step_ctx

    token = _on_step_ctx.set(queue.on_step)
    try:
        chat_task = asyncio.create_task(
            master_coordinator.chat_stream(
                text,
                session_id=sid,
                max_rounds=5,
                config=config,
                provider=provider,
                model=model,
                endpoint=endpoint,
            )
        )

        async def _finish() -> None:
            """主脑结束后: 补发最终回答事件 + 关闭队列(唤醒消费循环)。"""
            result = await chat_task
            final = result.get("final_answer") or result.get("error", "")
            if final:
                queue.on_step({"type": "text_delta", "squad_id": "master", "delta": final})
            queue.on_step(
                {
                    "type": "master_done",
                    "session_id": sid,
                    "status": result.get("status"),
                }
            )
            queue.close()

        # 保留任务引用防 GC
        finish_task = asyncio.create_task(_finish())
        _stream_tasks.add(finish_task)
        finish_task.add_done_callback(_stream_tasks.discard)

        # 消费事件队列 → SSE 帧(主脑事件流实时推送)
        while True:
            item = await queue._q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        _on_step_ctx.reset(token)
