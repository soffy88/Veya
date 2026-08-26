"""server.routes.cindy_compat — Cindy 兼容端点 (插件市场 / 定时任务 / 知识库 / MCP / Skill)。

背景: veya L4 gateway (veya/server/app.py) 原生提供这些 /api/v1 端点, 但线上
Agent OS 主 app (server/app.py) 的 Caddy 反代把 /api/v1/* 打到本 app ——
根 app 缺这些路由 → 前端「插件市场 / 定时任务」404 加载失败。

本模块照 legacy_agent 兼容模式, 把这些端点以自包含 router 挂到根 app,
使两条入口 (L4 gateway 与 Agent OS 主 app) 具备一致的能力面。

3O 主库元素全部惰性导入 (oskill/obase/omodul 由装配器注入 sys.path)。
"""

from __future__ import annotations

import contextlib
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server import auth as auth_mod

router = APIRouter(tags=["cindy-compat"])


# =========================================================================
# 请求模型 (与 veya/server/app.py 保持一致)
# =========================================================================

class SchedulerRequest(BaseModel):
    """Scheduler management request."""
    action: Literal["create", "list", "toggle", "delete"] = "list"
    id: str = ""
    name: str = ""
    prompt: str = ""
    cron: str = ""
    interval_ms: int = 0
    enabled: bool = True


class KnowledgeRequest(BaseModel):
    """Knowledge store request."""
    action: Literal["read", "write", "list", "stale", "delete", "skeleton"] = "list"
    id: str = ""
    type: str = "module"
    body: str = ""
    covers: list[str] = Field(default_factory=list)


class PluginActionRequest(BaseModel):
    action: Literal["install", "uninstall", "list", "toggle", "configure", "publish", "marketplace"] = "list"
    name: str = ""
    version: str = "1.0.0"
    capabilities: list[str] = Field(default_factory=list)
    source: str = "local"
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    author: str = ""
    tags: list[str] = Field(default_factory=list)


