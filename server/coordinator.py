"""
layer4/server/coordinator.py — 协调器主循环(veya 招牌)

实现 "聊天窗口分发命令 → 各分队执行 → 结构化结果回传"。
协调器拆任务 → 派角色分队(research/plan/execute)→ 分队 headless 执行
→ H4 hook 验证 → 结构化结果汇总。

每分队独立 context(orchestrator 运行时隔离),协调器只收摘要,
cost 经同一 CostTracker 跨引擎传播(§5.6 C1)。

checkpoint 在每个分队完成后落盘(obase.versionstore),
resume 从 RunState.completed_steps 跳过已完成分队继续跑。

认知模式 (Veya Core): 同文件提供 VeyaCoordinator — Plan-and-Solve × ReAct
多轮自循环状态机(DISCOVERY → PLANNING → EXECUTION ⇄ REFLECTION → DONE/FAILED)。
三大认知组件: 动态上下文切片(上下文压缩)、带反思的执行循环(错误回喂自纠)、
强约束系统提示词(CORE RULES)。Coordinator.handle(mode="cognitive") 路由进入该引擎。
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
from server.schemas import RequirementDoc
from veya.ast import create_ast_analyzer
from veya.autonomous_agent import create_autonomous_agent
from veya.cache import create_parallel_executor
from veya.context import SmartContextManager
from veya.cross_language import create_cross_language_translator
from veya.llm import calc_cost as _llm_calc_cost
from veya.llm import get_provider_config as _llm_get_provider_config
from veya.llm import llm_call as _llm_call
from veya.multimodal import create_multimodal_processor
from veya.performance import create_smart_cache
from veya.sandbox import SandboxConfig, create_safe_executor
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
# Veya Core 认知引擎 — Plan-and-Solve × ReAct 多轮自循环
#
# 彻底抛弃"一问一答"的单次 API 调用,改为状态机架构,最多允许大模型
# 在后台默默跑 N 轮(通常 N=5):
#
#   DISCOVERY(探索) → PLANNING(规划) → EXECUTION(执行) ⇄ REFLECTION(反思) → DONE / FAILED(HITL)
#
# 三大认知组件:
#   1. 动态上下文切片 (Context Slicing) — AST 骨架压缩 + 旧轮次工作日志压缩 + token 预算裁剪
#   2. 带反思的执行循环 (Reflection Loop) — 沙箱报错不返前端,包装成提示词回喂模型自纠
#   3. 强约束系统提示词 (System Prompt) — CORE RULES 逼迫模型像资深工程师一样思考
# =====================================================================

COGNITIVE_SYSTEM_PROMPT = (
    "You are Veya Core, an autonomous senior software engineer. You are operating in a headless environment.\n"
    "You have access to a suite of tools, including a 3O Core Engine for sandboxed execution and AST parsers for code discovery.\n"
    "\n"
    "# CORE RULES (STRICTLY ENFORCED)\n"
    "1. THINK BEFORE YOU ACT: Before making ANY code changes, you MUST use 'ast_search' or 'read_file' to understand the surrounding context.\n"
    "2. BE PARSIMONIOUS: Do NOT rewrite entire files if you only need to change 3 lines. Use the 'patch_file' tool.\n"
    "3. VERIFY EVERYTHING: After writing code, you MUST use the 'run_in_sandbox' tool to test it.\n"
    "4. SELF-CORRECTION: If a tool returns an error, do NOT immediately ask the user for help. Read the error trace, reflect on your mistake, and try a different approach. You have 5 internal retries before failing.\n"
    "5. NO HALLUCINATION: If you cannot find a required variable or file, STOP and ask the user. Do not invent paths.\n"
    "\n"
    "# WORKFLOW\n"
    "- Analyze Request -> Discover Context -> Formulate Plan -> Execute -> Test in Sandbox -> Finish.\n"
    "\n"
    "# STATE MACHINE PROTOCOL\n"
    "You operate inside an explicit cognitive state machine:\n"
    "- DISCOVERY (探索期): probe the codebase with 'ast_search' / 'grep' / 'read_file' / 'read_skeleton' / 'list_files'. "
    "Prefer 'read_skeleton' (AST signature map) over 'read_file' for large files to keep the context small.\n"
    "- PLANNING (规划期): call 'submit_plan' with a JSON array of executable steps (the Decision Trail).\n"
    "- EXECUTION (执行期): generate code with 'patch_file' / 'write_file', then IMMEDIATELY verify with 'run_in_sandbox'.\n"
    "- REFLECTION (反思期): when a tool fails, the traceback is fed back to you. Analyze the root cause and retry with a DIFFERENT approach.\n"
    "- FINISH: call 'finish' with your final answer only after the sandbox run passes.\n"
    "\n"
    "# OUTPUT DISCIPLINE\n"
    "- Tool arguments MUST be valid JSON.\n"
    "- Never fire a tool call without reasoning about the previous observation.\n"
    "- If the request cannot be satisfied with the available tools, call 'finish' and explain the blocker honestly.\n"
)


class CognitivePhase(StrEnum):
    """认知状态机阶段。"""

    DISCOVERY = "discovery"  # 探索期: AST/Grep 摸清项目结构
    PLANNING = "planning"  # 规划期: 产出 JSON 执行步骤列表 (Decision Trail)
    EXECUTION = "execution"  # 执行期: 3O 引擎生成代码 + 沙箱验证
    REFLECTION = "reflection"  # 反思期: 拦截错误回喂模型自纠
    DONE = "done"
    FAILED = "failed"


class ToolExecutionError(RuntimeError):
    """工具执行失败 → 触发反思回路(错误不回传前端)。"""


@dataclass
class DecisionTrail:
    """规划期产出的 JSON 执行步骤列表。"""

    steps: list[dict[str, Any]] = field(default_factory=list)
    submitted_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"steps": self.steps, "submitted_at": self.submitted_at}


# 探索类 / 执行类工具(驱动状态机迁移)
_DISCOVERY_TOOLS = frozenset({"ast_search", "grep", "read_file", "read_skeleton", "list_files"})
_EXECUTION_TOOLS = frozenset({"patch_file", "write_file", "run_in_sandbox"})
_WORKLOG_MAX_ENTRIES = 30


def _est_tokens(text: str) -> int:
    """Token 估算(字符数/4,与 SmartContextManager 一致)。"""
    return len(text) // 4


def _truncate(text: str, limit: int = 4000) -> str:
    """工具观察裁剪: 防止 Token 爆炸。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _extract_skeleton(source: str, filepath: str, max_chars: int = 8000) -> str:
    """上下文压缩: 只返回 AST 骨架(实现委托 veya.ast.extract_skeleton,单一来源)。"""
    from veya.ast import extract_skeleton as _public_extract_skeleton

    return _public_extract_skeleton(source, filepath, max_chars=max_chars)


