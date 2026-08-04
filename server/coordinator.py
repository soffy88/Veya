"""
layer4/server/coordinator.py — 协调器主循环(veya 招牌)

实现 "聊天窗口分发命令 → 各分队执行 → 结构化结果回传"。
协调器拆任务 → 派角色分队(research/plan/execute)→ 分队 headless 执行
→ H4 hook 验证 → 结构化结果汇总。

每分队独立 context(orchestrator 运行时隔离),协调器只收摘要,
cost 经同一 CostTracker 跨引擎传播(§5.6 C1)。

checkpoint 在每个分队完成后落盘(obase.versionstore),
resume 从 RunState.completed_steps 跳过已完成分队继续跑。
"""

from __future__ import annotations

import contextlib
import functools
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import veya.intent as veya_intent
from hooks.registry import build_coordinator_hooks

# 重模块(plotly/networkx/matplotlib 等)延迟到惰性 getter 首次访问时导入,
# 避免 `import server.coordinator` 即拉满 ~56MB(G9 惰性初始化)。
# from veya.advanced_visualization import (...)  → _three_d_graph/_interactive_debugger/_architecture_visualizer
# from veya.agent_collaboration import ...          → _agent_collaborator
# from veya.collaboration import ...                → _collaboration_manager
# from veya.integrations import ...                 → _integration_hub
# from veya.visualization import create_code_graph  → _code_graph
from server.assembly import assemble_orchestrator
from server.events import _on_step_ctx, fire_step
from veya.ast import create_ast_analyzer
from veya.autonomous_agent import create_autonomous_agent
from veya.cache import create_parallel_executor
from veya.context import SmartContextManager
from veya.cross_language import create_cross_language_translator
from veya.multimodal import create_multimodal_processor
from veya.performance import create_smart_cache
from veya.sandbox import create_safe_executor
from veya.semantic_search import create_semantic_search
from veya.streaming import StreamEventType, StreamingManager, TokenStreamer
from veya.tools import create_tool_executor
from veya.utils import CostTracker

# =====================================================================
# 数据结构
# =====================================================================


def _squad_to_dict(s: SquadTask) -> dict[str, Any]:
    """Serialize a SquadTask for checkpoint storage (G13 resume)."""
    return {
        "squad_id": s.squad_id,
        "role": s.role,
        "command": s.command,
        "depends_on": list(s.depends_on),
    }


@dataclass
class SquadTask:
    """派给单个分队的任务。"""

    squad_id: str
    role: str  # "research" | "plan" | "execute"
    command: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)  # 依赖的 squad_id(串行用)


@dataclass
class SquadPlan:
    """协调器对一个复杂任务的拆解结果。"""

    squads: list[SquadTask]
    schedule: str = "parallel"  # "parallel" | "sequential" | "dag"
    resume_from: set[str] = field(default_factory=set)  # G13: 断点续跑时已完成的 squad_id


@dataclass
class SquadResult:
    squad_id: str
    role: str
    status: str  # "success" | "failed"
    output: Any
    error: dict | None = None
    cost_usd: float = 0.0


# =====================================================================
# 协调器
# =====================================================================


