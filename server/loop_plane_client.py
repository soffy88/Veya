# 3O-IO-ALLOW loop-plane 转发客户端 — HTTP 转发是其本职（与 obase.llm 同类），
# 非业务直连 I/O; 由 feature flag 控制, 默认不启用。
"""server/loop_plane_client — Loop Plane 微服务客户端（薄转发层，SPEC §5）。

- feature flag: LOOP_PLANE_URL（HTTP 模式）/ LOOP_PLANE_INPROCESS=true（进程内）
  任一开启 → create_plan/plan_status/update_todo 转发到 loop-plane（事件溯源）；
  都未设置 → 旧 plan_todo 路径（T8: 迁移期可随时切回）。
- 新增工具: loop_plan_goal / loop_diagnose / loop_intervene（冻结架构「只加工具」）。
- 禁止在 coordinator 写分支（SPEC 禁令）——本层只是工具实现，无路由判断。
"""

from __future__ import annotations

import json
import os
from typing import Any

from server.tool_registry import master_tools


def loop_plane_enabled() -> bool:
    """feature flag: HTTP 或进程内模式。"""
    return bool(os.environ.get("LOOP_PLANE_URL")) or (
        os.environ.get("LOOP_PLANE_INPROCESS", "").strip().lower() == "true"
    )


# ---------------------------------------------------------------------------
# 进程内模式：直接调 loop-plane domain（同一代码路径, 无 HTTP）
# ---------------------------------------------------------------------------


def _loop_plane_path() -> str:
    """把 services/loop-plane 加入 sys.path（进程内模式, 幂等）。"""
    import sys
    from pathlib import Path

    lp = Path(__file__).resolve().parents[1] / "services" / "loop-plane"
    path = str(lp)
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


def _inprocess_goal_service():
    _loop_plane_path()
    from app.domain.state.service import GoalService
    from app.infra.event_store import EventStore

    from app.config import Settings

    settings = Settings.from_env()
    settings.ensure_dirs()
    return GoalService(EventStore(settings.data_dir, tenant_id=settings.default_tenant))


def _inprocess_causal():
    _loop_plane_path()
    from app.domain.causal.service import CausalService
    from app.infra.event_store import AuditLog, EventStore

    from app.config import Settings

    settings = Settings.from_env()
    settings.ensure_dirs()
    store = EventStore(settings.data_dir, tenant_id=settings.default_tenant)
    audit = AuditLog(settings.data_dir, tenant_id=settings.default_tenant)
    return CausalService(store=store, audit=audit)


# ---------------------------------------------------------------------------
# HTTP 模式
# ---------------------------------------------------------------------------


class LoopPlaneHttp:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self._base}{path}", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def get(self, path: str) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self._base}{path}")
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 兼容工具函数（返回形态与旧 plan_todo 一致）
# ---------------------------------------------------------------------------


class _LoopClient:
    """统一客户端门面：HTTP 或进程内（仅 flag 开启时构造，默认零副作用）。"""

    def __init__(self) -> None:
        if not loop_plane_enabled():
            raise RuntimeError("loop-plane 未启用 (设置 LOOP_PLANE_INPROCESS=true 或 LOOP_PLANE_URL)")
        url = os.environ.get("LOOP_PLANE_URL")
        if url:
            self._http = LoopPlaneHttp(url)
            self._svc = None
            self._causal = None
        else:
            self._http = None
            self._svc = _inprocess_goal_service()
            self._causal = _inprocess_causal()

