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

import os
from typing import Any, Awaitable, Callable

from veya.omodul.agent_loop import AgentLoop, LoopResult
from veya.omodul.tool_pipeline import ToolPipeline

# 工具注入形态: {name: (fn, schema | None)}; fn 可为 async
ToolRegistry = dict[str, tuple[Callable[..., Any] | Callable[..., Awaitable[Any]], dict | None]]


def strict_loop_enabled() -> bool:
    """feature flag: VEYA_AGENT_LOOP=strict → 新 omodul 心脏。"""
    return os.environ.get("VEYA_AGENT_LOOP", "").strip().lower() == "strict"


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


__all__ = ["run_strict", "strict_loop_enabled"]