class Coordinator:
    """协调器:派角色分队并汇总结构化结果。"""

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        *,
        decompose_model: str = "claude-sonnet-4-6",
        max_context_tokens: int = 100000,
        enable_streaming: bool = True,
    ):
        # Backward-compat: accept a settings dict positionally (E2E tests).
        if settings:
            decompose_model = settings.get("decompose_model", decompose_model)
            max_context_tokens = settings.get("max_context_tokens", max_context_tokens)
            enable_streaming = settings.get("enable_streaming", enable_streaming)
        self._decompose_model = decompose_model
        self.max_context_tokens = max_context_tokens
        self.enable_streaming = enable_streaming

        # LLM 意图分类器（替换关键词启发式路由；无 key 时自动回落启发式）
        from veya.intent import IntentClassifier

        self._classifier = IntentClassifier(model=decompose_model)

        # 上下文管理器(轻量字典,保持 eager)
        self.context_managers: dict[str, SmartContextManager] = {}

        # 流式管理器(轻量字典,保持 eager)
        self.streaming_managers: dict[str, StreamingManager] = {}

        # 以下子系统一律惰性构造(G9):cached_property 首次访问才实例化,
        # 重模块(plotly/networkx 等)也在 getter 内延迟导入。

    # ── G9 惰性子系统 getter ──────────────────────────────────────────
    # cached_property 是非数据描述符:实例属性赋值可遮蔽(测试 mock 兼容)。
    @functools.cached_property
    def parallel_executor(self) -> Any:
        return create_parallel_executor(max_concurrent=5)

    @functools.cached_property
    def ast_analyzer(self) -> Any:
        return create_ast_analyzer()

    @functools.cached_property
    def tool_executor(self) -> Any:
        return create_tool_executor()

    @functools.cached_property
    def safe_executor(self) -> Any:
        return create_safe_executor()

    @functools.cached_property
    def multimodal_processor(self) -> Any:
        return create_multimodal_processor()

    @functools.cached_property
    def integration_hub(self) -> Any:
        from veya.integrations import create_integration_hub  # 延迟: ~6MB

        return create_integration_hub()

    @functools.cached_property
    def collaboration_manager(self) -> Any:
        from veya.collaboration import create_collaboration_manager  # 延迟: ~3MB

        return create_collaboration_manager()

    @functools.cached_property
    def semantic_search(self) -> Any:
        return create_semantic_search()

    @functools.cached_property
    def autonomous_agent(self) -> Any:
        return create_autonomous_agent()

    @functools.cached_property
    def code_graph(self) -> Any:
        from veya.visualization import create_code_graph  # 延迟: ~31MB

        return create_code_graph()

    @functools.cached_property
    def cross_language_translator(self) -> Any:
        return create_cross_language_translator()

    @functools.cached_property
    def smart_cache(self) -> Any:
        return create_smart_cache(max_size=1000)

    @functools.cached_property
    def three_d_graph(self) -> Any:
        from veya.advanced_visualization import create_three_d_graph  # 延迟: ~18MB

        return create_three_d_graph()

    @functools.cached_property
    def interactive_debugger(self) -> Any:
        from veya.advanced_visualization import (
            create_interactive_debugger_enhanced,  # 延迟: ~18MB
        )

        return create_interactive_debugger_enhanced()

    @functools.cached_property
    def architecture_visualizer(self) -> Any:
        from veya.advanced_visualization import (
            create_architecture_visualizer_enhanced,  # 延迟: ~18MB
        )

        return create_architecture_visualizer_enhanced()

    @functools.cached_property
    def agent_collaborator(self) -> Any:
        from veya.agent_collaboration import create_agent_collaborator

        return create_agent_collaborator()

    async def initialize(self) -> None:
        """Initialization hook (backward-compat for E2E tests).

        G9: 子系统惰性构造(cached_property),首次访问才实例化;
        本方法保持 no-op 以兼容遗留调用方 await coordinator.initialize()。
        """
        return None

    async def handle(
        self,
        command: dict[str, Any],
        *,
        session_id: str | None = None,
        on_step: Callable | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """处理一条协调命令。返回结构化汇总结果。

        on_step: 可选回调 on_step(event: dict) — 每个分队状态变化/tool调用时触发。
                 event keys: type / squad_id / role / tool_name / text / status / cost_usd
        """
        sid = session_id or str(uuid.uuid4())

        # 获取或创建上下文管理器
        context_manager = self.context_managers.get(sid)
        if not context_manager:
            context_manager = SmartContextManager(max_tokens=self.max_context_tokens)
            self.context_managers[sid] = context_manager

        # 获取或创建流式管理器
        streaming_manager = self.streaming_managers.get(sid)
        if not streaming_manager:
            streaming_manager = StreamingManager(stream_id=sid)
            self.streaming_managers[sid] = streaming_manager

        # 设置 on_step contextvar(协程安全,不改函数签名)
        token = _on_step_ctx.set(on_step)
        try:
            # 顶层 CostTracker:所有分队共享同一对象(§5.6 C1)
            cost = CostTracker()

            # 发射会话开始事件
            if self.enable_streaming:
                await streaming_manager.emit(
                    StreamEventType.START, {"session_id": sid, "timestamp": time.time()}
                )
            else:
                fire_step({"type": "session_start", "session_id": sid})

            # 添加用户输入到上下文
            user_input = command.get("text", "")
            context_manager.add_message("user", user_input)

            # AST 代码理解分析
            project_path = command.get("project_path", ".")
            try:
                ast_stats = self.ast_analyzer.analyze_project(project_path)
                print(f"[AST] Project analyzed: {json.dumps(ast_stats, indent=2)}")

                # 基于 AST 加载相关文件（比简单列表更智能）
                all_files = [s.file_path for s in self.ast_analyzer.symbols.values()]
                relevant_files = self.ast_analyzer.predict_relevant_files(user_input, all_files)
                context_manager.load_relevant_files(relevant_files)
            except Exception as e:
                print(f"[AST] Analysis skipped: {e}")
                # 回退到简单文件加载
                project_files = ["main.py", "config.py", "utils.py"]
                context_manager.load_relevant_files(project_files)

            # 获取上下文统计
            context_stats = context_manager.get_stats()
            print(f"[Context] Current usage: {context_stats['usage_percent']}%")

            # 1. 拆解任务 → SquadPlan
            plan = await self._decompose(command, cost=cost)

            # 初始 checkpoint:保存原始 command + 完整 squad 计划,
            # 保证 resume 可确定性地重建(不依赖 LLM 重拆解)(G13)
            with contextlib.suppress(Exception):
                from server.checkpoint import save_checkpoint
                from veya.compat import RunState

                await save_checkpoint(
                    sid,
                    RunState(
                        session_id=sid,
                        step=0,
                        data={
                            "outputs": {},
                            "command": command,
                            "squads": [_squad_to_dict(s) for s in plan.squads],
                        },
                        completed_steps=[],
                    ),
                )

            # 2. 装配 orchestrator
            orchestrator = assemble_orchestrator(
                scheduler=self._make_scheduler(plan.schedule),
                cost_tracker=cost,
                coordinator_hooks=build_coordinator_hooks(),
            )

            # 3. 派分队(checkpoint 在 _run_dag / _run_parallel 内落盘)
            results = await self._run_squads(orchestrator, plan, session_id=sid, command=command)

            # 4. 汇总
            total_cost = sum(r.cost_usd for r in results)

            # 发射成本更新事件
            if self.enable_streaming:
                await streaming_manager.emit(
                    StreamEventType.PROGRESS, {"total_cost": total_cost, "session_id": sid}
                )
            else:
                fire_step({"type": "cost_update", "total_cost": total_cost, "session_id": sid})

            result = self._aggregate(results, total_cost=total_cost)
            result["session_id"] = sid

            # 发射会话完成事件
            if self.enable_streaming:
                await streaming_manager.emit(
                    StreamEventType.COMPLETE,
                    {"session_id": sid, "total_cost": total_cost, "result": result},
                )

            return result
        finally:
            _on_step_ctx.reset(token)

    async def resume(self, run_state: Any) -> dict[str, Any]:
        """从 checkpoint 的 RunState 续跑未完成的分队。

        run_state.completed_steps 列出已完成的 squad_id;
        run_state.data 含已完成分队的 output。优先使用 checkpoint 中保存的
        完整 squad 计划(确定性重建);旧格式 checkpoint 回退为重新拆解。
        """
        session_id = run_state.session_id
        completed = set(run_state.completed_steps)
        saved_outputs: dict[str, Any] = run_state.data.get("outputs", {})
        saved_squads = run_state.data.get("squads")

        cost = CostTracker()
        if isinstance(saved_squads, list) and saved_squads:
            # 新格式:从 checkpoint 重建计划(无需 LLM,确定性)
            squads = [SquadTask(**dict(s)) for s in saved_squads]
            plan = SquadPlan(squads=squads, schedule="dag", resume_from=completed)
        else:
            # 旧格式回退:重建分队计划(使用保存的 command)
            command = run_state.data.get("command", {})
            plan = await self._decompose(command, cost=cost)
            plan.resume_from = completed

        orchestrator = assemble_orchestrator(
            scheduler=self._make_scheduler(plan.schedule),
            cost_tracker=cost,
            coordinator_hooks=build_coordinator_hooks(),
        )
        results = await self._run_dag(
            orchestrator,
            plan.squads,
            session_id=session_id,
            skip_completed=completed,
            prior_outputs=saved_outputs,
            original_command=run_state.data.get("command"),
        )

        total_cost = sum(r.cost_usd for r in results)
        result = self._aggregate(results, total_cost=total_cost)
        result["session_id"] = session_id
        result["resumed_from_step"] = run_state.step
        result["resumed_squads"] = [r.squad_id for r in results if r.squad_id not in completed]
        return result

    # ── 任务拆解 ──────────────────────────────────────────────────────
    async def _decompose(self, command: dict, *, cost: CostTracker) -> SquadPlan:
        text = command.get("text", "")
        intent = await self._classifier.classify(text)
        if intent is veya_intent.Intent.SIMPLE:
            return SquadPlan(
                squads=[SquadTask(squad_id="s1", role="execute", command=command)],
                schedule="parallel",
            )
        return SquadPlan(
            squads=[
                SquadTask(squad_id="research", role="research", command=command),
                SquadTask(squad_id="plan", role="plan", command=command, depends_on=["research"]),
                SquadTask(squad_id="execute", role="execute", command=command, depends_on=["plan"]),
            ],
            schedule="dag",
        )

    def _is_simple(self, text: str) -> bool:
        """Legacy keyword heuristic — kept for compatibility; prefer the LLM
        classifier (:attr:`_classifier`) which uses this as its fallback."""
        return self._classifier.is_simple_heuristic(text)

    # ── 分队执行 ──────────────────────────────────────────────────────
    async def _run_squads(
        self,
        orchestrator,
        plan: SquadPlan,
        *,
        session_id: str,
        command: dict[str, Any] | None = None,
    ) -> list[SquadResult]:
        if plan.schedule == "parallel":
            return await self._run_parallel(
                orchestrator, plan.squads, session_id=session_id, original_command=command
            )
        return await self._run_dag(
            orchestrator, plan.squads, session_id=session_id, original_command=command
        )

    async def _run_parallel(
        self,
        orchestrator,
        squads: list[SquadTask],
        *,
        session_id: str,
        original_command: dict[str, Any] | None = None,
    ) -> list[SquadResult]:
        from server.checkpoint import save_checkpoint
        from veya.compat import RunState

        # 使用并行执行器(_execute_squad 的 session_id 为 keyword-only)
        tasks = [(self._execute_squad, (s,), {"session_id": session_id}) for s in squads]
        results_raw = await self.parallel_executor.execute_all(tasks)

        # 处理结果
        results = []
        for _i, (squad, result) in enumerate(zip(squads, results_raw, strict=False)):
            if isinstance(result, Exception):
                results.append(self._to_result(squad, {"status": "failed", "error": str(result)}))
            else:
                results.append(self._to_result(squad, result))

        # Checkpoint after all parallel squads complete
        completed_ids = [r.squad_id for r in results]
        outputs = {r.squad_id: r.output for r in results}
        run_state = RunState(
            session_id=session_id,
            step=len(results),
            data={
                "outputs": outputs,
                "command": original_command
                if original_command is not None
                else (squads[0].command if squads else {}),
                "squads": [_squad_to_dict(s) for s in squads],
            },
            completed_steps=completed_ids,
        )
        with contextlib.suppress(Exception):
            await save_checkpoint(session_id, run_state)

        return results

    async def _run_dag(
        self,
        orchestrator,
        squads: list[SquadTask],
        *,
        session_id: str,
        skip_completed: set[str] | None = None,
        prior_outputs: dict[str, Any] | None = None,
        original_command: dict[str, Any] | None = None,
    ) -> list[SquadResult]:
        """按 depends_on 拓扑串行;每分队完成后 checkpoint 落盘。"""
        from server.checkpoint import save_checkpoint
        from veya.compat import RunState

        skip_completed = skip_completed or set()
        done: dict[str, SquadResult] = {}

        # 注入已完成分队的历史 output(resume 用)
        if prior_outputs:
            for squad_id, output in prior_outputs.items():
                fake_task = next((s for s in squads if s.squad_id == squad_id), None)
                if fake_task:
                    done[squad_id] = SquadResult(
                        squad_id=squad_id,
                        role=fake_task.role,
                        status="success",
                        output=output,
                        cost_usd=0.0,
                    )

        order = self._topo_sort(squads)

        # 识别可并行的任务组
        parallel_groups: list[list[SquadTask]] = []
        current_group: list[SquadTask] = []
        for s in order:
            if not current_group or all(
                dep in [t.squad_id for t in current_group] for dep in s.depends_on
            ):
                current_group.append(s)
            else:
                parallel_groups.append(current_group)
                current_group = [s]
        if current_group:
            parallel_groups.append(current_group)

        # 按组执行
        for step_idx, group in enumerate(parallel_groups):
            # 跳过已完成的组
            group_completed = all(s.squad_id in skip_completed for s in group)
            if group_completed:
                continue

            # 执行组内任务(仅未完成分队;skip_completed 成员已由 prior_outputs 预填)
            pending = [s for s in group if s.squad_id not in skip_completed]
            tasks = []
            for s in pending:
                ctx = {dep: done[dep].output for dep in s.depends_on if dep in done}
                cmd = {**s.command, "_upstream": ctx}
                tasks.append(
                    (
                        self._execute_squad,
                        (
                            SquadTask(
                                squad_id=s.squad_id,
                                role=s.role,
                                command=cmd,
                                depends_on=s.depends_on,
                            ),
                        ),
                        {"session_id": session_id},
                    )
                )

            if not tasks:
                continue

            # 使用并行执行器
            results_raw = await self.parallel_executor.execute_all(tasks)

            # 处理结果(与 pending 对齐,completed 分队由 prior_outputs 提供)
            for _i, (squad, result) in enumerate(zip(pending, results_raw, strict=False)):
                if isinstance(result, Exception):
                    res = self._to_result(squad, {"status": "failed", "error": str(result)})
                else:
                    res = self._to_result(squad, result)
                done[squad.squad_id] = res

                # 检查点(失败分队不计入 completed,保证 resume 会重跑它)(G13)
                completed_ids = [sid for sid, r in done.items() if r.status == "success"]
                outputs_so_far = {sid: r.output for sid, r in done.items() if r.status == "success"}
                run_state = RunState(
                    session_id=session_id,
                    step=step_idx + 1,
                    data={
                        "outputs": outputs_so_far,
                        "command": original_command
                        if original_command is not None
                        else squad.command,
                        "squads": [_squad_to_dict(s) for s in squads],
                    },
                    completed_steps=completed_ids,
                )
                with contextlib.suppress(Exception):
                    await save_checkpoint(
                        session_id, run_state
                    )  # checkpoint failure must never abort the main flow

                if res.status == "failed":
                    break

            # 如果组内有失败，提前退出
            if any(done[s.squad_id].status == "failed" for s in group):
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
                "model": model,  # 注入模型
                "provider": provider,  # 注入提供者
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

            # 获取流式管理器
            streaming_manager = self.streaming_managers.get(session_id)

            # 创建流式生成器
            if streaming_manager and self.enable_streaming:
                token_streamer = TokenStreamer(streaming_manager)

                # 发送开始事件
                await streaming_manager.emit(
                    StreamEventType.START,
                    {
                        "squad_id": squad.squad_id,
                        "role": squad.role,
                        "command": squad.command,
                        "timestamp": time.time(),
                    },
                )

                # 执行流式响应
                try:
                    result = await engine.run_turn(messages, context=context)
                    status = (
                        "success"
                        if result.get("status", "failed") in ("completed", "success")
                        else "failed"
                    )
                    cost_usd = float(result.get("cost_usd", 0.0))

                    # 检查是否被中断
                    if streaming_manager.is_interrupted():
                        await streaming_manager.emit(
                            StreamEventType.INTERRUPTED,
                            {"squad_id": squad.squad_id, "reason": "user_request"},
                        )
                        return {
                            "status": "interrupted",
                            "output": None,
                            "error": "Stream interrupted by user",
                            "cost_usd": cost_usd,
                        }

                    # H4: run test_gate for execute/build personas
                    if status == "success" and squad.role in ("execute", "build"):
                        import os as _os

                        from hooks.builtin.test_gate import test_gate
                        from hooks.types import HookInput

                        veya_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                        hook_out = await test_gate(
                            HookInput(point="pre_result", persona=squad.role, cwd=veya_root)
                        )
                        if hook_out.decision == "block":
                            return {
                                "status": "failed",
                                "output": None,
                                "error": f"test_gate: {hook_out.reason}",
                                "cost_usd": cost_usd,
                                "test_gate": "failed",
                            }

                    # 流式发送结果
                    output = result.get("turn_result")
                    text_out = ""
                    if isinstance(output, str):
                        text_out = output
                    elif isinstance(output, dict):
                        text_out = output.get("content", "") or output.get("error", "")

                    if text_out:
                        await token_streamer.stream_response(
                            text_out, squad.command.get("text", "")
                        )

                    # 发送完成事件
                    await streaming_manager.emit(
                        StreamEventType.COMPLETE,
                        {
                            "squad_id": squad.squad_id,
                            "role": squad.role,
                            "status": status,
                            "cost_usd": cost_usd,
                            "timestamp": time.time(),
                        },
                    )

                    return {
                        "status": status,
                        "output": output,
                        "cost_usd": cost_usd,
                        "test_gate": "passed" if squad.role in ("execute", "build") else "skipped",
                    }
                except Exception as e:
                    await streaming_manager.emit(
                        StreamEventType.ERROR,
                        {
                            "squad_id": squad.squad_id,
                            "role": squad.role,
                            "error": str(e),
                            "timestamp": time.time(),
                        },
                    )
                    raise
            else:
                # 传统执行路径
                result = await engine.run_turn(messages, context=context)
                status = (
                    "success"
                    if result.get("status", "failed") in ("completed", "success")
                    else "failed"
                )
                cost_usd = float(result.get("cost_usd", 0.0))
                # H4: run test_gate for execute/build personas
                if status == "success" and squad.role in ("execute", "build"):
                    import os as _os

                    from hooks.builtin.test_gate import test_gate
                    from hooks.types import HookInput

                    veya_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                    hook_out = await test_gate(
                        HookInput(point="pre_result", persona=squad.role, cwd=veya_root)
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
                    fire_step(
                        {
                            "type": "text_delta",
                            "squad_id": squad.squad_id,
                            "role": squad.role,
                            "delta": text_out,
                        }
                    )

                fire_step(
                    {
                        "type": "squad_done",
                        "squad_id": squad.squad_id,
                        "role": squad.role,
                        "status": status,
                        "cost_usd": cost_usd,
                    }
                )
                return {
                    "status": status,
                    "output": output,
                    "cost_usd": cost_usd,
                    "test_gate": "passed" if squad.role in ("execute", "build") else "skipped",
                }
        except Exception as exc:
            fire_step(
                {
                    "type": "squad_done",
                    "squad_id": squad.squad_id,
                    "role": squad.role,
                    "status": "failed",
                    "cost_usd": 0.0,
                }
            )
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

    async def handle_interrupt(self, session_id: str):
        """处理中断请求"""
        streaming_manager = self.streaming_managers.get(session_id)
        if streaming_manager:
            await streaming_manager.interrupt()
            print(f"[Coordinator] Interrupted session {session_id}")

            # 清理资源
            if session_id in self.context_managers:
                del self.context_managers[session_id]
            if session_id in self.streaming_managers:
                del self.streaming_managers[session_id]

            return {"status": "interrupted", "session_id": session_id}
        return {"status": "not_found", "session_id": session_id}

    async def process_multimodal_input(self, file_path: str) -> dict[str, Any]:
        """处理多模态输入"""
        try:
            result = self.multimodal_processor.process(file_path)

            # 将结果添加到上下文
            if result.success:
                return {
                    "status": "success",
                    "result": result,
                    "message": f"Processed {file_path} as {result.source_type}",
                }
            else:
                return {"status": "failed", "error": result.error}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    async def create_collaborative_session(self, owner_id: str, name: str = "") -> dict[str, Any]:
        """创建协作会话"""
        try:
            session = await self.collaboration_manager.create_session(owner_id, name)
            return {
                "status": "success",
                "session_id": session.session_id,
                "info": session.get_info(),
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    async def semantic_search_query(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """语义搜索(原 semantic_search 方法被实例属性遮蔽,已改名避免与惰性属性冲突)。"""
        try:
            results = self.semantic_search.search(query, top_k=top_k)
            return [
                {
                    "id": r.id,
                    "text": r.text,
                    "file_path": r.file_path,
                    "score": r.score,
                    "start_line": r.start_line,
                    "end_line": r.end_line,
                }
                for r in results
            ]
        except Exception as e:
            return [{"status": "failed", "error": str(e)}]

    async def send_notification(
        self, event: str, data: dict[str, Any], platforms: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """发送通知到多个平台"""
        try:
            results = await self.integration_hub.notify(event, data, platforms)
            return [
                {
                    "platform": r.platform,
                    "action": r.action,
                    "success": r.success,
                    "message": r.message,
                    "data": r.data,
                }
                for r in results
            ]
        except Exception as e:
            return [{"status": "failed", "error": str(e)}]

    async def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        """获取会话信息"""
        try:
            session = await self.collaboration_manager.get_session(session_id)
            if session:
                info = session.get_info()
                return dict(info) if isinstance(info, dict) else {"raw": info}
            return None
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    async def execute_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        session_id: str | None = None,
        use_sandbox: bool = True,
    ) -> dict[str, Any]:
        """安全执行工具"""
        start_time = time.time()

        try:
            # 使用智能工具执行器
            tool_result = await self.tool_executor.execute_tool(tool_name, **params)

            # 如果需要沙箱执行且是危险操作
            if (
                use_sandbox
                and tool_result.status == "failed"
                and "unsafe" in tool_result.error.lower()
            ):
                print(f"[SafeExecutor] Falling back to sandbox execution for {tool_name}")

                async with self.safe_executor as executor:
                    if tool_name == "terminal" or tool_name == "git":
                        result = await executor.execute(params.get("command", ""))
                    else:
                        result = await self.safe_executor.execute_tool(tool_name, **params)

                    return {
                        "tool": tool_name,
                        "status": "success" if result["exit_code"] == 0 else "failed",
                        "output": result.get("stdout", ""),
                        "error": result.get("stderr", ""),
                        "duration": time.time() - start_time,
                        "sandboxed": True,
                    }

            return {
                "tool": tool_name,
                "status": tool_result.status.value,
                "output": tool_result.output,
                "error": tool_result.error,
                "duration": tool_result.duration,
                "suggestions": tool_result.suggestions,
                "sandboxed": False,
            }
        except Exception as e:
            return {
                "tool": tool_name,
                "status": "failed",
                "output": "",
                "error": str(e),
                "duration": time.time() - start_time,
                "sandboxed": False,
            }

    async def analyze_project(self, project_path: str = ".") -> dict[str, Any]:
        """分析项目代码结构"""
        try:
            stats = self.ast_analyzer.analyze_project(project_path)

            # 获取前 10 个最重要的函数
            top_functions = sorted(
                [s for s in self.ast_analyzer.symbols.values() if s.type == "function"],
                key=lambda x: len(x.docstring or ""),
                reverse=True,
            )[:10]

            return {
                "status": "success",
                "stats": stats,
                "top_functions": [
                    {
                        "name": f.name,
                        "file": f.file_path,
                        "line": f.line,
                        "params": [p["name"] for p in f.params],
                        "return_type": f.return_type,
                    }
                    for f in top_functions
                ],
                "dependency_graph": self.ast_analyzer.get_call_graph(),
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    # ── P4: advanced_visualization 方法 ──────────────────────────────────────
    async def generate_3d_graph(
        self, ast_data: dict | None = None, output_format: str = "json"
    ) -> dict:
        """生成3D图谱"""
        try:
            if ast_data:
                # 从 AST 数据构建图谱
                if "symbols" in ast_data:
                    for symbol in ast_data["symbols"]:
                        from veya.visualization import GraphNode  # 延迟导入(重模块)

                        node = GraphNode(
                            node_id=symbol.get("id", ""),
                            label=symbol.get("name", ""),
                            type=symbol.get("type", ""),
                            attributes={"file": symbol.get("file", "")},
                        )
                        self.three_d_graph.add_node(node)
                if "dependencies" in ast_data:
                    for dep in ast_data["dependencies"]:
                        self.three_d_graph.add_edge(
                            source=dep.source, target=dep.target, type=dep.type, weight=1.0
                        )
            result = self.three_d_graph.generate_3d_plot(output_format)
            return {"status": "success", "result": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def evaluate_expression(self, expression: str, context: dict) -> dict:
        """在调试上下文中评估表达式"""
        try:
            result = self.interactive_debugger.evaluate_expression(expression, context)
            return {"status": "success", "expression": expression, "result": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def step_debug(self, step_mode: str = "step_over") -> dict:
        """执行调试步进"""
        try:
            self.interactive_debugger.set_step_mode(step_mode)
            result = self.interactive_debugger.step()
            return {"status": "success", "step_result": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def edit_variable(self, variable_name: str, new_value: Any) -> dict:
        """编辑变量值"""
        try:
            success = self.interactive_debugger.edit_variable(variable_name, new_value)
            return {
                "status": "success" if success else "error",
                "variable_name": variable_name,
                "new_value": new_value,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def get_debug_state(self) -> dict:
        """获取调试状态"""
        try:
            state = self.interactive_debugger.get_debug_state()
            return {"status": "success", "debug_state": state}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def generate_deployment_topology(self, services: list[dict[str, Any]]) -> dict:
        """生成部署拓扑图"""
        try:
            topology = self.architecture_visualizer.generate_deployment_topology(services)
            return {"status": "success", "topology": topology}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def generate_data_flow_diagram(self, components: list[dict[str, Any]]) -> dict:
        """生成数据流图"""
        try:
            diagram = self.architecture_visualizer.generate_data_flow_diagram(components)
            return {"status": "success", "diagram": diagram}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── P5: agent_collaboration 方法 ──────────────────────────────────────
    async def create_collaboration_task(
        self, description: str, agent_role: str, dependencies: list[str] | None = None
    ) -> dict:
        """创建协作任务"""
        try:
            from veya.agent_collaboration import AgentRole

            # Convert string role to enum
            role_map = {
                "planner": AgentRole.PLANNER,
                "executor": AgentRole.EXECUTOR,
                "reviewer": AgentRole.REVIEWER,
                "coordinator": AgentRole.COORDINATOR,
            }

            role = role_map.get(agent_role.lower())
            if not role:
                return {"status": "error", "message": f"Invalid agent role: {agent_role}"}

            task_id = self.agent_collaborator.create_task(description, role, dependencies)

            return {"status": "success", "task_id": task_id, "message": "Task created successfully"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def assign_collaboration_task(self, task_id: str, agent_id: str) -> dict:
        """分配协作任务给代理"""
        try:
            success = self.agent_collaborator.assign_task(task_id, agent_id)
            if not success:
                return {"status": "error", "message": "Task or agent not found"}

            return {
                "status": "success",
                "task_id": task_id,
                "agent_id": agent_id,
                "message": "Task assigned successfully",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def complete_collaboration_task(
        self, task_id: str, result: Any = None, error: str | None = None
    ) -> dict:
        """完成协作任务"""
        try:
            success = self.agent_collaborator.complete_task(task_id, result, error)
            if not success:
                return {"status": "error", "message": "Task not found"}

            return {
                "status": "success",
                "task_id": task_id,
                "message": "Task completed successfully",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def get_collaboration_task_status(self, task_id: str) -> dict:
        """获取协作任务状态"""
        try:
            status = self.agent_collaborator.get_task_status(task_id)
            if not status:
                return {"status": "error", "message": "Task not found"}

            return {"status": "success", "task": status}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def get_collaboration_summary(self) -> dict:
        """获取协作摘要"""
        try:
            summary = self.agent_collaborator.get_collaboration_summary()
            return {"status": "success", "summary": summary}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def get_collaboration_task_graph(self) -> dict:
        """获取协作任务图"""
        try:
            graph = self.agent_collaborator.get_task_graph()
            return {"status": "success", "graph": graph}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def add_collaboration_agent(
        self, agent_id: str, role: str, capabilities: list[str] | None = None
    ) -> dict:
        """添加协作代理"""
        try:
            from veya.agent_collaboration import AgentRole

            # Convert string role to enum
            role_map = {
                "planner": AgentRole.PLANNER,
                "executor": AgentRole.EXECUTOR,
                "reviewer": AgentRole.REVIEWER,
                "coordinator": AgentRole.COORDINATOR,
            }

            role_enum = role_map.get(role.lower())
            if not role_enum:
                return {"status": "error", "message": f"Invalid agent role: {role}"}

            self.agent_collaborator.add_agent(agent_id, role_enum, capabilities)

            return {
                "status": "success",
                "agent_id": agent_id,
                "message": "Agent added successfully",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def remove_collaboration_agent(self, agent_id: str) -> dict:
        """移除协作代理"""
        try:
            success = self.agent_collaborator.remove_agent(agent_id)
            if not success:
                return {"status": "error", "message": "Agent not found"}

            return {
                "status": "success",
                "agent_id": agent_id,
                "message": "Agent removed successfully",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── 结果处理 ──────────────────────────────────────────────────────
    @staticmethod
    def _to_result(task: SquadTask, raw: Any) -> SquadResult:
        if isinstance(raw, Exception):
            return SquadResult(
                task.squad_id, task.role, "failed", output=None, error={"exc": str(raw)}
            )
        return SquadResult(
            squad_id=task.squad_id,
            role=task.role,
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
                {
                    "id": r.squad_id,
                    "role": r.role,
                    "status": r.status,
                    "output": r.output,
                    "error": r.error,
                    "cost_usd": r.cost_usd,
                }
                for r in results
            ],
            "cost_usd": total_cost,
        }

    # ── P3: autonomous_agent 方法 ──────────────────────────────────────
    async def autonomous_plan(
        self, goal: str, description: str, context: dict | None = None
    ) -> dict:
        """自主规划任务"""
        try:
            from veya.autonomous_agent import AgentGoal

            goal_map = {
                "code_generation": AgentGoal.CODE_GENERATION,
                "problem_solving": AgentGoal.PROBLEM_SOLVING,
                "system_design": AgentGoal.SYSTEM_DESIGN,
                "code_review": AgentGoal.CODE_REVIEW,
                "learning": AgentGoal.LEARNING,
            }

            agent_goal = goal_map.get(goal.lower())
            if not agent_goal:
                return {"status": "error", "message": f"Invalid goal: {goal}"}

            steps = self.autonomous_agent.plan_goal(agent_goal, description, context or {})
            plan_id = (
                next(iter(self.autonomous_agent.plans.keys()))
                if self.autonomous_agent.plans
                else "unknown"
            )

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
            return {"status": "error", "message": str(e)}

    async def visualize_code(self, ast_data: dict | None = None, format: str = "cytoscape") -> dict:
        """可视化代码图谱"""
        try:
            if ast_data and "symbols" in ast_data:
                # 从 AST 数据构建图谱
                for symbol in ast_data["symbols"]:
                    from veya.visualization import GraphNode

                    node = GraphNode(
                        node_id=symbol.get("id", ""),
                        label=symbol.get("name", ""),
                        type=symbol.get("type", ""),
                        attributes={"file": symbol.get("file", "")},
                    )
                    self.code_graph.add_node(node)

            metrics = self.code_graph.calculate_metrics()

            if format == "cytoscape":
                output = self.code_graph.export_to_cytoscape()
            elif format == "json":
                output = self.code_graph.export_to_json()
            elif format == "image":
                image_data = self.code_graph.generate_image()
                output = {"image": image_data} if image_data else {}
            else:
                output = {"error": f"Unsupported format: {format}"}

            return {"status": "success", "metrics": metrics, "output": output}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def translate_code(self, source_code: str, source_lang: str, target_lang: str) -> dict:
        """跨语言代码翻译"""
        try:
            from veya.cross_language import Language

            source_lang_enum = getattr(Language, source_lang.upper(), None)
            target_lang_enum = getattr(Language, target_lang.upper(), None)

            if not source_lang_enum:
                return {"status": "error", "message": f"Invalid source language: {source_lang}"}
            if not target_lang_enum:
                return {"status": "error", "message": f"Invalid target language: {target_lang}"}

            result = self.cross_language_translator.translate(
                source_code, source_lang_enum, target_lang_enum
            )

            return {
                "status": "success",
                "translation": {
                    "source_language": source_lang,
                    "target_language": target_lang,
                    "source_code": result.source_code,
                    "target_code": result.target_code,
                    "confidence": result.confidence,
                    "warnings": result.warnings,
                },
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def analyze_language_project(self, project_path: str) -> dict:
        """分析项目多语言文件"""
        try:
            stats = self.cross_language_translator.analyze_project(project_path)
            return {"status": "success", "language_stats": stats}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# 模块级单例(server 复用)
coordinator = Coordinator()