def _tool_schema(
    name: str, description: str, properties: dict, required: tuple[str, ...] = ()
) -> dict:
    """OpenAI 格式工具 schema 构造器。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


class VeyaCoordinator:
    """Veya Core 认知引擎 — 具备上下文组装与自动纠错的智能编排中枢。

    将 LLM API 包装进带反思与纠错能力的 ReAct 执行器:
    每次循环 = 一次 LLM 决策(Thought) + 工具派发(Action) + 观察回喂(Observation);
    沙箱报错被拦截为反思提示词(Reflection),不回传前端;
    超过 max_retries 判定死胡同 → 向前端抛出 HITL (人工介入)。
    """

    def __init__(
        self,
        max_retries: int = 5,
        *,
        max_context_tokens: int = 100000,
        system_prompt: str | None = None,
        llm_fn: Callable | None = None,
        model: str | None = None,
        provider: str | None = None,
        config: dict[str, Any] | None = None,
        sandbox_timeout: float = 30.0,
        sandbox_memory_limit: int = 256 * 1024 * 1024,
    ):
        self.max_retries = max_retries
        self.max_context_tokens = max_context_tokens
        self.system_prompt = system_prompt or COGNITIVE_SYSTEM_PROMPT
        self._llm_fn = llm_fn or _llm_call
        self.model = model
        self.provider = provider
        self.config = dict(config or {})
        self.sandbox_timeout = sandbox_timeout
        self.sandbox_memory_limit = sandbox_memory_limit

        # 状态机运行时状态(每次 execute_task 重置)
        self._phase = CognitivePhase.DISCOVERY
        self._project_path = "."
        self._session_id = ""
        self._work_log: list[str] = []
        self._analyzer: Any = None
        self._analyzed_project: str | None = None
        self.decision_trail = DecisionTrail()

        # 武器库接入点: 工具名 → 绑定方法(动态派发)
        self.tool_registry: dict[str, Callable] = {
            "ast_search": self._tool_ast_search,
            "grep": self._tool_grep,
            "read_file": self._tool_read_file,
            "read_skeleton": self._tool_read_skeleton,
            "list_files": self._tool_list_files,
            "patch_file": self._tool_patch_file,
            "write_file": self._tool_write_file,
            "run_in_sandbox": self._tool_run_sandbox,
            "submit_plan": self._tool_submit_plan,
            "finish": self._tool_finish,
        }
        self._schemas = self._build_schemas()
        # 终止类工具(结束 ReAct 循环并携带最终答案)。子类可扩展此集合
        # 以引入自己的终止工具(见 RequirementCoordinator.propose_requirement_doc)。
        self._terminal_tools: set[str] = {"finish"}

    def _load_system_prompt(self) -> str:
        """加载严苛约束规则(构造时注入,可被子类覆盖)。"""
        return self.system_prompt

    # ── 工具 schema (喂给 LLM tool-calling) ─────────────────────────
    @staticmethod
    def _build_schemas() -> list[dict]:
        return [
            _tool_schema(
                "ast_search",
                "Search the AST symbol index (functions/classes/methods) by name or docstring keyword. Use during DISCOVERY to locate code without reading whole files.",
                {
                    "query": {"type": "string", "description": "symbol name or keyword"},
                    "file_path": {
                        "type": "string",
                        "description": "restrict to one file (optional)",
                    },
                },
                ("query",),
            ),
            _tool_schema(
                "grep",
                "Run ripgrep over the project. Use during DISCOVERY to find usages and definitions.",
                {
                    "pattern": {"type": "string", "description": "regex pattern"},
                    "glob": {
                        "type": "string",
                        "description": "rg glob filter, e.g. '*.py' (optional)",
                    },
                },
                ("pattern",),
            ),
            _tool_schema(
                "read_file",
                "Read a file (or a line range) with a hard token cap. Prefer read_skeleton for large files.",
                {
                    "filepath": {"type": "string", "description": "path relative to project root"},
                    "start_line": {
                        "type": "integer",
                        "description": "1-based start line (optional)",
                    },
                    "end_line": {"type": "integer", "description": "1-based end line (optional)"},
                },
                ("filepath",),
            ),
            _tool_schema(
                "read_skeleton",
                "Context compression: return only the AST skeleton (signatures + line ranges + first docstring line) of a file.",
                {"filepath": {"type": "string"}},
                ("filepath",),
            ),
            _tool_schema(
                "list_files",
                "List files under a directory (noise dirs excluded).",
                {
                    "path": {
                        "type": "string",
                        "description": "directory relative to project root (optional)",
                    }
                },
                (),
            ),
            _tool_schema(
                "patch_file",
                "Parsimonious edit: replace an EXACT unique text block. Use instead of write_file when only a few lines change.",
                {
                    "filepath": {"type": "string"},
                    "old_text": {
                        "type": "string",
                        "description": "exact existing text (must appear exactly once)",
                    },
                    "new_text": {"type": "string", "description": "replacement text"},
                },
                ("filepath", "old_text", "new_text"),
            ),
            _tool_schema(
                "write_file",
                "Write an entire file (create or overwrite). Prefer patch_file for small changes.",
                {"filepath": {"type": "string"}, "content": {"type": "string"}},
                ("filepath", "content"),
            ),
            _tool_schema(
                "run_in_sandbox",
                "Run code (or a shell command) inside the 3O isolated sandbox (network blocked, memory/time limited). Non-zero exit is reported back as a FAILED observation — reflect and retry.",
                {
                    "code": {
                        "type": "string",
                        "description": "python source to execute (optional)",
                    },
                    "command": {
                        "type": "string",
                        "description": "shell command to execute (optional)",
                    },
                    "timeout": {"type": "number", "description": "seconds (optional, default 30)"},
                },
                (),
            ),
            _tool_schema(
                "submit_plan",
                "Submit the Decision Trail: a JSON array of execution steps. Transitions the state machine to PLANNING.",
                {
                    "steps_json": {
                        "type": "string",
                        "description": 'JSON array, e.g. [{"step": 1, "action": "..."}]',
                    }
                },
                ("steps_json",),
            ),
            _tool_schema(
                "finish",
                "Declare the task complete with the final answer.",
                {"answer": {"type": "string", "description": "final summary for the user"}},
                (),
            ),
        ]

    # ── 主执行循环 (The ReAct Loop) ──────────────────────────────────
    async def execute_task(
        self,
        user_prompt: str,
        session_id: str | None = None,
        project_path: str = ".",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """主执行循环 (The ReAct Loop / 认知状态机)。

        - 每次迭代 = 一轮 Thought → Action → Observation
        - 无工具调用 → 视为任务完成
        - 工具报错 → 包装为反思提示词回喂(不返前端)
        - 超过 max_retries → 判定死胡同,向前端抛出 HITL (人工介入)
        """
        if config:
            self.config = {**self.config, **config}
        self._session_id = session_id or str(uuid.uuid4())
        self._project_path = str(project_path or ".")
        self._phase = CognitivePhase.DISCOVERY
        self._work_log = []
        self.decision_trail = DecisionTrail()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._load_system_prompt()},
            {"role": "user", "content": user_prompt},
        ]

        step_count = 0
        total_cost = 0.0

        while step_count < self.max_retries:
            step_count += 1
            logging.info(
                "[Session %s] 认知循环轮次: %s/%s (phase=%s)",
                self._session_id,
                step_count,
                self.max_retries,
                self._phase.value,
            )
            fire_step(
                {
                    "type": "cognitive_round",
                    "session_id": self._session_id,
                    "round": step_count,
                    "max_rounds": self.max_retries,
                    "phase": self._phase.value,
                }
            )

            # 1. 触发模型思考与决策 (Thought)
            response = await self._invoke_llm(messages)
            total_cost += self._cost_of(response)

            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            # 空 tool_calls 不写字段 (DeepSeek 等拒绝 tool_calls: []) —
            # llm.py 发送前另有兜底清洗, 此处从源头杜绝
            turn: dict = {"role": "assistant", "content": content}
            if tool_calls:
                turn["tool_calls"] = tool_calls
            messages.append(turn)

            # 2. 模型认为任务已完成 (无工具调用) → 直接退出循环
            if not tool_calls:
                self._phase = CognitivePhase.DONE
                return self._result(
                    status="success",
                    final_answer=content,
                    rounds=step_count,
                    total_cost=total_cost,
                )

            # 3. 拦截并执行工具调用 (Action → Observation)
            for tool_call in tool_calls:
                fn = tool_call.get("function") or {}
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                if isinstance(raw_args, str):
                    try:
                        tool_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        tool_args = {}
                else:
                    tool_args = raw_args
                tc_id = tool_call.get("id", f"call_{tool_name}")

                try:
                    # 动态派发到 3O 底座或 AST 工具
                    tool_result = await self._dispatch_tool(tool_name, tool_args)

                    # 将成功的执行结果喂回给模型 (Observation)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"[Tool {tool_name} SUCCESS]\nResult:\n{_truncate(str(tool_result))}",
                        }
                    )

                    # 终止类工具(finish / 子类自定义终止工具): 结束 ReAct 循环
                    if tool_name in self._terminal_tools:
                        final_answer = self._terminal_answer(tool_name, tool_args, tool_result)
                        self._phase = CognitivePhase.DONE
                        fire_step(
                            {
                                "type": "cognitive_done",
                                "session_id": self._session_id,
                                "round": step_count,
                                "final_answer": final_answer,
                            }
                        )
                        return self._result(
                            status="success",
                            final_answer=final_answer,
                            rounds=step_count,
                            total_cost=total_cost,
                        )
                except ToolExecutionError as exc:
                    # 4. 反思与纠错核心 (Reflection)
                    # 拦截沙箱或代码错误,包装成提示词回喂模型自纠,不回传前端
                    self._phase = CognitivePhase.REFLECTION
                    logging.warning("工具 %s 执行失败,触发自我纠错回路。报错: %s", tool_name, exc)
                    fire_step(
                        {
                            "type": "cognitive_reflection",
                            "session_id": self._session_id,
                            "tool_name": tool_name,
                            "error": str(exc),
                            "round": step_count,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": (
                                f"[Tool {tool_name} FAILED]\nError Traceback:\n{exc!s}\n\n"
                                f"请仔细分析上述报错，思考哪里出了问题，并尝试另一种方法修复它。"
                            ),
                        }
                    )

            # 5. 动态上下文切片: 超预算时把旧轮次压缩为工作日志
            self._slice_context(messages)

        # 超过最大重试次数 → 复杂故障,向前端抛出 HITL (人工介入)
        fire_step(
            {
                "type": "cognitive_hitl",
                "session_id": self._session_id,
                "rounds": step_count,
                "error": "超过最大自动纠错次数，Agent 陷入死胡同，请求人工介入 (HITL)。",
            }
        )
        return {
            "status": "failed",
            "error": "超过最大自动纠错次数，Agent 陷入死胡同，请求人工介入 (HITL)。",
            "hitl": True,
            "rounds": step_count,
            "max_rounds": self.max_retries,
            "phase": self._phase.value,
            "decision_trail": self.decision_trail.to_dict(),
            "cost_usd": round(total_cost, 6),
            "last_messages": messages[-3:],
        }

    def _result(
        self,
        *,
        status: str,
        final_answer: str,
        rounds: int,
        total_cost: float,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "final_answer": final_answer,
            "rounds": rounds,
            "max_rounds": self.max_retries,
            "phase": self._phase.value,
            "decision_trail": self.decision_trail.to_dict(),
            "cost_usd": round(total_cost, 6),
            "work_log": list(self._work_log),
        }

    # ── LLM 接入 ─────────────────────────────────────────────────────
    async def _invoke_llm(self, messages: list[dict]) -> dict[str, Any]:
        """调用底层 LLM(默认 veya.llm.llm_call,支持工具调用;无 key 自动回落 stub)。"""
        return await self._llm_fn(
            messages,
            tools=self._schemas,
            model=self.model,
            provider=self.provider,
            config=self.config,
            max_tokens=4096,
        )

    def _cost_of(self, response: dict) -> float:
        usage = response.get("usage") or {}
        if not usage:
            return 0.0
        try:
            provider, _ = _llm_get_provider_config(
                self.config, provider=self.provider, model=self.model
            )
            return _llm_calc_cost(provider, usage)
        except Exception:
            return 0.0

    async def _dispatch_tool(self, tool_name: str, tool_args: dict[str, Any]) -> Any:
        """动态派发工具调用,并驱动状态机迁移。"""
        fn = self.tool_registry.get(tool_name)
        if fn is None:
            raise ToolExecutionError(
                f"unknown tool '{tool_name}'. Available: {', '.join(sorted(self.tool_registry))}"
            )
        if tool_name in _DISCOVERY_TOOLS:
            self._phase = CognitivePhase.DISCOVERY
        elif tool_name in _EXECUTION_TOOLS:
            self._phase = CognitivePhase.EXECUTION
        return await fn(**tool_args)

    # ── 动态上下文切片 (Context Slicing) ─────────────────────────────
    def _slice_context(self, messages: list[dict]) -> None:
        """Token 预算管理: 超阈值时把最老的完整工具轮次压缩为工作日志条目。

        工具调用轮次必须整轮丢弃(assistant tool_calls + 后续 tool 结果成对),
        否则违反 OpenAI/Anthropic 的消息配对约束。
        """
        threshold = int(self.max_context_tokens * 0.8)
        if self._estimate_tokens(messages) <= threshold:
            return

        idx = self._first_removable_round(messages)
        while idx is not None and self._estimate_tokens(messages) > threshold:
            end = idx + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            self._work_log.append(self._summarize_round(messages[idx:end]))
            del messages[idx:end]
            idx = self._first_removable_round(messages)

        if self._work_log:
            messages[0]["content"] = self._load_system_prompt() + self._work_log_section()

    def _first_removable_round(self, messages: list[dict]) -> int | None:
        """返回最老的可移除完整轮次起点(索引 >= 2,保护首条 user 消息)。"""
        for i in range(2, len(messages)):
            m = messages[i]
            # 该轮必须已有工具结果回喂,才算完整可压缩
            if (
                m.get("role") == "assistant"
                and m.get("tool_calls")
                and i + 1 < len(messages)
                and messages[i + 1].get("role") == "tool"
            ):
                return i
        return None

    def _summarize_round(self, msgs: list[dict]) -> str:
        parts = []
        for m in msgs:
            if m.get("role") == "assistant":
                names = [
                    (tc.get("function") or {}).get("name", "?") for tc in m.get("tool_calls") or []
                ]
                if names:
                    parts.append("called: " + ", ".join(names))
            elif m.get("role") == "tool":
                content = m.get("content") or ""
                head = content.splitlines()[0][:120] if content else ""
                parts.append("→ " + head)
        return " | ".join(parts)

    def _work_log_section(self) -> str:
        entries = self._work_log[-_WORKLOG_MAX_ENTRIES:]
        return "\n\n# WORK LOG (compressed history of earlier rounds)\n" + "\n".join(
            f"- {e}" for e in entries
        )

    @classmethod
    def _estimate_tokens(cls, messages: list[dict]) -> int:
        total = 0
        for m in messages:
            total += _est_tokens(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                total += _est_tokens((tc.get("function") or {}).get("arguments") or "")
        return total

    # ── 武器库接入点 ─────────────────────────────────────────────────
    def _get_analyzer(self) -> Any:
        """惰性 AST 分析器(按项目路径缓存分析结果)。"""
        if self._analyzer is None:
            self._analyzer = create_ast_analyzer()
        if self._analyzed_project != self._project_path:
            with contextlib.suppress(Exception):
                self._analyzer.analyze_project(self._project_path)
            self._analyzed_project = self._project_path
        return self._analyzer

    def _resolve_path(self, filepath: str, *, must_exist: bool = True) -> Path:
        """路径解析: 拒绝逃逸项目根目录的路径(NO HALLUCINATION 规则落地)。"""
        root = Path(self._project_path).resolve()
        p = Path(filepath)
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
        if p != root and root not in p.parents:
            raise ToolExecutionError(
                f"path '{filepath}' escapes project root '{root}' — do not invent paths"
            )
        if must_exist and not p.exists():
            raise ToolExecutionError(
                f"file not found: {filepath} (project root={self._project_path})"
            )
        return p

    async def _tool_ast_search(self, query: str, file_path: str | None = None) -> str:
        """上下文压缩式代码发现: 只返回符号定位 + 签名,不返回整文件。"""
        analyzer = self._get_analyzer()
        q = query.lower()
        results = []
        for symbol in analyzer.symbols.values():
            if file_path and symbol.file_path != file_path:
                continue
            doc = symbol.docstring or ""
            if q in symbol.name.lower() or q in doc.lower():
                sig = ""
                with contextlib.suppress(Exception):
                    sig = analyzer._build_signature(symbol)
                results.append(
                    f"{symbol.type} {symbol.name} @ {symbol.file_path}:{symbol.line}-{symbol.end_line} | {sig}"
                )
        if not results:
            return f"no symbols matched '{query}'"
        return "\n".join(results[:30])

    async def _tool_grep(self, pattern: str, glob: str | None = None) -> str:
        from server.assembly import ripgrep_search

        try:
            hits = ripgrep_search(pattern, root=self._project_path, glob=glob)
        except FileNotFoundError:
            raise ToolExecutionError("ripgrep (rg) binary not found on PATH")
        if not hits:
            return f"no matches for {pattern!r}"
        lines = []
        for hit in hits[:50]:
            data = hit.get("data", {})
            path = (data.get("path") or {}).get("text", "?")
            line_no = data.get("line_number", "?")
            text = (data.get("lines") or {}).get("text", "").rstrip("\n")
            lines.append(f"{path}:{line_no}: {text}")
        return "\n".join(lines)

    async def _tool_read_file(
        self, filepath: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        path = self._resolve_path(filepath)
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total = len(lines)
        start = max(1, int(start_line or 1))
        end = min(total, int(end_line or total))
        if start > end:
            raise ToolExecutionError(
                f"read_file: start_line {start} > end_line {end} (file has {total} lines)"
            )
        body = "\n".join(lines[start - 1 : end])
        return f"# {filepath} (lines {start}-{end}/{total})\n" + _truncate(body, limit=12000)

    async def _tool_read_skeleton(self, filepath: str) -> str:
        """上下文压缩: 只返回代码骨架(AST),防止 Token 爆炸。"""
        path = self._resolve_path(filepath)
        source = path.read_text(encoding="utf-8", errors="replace")
        return _extract_skeleton(source, filepath)

    async def _tool_list_files(self, path: str = ".") -> str:
        root = Path(self._project_path).resolve()
        target = root if path in (".", "") else self._resolve_path(path, must_exist=False)
        excluded = {
            "__pycache__",
            ".git",
            ".venv",
            "venv",
            "node_modules",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".coverage",
            "dist",
            "build",
        }
        lines = []
        count = 0
        for p in sorted(target.rglob("*")):
            if any(part in excluded for part in p.parts):
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            lines.append(f"{rel}/" if p.is_dir() else f"{rel} ({p.stat().st_size}b)")
            count += 1
            if count >= 200:
                lines.append("... (truncated)")
                break
        return "\n".join(lines) or "(empty)"

    async def _tool_patch_file(self, filepath: str, old_text: str, new_text: str) -> str:
        """精打细算式编辑: 精确替换唯一文本块(遵守 BE PARSIMONIOUS)。"""
        if not old_text:
            raise ToolExecutionError("patch_file: old_text must not be empty")
        path = self._resolve_path(filepath)
        content = path.read_text(encoding="utf-8", errors="replace")
        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ToolExecutionError(
                f"patch_file failed: old_text not found in {filepath} (0 occurrences). "
                "Re-read the file with read_file and retry with the exact text."
            )
        if occurrences > 1:
            raise ToolExecutionError(
                f"patch_file failed: old_text is ambiguous ({occurrences} occurrences in {filepath}). "
                "Include more surrounding context to make it unique."
            )
        updated = content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        return (
            f"patched {filepath}: replaced 1 unique occurrence "
            f"({len(old_text)} -> {len(new_text)} chars). "
            "Now VERIFY with run_in_sandbox or read_file."
        )

    async def _tool_write_file(self, filepath: str, content: str) -> str:
        path = self._resolve_path(filepath, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {filepath} ({len(content)} chars). Now VERIFY with run_in_sandbox."

    async def _tool_run_sandbox(
        self, code: str | None = None, command: str | None = None, timeout: float | None = None
    ) -> str:
        """安全验证: 调用 3O Core 的隔离沙箱(网络封锁 + 内存/时间限制)。"""
        if not code and not command:
            raise ToolExecutionError(
                "run_in_sandbox requires either 'code' (python source) or 'command' (shell)"
            )
        config = SandboxConfig(
            time_limit=float(timeout or self.sandbox_timeout),
            memory_limit=self.sandbox_memory_limit,
            network_blocked=True,
            audit_enabled=True,
        )
        executor = create_safe_executor(config)
        async with executor:
            if command:
                result = await executor.execute(command, session_id=self._session_id)
            else:
                result = await executor.run_script(code, session_id=self._session_id)
        if result.get("exit_code") != 0:
            # 主动抛出异常,触发上方的 Reflection 机制
            raise ToolExecutionError(
                f"exit_code={result.get('exit_code')} ({result.get('duration', 0.0):.2f}s)\n"
                f"stdout:\n{result.get('stdout', '')}\n"
                f"stderr:\n{result.get('stderr', '')}"
            )
        return f"exit_code=0 ({result.get('duration', 0.0):.2f}s)\n{result.get('stdout', '')}"

    async def _tool_submit_plan(self, steps_json: str) -> str:
        """规划期: 生成 JSON 格式的执行步骤列表 (Decision Trail)。"""
        try:
            steps = json.loads(steps_json)
            if not isinstance(steps, list):
                raise ValueError("must be a JSON array")
        except Exception as exc:
            raise ToolExecutionError(
                f"submit_plan failed: steps_json must be a JSON array of step objects. Parse error: {exc}"
            )
        self.decision_trail = DecisionTrail(steps=steps, submitted_at=time.time())
        self._phase = CognitivePhase.PLANNING
        fire_step({"type": "cognitive_plan", "session_id": self._session_id, "steps": steps})
        labels = []
        for i, s in enumerate(steps, start=1):
            if isinstance(s, dict):
                labels.append(str(s.get("step", s.get("action", s.get("description", i)))))
            else:
                labels.append(str(s))
        return f"Decision Trail accepted ({len(steps)} steps): " + " -> ".join(labels)

    async def _tool_finish(self, answer: str = "") -> str:
        return f"acknowledged. final answer: {answer}"

    def _terminal_answer(self, tool_name: str, tool_args: dict[str, Any], tool_result: Any) -> str:
        """从终止工具的调用中提取最终答案。子类可覆盖以支持自定义终止工具
        (见 RequirementCoordinator.propose_requirement_doc)。"""
        if tool_name == "finish":
            return str(tool_args.get("answer", ""))
        return str(tool_result)


# =====================================================================
# RequirementCoordinator — Phase 1: 需求调研与结构化提案
#
# 复用 VeyaCoordinator 的 DISCOVERY 工具集与 ReAct/反思循环,但去掉
# EXECUTION 类工具(patch_file/write_file/run_in_sandbox/submit_plan) ——
# 阶段一只调研、不动文件系统,以 'propose_requirement_doc' 作为终止工具。
# =====================================================================

REQUIREMENT_SYSTEM_PROMPT = (
    "You are Veya's Product Manager + Architect. A user has described something they want built.\n"
    "\n"
    "# WORKFLOW\n"
    "1. Use 'ast_search' / 'grep' / 'read_file' / 'read_skeleton' / 'list_files' to understand the existing "
    "project (if any) and ground your understanding in what actually exists — do not hallucinate files or APIs.\n"
    "2. When you have enough context, call 'propose_requirement_doc' exactly once with a structured requirement: "
    "a short title, a context_analysis paragraph explaining what you found and why the features below follow "
    "from it, and a list of concrete core_features.\n"
    "\n"
    "# RULES\n"
    "- You cannot edit files or run code in this phase. Your only job is to research and propose.\n"
    "- Be concrete: core_features should read like a spec a senior engineer could implement directly.\n"
    "- If the request is ambiguous, make a reasonable assumption and state it plainly in context_analysis "
    "rather than leaving a gap.\n"
)

_REQUIREMENT_DISCOVERY_TOOLS = ("ast_search", "grep", "read_file", "read_skeleton", "list_files")


class RequirementCoordinator(VeyaCoordinator):
    """认知引擎变体: 只调研 + 提案结构化需求文档,不触碰文件系统。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("system_prompt", REQUIREMENT_SYSTEM_PROMPT)
        super().__init__(*args, **kwargs)

        # 阶段一禁止 EXECUTION 类工具:只保留 DISCOVERY 工具 + 终止工具
        self.tool_registry = {
            name: fn
            for name, fn in self.tool_registry.items()
            if name in _REQUIREMENT_DISCOVERY_TOOLS
        }
        self._schemas = [
            s for s in self._schemas if s["function"]["name"] in _REQUIREMENT_DISCOVERY_TOOLS
        ]

        self.tool_registry["propose_requirement_doc"] = self._tool_propose_requirement_doc
        self._schemas.append(
            _tool_schema(
                "propose_requirement_doc",
                "Submit the final structured requirement document for user approval. Call this exactly once, "
                "when (and only when) you have enough grounded context to write concrete core_features.",
                {
                    "title": {"type": "string", "description": "short, descriptive title"},
                    "context_analysis": {
                        "type": "string",
                        "description": "what you found during research and why the features follow from it",
                    },
                    "core_features": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "concrete, implementable feature bullets",
                    },
                },
                ("title", "context_analysis", "core_features"),
            )
        )
        self._terminal_tools = {"propose_requirement_doc"}
        self._proposed_doc: RequirementDoc | None = None

    async def _tool_propose_requirement_doc(self, **kwargs: Any) -> str:
        # **kwargs (not positional params): a model omitting a required field must surface
        # as a ToolExecutionError (→ reflection/retry), not a bare TypeError (→ crash).
        try:
            doc = RequirementDoc.model_validate(kwargs)
        except ValidationError as exc:
            raise ToolExecutionError(f"propose_requirement_doc failed validation: {exc}") from exc
        self._proposed_doc = doc
        fire_step(
            {
                "type": "requirement_doc",
                "session_id": self._session_id,
                "doc": doc.model_dump(),
            }
        )
        return f"Requirement doc '{doc.title}' accepted ({len(doc.core_features)} features)."

    def _terminal_answer(self, tool_name: str, tool_args: dict[str, Any], tool_result: Any) -> str:
        if tool_name == "propose_requirement_doc" and self._proposed_doc is not None:
            return json.dumps(self._proposed_doc.model_dump(), ensure_ascii=False)
        return super()._terminal_answer(tool_name, tool_args, tool_result)


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
            cognitive_max_retries = settings.get("cognitive_max_retries", 5)
        else:
            cognitive_max_retries = 5
        self._cognitive_max_retries = cognitive_max_retries
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

        认知模式: command 带 mode="cognitive" 或 cognitive=True 时,
        不进分队编排,直接路由到 VeyaCoordinator 的 ReAct 状态机。
        """
        if command.get("mode") == "cognitive" or command.get("cognitive"):
            return await self.handle_cognitive(command, session_id=session_id, on_step=on_step)
        if command.get("mode") == "requirement":
            return await self.handle_requirement(command, session_id=session_id, on_step=on_step)

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

    # ── 认知模式 (Veya Core ReAct 状态机) ────────────────────────────
    def _make_cognitive_engine(self) -> VeyaCoordinator:
        """构造认知引擎(可被测试替换)。"""
        return VeyaCoordinator(
            max_retries=self._cognitive_max_retries,
            max_context_tokens=self.max_context_tokens,
        )

    async def handle_cognitive(
        self,
        command: dict[str, Any],
        *,
        session_id: str | None = None,
        on_step: Callable | None = None,
    ) -> dict[str, Any]:
        """认知模式入口: 把命令交给 VeyaCoordinator 的 ReAct 循环执行。

        command keys: text / project_path / model / provider / config
        (config 可含 max_retries 等认知引擎参数)
        """
        sid = session_id or str(uuid.uuid4())
        token = _on_step_ctx.set(on_step)
        try:
            engine = self._make_cognitive_engine()
            result = await engine.execute_task(
                command.get("text", ""),
                session_id=sid,
                project_path=command.get("project_path", "."),
                config={
                    "model": command.get("model"),
                    "provider": command.get("provider"),
                    **(command.get("config") or {}),
                },
            )
            result["session_id"] = sid
            return result
        finally:
            _on_step_ctx.reset(token)

    def _make_requirement_engine(self) -> RequirementCoordinator:
        """构造 Phase 1 需求调研引擎(可被测试替换)。"""
        return RequirementCoordinator(
            max_retries=self._cognitive_max_retries,
            max_context_tokens=self.max_context_tokens,
        )

    async def handle_requirement(
        self,
        command: dict[str, Any],
        *,
        session_id: str | None = None,
        on_step: Callable | None = None,
    ) -> dict[str, Any]:
        """Phase 1 入口: 把命令交给 RequirementCoordinator 的调研+提案循环执行。

        command keys: text / project_path / model / provider / config
        result["final_answer"] 为 RequirementDoc 的 JSON 序列化字符串
        (调用方按需 json.loads 后交给前端渲染审批卡片)。
        """
        sid = session_id or str(uuid.uuid4())
        token = _on_step_ctx.set(on_step)
        try:
            engine = self._make_requirement_engine()
            result = await engine.execute_task(
                command.get("text", ""),
                session_id=sid,
                project_path=command.get("project_path", "."),
                config={
                    "model": command.get("model"),
                    "provider": command.get("provider"),
                    **(command.get("config") or {}),
                },
            )
            result["session_id"] = sid
            return result
        finally:
            _on_step_ctx.reset(token)

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
