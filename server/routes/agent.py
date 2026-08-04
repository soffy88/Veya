"""Agent management route: persona switching and one-shot agent invocation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents import resolve_persona

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentInvokeRequest(BaseModel):
    text: str
    persona: str = "build"
    session_id: str = ""
    config: dict[str, Any] = {}


@router.post("/invoke")
async def invoke_agent(req: AgentInvokeRequest) -> dict[str, Any]:
    from server.assembly import assemble_main_agent

    try:
        resolve_persona(req.persona)  # validates persona
    except Exception:
        raise HTTPException(status_code=400, detail=f"Unknown persona: {req.persona!r}")

    engine = assemble_main_agent(persona=req.persona, session_ctx=req.config)
    messages = [{"role": "user", "content": req.text}]
    result = await engine.run_turn(messages)
    return {
        "status": result.get("status", "completed"),
        "output": result.get("turn_result"),
        "cost_usd": result.get("cost_usd", 0.0),
        "persona": req.persona,
    }


@router.get("/personas")
async def list_personas() -> dict[str, Any]:
    from agents import resolve_persona as _rp

    personas = []
    for name in ("build", "plan", "research"):
        p = _rp(name)
        personas.append({"name": p.name, "tools": p.tool_names, "mode": p.mode})
    return {"personas": personas}
