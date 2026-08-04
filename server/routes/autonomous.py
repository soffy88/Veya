"""
自主 AI 代理 API - P3 核心能力
提供自主规划、记忆系统、自我改进等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from veya.autonomous_agent import create_autonomous_agent

router = APIRouter(prefix="/autonomous", tags=["autonomous"])

# 全局自主 AI 代理（实际应用中应根据用户创建多个实例）
autonomous_agent = create_autonomous_agent()


class PlanGoalRequest(BaseModel):
    goal: str  # code_generation, problem_solving, system_design, code_review, learning
    description: str
    context: dict[str, Any] | None = None


class StoreMemoryRequest(BaseModel):
    content: str
    tags: list[str]


class ExecutePlanRequest(BaseModel):
    plan_id: str


@router.post("/plan")
async def plan_goal(request: PlanGoalRequest) -> dict[str, Any]:
    """为目标制定规划"""
    try:
        from veya.autonomous_agent import AgentGoal

        # 转换目标字符串为枚举
        goal_map = {
            "code_generation": AgentGoal.CODE_GENERATION,
            "problem_solving": AgentGoal.PROBLEM_SOLVING,
            "system_design": AgentGoal.SYSTEM_DESIGN,
            "code_review": AgentGoal.CODE_REVIEW,
            "learning": AgentGoal.LEARNING,
        }

        goal = goal_map.get(request.goal.lower())
        if not goal:
            raise HTTPException(status_code=400, detail=f"Invalid goal: {request.goal}")

        steps = autonomous_agent.plan_goal(goal, request.description, request.context or {})

        # 获取第一个 plan_id
        plan_id = next(iter(autonomous_agent.plans.keys())) if autonomous_agent.plans else "unknown"

        return {
            "status": "success",
            "plan_id": plan_id,
            "steps": [
                {
                    "step_id": step.step_id,
                    "description": step.description,
                    "action": step.action,
                    "estimated_time": step.estimated_time,
                }
                for step in steps
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Planning failed: {e!s}")


@router.post("/memory")
async def store_memory(request: StoreMemoryRequest) -> dict[str, Any]:
    """存储记忆"""
    try:
        memory_id = autonomous_agent.store_memory(request.content, request.tags)
        return {
            "status": "success",
            "memory_id": memory_id,
            "message": "Memory stored successfully",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory storage failed: {e!s}")


@router.get("/memory/search")
async def search_memory(query: str, limit: int = 5) -> dict[str, Any]:
    """搜索记忆"""
    try:
        memories = autonomous_agent.retrieve_memory(query, limit)
        return {
            "status": "success",
            "count": len(memories),
            "memories": [
                {
                    "memory_id": m.memory_id,
                    "content": m.content[:200] + "..." if len(m.content) > 200 else m.content,
                    "tags": m.tags,
                    "access_count": m.access_count,
                    "confidence": m.confidence,
                }
                for m in memories
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory search failed: {e!s}")


@router.post("/execute")
async def execute_plan(request: ExecutePlanRequest) -> dict[str, Any]:
    """执行规划"""
    try:
        result = autonomous_agent.execute_plan(request.plan_id)
        return {"status": "success", "execution_result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Execution failed: {e!s}")


@router.post("/evaluate")
async def evaluate_agent() -> dict[str, Any]:
    """自我评估"""
    try:
        evaluation = autonomous_agent.self_evaluate()
        return {"status": "success", "evaluation": evaluation.__dict__}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {e!s}")


@router.post("/learn")
async def learn_from_experience(experience: dict[str, Any]) -> dict[str, Any]:
    """从经验中学习"""
    try:
        learned = autonomous_agent.learn_from_experience(experience)
        return {
            "status": "success",
            "learned_patterns": learned,
            "message": f"Learned {len(learned)} patterns",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Learning failed: {e!s}")


@router.get("/stats")
async def get_agent_stats() -> dict[str, Any]:
    """获取代理统计"""
    try:
        stats = autonomous_agent.get_stats()
        return {"status": "success", "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {e!s}")