class SkillTeachRequest(BaseModel):
    """Skill teaching request."""
    description: str = Field(..., min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class SkillConfirmRequest(BaseModel):
    """Confirm a proposed skill candidate (P2-05 two-phase flow)."""
    skill_id: str = Field(..., min_length=1)


class SkillRejectRequest(BaseModel):
    """Reject a proposed skill candidate."""
    skill_id: str = Field(..., min_length=1)


# =========================================================================
# POST /api/v1/scheduler — 定时任务 (Cindy scheduler CRUD)
# =========================================================================

@router.post("/api/v1/scheduler")
async def scheduler_ep(req: SchedulerRequest) -> dict[str, Any]:
    from oskill.recurring_scheduler import RecurringScheduler

    sched = RecurringScheduler()
    if req.action == "list":
        return {"schedules": [
            {"id": s.id, "name": s.name, "enabled": s.enabled,
             "phase": s.phase, "run_count": s.run_count}
            for s in sched.list_all()
        ]}
    if req.action == "create":
        s = sched.create(
            req.id or f"sched_{len(sched.list_all())}",
            req.name or req.id,
            req.prompt,
            req.cron,
            req.interval_ms,
        )
        return {"status": "created", "id": s.id}
    if req.action == "toggle":
        ok = sched.update(req.id, enabled=req.enabled) is not None
        return {"status": "toggled" if ok else "not_found", "id": req.id}
    if req.action == "delete":
        return {"status": "deleted" if sched.delete(req.id) else "not_found"}
    return {"status": "failed", "error": f"unknown action: {req.action}"}


# =========================================================================
# POST /api/v1/plugin/manage — 插件市场
# =========================================================================

@router.post("/api/v1/plugin/manage")
async def plugin_manage(req: PluginActionRequest) -> dict[str, Any]:
    from veya.platform import load as _load_3o

    _load_3o("obase")
    from obase.plugin_registry import PluginRegistry

    reg = PluginRegistry()
    if req.action == "install":
        return reg.install(req.name, req.version, req.capabilities, req.source)
    if req.action == "uninstall":
        return reg.uninstall(req.name)
    if req.action == "list":
        return {"plugins": reg.list_installed(), "count": reg.count()}
    if req.action == "toggle":
        return reg.toggle(req.name, req.enabled)
    if req.action == "configure":
        return reg.configure(req.name, req.config)
    if req.action == "publish":
        return reg.publish_to_marketplace(req.name, req.description, req.author, req.tags)
    if req.action == "marketplace":
        return {"marketplace": reg.list_marketplace(), "installed": reg.list_installed()}
    return {"status": "failed", "error": f"unknown action: {req.action}"}


# =========================================================================
# POST /api/v1/knowledge — 知识库
# =========================================================================

@router.post("/api/v1/knowledge")
async def knowledge_ep(req: KnowledgeRequest) -> dict[str, Any]:
    from obase.knowledge_store import KnowledgeStore

    store = KnowledgeStore()
    if req.action == "list":
        entries = store.list_all(req.type or None)
        return {"entries": entries, "count": len(entries)}
    if req.action == "read":
        doc = store.read(req.id)
        return doc if doc else {"status": "not_found", "id": req.id}
    if req.action == "write":
        store.write(req.id, req.type, req.body, covers=req.covers)
        return {"status": "written", "id": req.id}
    if req.action == "stale":
        return {"stale": store.list_stale(), "count": len(store.list_stale())}
    if req.action == "delete":
        return {"status": "deleted" if store.delete(req.id) else "not_found"}
    if req.action == "skeleton":
        return {"skeleton": store.skeleton(req.id, req.type)}
    return {"status": "failed", "error": f"unknown action: {req.action}"}


# =========================================================================
# GET /api/v1/operators — 3O 算子账本 (delegate_to_genesis 固化查询)
# =========================================================================

@router.get("/api/v1/operators")
async def operators_ledger() -> dict[str, Any]:
    from server.operator_ledger import (
        ledger_summary,
        replica_ledger_summary,
        runtime_ledger_summary,
    )
    from server.runtimes import ALL_RUNTIMES

    healths = []
    for rt in ALL_RUNTIMES:
        try:
            healths.append(await rt.health())
        except Exception as e:
            healths.append({"name": rt.name, "ok": False, "error": str(e)[:200]})

    return {"operators": ledger_summary(),
            "runtimes": runtime_ledger_summary(),
            "runtime_health": healths,
            "replica": replica_ledger_summary()}


# =========================================================================
# Goal-Driven 长程编排 API: /goal-driven (while 循环: 工作→验证→续跑)
# =========================================================================

class GoalDrivenRequest(BaseModel):
    goal_id: str = Field(..., min_length=1)
    step: bool = False           # True=单步 (外部驱动), False=连续循环


@router.post("/api/v1/goal-driven/run")
async def goal_driven_run_ep(req: GoalDrivenRequest) -> dict[str, Any]:
    """启动 Goal-Driven 循环 (连续或单步)。引擎调用由装配层注入 (stub 兼容)。"""
    from pathlib import Path as _P

    from omodul.long_task_driver import open_long_task

    from veya.llm import llm_call

    loop_dir = _P.home() / ".veya" / "loops"
    driver = open_long_task(loop_dir, goal_id=req.goal_id, budget_usd=5.0)

    async def _engine(prompt_suffix: str) -> dict[str, Any]:
        result = await llm_call(
            [{"role": "user", "content": f"继续长程任务: {prompt_suffix}"}],
            provider="veya1.1", model="veya1.1", timeout=120)
        content = ""
        with contextlib.suppress(KeyError, IndexError):
            content = str(result["choices"][0]["message"].get("content", ""))
        return {"ok": bool(content), "output": content or "", "cost_usd": 0.0}

    if req.step:
        outcome = await driver.run_round(_engine)
        return {"mode": "step", **outcome}
    return {"mode": "loop", "note": "连续循环请使用单步驱动 (automata cron 唤醒), 或注入长驻执行器"}


@router.get("/api/v1/goal-driven/status")
async def goal_driven_status_ep(goal_id: str = "") -> dict[str, Any]:
    """Goal 投影状态 (跨进程可查)。"""
    from pathlib import Path as _P

    from omodul.long_task_goal import GoalKernel

    if not goal_id:
        return {"ok": False, "error": "goal_id 必填"}
    kernel = GoalKernel(
        __import__("obase.loop_event_store", fromlist=["AppendOnlyEventStore"]).AppendOnlyEventStore(
            str(_P.home() / ".veya" / "loops" / f"{goal_id}.jsonl")),
        goal_id=goal_id,
    ).rebuild()
    goal = kernel.goal
    if goal is None:
        return {"ok": False, "error": "goal 不存在"}
    return {
        "ok": True, "goal_id": goal.id, "title": goal.title,
        "status": goal.status,
        "todos_open": len(goal.open_todos()),
        "gates_pending": len(goal.pending_gates()),
        "complete": goal.is_complete(),
        "quota_remaining_usd": goal.quota.remaining_usd,
        "evidence_count": len(goal.evidence),
    }


# =========================================================================
# Spec 可执行化 API (spec-kit 内化): /spec/execute + /spec/presets
# =========================================================================

class SpecExecuteRequest(BaseModel):
    spec: str = Field(..., min_length=20, description="markdown spec (目标/验收标准/约束/测试门)")
    preset: str = ""
    variables: dict[str, str] = Field(default_factory=dict)
    async_impl: bool = False


@router.post("/api/v1/spec/execute")
async def spec_execute_ep(req: SpecExecuteRequest) -> dict[str, Any]:
    from oskill.spec_execute import SpecExecutor, render_preset

    text = req.spec
    if req.preset:
        text = render_preset(req.preset, req.variables)
    executor = SpecExecutor()
    # 装配层注入: implementer 用主脑 llm_call (stub 兼容)
    from veya.llm import llm_call

    async def _implementer(task: str, idx: int) -> dict[str, Any]:
        result = await llm_call([{"role": "user", "content": task}])
        content = ""
        with contextlib.suppress(KeyError, IndexError):
            content = result["choices"][0]["message"].get("content", "")
        return {"ok": bool(content), "output": content[:2000]}

    return await executor.execute(text, implementer=_implementer)


@router.get("/api/v1/spec/presets")
async def spec_presets() -> dict[str, Any]:
    from oskill.spec_execute import PRESETS

    return {"presets": list(PRESETS), "templates": PRESETS}


# =========================================================================
# POST /api/v1/browser/run — 浏览器自动化 (engine 选择 + 回退)
#   engine=browser_use (默认): 自然语言目标; 未装/失败回退 omodul (playwright)
# =========================================================================

class BrowserRunCompatRequest(BaseModel):
    url: str = ""
    instruction: str = Field(..., min_length=1)
    headless: bool = True
    timeout_ms: int = 30000
    max_steps: int = 20
    extract_schema: dict[str, Any] | None = None
    engine: Literal["browser_use", "omodul"] = "browser_use"


@router.post("/api/v1/browser/run")
async def browser_run_compat(req: BrowserRunCompatRequest) -> dict[str, Any]:
    if req.engine == "browser_use":
        try:
            from browser_use import Agent
        except ImportError:
            pass  # 未装 → 回退 omodul
        else:
            try:
                import asyncio as _asyncio
                import os as _os

                from langchain_openai import ChatOpenAI

                endpoint = _os.environ.get("VEYA_LLM_ENDPOINT", "")
                model = _os.environ.get("VEYA_LLM_MODEL", "gpt-4o-mini")
                llm = (ChatOpenAI(model=model, base_url=endpoint, api_key="local")
                       if endpoint else ChatOpenAI(model=model))

                async def _run_bu() -> dict[str, Any]:
                    agent = Agent(task=req.instruction, llm=llm,
                                  max_steps=req.max_steps, headless=req.headless)
                    result = await agent.run()
                    return {"engine": "browser_use", "status": "success",
                            "steps": len(getattr(result, "history", [])),
                            "output": str(getattr(result, "final_result", ""))[:4000]}

                return await _asyncio.run(_run_bu())
            except Exception as exc:
                fallback = await _omodul_browser(req)
                return {"engine": "browser_use", "status": "fallback",
                        "reason": f"browser_use 执行失败, 回退 omodul: {exc}", **fallback}

    return await _omodul_browser(req)


async def _omodul_browser(req: BrowserRunCompatRequest) -> dict[str, Any]:
    from pathlib import Path as _Path
    from types import SimpleNamespace as _NS

    from veya.omodul.browser_agent import run_browser_automation

    config = _NS(headless=req.headless, timeout_ms=req.timeout_ms,
                 max_steps=req.max_steps)
    input_data = _NS(url=req.url, instruction=req.instruction,
                     extract_schema=req.extract_schema)
    result = await run_browser_automation(config, input_data, _Path("/tmp/veya/browser"))
    result["engine"] = "omodul"
    return result


# =========================================================================
# GET /api/v1/models/catalog — ProviderCatalog (动态模型目录)
# =========================================================================

@router.get("/api/v1/models/catalog")
async def models_catalog_api() -> dict[str, Any]:
    from server.routes.models import models_catalog

    return await models_catalog()


# =========================================================================
# GET /api/v1/mcp/health — veya 服务探活 (前端 upstreamProbe 依赖此路径)
# =========================================================================

@router.get("/api/v1/mcp/health")
async def mcp_health() -> dict[str, Any]:
    from veya.mcp_server import create_mcp_server

    server = create_mcp_server()
    return {
        "status": "ok",
        "server": server.name,
        "version": getattr(server, "version", ""),
        "tools_count": len(server.tools.list_tools()),
    }


# =========================================================================
# GET /api/v1/mcp/categories — MCP 渐进式发现
# =========================================================================

@router.get("/api/v1/mcp/categories")
async def mcp_categories_ep() -> dict[str, Any]:
    from omodul.cindy_mcp_server import build_memory_mcp_server, build_scheduler_mcp_server

    mem = build_memory_mcp_server()
    sch = build_scheduler_mcp_server()
    return {"servers": {"cindy_memory": mem.categories(), "cindy_scheduler": sch.categories()}}


# =========================================================================
# POST /api/v1/agent/skills-inject — 动态 Skill 注入
# =========================================================================

@router.post("/api/v1/agent/skills-inject")
async def skills_inject_ep(req: SkillTeachRequest) -> dict[str, Any]:
    import uuid

    from oskill.skills_dynamic_inject import skills_dynamic_inject

    ctx = {"system_prompt": "", "tools": [], "config": {}}
    result = skills_dynamic_inject(ctx, context=req.config or {})
    result["session_id"] = uuid.uuid4().hex
    return result


# =========================================================================
# P2-05 Skill Teaching UX — Two-phase candidate → confirm flow
# =========================================================================

@router.post("/api/v1/skill/propose")
async def skill_propose_ep(req: SkillTeachRequest) -> dict[str, Any]:
    """Propose a new skill candidate (phase 1 of P2-05 two-phase flow).

    Creates a skill spec with status "candidate" — not yet verified.
    Frontend must call /api/v1/skill/confirm or /api/v1/skill/reject
    to finalize.
    """
    from server.capability_model import skill_registry

    spec = skill_registry.propose_skill(req.description, req.config or {})
    return {
        "status": spec.status,
        "skill_id": spec.skill_id,
        "description": spec.instructions,
        "version": spec.version,
        "phase": "proposed",
        "message": "Skill candidate created. Call /api/v1/skill/confirm to verify or /api/v1/skill/reject to discard."
    }


@router.post("/api/v1/skill/confirm")
async def skill_confirm_ep(req: SkillConfirmRequest) -> dict[str, Any]:
    """Confirm a proposed skill candidate (phase 2 of P2-05 two-phase flow).

    Changes status from "candidate" to "verified" — the skill is now
    permanently in the registry and discoverable by the model.
    """
    from server.capability_model import skill_registry

    spec = skill_registry.confirm_skill(req.skill_id)
    if spec is None:
        return {"status": "not_found", "skill_id": req.skill_id, "error": "Skill candidate not found"}
    return {
        "status": "confirmed",
        "skill_id": spec.skill_id,
        "description": spec.instructions,
        "version": spec.version,
        "phase": spec.status,
    }


@router.post("/api/v1/skill/reject")
async def skill_reject_ep(req: SkillRejectRequest) -> dict[str, Any]:
    """Reject a proposed skill candidate.

    Changes status to "deprecated" — the skill is removed from
    active consideration.
    """
    from server.capability_model import skill_registry

    ok = skill_registry.reject_skill(req.skill_id)
    if not ok:
        return {"status": "not_found", "skill_id": req.skill_id, "error": "Skill candidate not found"}
    return {"status": "rejected", "skill_id": req.skill_id}


# =========================================================================
# P1-05 / P3-01: 权限档位 (两条入口能力面一致)
# =========================================================================

class ProfileSetRequest(BaseModel):
    profile: str = Field(..., description="READ_ONLY | DEVELOPMENT | PRODUCTION")


@router.get("/api/v1/permission/profiles")
async def permission_profiles_ep() -> dict[str, Any]:
    """列出全部权限档位 (P1-05 档位选择器数据源)。"""
    from server.permission_profiles import list_profiles

    return {"profiles": list_profiles()}


@router.get("/api/v1/permission/profile")
async def permission_profile_current_ep(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """当前用户生效档位。"""
    from server.permission_profiles import default_profile

    return {"profile": default_profile().value}


@router.post("/api/v1/permission/profile")
async def permission_profile_set_ep(
    req: ProfileSetRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """切换当前用户档位，不改变主链语义决策；
    是否 enforce 由 VEYA_PERMISSION_PROFILE_ENFORCE 控制 (默认 observe)。
    """
    from server.permission_profiles import ProfileName, list_profiles, set_user_profile

    try:
        name = ProfileName(req.profile.strip().upper())
    except ValueError:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail=f"invalid profile: {req.profile}")
    set_user_profile(name, user_id=str(user.get("user_id") or "anonymous"))
    desc = next((p["description"] for p in list_profiles() if p["name"] == name.value), "")
    return {
        "status": "set",
        "profile": name.value,
        "description": desc,
        "note": "观察模式 (observe): 档位仅影响 tool_guard 决策记录; 设 VEYA_PERMISSION_PROFILE_ENFORCE=1 才拦截",
    }


__all__ = ["router"]
