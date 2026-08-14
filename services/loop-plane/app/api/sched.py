"""loop-plane api.sched — Sched 门面（SPEC §4.5）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.domain.sched.service import SchedService
from app.schemas import SchedJobBody

router = APIRouter(prefix="/v1/loop/sched", tags=["sched"])


def _service(request: Request) -> SchedService:
    svc: SchedService | None = getattr(request.app.state, "sched_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="sched service 未初始化")
    return svc


@router.post("/jobs", status_code=201)
async def register_job(body: SchedJobBody, svc: SchedService = Depends(_service)) -> dict[str, Any]:
    return svc.register(body.name, cron=body.cron, pattern=body.pattern, action=body.action)


@router.get("/jobs")
async def list_jobs(svc: SchedService = Depends(_service)) -> dict[str, Any]:
    return {"jobs": svc.list_jobs(), "count": len(svc.list_jobs())}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, svc: SchedService = Depends(_service)) -> dict[str, Any]:
    if not svc.delete(job_id):
        raise HTTPException(status_code=404, detail=f"job {job_id!r} 不存在")
    return {"deleted": job_id}


@router.post("/jobs/{job_id}/trigger")
async def trigger_job(job_id: str, svc: SchedService = Depends(_service)) -> dict[str, Any]:
    try:
        return svc.trigger(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


__all__ = ["router"]
