"""loop-plane main — FastAPI 入口（:8787）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import exec as exec_api
from app.api import goals as goals_api
from app.api import health as health_api
from app.api import plan as plan_api
from app.api import sched as sched_api
from app.api import skills as skills_api
from app.config import Settings
from app.deps import configure
from app.domain.exec.service import ExecService
from app.domain.sched.service import SchedService
from app.domain.state.service import GoalService


def build_app(settings: Settings | None = None) -> FastAPI:
    """构造 app（测试/生产共用；settings 可注入临时数据目录）。"""
    effective = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure(effective)
        app.state.goal_service = GoalService(_store(app))
        app.state.plan_service = _plan_service()
        app.state.exec_service = ExecService(workspace=effective.workspace)
        app.state.sched_service = SchedService(app.state.goal_service)
        yield

    app = FastAPI(title="loop-plane", version="1.0", lifespan=lifespan)
    app.include_router(health_api.router)
    app.include_router(goals_api.router)
    app.include_router(plan_api.router)
    app.include_router(exec_api.router)
    app.include_router(sched_api.router)
    app.include_router(skills_api.router)
    return app


def _store(app: FastAPI):
    from app.deps import get_store

    return get_store()


def _plan_service():
    from app.deps import get_audit, get_store
    from app.domain.causal.service import CausalService

    return CausalService(store=get_store(), audit=get_audit())


app = build_app()
