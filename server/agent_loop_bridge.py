"""server/agent_loop_bridge — omodul.AgentLoop 执行原语 (供工具调用, 非主链)。

2026-08-17 架构澄清 (docs/ARCHITECTURE_STABLE.md「冻结架构」): 面向用户的
唯一主链是 server/coordinator_master.py → 主库 MasterAgent ReAct 循环 (全量
工具面, 模型自主判断)。omodul.AgentLoop 不是第二条主链，run_strict_chat 只
被 server/tool_registry.py 的 agent_loop_run 工具调用，为 MasterAgent 委托的
隔离子任务提供一个注入式心脏 (独立会话/工具面/轮次上限)，完成后把结果文本
带回主链——不接管用户请求，不产生第二套持久会话历史。

用法: 代码内 await run_strict(user_prompt, tools={...}) / run_strict_chat(...)。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from server.events import _task_id_ctx, bind_event_context, reset_event_context
from veya.omodul.agent_loop import AgentLoop, LoopResult
from veya.omodul.tool_pipeline import ToolPipeline

# 工具注入形态: {name: (fn, schema | None)}; fn 可为 async
ToolRegistry = dict[str, tuple[Callable[..., Any] | Callable[..., Awaitable[Any]], dict | None]]
ToolExecutor = Callable[[str, dict[str, Any]], Any | Awaitable[Any]]


def _strict_tool_pipeline(
    schemas: list[dict],
    executor: ToolExecutor,
) -> ToolPipeline:
    """Build a pipeline whose callbacks all pass through one dispatcher.

    Schemas describe the complete model-visible surface; they do not grant a
    direct reference to a registry's physical callbacks.  Keeping dispatch
    behind ``executor`` preserves host guards and lets callers provide one
    coherent system/static/dynamic tool surface.
    """
    pipeline = ToolPipeline()

    def _callback(tool_name: str) -> Callable[..., Awaitable[Any]]:
        async def _execute(**kwargs: Any) -> Any:
            result = executor(tool_name, kwargs)
            return await result if inspect.isawaitable(result) else result

        return _execute

    for spec in schemas:
        function = spec["function"]
        name = function["name"]
        pipeline.register(
            name,
            _callback(name),
            schema=function.get("parameters"),
            description=function.get("description", ""),
        )
    return pipeline


class _BoundedLlm:
    """请求级 LLM 覆盖（config/provider/model/endpoint）透传 oprim_llm_call。"""

    def __init__(self, kwargs: dict, caller: Callable | None = None) -> None:
        self._kwargs = dict(kwargs)
        self._caller = caller

    async def complete(self, messages: list[dict], **kw: Any) -> dict:
        if self._caller is not None:
            result = self._caller(messages, **{**self._kwargs, **kw})
            return await result if inspect.isawaitable(result) else result
        from veya.oprim.llm import llm_call

        return await llm_call(messages, client=None, **{**self._kwargs, **kw})

    def stream(self, messages: list[dict], **kw: Any):
        from veya.oprim.llm import llm_stream

        return llm_stream(messages, client=None, **{**self._kwargs, **kw})

    async def close(self) -> None:
        return None


async def _relay_loop_events(barrier: Any, on_step: Callable | None = None) -> None:
    """事件桥：新心脏 barrier 事件 → on_step/fire_step（前端真实执行轨迹）。

    on_step 显式传入优先；否则 fire_step（命中 chat_stream 的 contextvar）。
    映射:
        agent_loop.tool_result → tool_call / tool_error
        agent_loop.round      → progress
    """
    from server.events import fire_step

    def _emit(ev: dict) -> None:
        if on_step is not None:
            on_step(ev)
        else:
            fire_step(ev)

    topics = ("agent_loop.round", "agent_loop.tool_result", "agent_loop.done")
    async for event in barrier.stream(*topics):
        p = event.payload or {}
        try:
            if event.topic == "agent_loop.done":
                break  # 自然退出：先处理完队列中的 tool_result 事件
            if event.topic == "agent_loop.tool_result":
                _emit(
                    {
                        "type": "tool_call",
                        "tool_name": p.get("tool", ""),
                        "status": "ok" if p.get("ok") else "error",
                        "session_id": p.get("session_id", ""),
                    }
                )
                if not p.get("ok"):
                    _emit(
                        {
                            "type": "tool_error",
                            "tool_name": p.get("tool", ""),
                            "error": p.get("error", ""),
                            "session_id": p.get("session_id", ""),
                        }
                    )
            elif event.topic == "agent_loop.round":
                _emit(
                    {
                        "type": "progress",
                        "session_id": p.get("session_id", ""),
                        "round": p.get("round"),
                    }
                )
        except Exception:  # 事件桥失败不阻断主流程
            pass


async def run_strict_chat(
    user_prompt: str,
    *,
    session_id: str | None = None,
    on_step: Callable | None = None,
    llm_kwargs: dict | None = None,
    max_rounds: int = 10,
    system_prompt: str = "",
    llm: Any = None,
    context_providers: list[Callable[[str, str], Awaitable[str]]] | None = None,
    on_finish: Callable[[str, list[dict]], Awaitable[None]] | None = None,
    kv_path: str | None = None,
    tool_schemas: list[dict] | None = None,
    tool_executor: ToolExecutor | None = None,
    llm_caller: Callable | None = None,
    budget_usd: float | None = None,
    deadline: str | datetime | None = None,
) -> dict:
    """omodul.AgentLoop 执行原语：用 AgentLoop 心脏跑一段隔离对话/子任务。

    调用方目前是 server/tool_registry.py 的 agent_loop_run 工具 (MasterAgent
    委托的隔离子任务)——不是主链入口。

    context_providers: 每轮注入钩子（记忆/代码地图）；on_finish: 结束回调；
    kv_path: 会话树持久化文件（默认 ~/.veya/loop/session_tree.db，容器
    veya-data volume 跨重启；隔离子任务用临时 session_id, 不与主链历史混）。
    tool_schemas/tool_executor: 可选的工具认知面与统一执行入口；应成对
    注入。缺省使用 master_tools 的 schema + execute，确保 ToolGuard 不被绕过。

    返回形态：{status, final_answer, rounds, tool_calls, session_id,
    stop_kind, loop_plane}。
    """
    from server import auth as auth_mod
    from server.tool_registry import master_tools
    from veya.obase.adapters import TelemetryEventBarrier
    from veya.omodul.agent_loop import AgentLoop
    from veya.omodul.session_tree import SessionTreeMgr

    _turn_started = time.time()
    if budget_usd is not None:
        try:
            budget_usd = float(budget_usd)
        except (TypeError, ValueError) as exc:
            raise ValueError("budget_usd must be a non-negative number") from exc
        if budget_usd < 0:
            raise ValueError("budget_usd must be a non-negative number")

    deadline_at: datetime | None = None
    if deadline:
        if isinstance(deadline, datetime):
            deadline_at = deadline
        else:
            try:
                deadline_at = datetime.fromisoformat(str(deadline).strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("deadline must be an ISO-8601 datetime") from exc
        if deadline_at.tzinfo is None:
            deadline_at = deadline_at.replace(tzinfo=UTC)
        deadline_at = deadline_at.astimezone(UTC)

    def _response_cost(response: dict) -> float:
        direct = response.get("cost_usd")
        if isinstance(direct, (int, float)):
            return float(direct)
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            return 0.0
        usage_cost = usage.get("cost_usd")
        if isinstance(usage_cost, (int, float)):
            return float(usage_cost)
        provider = str((llm_kwargs or {}).get("provider") or "")
        if not provider:
            return 0.0
        with contextlib.suppress(Exception):
            from veya.obase._llm_config import calc_cost

            return float(calc_cost(provider, usage))
        return 0.0

    # 会话归属: 已鉴权 user_id (由请求入口的 auth.get_current_user/set_user
    # 设过 contextvar); 未登录落 anonymous, 与 history_store 隔离口径一致。
    owner = auth_mod.current_user()["user_id"]

    # 空输入健壮性：路由层可能传入空串（旧路径容忍, 新路径需友好响应）
    if not user_prompt or not str(user_prompt).strip():
        return {
            "status": "success",
            "final_answer": "（空消息）请提供具体内容后重试。",
            "rounds": 0,
            "tool_calls": [],
            "session_id": session_id or "",
            "stop_kind": "completed",
            "loop_plane": "strict",
        }

    # 1. 工具认知面与执行面成对注入。默认仍是 master_tools，但物理执行只经
    # execute() 统一入口，不能直取 _functions 绕过 ToolGuard。
    effective_schemas = master_tools.get_all_schemas() if tool_schemas is None else tool_schemas
    effective_executor = master_tools.execute if tool_executor is None else tool_executor
    pipeline = _strict_tool_pipeline(effective_schemas, effective_executor)

    # 2. LLM（请求级覆盖透传；llm 显式注入优先——测试用）
    effective_llm = llm or (
        _BoundedLlm(llm_kwargs or {}, caller=llm_caller)
        if llm_kwargs or llm_caller is not None
        else None
    )

    # 3. 事件桥：barrier → fire_step（relay task 继承当前 contextvar）
    barrier = TelemetryEventBarrier()
    relay = asyncio.create_task(_relay_loop_events(barrier, on_step))
    # 让 relay 先运行到订阅注册（否则同步工具路径下事件全部丢失）
    await asyncio.sleep(0)

    loop = AgentLoop(
        llm=effective_llm,
        pipeline=pipeline,
        tree=SessionTreeMgr(kv=_session_kv(kv_path)),
        barrier=barrier,
        system_prompt=system_prompt,
        max_rounds=max_rounds,
        budget_usd=budget_usd,
        cost_calculator=_response_cost,
        context_providers=context_providers,
        on_finish=on_finish,
    )
    result = None
    timeout_error: str | None = None
    child_session_id = session_id or f"agent-loop-{uuid.uuid4().hex}"
    child_task_token = _task_id_ctx.set(None)
    event_context_tokens = bind_event_context(
        session_id=child_session_id,
        trace_id=child_session_id,
        turn_id=child_session_id,
    )
    try:
        run = loop.run(user_prompt, session_id=child_session_id, owner=owner)
        if deadline_at is None:
            result = await run
        else:
            remaining = (deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                run.close()
                timeout_error = "deadline exceeded before child task started"
            else:
                try:
                    result = await asyncio.wait_for(run, timeout=remaining)
                except TimeoutError:
                    timeout_error = "deadline exceeded"
    finally:
        reset_event_context(event_context_tokens)
        _task_id_ctx.reset(child_task_token)
        # 等待 relay 处理完排队事件（done 事件驱动自然退出）；超时兜底取消
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(relay), timeout=5)
        relay.cancel()

    if result is None:
        return {
            "status": "failed",
            "error": timeout_error or "child task did not return a result",
            "final_answer": f"⚠ {timeout_error or 'child task did not return a result'}",
            "rounds": 0,
            "tool_calls": [],
            "cost_usd": 0.0,
            "session_id": child_session_id,
            "stop_kind": "deadline_exceeded" if timeout_error else "failed",
            "loop_plane": "strict",
        }

    # 4. 返回形态兼容（tool_calls 明细从会话树快照提取）
    tool_trace: list[dict] = []
    snap = result.snapshot or {}
    for node in (snap.get("tree") or {}).get("nodes", {}).values():
        if node.get("role") == "tool":
            meta = node.get("meta") or {}
            tool_trace.append({"tool": meta.get("tool", ""), "ok": meta.get("ok", False)})
    _status = "success" if result.stop_kind in ("completed", "max_rounds") else "failed"
    # docs/VEYA_P1_P3_IMPLEMENTATION_SPEC.md §16 Trajectory (P2-09): 委托子任务
    # 有始有终、跟主链热路径隔离, 是"非 GoalRun 路径"里唯一现在就能安全接的一处
    # (对比 chat_stream() 见 server/trajectory.py 模块 docstring 的范围边界说明)。
    # 本地文件 append, 从不阻塞/从不影响返回结果。
    with contextlib.suppress(Exception):
        from server.trajectory import append_trajectory, build_trajectory

        append_trajectory(
            build_trajectory(
                task_id=result.session_id or (session_id or "unknown"),
                objective=user_prompt,
                outcome="completed" if _status == "success" else "failed",
                tool_calls=tool_trace,
                duration_ms=round((time.time() - _turn_started) * 1000),
                error=result.error,
            )
        )
    return {
        "status": _status,
        "final_answer": result.final_answer,
        # error 与旧路径 (master_agent.chat_stream) 返回形态对齐: chat_stream.py
        # 的 _finish() 在 final_answer 为空时会退回 result.get("error") 兜底——
        # 这里若不带上, AgentLoop 捕获到的具体失败原因 (LLM 调用异常等) 会在
        # 这一层被悄悄丢弃, 只剩通用的"网关抖动"文案。
        "error": result.error,
        "rounds": result.rounds,
        "tool_calls": tool_trace,
        "session_id": result.session_id,
        "stop_kind": result.stop_kind,
        "loop_plane": "strict",
        "cost_usd": round(float(result.cost_usd or 0.0), 6),
    }


async def run_strict(
    user_prompt: str,
    *,
    session_id: str | None = None,
    tools: ToolRegistry | None = None,
    llm: Any = None,
    system_prompt: str = "",
    max_rounds: int = 10,
) -> LoopResult:
    """用严格 3O 心脏执行一轮对话。

    - llm=None → container.get_llm()（obase LlmClient，默认适配 veya.obase.llm）
    - tools 注册到 ToolPipeline（五步管道：解析→校验→权限→执行→包装）
    - 返回 LoopResult（含 session_id / final_answer / stop_kind / snapshot）
    """
    pipeline = ToolPipeline()
    for name, (fn, schema) in (tools or {}).items():
        pipeline.register(name, fn, schema=schema)

    loop = AgentLoop(
        llm=llm,
        pipeline=pipeline,
        system_prompt=system_prompt,
        max_rounds=max_rounds,
    )
    return await loop.run(user_prompt, session_id=session_id)


def _session_kv(kv_path: str | None = None):
    """会话树持久化 KV：默认 ~/.veya/loop/session_tree.db（容器 veya-data
    volume 挂载 ~/.veya → 跨重启持久）；显式 kv_path 优先（测试隔离）。"""
    from pathlib import Path

    from veya.obase.adapters import SqliteKvStore

    if kv_path is None:
        kv_path = str(Path.home() / ".veya" / "loop" / "session_tree.db")
    path = Path(kv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteKvStore(str(path))


__all__ = ["run_strict", "run_strict_chat"]
