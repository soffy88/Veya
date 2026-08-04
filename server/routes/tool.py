"""Tool execution route: POST /tool"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.assembly import _ALL_TOOLS

router = APIRouter(prefix="/tool", tags=["tool"])

# Tools that write to disk — snapshot these before execution for undo support
_WRITE_TOOLS = {"write", "edit", "bash"}


class ToolRequest(BaseModel):
    name: str
    args: dict[str, Any] = {}
    persona: str = "build"
    session_id: str | None = None  # optional; enables file undo via /session/{id}/undo


@router.post("")
async def execute_tool_route(req: ToolRequest) -> dict[str, Any]:
    from hooks.registry import run_hook_chain
    from hooks.types import HookInput

    fn = _ALL_TOOLS.get(req.name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"Tool '{req.name}' not found")

    # H2 permission check
    hook_inp = HookInput(
        point="pre_tool",
        tool_call={"name": req.name, "args": req.args},
        persona=req.persona,
    )
    decision, reason, _ = await run_hook_chain("pre_tool", req.persona, hook_inp)
    if decision == "block":
        raise HTTPException(status_code=403, detail=reason)

    # Snapshot files before write/edit operations so undo can restore them
    if req.session_id and req.name in _WRITE_TOOLS:
        from server.routes.session import open_undo_turn, push_file_snapshot

        open_undo_turn(req.session_id)
        path = req.args.get("path")
        if path:
            push_file_snapshot(req.session_id, path)

    try:
        result = fn(**req.args)
        if asyncio.iscoroutine(result):
            result = await result
        result_str = str(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    # H3 post_tool (output redact)
    hook_out_inp = HookInput(point="post_tool", result=result_str, persona=req.persona)
    _, _, modified = await run_hook_chain("post_tool", req.persona, hook_out_inp)

    return {"status": "success", "result": modified or result_str}


@router.get("")
async def list_tools() -> dict[str, Any]:
    return {"tools": list(_ALL_TOOLS.keys())}
