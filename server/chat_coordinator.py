"""server/chat_coordinator.py — general-purpose chat, decoupled from the Coordinator/Genesis flow.

No tools, no ReAct loop: a single llm_call per turn plus in-memory per-session history
(same pattern as server/routes/session.py's _sessions). Exists specifically to host the
Artifacts protocol (see CHAT_SYSTEM_PROMPT below), which needs a freeform prose reply —
unlike server.coordinator.RequirementCoordinator, whose only output is a structured
RequirementDoc via a forced tool call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from server.omni_gateway import DISPATCH_TOOL_NAME, omni_gateway
from veya.llm import calc_cost, get_provider_config, llm_call

logger = logging.getLogger("chat_coordinator")

GRID_SEARCH_TOOL_NAME = "system_dispatch_grid_search"

CHAT_SYSTEM_PROMPT = (
    "You are Veya, a helpful engineering assistant.\n"
    "\n"
    "# ARTIFACTS PROTOCOL (UI & CHARTS)\n"
    "If the user asks for a UI component, a data dashboard, or a chart, you MUST output a "
    "dynamic artifact. Wrap the executable code in this exact XML format:\n"
    '<veya-artifact type="react" title="Name of the Component">\n'
    "// your code here\n"
    "</veya-artifact>\n"
    "\n"
    "Rules for react artifacts:\n"
    "1. You have access to Tailwind CSS classes.\n"
    "2. You have access to `React`, `ReactDOM`, and `echarts` (Apache ECharts) via the global "
    "window object.\n"
    "3. You MUST define a main component (e.g. `App`) and render it to the DOM at the end of "
    "your code like this:\n"
    "   `const root = ReactDOM.createRoot(document.getElementById('root')); "
    "root.render(<App />);`\n"
    "4. Do NOT use import statements. Assume React and echarts are globally available.\n"
    "\n"
    "# DISTRIBUTION\n"
    "If the user asks you to send, post, publish, or distribute content to an external "
    "platform (e.g. Feishu, Xiaohongshu, Twitter/X), call the "
    f"'{DISPATCH_TOOL_NAME}' tool instead of describing how you would do it manually.\n"
    "\n"
    "# GRID SEARCH\n"
    "If the user asks you to backtest a strategy across multiple parameter combinations "
    f"(a grid search), call the '{GRID_SEARCH_TOOL_NAME}' tool and do NOT wait for the "
    "result yourself — it runs in the background and the user is notified when it "
    "completes. Tell the user it has been submitted; do not pretend to already have results.\n"
)

# In-memory per-session history — same pattern as server/routes/session.py's _sessions.
_sessions: dict[str, list[dict[str, Any]]] = {}

# asyncio only holds a weak reference to a bare create_task() result — without a strong
# reference kept somewhere, the task can be GC'd mid-execution. Grid-search background
# tasks are now owned by the Automata daemon (start_grid_search_task), which keeps its
# own strong refs; nothing left here.


def _cost_of(response: dict[str, Any], *, config, provider, model) -> float:
    usage = response.get("usage") or {}
    if not usage:
        return 0.0
    resolved_provider, _ = get_provider_config(config, provider=provider, model=model)
    return calc_cost(resolved_provider, usage)


async def _run_dispatch_tool(tool_args: dict[str, Any]) -> str:
    try:
        return await omni_gateway.execute_dispatch(
            tool_args.get("targets") or [],
            tool_args.get("title") or "",
            tool_args.get("content") or "",
        )
    except Exception as exc:
        logger.warning("[chat_coordinator] dispatch tool failed: %s", exc)
        return f"dispatch failed: {exc}"


def _grid_search_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": GRID_SEARCH_TOOL_NAME,
            "description": (
                "Submit a heavy CPU-bound grid-search backtest to the background compute "
                "cluster. Do NOT wait for the result — this returns immediately with an "
                "acknowledgement; progress and the final result arrive as system toasts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "identifier of the market data parquet file to backtest against",
                    },
                    "strategy_code": {
                        "type": "string",
                        "description": (
                            "Python source defining run_strategy(df, hyper_params) -> df, "
                            "returning a DataFrame with daily_return and cum_return columns."
                        ),
                    },
                    "param_grid": {
                        "type": "object",
                        "description": (
                            "dict mapping each hyperparameter name to a list of values to "
                            "grid-search over, e.g. {'window': [10, 20, 30]}"
                        ),
                    },
                },
                "required": ["asset_id", "strategy_code", "param_grid"],
            },
        },
    }


def _submit_grid_search(tool_args: dict[str, Any], session_id: str) -> str:
    """主脑异步脱壳 (Fire-and-Forget): 工单扔给 Automata, 立即返回。"""
    from server.automata import get_automata

    task_id = get_automata().start_grid_search_task(
        tool_args.get("asset_id") or "",
        tool_args.get("strategy_code") or "",
        tool_args.get("param_grid") or {},
        session_id=session_id,
    )
    return (
        f"已提交至后台计算集群 (工单 {task_id})。任务在 Automata 守护进程中运行，"
        "进度与结果将通过系统弹窗实时播报，您无需等待，可继续其他工作。"
    )


async def chat(
    text: str,
    *,
    session_id: str,
    model: str | None = None,
    provider: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One chat turn. Returns {"content": assistant reply, "cost_usd": float}.

    Bounded tool round: if the model calls the dispatch tool, execute it, feed the
    result back, and make ONE more (tool-free) call for the final reply — not a full
    ReAct loop, since there's exactly one tool and no retry/reflection need here.
    """
    history = _sessions.setdefault(session_id, [])
    history.append({"role": "user", "content": text})

    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}, *history]
    response = await llm_call(
        messages,
        tools=[omni_gateway.get_llm_schema(), _grid_search_tool_schema()],
        model=model,
        provider=provider,
        config=config,
        max_tokens=4096,
    )
    total_cost = _cost_of(response, config=config, provider=provider, model=model)

    message = (response.get("choices") or [{}])[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []

    if not tool_calls:
        content = message.get("content") or ""
        history.append({"role": "assistant", "content": content})
        return {"content": content, "cost_usd": round(total_cost, 6)}

    history.append(
        {"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls}
    )
    for tool_call in tool_calls:
        fn = tool_call.get("function") or {}
        tool_name = fn.get("name", "")
        raw_args = fn.get("arguments") or "{}"
        try:
            tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            tool_args = {}

        if tool_name == DISPATCH_TOOL_NAME:
            tool_result = await _run_dispatch_tool(tool_args)
        elif tool_name == GRID_SEARCH_TOOL_NAME:
            tool_result = _submit_grid_search(tool_args, session_id)
        else:
            tool_result = f"unknown tool '{tool_name}'"

        history.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id", f"call_{tool_name}"),
                "content": tool_result,
            }
        )

    follow_up = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}, *history]
    response2 = await llm_call(
        follow_up, model=model, provider=provider, config=config, max_tokens=4096
    )
    total_cost += _cost_of(response2, config=config, provider=provider, model=model)
    content = ((response2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    history.append({"role": "assistant", "content": content})
    return {"content": content, "cost_usd": round(total_cost, 6)}
