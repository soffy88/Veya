"""
VS Code Integration API - Support for hicode VS Code extension
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/vscode", tags=["vscode"])


class RunAgentRequest(BaseModel):
    agent: str
    input_text: str
    session_id: str | None = None
    project: str | None = "default"


class RunAgentResponse(BaseModel):
    session_id: str
    status: str
    result: str | None = None
    error: str | None = None


class DebugSession(BaseModel):
    session_id: str
    breakpoints: list[dict[str, Any]]
    variables: dict[str, Any]
    stack_trace: list[dict[str, Any]] | None


@router.post("/run-agent", response_model=RunAgentResponse)
async def run_agent(req: RunAgentRequest):
    """Run an agent from VS Code (synchronous summary, no SSE)."""
    try:
        from server.coordinator import Coordinator

        # Run agent via real coordinator handle()
        coordinator = Coordinator()
        result = await coordinator.handle(
            {"text": req.input_text, "persona": req.agent, "project": req.project},
            session_id=req.session_id,
        )
        session_id = result.get("session_id", req.session_id or "")
        status = result.get("status", "completed")
        error = result.get("error")
        output = (
            json.dumps(result, ensure_ascii=False, default=str)
            if status != "success"
            else _summarize_result(result)
        )
        return RunAgentResponse(
            session_id=session_id,
            status="completed" if status == "success" else "error",
            result=output,
            error=error,
        )
    except Exception as e:
        return RunAgentResponse(
            session_id=req.session_id or "", status="error", result=None, error=str(e)
        )


class RunStreamRequest(BaseModel):
    persona: str = "build"
    text: str
    project: str | None = None
    session_id: str | None = None


class RunStreamResponse(BaseModel):
    session_id: str
    stream_url: str
    status: str = "started"


@router.post("/run-stream", response_model=RunStreamResponse)
async def run_stream(req: RunStreamRequest):
    """G6 闭环:后台执行 agent + SSE 流(发起任务 → 接受 SSE 流)。

    返回 session_id,扩展随即 GET ``/stream/{session_id}`` 消费事件;
    执行在后台 task 中,``on_step`` 经 SSEQueue 桥接为 ``data: {{...}}\n\n``。
    """
    import asyncio as _asyncio
    import uuid as _uuid

    from server.coordinator import Coordinator
    from server.sse import get_or_create_queue

    session_id = req.session_id or _uuid.uuid4().hex
    queue = get_or_create_queue(session_id)
    # enable_streaming=False:SSE 闭环走 fire_step 通道(/stream 队列),
    # 避免 StreamingManager 独立事件队列与 on_step 双通道不一致(G6)。
    coordinator = Coordinator(enable_streaming=False)

    async def _run() -> None:
        try:
            result = await coordinator.handle(
                {"text": req.text, "persona": req.persona, "project": req.project or "."},
                session_id=session_id,
                on_step=queue.on_step,
            )
            # 终结事件:结构化汇总(SSEQueue 的 on_step 不丢事件)
            queue.on_step(
                {"type": "task_done", "session_id": session_id, "result": _summarize_result(result)}
            )
        except Exception as exc:
            queue.on_step({"type": "task_error", "session_id": session_id, "error": str(exc)})
        finally:
            queue.close()

    # 后台任务引用保存在模块级,避免 GC 提前回收(RUF006)
    _BG_TASKS.add(_asyncio.create_task(_run()))
    return RunStreamResponse(session_id=session_id, stream_url=f"/stream/{session_id}")


# 后台任务强引用集合(任务结束后自清理)
_BG_TASKS: set = set()


def _summarize_result(result: dict) -> str:
    """把结构化结果压成人类可读摘要。"""
    lines: list[str] = []
    for squad in result.get("squads", []):
        role = squad.get("role", "?")
        status = squad.get("status", "?")
        out = squad.get("output")
        if isinstance(out, dict):
            out = out.get("content") or out.get("error") or ""
        lines.append(f"[{role}::{status}] {str(out or '')[:200]}")
    cost = result.get("cost_usd", 0.0)
    lines.append(f"cost: ${cost:.4f}")
    return "\n".join(lines)


@router.get("/session/{session_id}")
async def get_session(session_id: str):
    """Get session details for VS Code(基于协调器流式/上下文管理器)。"""
    try:
        from server.coordinator import Coordinator

        coordinator = Coordinator()
        context = coordinator.context_managers.get(session_id)
        stream = coordinator.streaming_managers.get(session_id)
        history = context.get_history() if context else []
        return {
            "session_id": session_id,
            "status": "active" if stream else "idle",
            "messages": history,
            "persona": "",
            "project": "",
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Session not found: {e!s}")


@router.post("/debug/start")
async def start_debug_session(session_id: str):
    """Start a debug session for VS Code"""
    try:
        # In a real implementation, this would set up pdb/debugpy
        # For now, return mock debug info
        return DebugSession(
            session_id=session_id,
            breakpoints=[],
            variables={"session_id": session_id, "status": "debugging", "frame": 0},
            stack_trace=[{"file": "hicode/agents/plan.py", "line": 42, "function": "run"}],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start debug: {e!s}")


@router.post("/debug/breakpoint")
async def add_breakpoint(session_id: str, file: str, line: int, condition: str | None = None):
    """Add a breakpoint in VS Code"""
    try:
        # In a real implementation, this would interact with debugpy
        return {
            "session_id": session_id,
            "breakpoint": {"file": file, "line": line, "condition": condition, "enabled": True},
            "status": "added",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add breakpoint: {e!s}")


@router.get("/projects")
async def list_vscode_projects():
    """List projects accessible from VS Code"""
    try:
        import os
        from pathlib import Path

        # Scan common project locations
        project_dirs = []
        home = Path.home()
        workspace = os.environ.get("HICODE_WORKSPACE", home / "projects")

        if workspace.exists():
            for item in workspace.iterdir():
                if item.is_dir() and (item / ".git").exists():
                    project_dirs.append({"name": item.name, "path": str(item), "type": "git"})

        return {"projects": project_dirs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list projects: {e!s}")


@router.get("/workspace-status")
async def get_workspace_status(project_path: str):
    """Get Git workspace status for VS Code"""
    try:
        import subprocess
        from pathlib import Path

        path = Path(project_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Project path not found")

        # Get Git status
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True, timeout=10
        )

        changes = []
        for line in result.stdout.strip().split("\n"):
            if line:
                status = line[:2]
                file = line[3:]
                changes.append({"status": status, "file": file})

        # Get current branch
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True, timeout=5
        )

        return {
            "path": project_path,
            "branch": branch_result.stdout.strip(),
            "changes": changes,
            "dirty": len(changes) > 0,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Git operation timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get workspace status: {e!s}")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@router.post("/chat")
async def vscode_chat(req: ChatRequest):
    """Simple chat interface for VS Code sidebar(真实 coordinator.handle)。"""
    try:
        from server.coordinator import Coordinator

        coordinator = Coordinator()
        result = await coordinator.handle(
            {"text": req.message, "persona": "build", "project": "."},
            session_id=req.session_id,
        )
        return {
            "session_id": result.get("session_id", req.session_id or ""),
            "response": _summarize_result(result),
            "status": result.get("status", "completed"),
        }
    except Exception as e:
        return {"session_id": req.session_id or "", "response": f"ERROR: {e!s}", "status": "error"}
        raise HTTPException(status_code=500, detail=f"Chat error: {e!s}")
