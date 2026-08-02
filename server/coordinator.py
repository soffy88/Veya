"""
layer4/server/coordinator.py — 协调器主循环(hicode 招牌)

实现 "聊天窗口分发命令 → 各分队执行 → 结构化结果回传"。
协调器拆任务 → 派角色分队(research/plan/execute)→ 分队 headless 执行
→ H4 hook 验证 → 结构化结果汇总。

每分队独立 context(orchestrator 运行时隔离),协调器只收摘要,
cost 经同一 CostTracker 跨引擎传播(§5.6 C1)。

checkpoint 在每个分队完成后落盘(obase.versionstore),
resume 从 RunState.completed_steps 跳过已完成分队继续跑。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from obase import CostTracker
from omodul import process_prompt   # 用于任务拆解(让 LLM 分解)

from server.assembly import assemble_orchestrator
from server.events import _on_step_ctx, fire_step
from hooks.registry import build_coordinator_hooks


# =====================================================================
# 数据结构
# =====================================================================

@dataclass
class SquadTask:
    """派给单个分队的任务。"""
    squad_id: str
    role: str                       # "research" | "plan" | "execute"
    command: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)   # 依赖的 squad_id(串行用)


@dataclass
class SquadPlan:
    """协调器对一个复杂任务的拆解结果。"""
    squads: list[SquadTask]
    schedule: str = "parallel"      # "parallel" | "sequential" | "dag"


@dataclass
class SquadResult:
    squad_id: str
    role: str
    status: str                     # "success" | "failed"
    output: Any
    error: dict | None = None
    cost_usd: float = 0.0


# =====================================================================
# 协调器
# =====================================================================

class Coordinator:
    """协调器:派角色分队并汇总结构化结果。"""

    def __init__(self, *, decompose_model: str = "claude-sonnet-4-6"):
        self._decompose_model = decompose_model

    async def handle(
        self,
        command: dict[str, Any],
        *,
        session_id: str | None = None,
        on_step: Callable | None = None,
    ) -> dict[str, Any]:
        """处理一条协调命令。返回结构化汇总结果。

        on_step: 可选回调 on_step(event: dict) — 每个分队状态变化/tool调用时触发。
                 event keys: type / squad_id / role / tool_name / text / status / cost_usd
        """
        sid = session_id or str(uuid.uuid4())

        # 设置 on_step contextvar(协程安全,不改函数签名)
        token = _on_step_ctx.set(on_step)
        try:
            # 顶层 CostTracker:所有分队共享同一对象(§5.6 C1)
            cost = CostTracker()

            fire_step({"type": "session_start", "session_id": sid})

            # 1. 拆解任务 → SquadPlan
            plan = await self._decompose(command, cost=cost)

            # 2. 装配 orchestrator
            orchestrator = assemble_orchestrator(
                scheduler=self._make_scheduler(plan.schedule),
                cost_tracker=cost,
                coordinator_hooks=build_coordinator_hooks(),
            )

            # 3. 派分队(checkpoint 在 _run_dag / _run_parallel 内落盘)
            results = await self._run_squads(orchestrator, plan, session_id=sid)

            # 4. 汇总
            total_cost = sum(r.cost_usd for r in results)
            fire_step({"type": "cost_update", "total_cost": total_cost, "session_id": sid})

            result = self._aggregate(results, total_cost=total_cost)
            result["session_id"] = sid
            return result
        finally:
            _on_step_ctx.reset(token)

    async def resume(self, run_state: Any) -> dict[str, Any]:
        """从 checkpoint 的 RunState 续跑未完成的分队。

        run_state.completed_steps 列出已完成的 squad_id;
        run_state.data 含已完成分队的 output。
        """
        from oprim._make_checkpoint import RunState
        session_id = run_state.session_id
        completed = set(run_state.completed_steps)
        saved_outputs: dict[str, Any] = run_state.data.get("outputs", {})

        # 重建分队计划(使用保存的 command)
        command = run_state.data.get("command", {})
        plan = await self._decompose(command, cost=CostTracker())

        # 跳过已完成的分队,从断点继续
        cost = CostTracker()
        orchestrator = assemble_orchestrator(
            scheduler=self._make_scheduler(plan.schedule),
            cost_tracker=cost,
            coordinator_hooks=build_coordinator_hooks(),
        )
        results = await self._run_dag(
            orchestrator, plan.squads,
            session_id=session_id,
            skip_completed=completed,
            prior_outputs=saved_outputs,
        )

        total_cost = sum(r.cost_usd for r in results)
        result = self._aggregate(results, total_cost=total_cost)
        result["session_id"] = session_id
        result["resumed_from_step"] = run_state.step
        return result

    # ── 任务拆解 ──────────────────────────────────────────────────────
    async def _decompose(self, command: dict, *, cost: CostTracker) -> SquadPlan:
        text = command.get("text", "")
        if self._is_simple(text):
            return SquadPlan(
                squads=[SquadTask(squad_id="s1", role="execute", command=command)],
                schedule="parallel",
            )
        return SquadPlan(
            squads=[
                SquadTask(squad_id="research", role="research", command=command),
                SquadTask(squad_id="plan", role="plan", command=command,
                          depends_on=["research"]),
                SquadTask(squad_id="execute", role="execute", command=command,
                          depends_on=["plan"]),
            ],
            schedule="dag",
        )

    def _is_simple(self, text: str) -> bool:
        _complex_signals = ("重构", "重構", "refactor", "リファクタ", "全体", "モジュール全")
        if len(text) >= 200:
            return False
        return not any(s in text or s in text.lower() for s in _complex_signals)

    # ── 分队执行 ──────────────────────────────────────────────────────
    async def _run_squads(
        self,
        orchestrator,
        plan: SquadPlan,
        *,
        session_id: str,
    ) -> list[SquadResult]:
        if plan.schedule == "parallel":
            return await self._run_parallel(orchestrator, plan.squads, session_id=session_id)
        return await self._run_dag(orchestrator, plan.squads, session_id=session_id)

    async def _run_parallel(
        self,
        orchestrator,
        squads: list[SquadTask],
        *,
        session_id: str,
    ) -> list[SquadResult]:
        from server.checkpoint import save_checkpoint
        from oprim._make_checkpoint import RunState

        coros = [self._execute_squad(s, session_id=session_id) for s in squads]
        raw = await asyncio.gather(*coros, return_exceptions=True)
        results = [self._to_result(s, r) for s, r in zip(squads, raw)]

        # Checkpoint after all parallel squads complete
        completed_ids = [r.squad_id for r in results]
        outputs = {r.squad_id: r.output for r in results}
        run_state = RunState(
            session_id=session_id,
            step=len(results),
            data={"outputs": outputs, "command": squads[0].command if squads else {}},
            completed_steps=completed_ids,
        )
        try:
            await save_checkpoint(session_id, run_state)
        except Exception:
            pass

        return results

    async def _run_dag(
        self,
        orchestrator,
        squads: list[SquadTask],
        *,
        session_id: str,
        skip_completed: set[str] | None = None,
        prior_outputs: dict[str, Any] | None = None,
    ) -> list[SquadResult]:
        """按 depends_on 拓扑串行;每分队完成后 checkpoint 落盘。"""
        from server.checkpoint import save_checkpoint
        from oprim._make_checkpoint import RunState

        skip_completed = skip_completed or set()
        done: dict[str, SquadResult] = {}

        # 注入已完成分队的历史 output(resume 用)
        if prior_outputs:
            for squad_id, output in prior_outputs.items():
                fake_task = next((s for s in squads if s.squad_id == squad_id), None)
                if fake_task:
                    done[squad_id] = SquadResult(
                        squad_id=squad_id, role=fake_task.role,
                        status="success", output=output, cost_usd=0.0,
                    )

        order = self._topo_sort(squads)
        for step_idx, s in enumerate(order):
            if s.squad_id in skip_completed:
                continue  # 已完成,跳过

            ctx = {dep: done[dep].output for dep in s.depends_on if dep in done}
            cmd = {**s.command, "_upstream": ctx}
            raw = await self._execute_squad(
                SquadTask(squad_id=s.squad_id, role=s.role,
                          command=cmd, depends_on=s.depends_on),
                session_id=session_id,
            )
            res = self._to_result(s, raw)
            done[s.squad_id] = res

            # Checkpoint after each squad
            completed_ids = list(done.keys())
            outputs_so_far = {sid: r.output for sid, r in done.items()}
            run_state = RunState(
                session_id=session_id,
                step=step_idx + 1,
                data={"outputs": outputs_so_far, "command": s.command},
                completed_steps=completed_ids,
            )
            try:
                await save_checkpoint(session_id, run_state)
            except Exception:
                pass  # checkpoint failure must never abort the main flow

            if res.status == "failed":
                break

        return list(done.values())

    async def _execute_squad(
        self,
        squad: SquadTask,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Layer4: 直接装 agentic_loop + run_turn 执行单分队。"""
        from server.assembly import assemble_main_agent
        
        # 从 command 中提取 model/provider
        model = squad.command.get("model")
        provider = squad.command.get("provider")
        
        engine = assemble_main_agent(
            persona=squad.role,
            session_ctx={
                "squad": True, 
                "session_id": session_id,
                "model": model,      # 注入模型
                "provider": provider # 注入提供者
            },
        )
        text = squad.command.get("text", "")
        upstream = squad.command.get("_upstream")
        messages = [{"role": "user", "content": text}]
        context: dict[str, Any] = {}
        if upstream:
            context["_upstream"] = upstream
        try:
            fire_step({"type": "squad_start", "squad_id": squad.squad_id, "role": squad.role})
            result = await engine.run_turn(messages, context=context)
            status = "success" if result.get("status", "failed") in ("completed", "success") else "failed"
            cost_usd = float(result.get("cost_usd", 0.0))
            # H4: run test_gate for execute/build personas
            if status == "success" and squad.role in ("execute", "build"):
                from hooks.builtin.test_gate import test_gate
                from hooks.types import HookInput
                import os as _os
                hicode_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                hook_out = await test_gate(
                    HookInput(point="pre_result", persona=squad.role, cwd=hicode_root)
                )
                if hook_out.decision == "block":
                    return {
                        "status": "failed",
                        "output": None,
                        "error": f"test_gate: {hook_out.reason}",
                        "cost_usd": cost_usd,
                        "test_gate": "failed",
                    }
            output = result.get("turn_result")
            text_out = ""
            if isinstance(output, str):
                text_out = output
            elif isinstance(output, dict):
                text_out = output.get("content", "") or output.get("error", "")
            
            # Fire text_delta if there's text content (for GUI/TUI display)
            if text_out:
                fire_step({"type": "text_delta", "squad_id": squad.squad_id,
                           "role": squad.role, "delta": text_out})
            
            fire_step({"type": "squad_done", "squad_id": squad.squad_id,
                       "role": squad.role, "status": status, "cost_usd": cost_usd})
            return {
                "status": status,
                "output": output,
                "cost_usd": cost_usd,
                "test_gate": "passed" if squad.role in ("execute", "build") else "skipped",
            }
        except Exception as exc:
            fire_step({"type": "squad_done", "squad_id": squad.squad_id,
                       "role": squad.role, "status": "failed", "cost_usd": 0.0})
            return {"status": "failed", "output": None, "error": str(exc), "cost_usd": 0.0}

    @staticmethod
    def _topo_sort(squads: list[SquadTask]) -> list[SquadTask]:
        by_id = {s.squad_id: s for s in squads}
        visited: set[str] = set()
        order: list[SquadTask] = []

        def visit(sid: str):
            if sid in visited:
                return
            visited.add(sid)
            for dep in by_id[sid].depends_on:
                visit(dep)
            order.append(by_id[sid])

        for s in squads:
            visit(s.squad_id)
        return order

    # ── 结果处理 ──────────────────────────────────────────────────────
    @staticmethod
    def _to_result(task: SquadTask, raw: Any) -> SquadResult:
        if isinstance(raw, Exception):
            return SquadResult(task.squad_id, task.role, "failed",
                               output=None, error={"exc": str(raw)})
        return SquadResult(
            squad_id=task.squad_id, role=task.role,
            status=raw.get("status", "failed"),
            output=raw.get("output"),
            error=raw.get("error"),
            cost_usd=raw.get("cost_usd", 0.0),
        )

    @staticmethod
    def _make_scheduler(schedule: str) -> Callable:
        def scheduler(squads):
            return schedule
        return scheduler

    def _aggregate(self, results: list[SquadResult], *, total_cost: float) -> dict:
        any_failed = any(r.status == "failed" for r in results)
        return {
            "status": "failed" if any_failed else "success",
            "squads": [
                {"id": r.squad_id, "role": r.role, "status": r.status,
                 "output": r.output, "error": r.error, "cost_usd": r.cost_usd}
                for r in results
            ],
            "cost_usd": total_cost,
        }


# 模块级单例(server 复用)
coordinator = Coordinator()
