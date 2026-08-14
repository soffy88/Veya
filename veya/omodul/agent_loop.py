"""veya/omodul/agent_loop — omodul_agent_loop（主循环心脏）。

生成 → 调用 → 工具 → 更新树 → 停止判断 + 熔断/退避：

    for round in range(max_rounds):
        ctx      = tree.messages(sid)            # 时空回溯上下文
        ctx      = context_compress 滑窗裁剪
        msgs     = protocol_translate 打包        # 纯函数
        resp     = oprim.llm_call(注入 client)    # 物理触手
        agent_msg= llm_message_to_agent 翻译
        tree.append(assistant)
        calls    = parse_tool_calls
        decision = evaluate_stop_condition
        stop? → 收尾
        for call: ToolPipeline.run_call → tree.append(tool 结果)
        连续失败 ≥ max_consecutive_errors → 熔断停止

注入（全部经句柄/接口，零直接 I/O）:
    llm / pipeline / tree / barrier — 默认取 container 全局句柄或新建
    system_prompt / max_rounds / max_consecutive_errors / backoff_sleep

事件流: agent_loop.round / agent_loop.tool_result / agent_loop.done
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline, ToolRunResult
from veya.oprim.event import emit_event
from veya.oprim.llm import llm_call as _oprim_llm_call
from veya.oskill.pure.context_compress import sliding_window
from veya.oskill.pure.evaluate_stop_condition import StopDecision, evaluate_stop_condition
from veya.oskill.pure.parse_tool_call import parse_tool_calls
from veya.oskill.pure.protocol_translate import agent_messages_to_llm, llm_message_to_agent

_MAX_CTX_MESSAGES = 40


@dataclass
class LoopResult:
    """主循环结果。"""

    session_id: str
    final_answer: str = ""
    rounds: int = 0
    stop_kind: str = "continue"
    stop_reason: str = ""
    tool_calls: int = 0
    tool_failures: int = 0
    error: str = ""
    snapshot: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "final_answer": self.final_answer,
            "rounds": self.rounds,
            "stop_kind": self.stop_kind,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "error": self.error,
        }


class AgentLoop:
    """注入式主循环：LLM ↔ 工具 ↔ 会话树，纯机制、零业务。"""

    def __init__(
        self,
        *,
        llm: Any = None,
        pipeline: ToolPipeline | None = None,
        tree: SessionTreeMgr | None = None,
        barrier: Any = None,
        system_prompt: str = "",
        max_rounds: int = 10,
        max_consecutive_errors: int = 3,
        backoff_sleep: float = 1.0,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        gate: Callable[[], Awaitable[None]] | None = None,
        context_providers: list[Callable[[str, str], Awaitable[str]]] | None = None,
        on_finish: Callable[[str, list[dict]], Awaitable[None]] | None = None,
    ) -> None:
        """gate: 每轮开始前 await 的挂起检查点（阶段 5 daemon 注入：
        paused 时阻塞等待 resume；默认 None = 无挂起能力，行为不变）。

        context_providers: 每轮构造 LLM 消息前的上下文注入钩子
            (sid, user_input) → 附加文本块（记忆/代码地图等，刷新不累积）；
        on_finish: 循环结束回调 (sid, 最终消息列表) → 蒸馏/落库等。
        """
        if pipeline is None:
            pipeline = ToolPipeline(barrier=barrier)
        if tree is None:
            tree = SessionTreeMgr()
        self._llm = llm  # None → oprim.llm_call 默认 container 句柄
        self._pipeline = pipeline
        self._tree = tree
        self._barrier = barrier
        self._system_prompt = system_prompt
        self._max_rounds = max(1, max_rounds)
        self._max_consecutive_errors = max(1, max_consecutive_errors)
        self._backoff_sleep = backoff_sleep
        self._sleep = sleep_fn or asyncio.sleep
        self._gate = gate
        self._context_providers = list(context_providers or [])
        self._on_finish = on_finish

    # ------------------------------------------------------------------ 主循环

    async def run(self, user_input: str, *, session_id: str | None = None) -> LoopResult:
        """执行一轮完整对话（生成 → 工具 → 更新树 → 停止判断）。"""
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input 不能为空")

        # 外部传入 session_id: 树中不存在时用该 sid 创建（兼容旧历史会话 id）
        if session_id is not None and self._tree.leaf(session_id) is None:
            self._tree.ensure_session(session_id, system=self._system_prompt or None)
        sid = session_id or self._tree.create_session(system=self._system_prompt or None)
        self._tree.append(sid, role="user", content=user_input)
        emit_event("agent_loop.start", {"session_id": sid}, barrier=self._barrier)

        result = LoopResult(session_id=sid)
        consecutive_errors = 0
        decision: StopDecision | None = None

        for round_no in range(self._max_rounds):
            # 0. 挂起检查点（daemon 注入；paused 时阻塞等待 resume）
            if self._gate is not None:
                await self._gate()
            # 1. 上下文（时空回溯路径 + 滑窗压缩 + 注入钩子）
            ctx = self._tree.messages(sid)
            ctx = sliding_window(ctx, max_messages=_MAX_CTX_MESSAGES)
            for provider in self._context_providers:
                block = await provider(sid, user_input)
                if block:
                    ctx = [{"role": "system", "content": block}] + ctx
            msgs = agent_messages_to_llm(ctx)

            # 2. LLM 调用（物理触手；异常 = 致命错误）
            # tools 声明随请求发出（OpenAI 格式）→ 模型返回结构化 tool_calls
            tools = self._pipeline.schemas()
            try:
                resp = await _oprim_llm_call(msgs, client=self._llm, tools=tools or None)
            except Exception as exc:  # noqa: BLE001
                result.stop_kind = "fatal_error"
                result.stop_reason = f"LLM 调用失败: {exc}"
                result.error = str(exc)
                break

            # 3. 翻译 + 入树
            agent_msg = llm_message_to_agent((resp.get("choices") or [{}])[0].get("message") or {})
            content = agent_msg.get("content") or ""
            self._tree.append(
                sid, role="assistant", content=content,
                tool_calls=agent_msg.get("tool_calls") or [],
            )

            # 4. 解析 + 停止判断
            calls = parse_tool_calls(agent_msg)
            decision = evaluate_stop_condition(
                round_count=round_no,
                max_rounds=self._max_rounds,
                tool_calls=calls,
                last_content=content,
            )
            if decision.stop:
                result.stop_kind = decision.kind
                result.stop_reason = decision.reason
                result.final_answer = (
                    content
                    if decision.kind == "completed"
                    else f"循环停止 ({decision.kind}): {decision.reason}"
                )
                break

            # 5. 工具执行（经 ToolPipeline 五步管道）+ 入树
            round_ok = True
            for call in calls:
                tr: ToolRunResult = await self._pipeline.run_call(call, session_id=sid)
                result.tool_calls += 1
                if tr.ok:
                    consecutive_errors = 0
                    self._tree.append(
                        sid, role="tool", content=tr.output,
                        meta={"tool": call.name, "ok": True, "error": "",
                              "tool_call_id": call.id},
                    )
                else:
                    round_ok = False
                    result.tool_failures += 1
                    consecutive_errors += 1
                    self._tree.append(
                        sid, role="tool", content=tr.error or "(无输出)",
                        meta={"tool": call.name, "ok": False, "error": tr.error,
                              "rejected": tr.rejected, "stage": tr.reject_stage,
                              "tool_call_id": call.id},
                    )
                emit_event(
                    "agent_loop.tool_result",
                    {"session_id": sid, "tool": call.name, "ok": tr.ok, "error": tr.error},
                    barrier=self._barrier,
                )

            # 6. 熔断/退避：连续失败达到上限 → 提前停止
            if consecutive_errors >= self._max_consecutive_errors:
                result.stop_kind = "fatal_error"
                result.stop_reason = (
                    f"工具连续失败 {consecutive_errors} 次, 触发熔断 (退避 {self._backoff_sleep}s)"
                )
                result.error = result.stop_reason
                await self._sleep(self._backoff_sleep)
                break

            emit_event(
                "agent_loop.round",
                {"session_id": sid, "round": round_no, "tools_ok": round_ok},
                barrier=self._barrier,
            )
        else:
            # 循环自然耗尽 = 达到最大轮次
            result.stop_kind = "max_rounds"
            result.stop_reason = f"达到最大轮次 {self._max_rounds}"

        result.rounds = min(round_no + 1, self._max_rounds)
        if result.stop_kind == "continue":
            result.stop_kind = "max_rounds"
            result.stop_reason = f"达到最大轮次 {self._max_rounds}"
        result.snapshot = self._tree.snapshot(sid)
        emit_event(
            "agent_loop.done",
            {"session_id": sid, "stop_kind": result.stop_kind, "rounds": result.rounds},
            barrier=self._barrier,
        )
        # 结束回调（蒸馏/落库；失败不阻断返回）
        if self._on_finish is not None:
            with contextlib.suppress(Exception):
                await self._on_finish(sid, self._tree.messages(sid))
        return result


__all__ = ["AgentLoop", "LoopResult"]
