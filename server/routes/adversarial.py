"""Adversarial route — 红蓝对抗审判庭 API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.adversarial_chamber import VeyaAdversarialChamber
from server.notification_center import global_notifier

router = APIRouter(prefix="/adversarial", tags=["adversarial"])


class AdversarialReviewRequest(BaseModel):
    strategy_code: str
    strategy_name: str = "unnamed_strategy"
    context: str = ""


@router.post("/review")
async def adversarial_review(req: AdversarialReviewRequest) -> dict[str, Any]:
    """红蓝对抗审判: 蓝队辩护 → 红队质疑 → 主脑裁决 → 《红蓝对抗审计报告》。

    通知中心同步推送 SUCCESS 摘要 (红队 N 处风险 / 安全系数 x → y).
    """
    if not req.strategy_code.strip():
        raise HTTPException(status_code=422, detail="strategy_code 不能为空")

    chamber = VeyaAdversarialChamber()
    result = await chamber.review(
        strategy_code=req.strategy_code,
        strategy_name=req.strategy_name,
        context=req.context,
    )

    global_notifier.push(
        type="SUCCESS",
        title=f"红蓝对抗审计完成: {req.strategy_name}",
        content=(
            f"裁决 {result['status']} | 红队指出 {result['red_points']} 处风险, "
            f"安全系数 {result['safety_score_before']} → {result['safety_score_after']}"
        ),
        payload={
            "strategy_name": req.strategy_name,
            "verdict": result["status"],
            "safety_score_before": result["safety_score_before"],
            "safety_score_after": result["safety_score_after"],
            "report_path": str(result["report_path"]),
            "fingerprint": result.get("fingerprint"),
        },
    )
    return result
