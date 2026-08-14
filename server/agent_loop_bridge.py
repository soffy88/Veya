"""server/agent_loop_bridge — 双轨运行桥（阶段 4，新增文件，不改主链）。

旧路径（默认）: server/coordinator_master.py → 主库 MasterAgent ReAct 循环。
新路径（VEYA_AGENT_LOOP=strict）: omodul_agent_loop 注入式心脏。

本桥是**新增装配点**：不修改任何现有文件，主链行为零变化；
阶段 5 oservi_api_gateway / daemon 直接调用本桥即可完成切换。

用法:
    VEYA_AGENT_LOOP=strict veya serve ...   # 或任何入口前设置 env
    代码内: await run_strict(user_prompt, tools={...})
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any, Awaitable, Callable

from veya.omodul.agent_loop import AgentLoop, LoopResult
from veya.omodul.tool_pipeline import ToolPipeline

# 工具注入形态: {name: (fn, schema | None)}; fn 可为 async
ToolRegistry = dict[str, tuple[Callable[..., Any] | Callable[..., Awaitable[Any]], dict | None]]


def strict_loop_enabled() -> bool:
    """feature flag: VEYA_AGENT_LOOP=strict → 新 omodul 心脏。"""
    return os.environ.get("VEYA_AGENT_LOOP", "").strip().lower() == "strict"


class _BoundedLlm:
    """请求级 LLM 覆盖（config/provider/model/endpoint）透传 oprim_llm_call。"""

    def __init__(self, kwargs: dict) -> None:
        self._kwargs = dict(kwargs)

    async def complete(self, messages: list[dict], **kw: Any) -> dict:
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
                _emit({
                    "type": "tool_call",
                    "tool_name": p.get("tool", ""),
                    "status": "ok" if p.get("ok") else "error",
                    "session_id": p.get("session_id", ""),
                })
                if not p.get("ok"):
                    _emit({
                        "type": "tool_error",
                        "tool_name": p.get("tool", ""),
                        "error": p.get("error", ""),
                        "session_id": p.get("session_id", ""),
                    })
            elif event.topic == "agent_loop.round":
                _emit({
                    "type": "progress",
                    "session_id": p.get("session_id", ""),
                    "round": p.get("round"),
                })
        except Exception:  # noqa: BLE001 — 事件桥失败不阻断主流程
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
) -> dict:
    """主链切换桥（VEYA_AGENT_LOOP=strict）：master_tools 全量工具面 + 提示词
    + SSE 事件桥 + 会话树，用新 omodul 心脏执行一轮对话。

    返回形态兼容旧 chat_stream：{status, final_answer, rounds, tool_calls,
    session_id, stop_kind, loop_plane}。
    """
    from server.tool_registry import master_tools
    from veya.obase.adapters import TelemetryEventBarrier
    from veya.obase.container import get_kv
    from veya.omodul.agent_loop import AgentLoop
    from veya.omodul.session_tree import SessionTreeMgr
    from veya.omodul.tool_pipeline import ToolPipeline

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

    # 1. 工具面全量注入（master_tools 静态 + mcp wire 后全量）
    pipeline = ToolPipeline()
    for spec in master_tools.get_all_schemas():
        name = spec["function"]["name"]
        fn = master_tools._functions.get(name)  # noqa: SLF001 — registry 内部形态
        if fn is not None:
            pipeline.register(
                name, fn,
                schema=spec["function"].get("parameters"),
                description=spec["function"].get("description", ""),
            )

    # 2. LLM（请求级覆盖透传；llm 显式注入优先——测试用）
    effective_llm = llm or (_BoundedLlm(llm_kwargs) if llm_kwargs else None)

    # 3. 事件桥：barrier → fire_step（relay task 继承当前 contextvar）
    barrier = TelemetryEventBarrier()
    relay = asyncio.create_task(_relay_loop_events(barrier, on_step))
    # 让 relay 先运行到订阅注册（否则同步工具路径下事件全部丢失）
    await asyncio.sleep(0)

    loop = AgentLoop(
        llm=effective_llm,
        pipeline=pipeline,
        tree=SessionTreeMgr(kv=get_kv()),
        barrier=barrier,
        system_prompt=system_prompt,
        max_rounds=max_rounds,
    )
    try:
        result = await loop.run(user_prompt, session_id=session_id)
    finally:
        # 等待 relay 处理完排队事件（done 事件驱动自然退出）；超时兜底取消
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(relay), timeout=5)
        relay.cancel()

    # 4. 返回形态兼容（tool_calls 明细从会话树快照提取）
    tool_trace: list[dict] = []
    snap = result.snapshot or {}
    for node in (snap.get("tree") or {}).get("nodes", {}).values():
        if node.get("role") == "tool":
            meta = node.get("meta") or {}
            tool_trace.append({"tool": meta.get("tool", ""), "ok": meta.get("ok", False)})
    return {
        "status": "success" if result.stop_kind in ("completed", "max_rounds") else "failed",
        "final_answer": result.final_answer,
        "rounds": result.rounds,
        "tool_calls": tool_trace,
        "session_id": result.session_id,
        "stop_kind": result.stop_kind,
        "loop_plane": "strict",
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


__all__ = ["run_strict", "run_strict_chat", "strict_loop_enabled"]
