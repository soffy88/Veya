"""Veya chat streaming — master-brain SSE event pump (single source).

Produces the OpenAI-style SSE frame stream for a chat request:
  text_delta / tool_call / master_round / master_done → data: {...} → [DONE]

Used by both the Agent OS backend (server.app) and the unified gateway
(veya.server.app, systemd :8767) so the two never drift on stream semantics.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TYPE_CHECKING

from server.coordinator_master import master_coordinator
from server.session_identity import new_session_id
from server.sse import get_or_create_queue

if TYPE_CHECKING:
    from fastapi import Request

# 后台任务引用集(防 GC 回收进行中的流式任务)
_stream_tasks: set[asyncio.Task] = set()


async def new_agent_stream_events(
    text: str,
    session_id: str | None = None,
    *,
    turn_id: str | None = None,
    config: dict | None = None,
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    user: dict | None = None,
    images: list[str] | None = None,
    request: Request | None = None,
    mode: str | None = None,
    require_approval: bool = False,
    freeze_allow: str | None = None,
) -> AsyncIterator[str]:
    """主脑 SSE 事件泵: 消费事件队列 → SSE 帧。

    text_delta / tool_call / master_done 事件流实时推送, 末尾 [DONE]。
    config/provider/model/endpoint 为请求级 LLM 覆盖(前端传入的 user key)。
    user: 登录用户 {user_id, username} — 在流式 task 内显式设置 (contextvar
    不跨 asyncio task 传播, 否则会话历史/计划会落回 anonymous)。
    request: FastAPI 请求对象 — 心跳节拍上检测客户端断开, 断开则停止推流并
    释放本生成器协程。注意: 后台 ``chat_task``/``_finish`` 仍跑完 (跨端完成通知
    依赖它), 故此处只停消费、不取消后台任务。
    """
    sid = session_id or new_session_id()
    now = time.monotonic()
    from server.coordinator_master import (
        _active_generations,
        _active_stream_queues,
        _active_streams,
        _active_turn_ids,
        _cancelled_generations,
        _cancelled_turn_ids,
        _last_stop_meta,
    )

    # 1. 检查当前 turn 或请求是否已被 Stop 阻断 (防止 stale retry / reconnect 重启已停止任务)
    if turn_id and turn_id in _cancelled_turn_ids.get(sid, set()):
        yield "retry: 3000\n\n"
        yield f"id: 1\ndata: {json.dumps({'type': 'master_done', 'session_id': sid, 'status': 'cancelled', 'reason': 'stale_request_after_stop'}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    stop_meta = _last_stop_meta.get(sid)
    if stop_meta and (now - stop_meta.get("time", 0.0) < 5.0):
        # 5秒内针对该 session 的 Stop 发生过，且为同一内容且未指明新 turn_id 时拦截
        if not turn_id and (stop_meta.get("prompt") == text or not text):
            yield "retry: 3000\n\n"
            yield f"id: 1\ndata: {json.dumps({'type': 'master_done', 'session_id': sid, 'status': 'cancelled', 'reason': 'stale_request_after_stop'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

    # 2. 检查会话是否已有正在运行的 chat_task (重连/并发请求附着旧 stream, 绝不重复起任务)
    active_task = _active_streams.get(sid)
    if active_task is not None and not active_task.done():
        queue = _active_stream_queues.get(sid) or get_or_create_queue(sid)
        sub_q = queue.subscribe()
        try:
            last_event_id: int | None = None
            if request is not None:
                raw_last_id = request.headers.get("Last-Event-ID", "")
                if raw_last_id.isdigit():
                    last_event_id = int(raw_last_id)
            yield "retry: 3000\n\n"
            replay_highwater = last_event_id if last_event_id is not None else -1
            if last_event_id is not None:
                for replayed in queue._replay_from(last_event_id):
                    replay_highwater = max(replay_highwater, int(replayed.get("id", 0)))
                    event_id = replayed.get("id", 0)
                    yield f"id: {event_id}\ndata: {json.dumps(replayed, ensure_ascii=False)}\n\n"
            _HEARTBEAT_S = 20.0
            while True:
                try:
                    item = await asyncio.wait_for(sub_q.get(), timeout=_HEARTBEAT_S)
                except TimeoutError:
                    if request is not None and await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                if last_event_id is not None and item.get("id", 0) <= replay_highwater:
                    continue
                event_id = item.get("id", 0)
                yield f"id: {event_id}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            queue.unsubscribe(sub_q)
        return

    # 3. 正常发起新一轮对话任务 (Generation 自增并记录执行身份)
    gen = _active_generations.get(sid, 0) + 1
    _active_generations[sid] = gen
    effective_turn_id = turn_id or f"gen_{gen}_{int(now * 1000)}"
    _active_turn_ids[sid] = effective_turn_id

    queue = get_or_create_queue(sid)
    _active_stream_queues[sid] = queue
    sub_q = queue.subscribe()

    from server import auth as auth_mod
    from server.events import _on_step_ctx

    token = _on_step_ctx.set(queue.on_step)

    async def _run_chat() -> None:
        # 启动即检查是否已取消 (覆盖任务刚创建即触发 Stop 的竞态窗口)
        if gen in _cancelled_generations.get(sid, set()) or effective_turn_id in _cancelled_turn_ids.get(sid, set()):
            raise asyncio.CancelledError("Turn cancelled before startup")
        if user:
            auth_mod.set_user(user)
        return await master_coordinator.chat_stream(
            text,
            session_id=sid,
            config=config,
            provider=provider,
            model=model,
            endpoint=endpoint,
            images=images,
            mode=mode,
            require_approval=require_approval,
            freeze_allow=freeze_allow,
        )

    try:
        chat_task = asyncio.create_task(_run_chat())
        _active_streams[sid] = chat_task

        def _unregister(_t: asyncio.Task) -> None:
            if _active_streams.get(sid) is _t:
                _active_streams.pop(sid, None)
            if _active_stream_queues.get(sid) is queue:
                _active_stream_queues.pop(sid, None)

        chat_task.add_done_callback(_unregister)

        async def _finish() -> None:
            """主脑结束后: 补发最终回答事件 + 关闭队列(唤醒消费循环)。"""
            try:
                result = await chat_task
            except asyncio.CancelledError:
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
            if result is None:
                result = {}
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
                    "cost_usd": result.get("cost_usd") or 0,
                    "rounds": result.get("rounds") or 0,
                }
            )
            queue.close()
            if user:
                try:
                    from server.notification_center import global_notifier

                    global_notifier.push(
                        "SUCCESS",
                        "任务完成",
                        f"会话 {sid[:12]}…: {final[:80]}",
                        payload={"session_id": sid},
                        user_id=user["user_id"],
                    )
                except Exception as exc:
                    import logging

                    logging.getLogger("chat_stream").warning("完成通知推送失败: %s", exc)

        finish_task = asyncio.create_task(_finish())
        _stream_tasks.add(finish_task)
        finish_task.add_done_callback(_stream_tasks.discard)

        mirror_uid = (user or {}).get("user_id") or ""
        _notifier = None
        if mirror_uid:
            try:
                from server.notification_center import global_notifier as _notifier
            except Exception:
                _notifier = None
        if _notifier is not None:
            with suppress(Exception):
                _notifier.push_stream(
                    sid, {"type": "user_prompt", "text": text}, user_id=mirror_uid
                )

        _HEARTBEAT_S = 20.0
        last_event_id: int | None = None
        if request is not None:
            raw_last_id = request.headers.get("Last-Event-ID", "")
            if raw_last_id.isdigit():
                last_event_id = int(raw_last_id)
        yield "retry: 3000\n\n"
        replay_highwater = last_event_id if last_event_id is not None else -1
        if last_event_id is not None:
            for replayed in queue._replay_from(last_event_id):
                replay_highwater = max(replay_highwater, int(replayed.get("id", 0)))
                event_id = replayed.get("id", 0)
                yield f"id: {event_id}\ndata: {json.dumps(replayed, ensure_ascii=False)}\n\n"
        while True:
            try:
                item = await asyncio.wait_for(sub_q.get(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                if request is not None and await request.is_disconnected():
                    break
                yield ": ping\n\n"
                continue
            if item is None:
                break
            if last_event_id is not None and item.get("id", 0) <= replay_highwater:
                continue
            if _notifier is not None:
                with suppress(Exception):
                    _notifier.push_stream(sid, item, user_id=mirror_uid)
            event_id = item.get("id", 0)
            yield f"id: {event_id}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        queue.unsubscribe(sub_q)
        with suppress(Exception):
            _on_step_ctx.reset(token)
