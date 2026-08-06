"""Threat-model route — Phase 3 威胁模型闭环演化 API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.notification_center import global_notifier
from server.threat_model import VeyaThreatModel

router = APIRouter(prefix="/threat-model", tags=["threat-model"])


class EvolveRequest(BaseModel):
    signals: list[dict[str, Any]]                # [{"kind", "severity"}]
    prior: list[float] | None = None
    quarantine_threshold: float = 0.7
    profile_path: str | None = None           # 持久化画像(读旧先验/写新后验)
    entity: str = "default"


@router.post("/evolve")
async def threat_model_evolve(req: EvolveRequest) -> dict[str, Any]:
    """蜜罐敌对信号 → Bayesian ToM 后验更新 → 隔离决策 → 威胁画像持久化。"""
    if not req.signals:
        raise HTTPException(status_code=422, detail="signals 不能为空")
    model = VeyaThreatModel()
    result = await model.evolve(
        req.signals,
        prior=req.prior,
        quarantine_threshold=req.quarantine_threshold,
        profile_path=req.profile_path,
        entity=req.entity,
    )
    if result["quarantined"]:
        global_notifier.push(
            "HITL_REQUIRED",
            "🚨 威胁实体已隔离",
            f"{req.entity}: P(hostile)={result['hostile_prob']:.2f} ≥ "
            f"{req.quarantine_threshold}, 已自动 quarantined",
            {"entity": req.entity, "posterior": result["posterior"]},
        )
    else:
        global_notifier.push(
            "INFO", f"威胁监控: {req.entity}",
            f"P(hostile)={result['hostile_prob']:.2f} (阈值 {req.quarantine_threshold})", {},
        )
    return result