# -- state（≡ create_plan / plan_status / update_todo） -----------------

    async def create_plan(self, objective: str, todos: list[dict]) -> str:
        goal = await self._goal_call("create", {"objective": objective, "todos": todos})
        return f"已创建计划 {goal['goal_id']}\n" + goal.get("render_text", "")

    async def plan_status(self, plan_id: str = "") -> str:
        if not plan_id:
            goals = await self._goal_call("list", {})
            items = goals.get("goals", [])
            if not items:
                return "暂无计划 (复杂任务可先 create_plan 拆解再执行)。"
            lines = ["最近计划:"]
            for g in items[:6]:
                done = sum(1 for t in g["todos"].values() if t["status"] == "done")
                total = len(g["todos"])
                lines.append(f"  {g['goal_id']} ({done}/{total}) {g['objective'][:60]}")
            lines.append("查看详情: plan_status(plan_id=<id>)。")
            return "\n".join(lines)
        goal = await self._goal_call("get", {"goal_id": plan_id})
        return goal.get("render_text", json.dumps(goal, ensure_ascii=False))

    async def update_todo(self, plan_id: str, todo_id: str, status: str, evidence: str = "") -> str:
        goal = await self._goal_call("update", {
            "goal_id": plan_id, "todo_id": todo_id,
            "status": status, "evidence": evidence,
        })
        return goal.get("render_text", "")

    # -- causal（新增 loop_* 工具） ------------------------------------------

    async def plan_goal(self, goal: str, criteria: str = "") -> dict[str, Any]:
        return await self._causal_call("plan", {"goal": goal, "criteria": criteria})

    async def diagnose(self, symptom: str, context: dict | None = None) -> dict[str, Any]:
        return await self._causal_call("diagnose", {"symptom": symptom, "context": context or {}})

    async def intervene(self, mode: str, tool_name: str, args: dict) -> dict[str, Any]:
        return await self._exec_call({"mode": mode, "tool_name": tool_name, "args": args})

    # -- 内部 ----------------------------------------------------------------

    async def _goal_call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._svc is not None:  # 进程内
            if op == "create":
                return self._svc.create_goal(payload["objective"], payload["todos"])
            if op == "list":
                return {"goals": self._svc.list_goals()}
            if op == "get":
                return self._svc.get_goal(payload["goal_id"])
            if op == "update":
                return self._svc.update_todo(
                    payload["goal_id"], payload["todo_id"],
                    payload["status"], payload["evidence"],
                )
            raise ValueError(f"未知 op {op}")
        assert self._http is not None
        if op == "create":
            return await self._http.post("/v1/loop/goals", payload)
        if op == "list":
            return await self._http.get("/v1/loop/goals")
        if op == "get":
            return await self._http.get(f"/v1/loop/goals/{payload['goal_id']}")
        if op == "update":
            return await self._http.post(
                f"/v1/loop/goals/{payload['goal_id']}/todos/{payload['todo_id']}",
                {"status": payload["status"], "evidence": payload.get("evidence", "")},
            )
        raise ValueError(f"未知 op {op}")

    async def _causal_call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._causal is not None:
            if op == "plan":
                return self._causal.plan_goal(payload["goal"], payload.get("criteria", ""))
            return self._causal.diagnose(payload["symptom"], payload.get("context"))
        assert self._http is not None
        return await self._http.post(f"/v1/loop/plan/{op}", payload)

    async def _exec_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._http is None:
            # 进程内 exec: 轻量直调（domain 层）
            _loop_plane_path()
            from app.domain.exec.service import ExecService

            return ExecService().dispatch(mode=payload["mode"], tool_name=payload["tool_name"], args=payload["args"])
        return await self._http.post("/v1/loop/exec/dispatch", payload)


_client: _LoopClient | None = None


def _get_client() -> _LoopClient:
    global _client  # noqa: PLW0603
    if _client is None:
        _client = _LoopClient()
    return _client


def make_plan_func(name: str):
    """wire_master_tools 转发工厂: flag 开启 → loop-plane; 否则旧函数。"""

    async def _forward(**kw: Any) -> str:
        client = _get_client()
        if name == "create_plan":
            return await client.create_plan(kw["objective"], kw.get("todos") or [])
        if name == "plan_status":
            return await client.plan_status(kw.get("plan_id", ""))
        if name == "update_todo":
            return await client.update_todo(kw["plan_id"], kw["todo_id"], kw["status"], kw.get("evidence", ""))
        raise ValueError(f"未知 plan 工具 {name}")

    if loop_plane_enabled():
        return _forward
    from server.plan_todo import create_plan, plan_status, update_todo

    return {"create_plan": create_plan, "plan_status": plan_status, "update_todo": update_todo}[name]


