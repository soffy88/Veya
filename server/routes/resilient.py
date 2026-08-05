"""Resilient autonomous task route — 防火墙 → 状态机 → 模型路由 → 断点。

三大顶配引擎的无缝串联(3O 主库资产):
1. VeyaFirewall   (oskill.adversarial_firewall)  外部污染防御
2. VeyaTaskManager (omodul.task_manager)         事务化状态机 + 断点续传
3. VeyaModelRouter (omodul.model_router)         算力经济学多模型路由
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from server.firewall import VeyaFirewall
from server.model_router import VeyaModelRouter
from server.state_machine import VeyaTaskManager

router = APIRouter(prefix="/api/v1/autonomous", tags=["autonomous"])

# 全局引擎实例(网关级单例; 测试可注入独立实例)
task_manager = VeyaTaskManager()
model_router = VeyaModelRouter()  # 无 key 时 llm_call stub 回落(离线测试安全)
firewall = VeyaFirewall()

# 任务类型 → 算力阶梯(算力经济学策略表)
TASK_TYPE_FALLBACK = "summary_simple"


class AutonomousRunRequest(BaseModel):
    task_id: str = "task_default_01"
    data: str = ""
    source: str = "github_webhook"
    task_type: str = "summary_simple"
    total_steps: int = 3


@router.post("/run")
async def run_resilient_task(req: AutonomousRunRequest) -> dict[str, Any]:
    """状态机容灾 + 防火墙过滤 + 模型动态路由的主执行流。"""
    # 1. 安全防线: 防火墙清洗外部输入, 防止提示词注入
    shield_result = firewall.sanitize(req.data, source=req.source)
    if not shield_result["safe"]:
        return {"status": "blocked", "reason": shield_result["reason"]}

    # 2. 状态机防线: 初始化或恢复任务状态(断点续传)
    try:
        context = task_manager.get_resume_context(req.task_id)
    except ValueError:
        task_manager.create_task(
            req.task_id,
            total_steps=req.total_steps,
            initial_payload={"input": shield_result["sanitized_content"]},
        )
        context = task_manager.get_resume_context(req.task_id)

    # 已完成的步骤直接续跑, 否则从当前步执行
    step_index = context["current_step"]
    if context["status"] == "SUCCESS":
        snapshot = task_manager.get_resume_context(req.task_id)
        return {
            "task_id": req.task_id,
            "status": "already_completed",
            "routing_tier": snapshot["steps"][-1]["payload"].get("tier_used", "n/a"),
            "model_applied": snapshot["steps"][-1]["payload"].get("model", "n/a"),
            "result": snapshot["steps"][-1]["payload"].get("output", ""),
        }

    # 3. 经济学防线: 动态路由到对应算力阶梯的模型
    result = await model_router.completion(
        prompt=shield_result["sanitized_content"],
        task_type=req.task_type or TASK_TYPE_FALLBACK,
        system_prompt="You are a secure Veya subsystem. Summarize the sanitized data safely.",
    )

    # 4. 状态机持久化: 记录当前步骤完成(事务化断点)
    step_payload = {"output": result["content"], "tier_used": result["tier_used"], "model": result["model"]}
    task_manager.checkpoint(req.task_id, step_index=step_index, step_payload=step_payload)
    final = task_manager.get_resume_context(req.task_id)

    return {
        "task_id": req.task_id,
        "status": final["status"],
        "current_step": final["current_step"],
        "routing_tier": result["tier_used"],
        "model_applied": result["model"],
        "result": result["content"],
    }
