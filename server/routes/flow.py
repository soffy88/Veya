"""Coordinator -> approval -> Genesis HITL flow: phase1 (propose) / phase2 (map) / phase3 (forge+assemble)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import flow_engine
from server.coordinator import coordinator
from server.schemas import GenesisManifest, RequirementDoc
from server.sse import get_or_create_queue

router = APIRouter()

# asyncio only holds a weak reference to a bare create_task() result — without a strong
# reference kept somewhere, the task can be GC'd mid-execution. Keep one here.
_background_tasks: set[asyncio.Task] = set()


class Phase1Request(BaseModel):
    prompt: str
    session_id: str | None = None
    project_path: str = "."
    model: str | None = None
    provider: str | None = None
    config: dict[str, Any] = {}


class Phase2Request(BaseModel):
    doc: RequirementDoc
    session_id: str
    model: str | None = None
    provider: str | None = None
    config: dict[str, Any] = {}


class Phase3Request(BaseModel):
    manifest: GenesisManifest
    session_id: str
    config: dict[str, Any] = {}


@router.post("/flow/phase1")
async def flow_phase1(req: Phase1Request) -> dict[str, Any]:
    sid = req.session_id or str(uuid.uuid4())
    queue = get_or_create_queue(sid)

    command = {
        "mode": "requirement",
        "text": req.prompt,
        "project_path": req.project_path,
        "model": req.model,
        "provider": req.provider,
        "config": req.config,
    }
    result = await coordinator.handle(command, session_id=sid, on_step=queue.on_step)
    return result


@router.post("/flow/phase2")
async def flow_phase2(req: Phase2Request) -> dict[str, Any]:
    try:
        manifest = await flow_engine.propose_manifest(
            req.doc,
            session_id=req.session_id,
            model=req.model,
            provider=req.provider,
            config=req.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"session_id": req.session_id, "manifest": manifest.model_dump()}


@router.post("/flow/phase3")
async def flow_phase3(req: Phase3Request) -> dict[str, Any]:
    task = asyncio.create_task(
        flow_engine.run_phase3(req.manifest, session_id=req.session_id, config=req.config)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "started", "session_id": req.session_id}
