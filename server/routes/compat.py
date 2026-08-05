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


@router.post("/agent/stream")
async def agent_stream(req: StreamRequest):
    """SSE 流式对话(桥接主脑事件流)。"""
    sid = req.session_id or "compat_stream"
    queue = get_or_create_queue(sid)

    async def _stream_events():
        token = None
        from server.events import _on_step_ctx

        token = _on_step_ctx.set(queue.on_step)
        try:
            result = await master_coordinator.chat_stream(req.text, session_id=sid, max_rounds=3)
            final = result.get("final_answer", "") or result.get("error", "")
            # 最终回答作为 text_delta 推送
            if final:
                queue.on_step({"type": "text_delta", "squad_id": "master", "delta": final})
            queue.on_step({"type": "master_done", "session_id": sid, "final": final, "status": result.get("status")})
        finally:
            _on_step_ctx.reset(token)
            queue.close()

    return StreamingResponse(_stream_events(), media_type="text/event-stream")


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
