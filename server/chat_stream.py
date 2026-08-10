"""Veya chat streaming — master-brain SSE event pump (single source).

Produces the OpenAI-style SSE frame stream for a chat request:
  text_delta / tool_call / master_round / master_done → data: {...} → [DONE]

Used by both the Agent OS backend (server.app) and the unified gateway
(veya.server.app, systemd :8767) so the two never drift on stream semantics.
"""

from __future__ import annotations

import asyncio
import json
import uuid
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
    # P3: 无 sid 时生成独立 id (旧默认 "chat_stream" 是公共桶, 会跨会话/跨用户串味)
    sid = session_id or uuid.uuid4().hex
    queue = get_or_create_queue(sid)
    from server.events import _on_step_ctx

    token = _on_step_ctx.set(queue.on_step)
    try:
        chat_task = asyncio.create_task(
            master_coordinator.chat_stream(
                text,
                session_id=sid,
                max_rounds=8,
                config=config,
                provider=provider,
                model=model,
                endpoint=endpoint,
            )
        )
        # Stop 支持: 注册到 master (前端 Stop → cancel_session → 真正中断)
        from server.coordinator_master import _active_streams

        _active_streams[sid] = chat_task

        def _unregister(_t: asyncio.Task) -> None:
            _active_streams.pop(sid, None)

        chat_task.add_done_callback(_unregister)

        async def _finish() -> None:
            """主脑结束后: 补发最终回答事件 + 关闭队列(唤醒消费循环)。

            绝不静默: 主脑返回空/'None' 时也发可见提示帧, 前端不会留空白气泡。
            """
            try:
                result = await chat_task
            except asyncio.CancelledError:
                # 用户点 Stop → chat_task 被 cancel_session 取消 → 明确告知
                queue.on_step(
                    {
                        "type": "text_delta",
                        "squad_id": "master",
                        "delta": "⏹ 已停止。后台 Hicode 任务也已真正中断。",
                    }
                )
                queue.on_step({"type": "master_done", "session_id": sid, "status": "cancelled"})
                queue.close()
                return
            final = str(result.get("final_answer") or result.get("error") or "").strip()
            if not final or final.lower() in ("none", "null"):
                final = (
                    "⚠ 主脑未生成有效回答 (模型返回空内容 / 网关抖动)。"
                    "请重试, 或在上方更换模型/引擎。"
                )
            queue.on_step({"type": "text_delta", "squad_id": "master", "delta": final})
            queue.on_step(
                {
                    "type": "master_done",
                    "session_id": sid,
                    "status": result.get("status"),
                    # P2 上下文用量: 主脑 cost 透传前端 (engine_meta 简版)
                    "cost_usd": result.get("cost_usd") or 0,
                    "rounds": result.get("rounds") or 0,
                }
            )
            queue.close()

        # 保留任务引用防 GC
        finish_task = asyncio.create_task(_finish())
        _stream_tasks.add(finish_task)
        finish_task.add_done_callback(_stream_tasks.discard)

        # 消费事件队列 → SSE 帧(主脑事件流实时推送)
        # 心跳: 队列静默 >20s 时发 SSE 注释行 (: ping) — 前端 EventSource 规范忽略,
        # 但响应体持续流动 → 防止 Cloudflare Tunnel 100s 无数据掐断 (HTTP 524)。
        _HEARTBEAT_S = 20.0
        while True:
            try:
                item = await asyncio.wait_for(queue._q.get(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        _on_step_ctx.reset(token)
