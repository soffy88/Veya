"""Closed-loop route — Phase 3 反脆弱闭环事务 API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.closed_loop import VeyaClosedLoop
from server.notification_center import global_notifier

router = APIRouter(prefix="/closed-loop", tags=["closed-loop"])


class InterveneRequest(BaseModel):
    cpd: dict[str, Any]  # 当前因果模型 (child_states/counts/parents)
    interventions: list[dict[str, Any]] | None = None
    diagnosis: dict[str, Any] | None = None  # Phase 2 诊断报告 (自动转换候选)
    baseline_config: str = "degraded"
    fault_state: str = "fault"
    lambda_cost: float = 1.0
    risk_aversion: float = 1.0
    update_mode: str = "dirichlet"  # dirichlet | ema
    rounds: int = 1


@router.post("/intervene")
async def closed_loop_intervene(req: InterveneRequest) -> dict[str, Any]:
    """感知-决策-行动-学习闭环: 效用选择 → 执行/模拟 → 观测回灌 CPD。

    返回 cpd_after —— 调用方应持久化, 作为下一次请求的 cpd 输入(在线积累)。
    """
    if not req.cpd.get("child_states"):
        raise HTTPException(status_code=422, detail="cpd 需要 child_states")
    chamber = VeyaClosedLoop()
    result = await chamber.run(
        req.cpd,
        interventions=req.interventions,
        diagnosis=req.diagnosis,
        baseline_config=req.baseline_config,
        fault_state=req.fault_state,
        lambda_cost=req.lambda_cost,
        risk_aversion=req.risk_aversion,
        update_mode=req.update_mode,
        rounds=req.rounds,
    )
    if result["status"] == "executed":
        global_notifier.push(
            "SUCCESS",
            "🎯 闭环干预完成",
            f"执行 {result['success_count']} 次成功 / {result['failure_count']} 次失败, "
            f"失败率 {result['executed_failure_rate']:.1%}",
            {"fingerprint": result["fingerprint"], "cpd_path": result.get("cpd_path")},
        )
    else:
        global_notifier.push(
            "INFO",
            "闭环诊断: 无需干预",
            "无正效用干预动作(不输出 least-bad), 系统保持监控",
            {},
        )
    return result
