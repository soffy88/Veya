"""Legacy L4 gateway compatibility routes — 让新版 Agent OS 后端服务旧前端契约。

apps/web SvelteKit console 的 capabilities 目录按旧 L4 gateway 契约调用:
  /api/v1/agent/verify · /api/v1/agent/run · /api/v1/agent/stream ·
  /api/v1/agent/history/{sid} · /api/v1/kanban · /api/v1/sandbox/execute

本层把这些端点桥接到新 Agent OS 能力(主脑 / 沙箱 / 看板)。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.coordinator_master import master_coordinator
from server.sse import get_or_create_queue

router = APIRouter(prefix="/api/v1", tags=["compat"])

# 看板内存持久(进程级; 生产可换 SQLite)
_kanban_boards: dict[str, Any] = {}

# 后台任务引用集(防 GC 回收进行中的流式任务)
_stream_tasks: set[asyncio.Task] = set()

class VerifyRequest(BaseModel):
    statement: str = ""


class RunRequest(BaseModel):
    text: str = ""
    session_id: str | None = None


class StreamRequest(BaseModel):
    text: str = ""
    session_id: str | None = None


class SandboxRequest(BaseModel):
    command: str | None = None
    script: str | None = None
    time_limit: float = 30.0
    memory_limit: int = 256 * 1024 * 1024
    network_blocked: bool = True


class KanbanRequest(BaseModel):
    action: str = "get"
    board_id: str = "default"
    board_name: str = "Default Board"
    card_id: str = ""
    card_title: str = ""
    card_description: str = ""
    to_status: str = "todo"


@router.post("/agent/verify")
async def agent_verify(req: VerifyRequest) -> dict[str, Any]:
    """神经符号命题验证(桥接主脑)。"""
    if not req.statement.strip():
        return {"ok": False, "statement": "", "verdict": "empty", "content": "statement 不能为空"}
    result = await master_coordinator.chat_stream(
        f"请验证以下自然语言命题的真伪, 给出结论与简要推理: {req.statement}",
        session_id="compat_verify",
        max_rounds=1,
    )
    content = result.get("final_answer", "") or result.get("error", "")
    return {"ok": result.get("status") == "success", "statement": req.statement, "verdict": content, "content": content}


@router.post("/agent/run")
async def agent_run(req: RunRequest) -> dict[str, Any]:
    """单轮对话(桥接主脑)。"""
    result = await master_coordinator.chat_stream(
        req.text, session_id=req.session_id or "compat_run", max_rounds=3
    )
    return {
        "status": result.get("status"),
        "content": result.get("final_answer", "") or result.get("error", ""),
        "cost_usd": result.get("cost_usd", 0.0),
        "session_id": result.get("session_id"),
    }


async def _new_agent_stream_events(text: str, session_id: str | None = None):
    """主脑 SSE 事件泵(可复用): 消费事件队列 → SSE 帧。

    text_delta / tool_call / master_done 事件流实时推送, 末尾 [DONE]。
    """
    sid = session_id or "compat_stream"
    queue = get_or_create_queue(sid)
    from server.events import _on_step_ctx

    token = _on_step_ctx.set(queue.on_step)
    try:
        chat_task = asyncio.create_task(
            master_coordinator.chat_stream(text, session_id=sid, max_rounds=3)
        )

        async def _finish():
            """主脑结束后: 补发最终回答事件 + 关闭队列(唤醒消费循环)。"""
            result = await chat_task
            final = result.get("final_answer") or result.get("error", "")
            if final:
                queue.on_step(
                    {"type": "text_delta", "squad_id": "master", "delta": final}
                )
            queue.on_step(
                {
                    "type": "master_done",
                    "session_id": sid,
                    "status": result.get("status"),
                }
            )
            queue.close()

        # 主脑结束后: 补发最终回答 + 关闭队列(保留引用防 GC)
        _finish_task = asyncio.create_task(_finish())
        _stream_tasks.add(_finish_task)
        _finish_task.add_done_callback(_stream_tasks.discard)

        # 消费事件队列 → SSE 帧(主脑事件流实时推送)
        while True:
            item = await queue._q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        _on_step_ctx.reset(token)


@router.post("/agent/stream")
async def agent_stream(req: StreamRequest):
    """SSE 流式对话(桥接主脑事件流: text_delta / tool_call / master_done)。"""
    return StreamingResponse(
        _new_agent_stream_events(req.text, req.session_id),
        media_type="text/event-stream",
    )


@router.get("/agent/history/{sid}")
async def agent_history(sid: str) -> dict[str, Any]:
    """会话历史(前端本地管理, 返回空壳)。"""
    return {"session_id": sid, "messages": []}


@router.post("/sandbox/execute")
async def sandbox_execute(req: SandboxRequest) -> dict[str, Any]:
    """沙箱执行(桥接 veya.sandbox)。"""
    from veya.sandbox import SandboxConfig, create_safe_executor

    config = SandboxConfig(
        time_limit=req.time_limit,
        memory_limit=req.memory_limit,
        network_blocked=req.network_blocked,
        audit_enabled=True,
        env_extra={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
    )
    executor = create_safe_executor(config)
    async with executor:
        if req.script:
            result = await executor.run_script(req.script)
        elif req.command:
            result = await executor.execute(req.command)
        else:
            return {"status": "failed", "error": "command 或 script 必填其一"}
    return {
        "status": "success" if result.get("exit_code") == 0 else "failed",
        "exit_code": result.get("exit_code"),
        "output": result.get("stdout", ""),
        "error": result.get("stderr", ""),
        "duration": result.get("duration", 0.0),
    }


@router.post("/kanban")
async def kanban_op(req: KanbanRequest) -> dict[str, Any]:
    """看板操作(桥接 veya.kanban)。"""
    from veya.kanban import CardStatus, KanbanBoard, KanbanCard

    def _get_board(board_id: str) -> KanbanBoard:
        if board_id not in _kanban_boards:
            _kanban_boards[board_id] = KanbanBoard.create_default(req.board_name)
        return _kanban_boards[board_id]

    board = _get_board(req.board_id)
    action = req.action
    if action == "create":
        return {"status": "success", "board_id": req.board_id, "board": board.to_dict()}
    if action == "get":
        return {"status": "success", "board_id": req.board_id, "board": board.to_dict()}
    if action == "add_card":
        card = KanbanCard(card_id=req.card_id or f"card_{len(board.cards) + 1}", title=req.card_title, description=req.card_description)
        board.add_card(card, CardStatus(req.to_status) if req.to_status else None)
        return {"status": "success", "card": card.to_dict()}
    if action == "move":
        moved = board.move_card(req.card_id, CardStatus(req.to_status))
        return {"status": "success" if moved else "failed", "moved": moved}
    if action == "ready":
        return {"status": "success", "ready": [c.to_dict() for c in board.get_ready_cards()]}
    return {"status": "failed", "error": f"unknown action {action}"}


@router.get("/kanban/graph")
async def kanban_graph(board_id: str = "default") -> dict[str, Any]:
    from veya.kanban import KanbanBoard

    board = KanbanBoard.create_default()
    return {"status": "success", "board_id": board_id, "graph": board.get_dependency_graph()}
