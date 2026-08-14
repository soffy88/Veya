"""loop-plane api.skills — Skills API（SPEC §4.6，P2 stub，路由先定）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/v1/loop/skills", tags=["skills"])


@router.post("", status_code=201)
async def register_skill() -> dict[str, Any]:
    raise HTTPException(status_code=501, detail="Skills P2 stub — 接口已定, 未实现")


@router.post("/{skill_id}/experiments", status_code=202)
async def start_experiment(skill_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=501, detail="Skills P2 stub — 接口已定, 未实现")


@router.post("/experiments/{experiment_id}/optimize")
async def optimize_experiment(experiment_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=501, detail="Skills P2 stub — 接口已定, 未实现")


@router.post("/{skill_id}/release")
async def release_skill(skill_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=501, detail="Skills P2 stub — 接口已定, 未实现")


@router.post("/{skill_id}/rollback")
async def rollback_skill(skill_id: str) -> dict[str, Any]:
    raise HTTPException(status_code=501, detail="Skills P2 stub — 接口已定, 未实现")


__all__ = ["router"]
