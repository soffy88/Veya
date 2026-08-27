"""Plan board REST — 计划看板数据源 (前端 PlanBoard 页面)。

只读端点: 列出所有计划 + 详情 (todos/evidence/spends), 附 quota 摘要。
数据复用 plan_todo 的 JSON (单一真相源), 不重造状态。主脑零改动。
"""

from __future__ import annotations

import glob
import json
import os

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["plan-board"])


def _plans_dir() -> str:
    from server.plan_todo import _plans_dir as _pd

    return str(_pd())


async def _quota_summary(plan: dict) -> dict:
    """quota 摘要 (前端状态行): 该不该动 + 原因。"""
    from server.state_kernel import quota_should_run

    try:
        raw = await quota_should_run(plan.get("plan_id", ""))
        return json.loads(raw)
    except Exception:
        return {"should_run": None, "action": "unknown", "reason": ""}


@router.get("/api/v1/plan/list")
async def plan_list() -> dict:
    """列出所有计划 (最新在前) + quota 摘要。"""
    plans: list[dict] = []
    try:
        files = sorted(
            glob.glob(os.path.join(_plans_dir(), "*.json")), key=os.path.getmtime, reverse=True
        )
    except OSError:
        files = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fp:
                plan = json.load(fp)
        except Exception:
            continue
        todos = plan.get("todos", [])
        done = sum(1 for t in todos if t.get("status") == "done")
        plans.append(
            {
                "plan_id": plan.get("plan_id"),
                "objective": plan.get("objective", ""),
                "updated_at": plan.get("updated_at", ""),
                "progress": {"done": done, "total": len(todos)},
                "todos": [
                    {
                        "id": t.get("id"),
                        "title": t.get("title", ""),
                        "status": t.get("status", "open"),
                        "depends_on": t.get("depends_on", []),
                        "assignee": t.get("assignee"),
                        "claim": t.get("claim"),
                        "evidence": t.get("evidence", [])[-3:],
                    }
                    for t in todos
                ],
                "spends": len(plan.get("spends", [])),
                "quota": await _quota_summary(plan),
            }
        )
    return {"plans": plans, "total": len(plans)}


@router.get("/api/v1/plan/{plan_id}")
async def plan_detail(plan_id: str) -> dict:
    """单个计划详情 (含完整证据链)。"""
    from server.plan_todo import _path

    p = _path(plan_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"计划不存在: {plan_id}")
    try:
        with p.open(encoding="utf-8") as fp:
            plan = json.load(fp)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"计划读取失败: {exc}")
    plan["quota"] = await _quota_summary(plan)
    return plan
