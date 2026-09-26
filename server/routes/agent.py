"""Agent management route: persona switching and one-shot agent invocation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(auth_mod.require_user)])


class AgentInvokeRequest(BaseModel):
    text: str
    persona: str = "build"
    session_id: str = ""
    config: dict[str, Any] = {}


@router.post("/invoke")
async def invoke_agent(req: AgentInvokeRequest) -> dict[str, Any]:
    from agents import resolve_persona
    from server.coordinator_master import master_coordinator

    try:
        resolve_persona(req.persona)  # validate the typed compatibility field
    except Exception:
        raise HTTPException(status_code=400, detail=f"Unknown persona: {req.persona!r}")

    result = await master_coordinator.chat_stream(
        req.text,
        session_id=req.session_id or None,
        config=req.config or None,
        system_context=f"Requested persona: {req.persona}",
    )
    return {
        "status": result.get("status", "completed"),
        "output": result.get("final_answer") or result.get("error", ""),
        "cost_usd": result.get("cost_usd", 0.0),
        "persona": req.persona,
        "session_id": result.get("session_id") or req.session_id or "",
    }


@router.get("/personas")
async def list_personas() -> dict[str, Any]:
    from agents import resolve_persona as _rp

    personas = []
    for name in ("build", "plan", "research"):
        p = _rp(name)
        personas.append({"name": p.name, "tools": p.tool_names, "mode": p.mode})
    return {"personas": personas}
