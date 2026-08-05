"""Automata routes — 后台自动化任务管理 + 外部事件 Webhook。

Agent OS 的对外接口:
- GET  /automata/jobs            查看后台任务
- POST /automata/jobs            手动注册 Cron 任务(前端/调试)
- POST /automata/run-now         立即触发一次无头执行(测试/手动)
- POST /webhooks/{source}        外部系统事件入口(Github / CI / Webhook)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from server.automata import get_automata

logger = logging.getLogger("automata.routes")
router = APIRouter(prefix="/automata", tags=["automata"])


class CronJobRequest(BaseModel):
    cron_expr: str
    task_prompt: str
    task_id: str | None = None


class RunNowRequest(BaseModel):
    task_prompt: str
    trigger: str = "manual"


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    automata = get_automata()
    return {"jobs": automata.get_jobs(), "recent_results": automata.get_recent_results(5)}


@router.post("/jobs")
async def create_job(req: CronJobRequest) -> dict[str, Any]:
    automata = get_automata()
    try:
        msg = automata.register_cron_task(req.cron_expr, req.task_prompt, task_id=req.task_id)
        return {"status": "ok", "message": msg}
    except ValueError as exc:
        return {"status": "failed", "error": str(exc)}


@router.delete("/jobs/{task_id}")
async def remove_job(task_id: str) -> dict[str, Any]:
    automata = get_automata()
    return {"status": "ok", "message": automata.remove_task(task_id)}


@router.post("/run-now")
async def run_now(req: RunNowRequest) -> dict[str, Any]:
    """立即触发一次无头执行(不等 Cron), 结果异步落盘留痕。"""
    automata = get_automata()
    await automata._run_headless_mission(f"[MANUAL TRIGGER: {req.trigger}]", req.task_prompt)
    return {"status": "ok", "message": "后台任务已执行"}


@router.post("/webhooks/{source}")
async def webhook_event(source: str, payload: dict) -> dict[str, Any]:
    """外部系统 (Webhook, Github, CI) 的事件入口 → 立即后台异步处理。"""
    automata = get_automata()
    automata.trigger_event(event_name=source, payload=payload)
    return {"status": "Event received, Veya is investigating in the background."}


# 兼容脚手架路径: /api/v1/webhooks/github 形态(挂到 app 时通过 include 前缀)
webhook_router = APIRouter(tags=["webhooks"])


@webhook_router.post("/api/v1/webhooks/{source}")
async def webhook_event_compat(source: str, payload: dict) -> dict[str, Any]:
    return await webhook_event(source, payload)
