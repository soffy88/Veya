"""General-purpose chat route — hosts the Artifacts protocol (server/chat_coordinator.py)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from server import chat_coordinator

router = APIRouter()


class ChatRequest(BaseModel):
    text: str
    session_id: str | None = None
    model: str | None = None
    provider: str | None = None
    config: dict[str, Any] = {}


@router.post("/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    sid = req.session_id or str(uuid.uuid4())
    result = await chat_coordinator.chat(
        req.text,
        session_id=sid,
        model=req.model,
        provider=req.provider,
        config=req.config,
    )
    result["session_id"] = sid
    return result