# ---------------------------------------------------------------------------
# 新增 loop_* 工具（冻结架构「只加工具」，无路由判断）
# ---------------------------------------------------------------------------


async def loop_plan_goal(goal: str, criteria: str = "") -> str:
    """目标规划（因果）：Goal → ranked_actions 报告。只规划不执行。"""
    if not loop_plane_enabled():
        return "loop-plane 未启用 (设置 LOOP_PLANE_INPROCESS=true 或 LOOP_PLANE_URL 后可用)"
    report = await _get_client().plan_goal(goal, criteria)
    return json.dumps(report, ensure_ascii=False, default=str)


async def loop_diagnose(symptom: str, context: dict | None = None) -> str:
    """故障诊断（因果）：symptom → root_causes + intervention。"""
    if not loop_plane_enabled():
        return "loop-plane 未启用 (设置 LOOP_PLANE_INPROCESS=true 或 LOOP_PLANE_URL 后可用)"
    report = await _get_client().diagnose(symptom, context)
    return json.dumps(report, ensure_ascii=False, default=str)


async def loop_intervene(mode: str = "sandbox", tool_name: str = "", args: dict | None = None) -> str:
    """硬化干预执行：mode 服务端收缩（sandbox/shadow/live_canary），白名单限制。"""
    if not loop_plane_enabled():
        return "loop-plane 未启用 (设置 LOOP_PLANE_INPROCESS=true 或 LOOP_PLANE_URL 后可用)"
    result = await _get_client().intervene(mode, tool_name, args or {})
    return json.dumps(result, ensure_ascii=False, default=str)


def wire_loop_tools() -> int:
    """注册 3 个 loop_* 工具（幂等）。返回新注册数量。"""
    added = 0
    specs: list[tuple[str, str, dict]] = [
        (
            "loop_plan_goal",
            "目标规划: 用因果规划把 Goal 展开为 ranked_actions 报告 (只规划, 不执行)。"
            "适合长程目标拆解/验收标准规划; 结果可配合 create_plan 落地。",
            {"type": "object", "properties": {
                "goal": {"type": "string", "description": "目标描述 (可验收)。"},
                "criteria": {"type": "string", "description": "可选验收标准。"},
            }, "required": ["goal"]},
        ),
        (
            "loop_diagnose",
            "故障诊断: 因果推理定位 root_causes 并给出 intervention 建议。"
            "适合用户报错/流程失败但原因不明时, 先诊断再动手。",
            {"type": "object", "properties": {
                "symptom": {"type": "string", "description": "故障症状 (如 '登录偶发 500')。"},
                "context": {"type": "object", "description": "可选上下文 (服务/时间窗等)。"},
            }, "required": ["symptom"]},
        ),
        (
            "loop_intervene",
            "硬化干预执行: 在白名单适配器上执行动作, mode 由服务端强制收缩权限。"
            "sandbox 模式禁止 python -m 任意路径; 未知工具一律拒绝。",
            {"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["sandbox", "shadow", "live_canary"], "default": "sandbox"},
                "tool_name": {"type": "string", "description": "白名单适配器名。"},
                "args": {"type": "object", "description": "适配器参数。"},
            }, "required": ["tool_name"]},
        ),
    ]
    for name, desc, params in specs:
        if master_tools.has(name):
            continue
        master_tools.register(name, desc, params, {"loop_plan_goal": loop_plan_goal,
                                                   "loop_diagnose": loop_diagnose,
                                                   "loop_intervene": loop_intervene}[name],
                              max_result_chars=12000)
        added += 1
    return added


__all__ = ["loop_plane_enabled", "make_plan_func", "wire_loop_tools"]
