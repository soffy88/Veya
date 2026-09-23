"""Coordinator -> approval -> Genesis HITL flow: phase1 (propose) / phase2 (map) / phase3 (forge+assemble)."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import flow_engine
from server.flow_goal_run import GenesisGoalRunAdapter, phase3_task
from server.goal_run import project_run_goal
from server.schemas import GenesisManifest, RequirementDoc

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
    try:
        doc = await flow_engine.propose_requirement(
            req.prompt,
            session_id=sid,
            model=req.model,
            provider=req.provider,
            config=req.config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "proposed",
        "session_id": sid,
        "execution_plane": "workflow",
        "requirement_doc": doc.model_dump(),
    }


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
    project_root = str(req.config.get("project_root") or os.environ.get("VEYA_PROJECT_ROOT") or ".")

    async def _run_durable() -> None:
        await project_run_goal(
            project_root=project_root,
            goal=f"Genesis workflow {req.manifest.mission_id}",
            tasks=[phase3_task(req.manifest)],
            mode="act_eager",
            max_wall_s=7200,
            integration_adapter=GenesisGoalRunAdapter(
                req.manifest, config=req.config, project_root=project_root
            ),
        )

    task = asyncio.create_task(
        _run_durable(),
        name=f"veya-flow-phase3-{req.manifest.mission_id}",
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {
        "status": "started",
        "execution_plane": "workflow",
        "durability": "goal_run",
        "session_id": req.session_id,
        "mission_id": req.manifest.mission_id,
        "project_root": project_root,
    }
