"""veya/omodul/agent_loop — omodul_agent_loop（主循环心脏）。

生成 → 调用 → 工具 → 更新树 → 停止判断 + 熔断/退避：

    while not terminal:
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
        无进展/重复动作/资源耗尽 → 安全停止

注入（全部经句柄/接口，零直接 I/O）:
    llm / pipeline / tree / barrier — 默认取 container 全局句柄或新建
    system_prompt / max_consecutive_errors / backoff_sleep

事件流: agent_loop.round / agent_loop.tool_result / agent_loop.done
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from veya.omodul.session_tree import SessionTreeMgr
from veya.omodul.tool_pipeline import ToolPipeline, ToolRunResult
from veya.oprim.event import emit_event
from veya.oprim.llm import llm_call as _oprim_llm_call
from veya.oskill.pure.context_compress import sliding_window
from veya.oskill.pure.evaluate_stop_condition import StopDecision, evaluate_stop_condition
from veya.oskill.pure.parse_tool_call import parse_tool_calls
from veya.oskill.pure.protocol_translate import agent_messages_to_llm, llm_message_to_agent

_MAX_CTX_MESSAGES = 40


def _canonical_action_fp(tool_name: str, args: Any) -> str:
    """Canonical action identity: tool name + stably sorted JSON arguments."""
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            canonical_args = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        except Exception:
            canonical_args = args.strip()
    elif isinstance(args, dict):
        canonical_args = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    else:
        canonical_args = str(args)
    return f"{tool_name}:{canonical_args}"


def _normalize_evidence_fp(output: Any, error: Any = None) -> str:
    """Normalize tool execution result to a stable evidence fingerprint."""
    raw = (str(output) if output is not None else "") + (f"|err:{error}" if error else "")
    norm = " ".join(raw.strip().split())
    if len(norm) > 128:
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return norm


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
    cost_usd: float = 0.0
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
            "cost_usd": self.cost_usd,
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
        max_consecutive_errors: int = 3,
        backoff_sleep: float = 1.0,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        gate: Callable[[], Awaitable[None]] | None = None,
        context_providers: list[Callable[[str, str], Awaitable[str]]] | None = None,
        on_finish: Callable[[str, list[dict]], Awaitable[None]] | None = None,
        budget_usd: float | None = None,
        cost_calculator: Callable[[dict], float] | None = None,
        **_legacy_kwargs: Any,
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
        self._max_consecutive_errors = max(1, max_consecutive_errors)
        self._backoff_sleep = backoff_sleep
        self._sleep = sleep_fn or asyncio.sleep
        self._gate = gate
        self._context_providers = list(context_providers or [])
        self._on_finish = on_finish
        if budget_usd is not None and budget_usd < 0:
            raise ValueError("budget_usd must be non-negative")
        self._budget_usd = budget_usd
        self._cost_calculator = cost_calculator

    # ------------------------------------------------------------------ 主循环

    async def run(
        self, user_input: str, *, session_id: str | None = None, owner: str | None = None
    ) -> LoopResult:
        """执行一轮完整对话（生成 → 工具 → 更新树 → 停止判断）。

        owner: 会话归属 (已鉴权 user_id)。传了 session_id 且该会话已存在时,
        由 SessionTreeMgr.ensure_session 校验归属——不属于当前 owner 的
        sid 会被拒绝, 而不是静默放行读写 (2026-08-16 修复: 此前完全没有
        归属校验, 拿到/猜到别人的 sid 就能续接读写其会话树)。
        """
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input 不能为空")

        # 外部传入 session_id: 树中不存在则创建, 存在则校验归属 (ensure_session
        # 统一处理两种情况)。
        try:
            if session_id is not None:
                sid = self._tree.ensure_session(
                    session_id, system=self._system_prompt or None, owner=owner
                )
            else:
                sid = self._tree.create_session(system=self._system_prompt or None, owner=owner)
        except PermissionError as exc:
            return LoopResult(
                session_id=session_id or "",
                stop_kind="fatal_error",
                stop_reason=str(exc),
                error=str(exc),
                final_answer=f"⚠ {exc}",
            )
        self._tree.append(sid, role="user", content=user_input)
        emit_event("agent_loop.start", {"session_id": sid}, barrier=self._barrier)

        result = LoopResult(session_id=sid)
        consecutive_errors = 0
        decision: StopDecision | None = None
        round_count = 0
        start_time = time.monotonic()
        # Safety limits (configurable via env for product tuning)
        MAX_WALL_TIME_S = float(os.environ.get("VEYA_MAX_WALL_TIME_S", "0") or 0)
        MAX_NO_PROGRESS_ROUNDS = int(os.environ.get("VEYA_MAX_NO_PROGRESS_ROUNDS", "3") or 3)
        action_evidence_seen: set[tuple[str, str]] = set()
        no_progress_rounds = 0

        while True:
            round_count += 1
            # 0. 挂起检查点（daemon 注入；paused 时阻塞等待 resume）
            if self._gate is not None:
                await self._gate()
            # Safety: wall-clock deadline
            if MAX_WALL_TIME_S > 0 and (time.monotonic() - start_time) >= MAX_WALL_TIME_S:
                result.stop_kind = "safety_resource_exhausted"
                result.stop_reason = f"wall-clock deadline exceeded ({MAX_WALL_TIME_S}s)"
                result.error = result.stop_reason
                result.final_answer = (
                    f"本次执行已暂停 (安全/资源限制: {result.stop_reason})。可继续运行。"
                )
                break
            if self._budget_usd is not None and result.cost_usd >= self._budget_usd:
                result.stop_kind = "safety_resource_exhausted"
                result.stop_reason = f"预算上限 ${self._budget_usd:.6f} 已用尽"
                result.error = result.stop_reason
                result.final_answer = f"⚠ {result.stop_reason}"
                break
            # 1. 上下文（时空回溯路径 + 滑窗压缩 + 注入钩子）
            ctx = self._tree.messages(sid)
            ctx = sliding_window(ctx, max_messages=_MAX_CTX_MESSAGES)
            for provider in self._context_providers:
                block = await provider(sid, user_input)
                if block:
                    ctx = [{"role": "system", "content": block}, *ctx]
            msgs = agent_messages_to_llm(ctx)

            # 2. LLM 调用（物理触手；异常 = 致命错误）
            # tools 声明随请求发出（OpenAI 格式）→ 模型返回结构化 tool_calls
            tools = self._pipeline.schemas()
            try:
                resp = await _oprim_llm_call(msgs, client=self._llm, tools=tools or None)
            except Exception as exc:
                result.stop_kind = "fatal_error"
                result.stop_reason = f"LLM 调用失败: {exc}"
                result.error = str(exc)
                # final_answer 绝不留空: 否则真实原因在这里被吞, 调用方只能看到
                # 通用的"网关抖动"兜底文案 (result.error 不会被所有调用方转发)。
                result.final_answer = (
                    f"⚠ 模型调用失败: {exc}\n请重试，或检查 API key / 网络连通性。"
                )
                break

            try:
                if self._cost_calculator is not None:
                    result.cost_usd += max(0.0, float(self._cost_calculator(resp)))
                elif isinstance(resp.get("cost_usd"), (int, float)):
                    result.cost_usd += max(0.0, float(resp["cost_usd"]))
                elif isinstance(resp.get("usage"), dict):
                    usage_cost = resp["usage"].get("cost_usd")
                    if isinstance(usage_cost, (int, float)):
                        result.cost_usd += max(0.0, float(usage_cost))
            except (TypeError, ValueError):
                # Malformed provider usage must not bypass loop safeguards.
                pass
            if self._budget_usd is not None and result.cost_usd > self._budget_usd:
                result.stop_kind = "safety_resource_exhausted"
                result.stop_reason = (
                    f"本轮估算成本 ${result.cost_usd:.6f} 超过预算上限 ${self._budget_usd:.6f}"
                )
                result.error = result.stop_reason
                result.final_answer = f"⚠ {result.stop_reason}"
                break

            # 3. 翻译 + 入树
            agent_msg = llm_message_to_agent((resp.get("choices") or [{}])[0].get("message") or {})
            content = agent_msg.get("content") or ""
            self._tree.append(
                sid,
                role="assistant",
                content=content,
                tool_calls=agent_msg.get("tool_calls") or [],
            )

            # 4. 解析 + 停止判断（语义化）
            calls = parse_tool_calls(agent_msg)
            decision = evaluate_stop_condition(
                round_count=round_count,
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
            new_evidence_in_batch = False
            for call in calls:
                tr: ToolRunResult = await self._pipeline.run_call(call, session_id=sid)
                result.tool_calls += 1
                action_fp = _canonical_action_fp(call.name, call.arguments)
                evidence_fp = _normalize_evidence_fp(tr.output, tr.error)
                if (action_fp, evidence_fp) not in action_evidence_seen:
                    new_evidence_in_batch = True
                    action_evidence_seen.add((action_fp, evidence_fp))
                if tr.ok:
                    consecutive_errors = 0
                    self._tree.append(
                        sid,
                        role="tool",
                        content=tr.output,
                        meta={"tool": call.name, "ok": True, "error": "", "tool_call_id": call.id},
                    )
                else:
                    round_ok = False
                    result.tool_failures += 1
                    consecutive_errors += 1
                    self._tree.append(
                        sid,
                        role="tool",
                        content=tr.error or "(无输出)",
                        meta={
                            "tool": call.name,
                            "ok": False,
                            "error": tr.error,
                            "rejected": tr.rejected,
                            "stage": tr.reject_stage,
                            "tool_call_id": call.id,
                        },
                    )
                emit_event(
                    "agent_loop.tool_result",
                    {"session_id": sid, "tool": call.name, "ok": tr.ok, "error": tr.error},
                    barrier=self._barrier,
                )

            # 6. 熔断/退避：连续失败达到上限 → 致命错误停止
            if consecutive_errors >= self._max_consecutive_errors:
                result.stop_kind = "fatal_error"
                result.stop_reason = (
                    f"工具连续失败 {consecutive_errors} 次, 触发熔断 (退避 {self._backoff_sleep}s)"
                )
                result.error = result.stop_reason
                last_tool_error = (tr.error or "") if not tr.ok else ""
                result.final_answer = (
                    f"⚠ {result.stop_reason}"
                    + (f"\n最近一次错误: {last_tool_error}" if last_tool_error else "")
                    + "\n请重试，或换一种方式描述任务。"
                )
                await self._sleep(self._backoff_sleep)
                break

            # 7. 无进展/重复动作检测（安全护栏，非语义路由）
            if calls:
                if new_evidence_in_batch:
                    no_progress_rounds = 0
                else:
                    no_progress_rounds += 1

            if no_progress_rounds >= MAX_NO_PROGRESS_ROUNDS:
                result.stop_kind = "no_progress_detected"
                result.stop_reason = f"连续 {no_progress_rounds} 轮无新进展 (工具结果重复/无新证据)"
                result.error = result.stop_reason
                result.final_answer = f"当前执行没有继续取得有效进展: {result.stop_reason}"
                break

            emit_event(
                "agent_loop.round",
                {"session_id": sid, "round": round_count, "tools_ok": round_ok},
                barrier=self._barrier,
            )

        result.rounds = round_count
        # 兜底: 如果 final_answer 仍为空，从会话树回填
        if not result.final_answer.strip():
            last_assistant = ""
            for msg in reversed(self._tree.messages(sid)):
                if (
                    msg.get("role") == "assistant"
                    and not msg.get("tool_calls")
                    and (msg.get("content") or "").strip()
                ):
                    last_assistant = msg["content"]
                    break
            if last_assistant:
                result.final_answer = last_assistant
            elif result.tool_calls > 0:
                result.final_answer = (
                    f"已执行 {result.tool_calls} 次工具调用 "
                    f"({result.tool_calls - result.tool_failures} 成功/{result.tool_failures} 失败), "
                    "但未给出总结。可以让我接着处理，或换个更具体的说法。"
                )
            else:
                result.final_answer = (
                    "未产出回答，且未执行任何工具。请重试，或检查模型/网关是否正常。"
                )
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
