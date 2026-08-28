"""Veya: 全局能力注册表 (Master Tool Registry)。

把后端物理能力翻译成"大模型能听懂的语言" — JSON Schema (Function Calling 协议)。
主脑 (MasterCoordinator) 通过本注册表看到所有可用武器,并在模型决定调用时
动态派发到真实物理实现。

设计要点:
- 零前端感知: 前端只发文本、收 SSE 流;新增能力只改这里。
- Plug & Play: 新能力 = 一个 Python 函数 + 一次 register(),大模型瞬间"学会"。
- 注册时自动检测 async 函数;execute 统一 await,调用方无感。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

from runtime.coding.tools import register_tools as _register_coding_tools
from server.events import append_canonical_event, current_task_id
from server.tool_guard import ToolDenied as _ToolDenied
from server.tool_guard import global_tool_guard as _tool_guard
from veya.obase import telemetry
from veya.obase.async_utils import run_sync_in_daemon_thread
from veya.oskill.pure.validate_args import validate_args

logger = __import__("logging").getLogger("master.tools")


class ToolExecutionError(RuntimeError):
    """工具执行失败 → 由主脑捕获并回喂模型反思(不直接暴露给用户)。"""


# ================= 工具分组 (仅供 agent_loop_run 隔离子任务内部使用) =========
# 2026-08-17 架构澄清 (docs/ARCHITECTURE_STABLE.md「冻结架构」): 面向用户的
# 唯一主链是 MasterAgent ReAct, 全量工具面 + 模型自主判断, 程序不裁藏 —
# 这份分组表**不会**用来过滤 MasterAgent 看到的工具 (get_all_tool_schemas
# 不受影响)。它只服务 agent_loop_run 这一个工具: 该工具把 omodul.AgentLoop
# 作为「隔离子任务执行器」暴露给 MasterAgent 调用, 子任务自己的临时会话按
# 请求方指定的 tool_group 给一个有边界的工具面, 避免隔离执行阶段拿到全部
# ~60 个工具 (那是另一件事: 执行边界收紧, 不是主脑认知裁剪)。
_RESIDENT_TOOLS: frozenset[str] = frozenset(
    {
        "ask_user",  # 意图理解
        "project_ask",  # 派工唯一入口 (自动路由 builtin/hicode/dsh)
        "project_status",  # 监督长程执行 (只读进度)
        "project_eng_gates",  # 工程纪律门禁 (S1–S5 编排; 非派工路由)
        "hicode_review",  # 审查
        "decision_record",  # 决策留痕
        "decision_query",  # 决策回溯
    }
)


# 当前主链会话 id (contextvar): coordinator_master.chat_stream 在 await agent 流前
# set, submodule 里 get_all_schemas() 无 session 形参也能读到 → 不改只读 submodule。
_current_master_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "veya_master_session", default=None
)


_delegation_depth_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "veya_delegation_depth", default=0
)

# One guard per MasterAgent request context.  ContextVar keeps concurrent user
# sessions isolated while nested AgentLoop calls share the same depth,
# concurrency and root budget.
_spawn_guard_ctx: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "veya_execution_spawn_guard", default=None
)


# ================= ToolSpec v1 (docs/dev/rfc-06-toolspec-v1.md 最小步骤) ====
# 增量元数据, 只启用 side_effect 一个维度 (risk/idempotency 先占位, §7.1 全量字段
# 要等真有判断逻辑要读它们时再填, 现在填了也没人用, 是形式主义)。纯附加信息 —
# 不参与任何执行时判断: 并发与否仍读 _parallel_safe 集合 (下面), 这里只是把已经
# 做过的判断显式标注出来, 不改变一行运行行为。
class SideEffect(Enum):
    """docs/VEYA_10_OF_10_PLAN.md §7.2 的六档分类。目前只有 PURE_READ 被实际标注过
    (2026-08-24)；其余档位先占位, 避免以后要扩展枚举还要动一次这里。"""

    PURE_READ = "pure_read"
    LOCAL_WRITE = "local_write"
    PROCESS_EXEC = "process_exec"
    NETWORK_WRITE = "network_write"
    EXTERNAL_MUTATION = "external_mutation"
    PRIVILEGED = "privileged"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    side_effect: SideEffect | None = None
    # Provider capability is explicit so durable recovery cannot infer that a
    # tool is replay-safe from its name or natural-language description.
    effect_capability: Literal[
        "none", "idempotency_key", "status_probe", "compensation", "manual_only"
    ] = "none"
    operation_version: str = "1"


# ================= 并发安全工具 (多工具并行执行白名单) ======================
# 2026-08-17: 主链 ReAct 一轮可能返回多个 tool_call。默认逐个顺序执行；这份
# 白名单里的工具是**纯只读、无副作用、彼此独立**的, 主库循环遇到「整批都在
# 白名单内」时并发执行 (asyncio.gather), 直接吃到「一轮读多个文件/搜多个模式」
# 的延迟收益。保守策略 (pi 同款): 只要一批里有一个不在白名单, 整批退回顺序 —
# 有副作用/写文件/跑命令的工具永远不并发, 零竞态风险。追加消息顺序恒等于原始
# 顺序, 对模型完全透明。新工具默认非并发安全, 显式确认无副作用再加入。
_PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        # 纯文件/代码读取
        "list_files",
        "grep",
        "read_file_ast",
        "read_hashline",
        "runtime_calls_query",
        "ast_grep_search",
        "assemble_code_context",
        # 只读检索/查询
        "fetch_url",
        "search_genesis_ledger",
        "decision_query",
        "graph_query",
        "get_market_data_schema",
        "project_status",
        "plan_status",
        # 只读状态检查 (状态内核)
        "system_quota_should_run",
        "system_gate_check",
        "system_terminal_gate_check",
        "system_boundary_scan",
        # hicode 只读诊断
        "hicode_status",
        "hicode_sessions",
        "hicode_tasks",
        # local coding workspace read-only inspection
        "coding_workspace_detect",
        "coding_worktree_status",
        "coding_diff",
        # Memory tools are read-only lookups; writes/corrections remain outside
        # this set and therefore never join a parallel batch.
        "memory_search",
        "memory_get",
        "memory_explain",
        "memory_show_source",
        "skill_search",
        "skill_show",
    }
)

# 工具名 → 职能分组。未登记的名字兜底进 "other" (mcp_* 网关例外, 见 _group_for)。
_TOOL_GROUPS: dict[str, str] = {
    # code_exec: 直接读写代码/跑命令 — 多数场景应走 project_ask 派给 hicode,
    # 只有需要亲自核查/兜底时才启用。
    "write_file": "code_exec",
    "edit_hashline": "code_exec",
    "read_hashline": "code_exec",
    "runtime_calls_ingest": "code_exec",
    "runtime_calls_query": "code_exec",
    "code_blast_radius": "code_exec",
    "ast_grep_search": "code_exec",
    "ast_grep_rewrite": "code_exec",
    "read_file_ast": "code_exec",
    "list_files": "code_exec",
    "grep": "code_exec",
    "run_in_sandbox": "code_exec",
    "assemble_code_context": "code_exec",
    "hicode_run": "code_exec",
    "hicode_sessions": "code_exec",
    "hicode_rollback": "code_exec",
    "hicode_status": "code_exec",
    "hicode_tasks": "code_exec",
    "hicode_stop": "code_exec",
    # vision: 视觉取证/像素级图像操作
    "vision_glance": "vision",
    "vision_ground": "vision",
    "vision_detect": "vision",
    "vision_crop": "vision",
    "vision_trace": "vision",
    "vision_pixel_diff": "vision",
    "vision_long_screenshot_ocr": "vision",
    "vision_extract_foreground": "vision",
    "vision_dominant_colors": "vision",
    "vision_html_screenshot": "vision",
    # quant: 量化数据/回测协议
    "get_market_data_schema": "quant",
    "run_backtest_coprocessor": "quant",
    # graph_memory: 上下文图谱读写
    "graph_query": "graph_memory",
    "graph_store": "graph_memory",
    # genesis: 3O Engine Genesis 工作流/进化搜索/账本
    "evolve_solution": "genesis",
    "search_genesis_ledger": "genesis",
    "delegate_to_genesis": "genesis",
    # state_kernel: 计划状态内核控制面, 一般由自动化流程内部调用
    "system_boundary_scan": "state_kernel",
    "system_gate_check": "state_kernel",
    "system_graph_cycle": "state_kernel",
    "system_graph_review": "state_kernel",
    "system_quota_should_run": "state_kernel",
    "system_quota_spend_slot": "state_kernel",
    "system_terminal_gate_check": "state_kernel",
    "system_todo_claim": "state_kernel",
    # business: 公众号图文生产
    "produce_wechat_article": "business",
    "wechat_discover": "business",
    # automation: loop-plane 目标/诊断/干预
    "loop_plan_goal": "automation",
    "loop_diagnose": "automation",
    "loop_intervene": "automation",
    # web: 浏览器自动化/网页抓取
    "browser_run": "web",
    "fetch_url": "web",
    # planning: 旧版计划系统 (create_plan/plan_status/update_todo), project_ask
    # 是首选派工入口, 这套留作需要显式多步 todo 追踪时的备选。
    "create_plan": "planning",
    "plan_status": "planning",
    "update_todo": "planning",
    # gates: 工程纪律门禁。不是派工路由，内部编排 S1–S5。
    "project_eng_gates": "gates",
    # wayfinding: 目标模糊时先探路收敛, 再编译成 Runbook 执行
    "wayfind_chart": "wayfinding",
    "wayfind_add_ticket": "wayfinding",
    "wayfind_wire_blocking": "wayfinding",
    "wayfind_frontier": "wayfinding",
    "wayfind_claim": "wayfinding",
    "wayfind_resolve": "wayfinding",
    "wayfind_rule_out_of_scope": "wayfinding",
    "wayfind_add_fog": "wayfinding",
    "wayfind_graduate_fog": "wayfinding",
    "wayfind_decisions": "wayfinding",
    "wayfind_complete": "wayfinding",
    "wayfind_to_spec": "wayfinding",
    "wayfind_compile_runbook": "wayfinding",
    "stateful_start": "wayfinding",
    "stateful_current": "wayfinding",
    "stateful_goto": "wayfinding",
    "stateful_history": "wayfinding",
    # wayfinding_github: 同一套探路概念, 落在真实 GitHub Issues 上 (map=issue,
    # ticket=原生 sub-issue, blocking=原生 issue dependency, 网页上直接可见)
    "wayfind_gh_chart": "wayfinding_github",
    "wayfind_gh_add_ticket": "wayfinding_github",
    "wayfind_gh_wire_blocking": "wayfinding_github",
    "wayfind_gh_frontier": "wayfinding_github",
    "wayfind_gh_claim": "wayfinding_github",
    "wayfind_gh_resolve": "wayfinding_github",
    "wayfind_gh_rule_out_of_scope": "wayfinding_github",
    "wayfind_gh_add_fog": "wayfinding_github",
    "wayfind_gh_graduate_fog": "wayfinding_github",
    "wayfind_gh_decisions": "wayfinding_github",
    "wayfind_gh_complete": "wayfinding_github",
    # long_task: GoalKernel 事件溯源治理接主链, goal_start 后本 session 自动生效
    "goal_start": "long_task",
    "goal_add_todo": "long_task",
    "goal_status": "long_task",
    # team: 点对点协作(邮箱+共享任务列表+协商式关闭), 主脑自己协调多个
    # session/工作项用, 不是塞进外部 CLI 子进程的
    "team_create": "team",
    "team_delete": "team",
    "team_list": "team",
    "team_send_message": "team",
    "team_read_messages": "team",
    "team_task_create": "team",
    "team_task_list": "team",
    "team_task_get": "team",
    "team_task_update": "team",
    "team_shutdown_request": "team",
    "team_approve_shutdown": "team",
    "team_reject_shutdown": "team",
    "team_status": "team",
}

_GROUP_DESCRIPTIONS: dict[str, str] = {
    "code_exec": "直接读写代码/跑沙箱命令 (write_file/grep/run_in_sandbox/hicode_*)。"
    "多数编码需求应优先 project_ask 派给 hicode, 只有需要亲自核查时才启用。",
    "vision": "视觉取证/像素级图像操作 (vision_glance/crop/trace/pixel_diff/...)。",
    "quant": "量化数据协议 + 隔离回测 (get_market_data_schema/run_backtest_coprocessor)。",
    "graph_memory": "上下文图谱读写 (graph_query/graph_store)。",
    "genesis": "3O Engine Genesis 工作流/进化搜索/账本查询。",
    "state_kernel": "计划状态内核控制面 (gate/quota/todo_claim), 一般由自动化流程内部调用。",
    "business": "公众号图文生产 (produce_wechat_article/wechat_discover)。",
    "automation": "loop-plane 目标设定/诊断/干预。",
    "web": "浏览器自动化/网页抓取 (browser_run/fetch_url)。",
    "planning": "旧版计划系统 (create_plan/plan_status/update_todo); project_ask 是首选派工入口。",
    "gates": "工程纪律门禁 (project_eng_gates)。不是派工入口，不替代 project_ask。",
    "wayfinding": "目标模糊时先探路收敛范围 (wayfind_chart/ticket/claim/resolve/complete), "
    "收敛完成后编译成 Runbook 并按检查点执行 (wayfind_compile_runbook/stateful_*)。",
    "wayfinding_github": "同一套探路概念落在真实 GitHub Issues 上 (wayfind_gh_*): map=issue, "
    "ticket=原生 sub-issue, blocking=原生 issue dependency — frontier 在 GitHub 网页上直接可见, "
    "不用调工具查。",
    "long_task": "长程任务预算/进度治理 (goal_start/goal_add_todo/goal_status)。事件溯源 "
    "(GoalKernel), goal_start 后本 session 每轮自动追踪花费, 超支自动暂停。",
    "mcp_gateway": "兄弟服务网关 (mcp_hevi/mcp_stratum/mcp_od/mcp_codebase)。",
    "team": "多智能体点对点协作 (team_create/send_message/task_*/shutdown_*): 邮箱式消息 + "
    "共享任务列表(claim 认领) + 协商式关闭。用于主脑自己协调多个 session/工作项, 不是给外部 "
    "CLI 子进程(hicode/codex 等)用的。",
}


def _group_for(name: str) -> str:
    if name in _TOOL_GROUPS:
        return _TOOL_GROUPS[name]
    if name.startswith("mcp_"):
        return "mcp_gateway"
    return "other"


# agent_loop_run 隔离子会话 id → 该次调用请求解锁的工具组集合。子任务跑完
# 即弹出清理 (见 _agent_loop_run), 不跨调用持久化。
_session_enabled_groups: dict[str, set[str]] = {}


def get_enabled_groups(session_id: str | None) -> set[str]:
    """该隔离子会话已解锁的工具组 (只读快照)。"""
    return set(_session_enabled_groups.get(session_id or "", ()))


_TOOL_TIMEOUT_ENV = "VEYA_TOOL_TIMEOUT_S"


def parse_optional_timeout(value: float | str | None, *, source: str) -> float | None:
    """把可选超时归一化为秒；空值/0 表示不设限。"""
    if value in (None, ""):
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be a non-negative number of seconds") from exc
    if timeout < 0:
        raise ValueError(f"{source} must be a non-negative number of seconds")
    return timeout or None


def _schema_validator(schema: dict, *, owner: str) -> dict[str, Any]:
    """校验工具 schema 的基本形态，并复用 3O 零依赖参数校验器。"""
    if not isinstance(schema, dict):
        raise ValueError(f"{owner}: invalid JSON schema: expected object")
    if schema.get("type", "object") != "object":
        raise ValueError(f"{owner}: invalid JSON schema: root type must be object")
    if not isinstance(schema.get("properties"), dict):
        raise ValueError(f"{owner}: invalid JSON schema: properties must be object")
    return dict(schema)


def _validate_arguments(
    name: str,
    arguments: Any,
    validator: Any,
    *,
    owner: str = "Tool",
) -> None:
    """按已注册 schema 校验实际执行参数，输出稳定且可反思的错误。"""
    verdict = validate_args(arguments, validator)
    if verdict.ok:
        return
    raise ToolExecutionError(
        f"{owner} '{name}' arguments failed JSON schema validation: "
        + "; ".join(verdict.errors[:3])
    )


async def _run_sync_callback(func: Callable, kwargs: dict[str, Any]) -> Any:
    """在线程中跑同步工具，不依赖本环境 Python 3.14 会卡死的默认 executor。"""
    return await run_sync_in_daemon_thread(func, **kwargs)


async def _invoke_callback(func: Callable, kwargs: dict[str, Any]) -> Any:
    """执行 callback：同步函数进线程池，且兼容同步函数返回 awaitable。"""
    call = func
    if inspect.iscoroutinefunction(call):
        result = call(**kwargs)
    else:
        result = await _run_sync_callback(call, kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _to_str(result: Any, limit: int = 8000) -> str:
    """工具结果统一转字符串(截断防 Token 爆炸)。"""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2)
        except TypeError:
            text = str(result)
    else:
        text = str(result)
    return text[:limit] + (
        f"\n... [truncated {len(text) - limit} chars]" if len(text) > limit else ""
    )


class MasterToolRegistry:
    """全局能力注册表: 物理函数 ↔ 大模型可见的 JSON Schema 双向映射。"""

    def __init__(self, *, timeout_s: float | None = None) -> None:
        self._functions: dict[str, Callable] = {}
        self._schemas: list[dict] = []
        self._validators: dict[str, Any] = {}
        self._descriptions: dict[str, str] = {}
        self._result_limits: dict[str, int] = {}  # 工具名 → 结果截断上限(协处理器需大上限)
        configured_timeout: float | str | None = timeout_s
        if configured_timeout is None:
            configured_timeout = os.environ.get(_TOOL_TIMEOUT_ENV)
        self._default_timeout_s = parse_optional_timeout(
            configured_timeout, source=f"timeout_s/{_TOOL_TIMEOUT_ENV}"
        )
        self._tool_timeouts: dict[str, float | None] = {}
        # 并发安全工具集: 模块级白名单 + 注册时显式 opt-in。主库循环据此决定
        # 一批 tool_call 能否并发 (整批都在集内才并发, 否则顺序)。
        self._parallel_safe: set[str] = set(_PARALLEL_SAFE_TOOLS)
        # ToolSpec v1 (rfc-06): 纯附加元数据, 不参与上面 _parallel_safe 的判断。
        self._tool_specs: dict[str, ToolSpec] = {}

    # ── 注册 ─────────────────────────────────────────────────────────
    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        func: Callable,
        *,
        max_result_chars: int = 8000,
        timeout_s: float | None = None,
        parallel_safe: bool | None = None,
        side_effect: SideEffect | None = None,
        effect_capability: Literal[
            "none", "idempotency_key", "status_probe", "compensation", "manual_only"
        ] = "none",
        operation_version: str = "1",
    ) -> None:
        """注册一个能力,使其对大模型可见。

        Args:
            name: 工具名(大模型调用时使用, 小写蛇形)。
            description: 认知描述(大模型靠它决定何时调用 — 写清触发条件)。
            parameters: JSON Schema 对象, 形如 {"type": "object", "properties": {...}, "required": [...]}。
            func: 物理实现(同步或 async 均可)。
            max_result_chars: 结果回喂大模型前的截断上限(浓缩 JSON 类工具需调大)。
            timeout_s: 此工具的可选超时秒数；未配置时继承 registry/env，0 表示不设限。
            parallel_safe: 显式声明此工具纯只读/无副作用可与同批工具并发执行；
                缺省 None 时按模块级 _PARALLEL_SAFE_TOOLS 白名单判定 (默认不并发)。
            side_effect: ToolSpec v1 side-effect category.
            effect_capability: provider recovery contract; ``none`` means the
                operation is not declared replay-safe.
            operation_version: stable version included in durable operation keys.
        """
        if not name or not callable(func):
            raise ValueError("register requires a non-empty tool name and a callable")
        if effect_capability not in {
            "none",
            "idempotency_key",
            "status_probe",
            "compensation",
            "manual_only",
        }:
            raise ValueError(f"Tool '{name}': unsupported effect capability {effect_capability!r}")
        if not operation_version.strip():
            raise ValueError(f"Tool '{name}': operation_version must not be empty")
        if name in self._functions:
            raise ValueError(f"Tool '{name}' already registered — 名字冲突会混淆大模型")
        # 归一化 parameters: 允许只传 properties 的简写
        params = dict(parameters)
        params.setdefault("type", "object")
        if "properties" not in params:
            raise ValueError(f"Tool '{name}': parameters must include 'properties'")
        validator = _schema_validator(params, owner=f"Tool '{name}'")

        self._functions[name] = func
        self._validators[name] = validator
        self._descriptions[name] = description
        self._result_limits[name] = max_result_chars
        self._tool_specs[name] = ToolSpec(
            name=name,
            side_effect=side_effect,
            effect_capability=effect_capability,
            operation_version=operation_version,
        )
        if timeout_s is not None:
            self._tool_timeouts[name] = parse_optional_timeout(
                timeout_s, source=f"Tool '{name}' timeout_s"
            )
        if parallel_safe is True:
            self._parallel_safe.add(name)
        elif parallel_safe is False:
            self._parallel_safe.discard(name)
        self._schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params,
                },
            }
        )

    def spec_for(self, name: str) -> ToolSpec | None:
        """ToolSpec v1 (rfc-06) 查询入口, 未注册/未标注 side_effect 均返回对应默认值。"""
        return self._tool_specs.get(name)

    def side_effect_policy_for(self, name: str) -> dict[str, str | None]:
        """Return the declared side-effect recovery contract for a tool."""
        spec = self._tool_specs.get(name)
        if spec is None:
            return {"side_effect": None, "effect_capability": "none", "operation_version": "1"}
        return {
            "side_effect": spec.side_effect.value if spec.side_effect else None,
            "effect_capability": spec.effect_capability,
            "operation_version": spec.operation_version,
        }

    def unregister(self, name: str) -> None:
        if name in self._functions:
            del self._functions[name]
            self._validators.pop(name, None)
            del self._descriptions[name]
            self._result_limits.pop(name, None)
            self._tool_timeouts.pop(name, None)
            self._parallel_safe.discard(name)
            self._tool_specs.pop(name, None)
            self._schemas = [s for s in self._schemas if s["function"]["name"] != name]

    # ── 查询 ─────────────────────────────────────────────────────────
    def get_all_schemas(self) -> list[dict]:
        """暴露给大模型的完整认知描述；工具检索不改变可见工具面。"""
        return list(self._schemas)

    def is_parallel_safe(self, name: str) -> bool:
        """此工具是否纯只读/无副作用, 可与同批工具并发执行。

        主库 ReAct 循环调用: 一批 tool_call 全部 parallel_safe 才整批并发,
        否则顺序执行 (保守策略, 有副作用工具永不并发)。未注册的名字返回 False。
        """
        return name in self._parallel_safe

    def get_resident_schemas(self, *, session_id: str | None = None) -> list[dict]:
        """精简工具面: 常驻工具 (意图理解/派工/监督/审查) + 该会话已解锁的专项组。

        仅供 agent_loop_run 的隔离子任务执行使用 (给子任务一个有边界的工具面),
        不用于 MasterAgent ReAct 主链 —— 主链看到的仍是全量 get_all_schemas(),
        冻结架构要求「程序不裁藏」。
        """
        enabled = get_enabled_groups(session_id)
        out = []
        for schema in self._schemas:
            name = schema["function"]["name"]
            if name in _RESIDENT_TOOLS or _group_for(name) in enabled:
                out.append(schema)
        return out

    def list_tools(self) -> list[str]:
        return sorted(self._functions)

    def has(self, name: str) -> bool:
        return name in self._functions

    def describe(self, name: str) -> str:
        """单行摘要(注入 System Prompt 的 SOP 用): "name — description"。"""
        return f"{name} — {self._descriptions.get(name, '')}"

    def to_dict(self) -> dict:
        return {
            "tools": [
                {"name": s["function"]["name"], "description": s["function"]["description"]}
                for s in self._schemas
            ]
        }

    def __len__(self) -> int:
        return len(self._functions)

    # ── 执行 ─────────────────────────────────────────────────────────
    async def execute(self, name: str, kwargs: dict, *, timeout_s: float | None = None) -> str:
        """执行物理函数,返回字符串结果。async 函数自动 await。

        Raises:
            ToolExecutionError: 工具不存在或执行抛异常(由主脑回喂反思)。
        """
        func = self._functions.get(name)
        if func is None:
            raise ToolExecutionError(
                f"Tool '{name}' not found. Available: {', '.join(self.list_tools())}"
            )

        def _event(topic: str, **payload: Any) -> None:
            # Event persistence is observability, never a reason to fail a tool.
            with contextlib.suppress(Exception):
                append_canonical_event(
                    topic,
                    {"tool_name": name, **payload},
                    actor="master",
                    task_id=current_task_id(),
                )

        def _checkpoint(stage: str) -> None:
            """Create a safe checkpoint around non-read-only physical work."""
            task_id = current_task_id()
            if not task_id:
                return
            with contextlib.suppress(Exception):
                from server.permission_profiles import RiskLevel, classify_risk
                from server.task_store import task_store

                if classify_risk(name, kwargs) == RiskLevel.R0:
                    return
                checkpoint_id = uuid.uuid4().hex
                task_store.set_checkpoint(task_id, checkpoint_id, stage=stage)

        _event("tool.requested")
        try:
            _validate_arguments(name, kwargs, self._validators[name])
        except Exception as exc:
            _event("tool.failed", error_type=type(exc).__name__)
            raise
        _checkpoint("before")
        # 统一守卫通道: 执行前过策略链 + 记决策轨迹 (缺省全放行, 零行为变化)。
        try:
            await _tool_guard.acheck(name, kwargs, source="master_tool")
        except _ToolDenied as denied:
            _event("tool.denied", error_type=type(denied).__name__)
            raise ToolExecutionError(str(denied)) from denied
        _event("tool.started")
        effective_timeout = parse_optional_timeout(
            timeout_s, source=f"Tool '{name}' execute timeout_s"
        )
        if timeout_s is None:
            effective_timeout = self._tool_timeouts.get(name, self._default_timeout_s)
        # 工具执行 span (docs/dev/rfc-10-observability-scoping.md): emit() 目前无人
        # set_emitter/绑定 trace, 是安全 no-op——先把 tool_span 埋点接进唯一的执行
        # 收口点, 真正接进 SSE/trace_id 是设计决策, 留给后续单独一轮做, 不在这次
        # 顺手改热路径的控制流。
        _span_start = time.time()
        try:
            execution = _invoke_callback(func, kwargs)
            if effective_timeout is None:
                raw = await execution
            else:
                raw = await asyncio.wait_for(execution, timeout=effective_timeout)
            result = _to_str(raw, limit=self._result_limits.get(name, 8000))
            duration_ms = round((time.time() - _span_start) * 1000, 3)
            _event("tool.completed", duration_ms=duration_ms, result_chars=len(result))
            _checkpoint("after")
            telemetry.emit(
                {
                    "span": "tool_execute",
                    "tool": name,
                    "event": "exit",
                    "status": "completed",
                    "duration_ms": round((time.time() - _span_start) * 1000, 3),
                }
            )
            return result
        except asyncio.CancelledError:
            _checkpoint("after_cancelled")
            _event(
                "tool.cancelled",
                duration_ms=round((time.time() - _span_start) * 1000, 3),
            )
            raise
        except TimeoutError as exc:
            _checkpoint("after_failed")
            _event(
                "tool.failed",
                error_type="TimeoutError",
                duration_ms=round((time.time() - _span_start) * 1000, 3),
            )
            telemetry.emit(
                {
                    "span": "tool_execute",
                    "tool": name,
                    "event": "error",
                    "status": "timeout",
                    "duration_ms": round((time.time() - _span_start) * 1000, 3),
                }
            )
            raise ToolExecutionError(
                f"tool '{name}' timed out after {effective_timeout:g}s"
            ) from exc
        except ToolExecutionError as exc:
            _checkpoint("after_failed")
            _event(
                "tool.failed",
                error_type=type(exc).__name__,
                duration_ms=round((time.time() - _span_start) * 1000, 3),
            )
            telemetry.emit(
                {
                    "span": "tool_execute",
                    "tool": name,
                    "event": "error",
                    "status": "failed",
                    "error": str(exc),
                    "duration_ms": round((time.time() - _span_start) * 1000, 3),
                }
            )
            raise
        except Exception as exc:
            _checkpoint("after_failed")
            _event(
                "tool.failed",
                error_type=type(exc).__name__,
                duration_ms=round((time.time() - _span_start) * 1000, 3),
            )
            telemetry.emit(
                {
                    "span": "tool_execute",
                    "tool": name,
                    "event": "error",
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": round((time.time() - _span_start) * 1000, 3),
                }
            )
            raise ToolExecutionError(f"tool '{name}' failed: {type(exc).__name__}: {exc}") from exc


# =========================================================================
# 实例化并挂载 Veya 后端能力
# =========================================================================

master_tools = MasterToolRegistry()

# ②-B mcp 静态收口: 每个 mcp 服务的 N 个工具收成 1 个网关 mcp_<server>(action, args)。
# VEYA_MCP_GATEWAY=0 回退逐工具注册。合冻结 §2.1 (静态, 非动态裁藏)。
_MCP_GATEWAY = os.environ.get("VEYA_MCP_GATEWAY", "1") != "0"


def register_mcp_tools(server: str, adapters: list[dict], *, max_result_chars: int = 16000) -> int:
    """把某 mcp 服务的工具收成 1 个网关 (或 gateway=0 时逐个注册)。幂等。"""
    if not _MCP_GATEWAY:
        n = 0
        for a in adapters:
            if not master_tools.has(a["name"]):
                master_tools.register(
                    a["name"],
                    a["description"],
                    a["parameters"],
                    a["func"],
                    max_result_chars=max_result_chars,
                )
                n += 1
        return n
    name = f"mcp_{server}"
    if master_tools.has(name):
        return 0
    action_map = {a["name"]: a["func"] for a in adapters}
    catalog = "\n".join(f"- {a['name']}: {(a.get('description') or '')[:80]}" for a in adapters)

    async def _gateway(action: str, args: dict | None = None) -> Any:
        fn = action_map.get(action)
        if fn is None:
            raise ToolExecutionError(
                f"mcp_{server}: unknown action '{action}'. "
                f"Available: {', '.join(sorted(action_map))}"
            )
        res = fn(**(args or {}))
        return await res if inspect.isawaitable(res) else res

    master_tools.register(
        name,
        f"Gateway to the {server} MCP server ({len(adapters)} actions). "
        f"Pick `action` from the catalog and pass its `args`.\n# ACTIONS:\n{catalog}",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": f"Which {server} action (from catalog).",
                },
                "args": {"type": "object", "description": "Arguments for the action."},
            },
            "required": ["action"],
        },
        _gateway,
        max_result_chars=max_result_chars,
    )
    return 1


# 3O 主库根(Genesis 默认)
_DEFAULT_LIBRARY_ROOT = Path(__file__).resolve().parent.parent / "platform" / "3O"


def _resolve_workspace_root() -> Path:
    """工具读写文件的根: 优先 VEYA_WORKSPACE env, 默认项目根。"""
    return Path(
        os.environ.get("VEYA_WORKSPACE", str(Path(__file__).resolve().parent.parent))
    ).resolve()


def _resolve_extra_roots() -> list[Path]:
    """额外允许访问的目录 (VEYA_WORKSPACE_EXTRA_DIRS, ':' 分隔) — 同宿主兄弟项目等,
    仿 vision_toolkit_tools 的 VEYA_VISION_ALLOWED_DIRS 白名单模式。"""
    raw = os.environ.get("VEYA_WORKSPACE_EXTRA_DIRS", "")
    roots: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        with contextlib.suppress(OSError):
            roots.append(Path(part).resolve())
    return roots


def _resolve_path(filepath: str, *, must_exist: bool = True) -> Path:
    """路径安全: 拒绝逃逸工作区根 (含 VEYA_WORKSPACE_EXTRA_DIRS 额外允许目录)。"""
    root = _resolve_workspace_root()
    p = Path(filepath)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    allowed_roots = [root, *_resolve_extra_roots()]
    if not any(p == r or r in p.parents for r in allowed_roots):
        raise ToolExecutionError(f"path '{filepath}' escapes workspace root '{root}'")
    if must_exist and not p.exists():
        raise ToolExecutionError(f"file not found: {filepath}")
    return p


# ── 1. 外部世界感知 (浏览器自动化, Playwright 真实接入) ─────────────
def _html_to_text(html: str, max_chars: int = 12000) -> str:
    """HTML → 纯文本 (去 script/style/标签, 压空白), 截断防爆。"""
    import re

    html = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    text = re.sub(
        r"(?i)<br\s*/?>|<p[^>]*>|</p>|</div>|<div[^>]*>|<li[^>]*>|</li>|<h[1-6][^>]*>|</h[1-6]>",
        "\n",
        html,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:max_chars]


async def _proxy_url() -> str | None:
    """容器内 → 宿主代理 (17890 → clash 7890) 兜底 (GFW 海外站点)。

    与 veya.llm._custom_proxy_url 同源探活: 容器桥网关可达宿主 python
    代理端口时返回代理 URL, 否则 None (直连)。async httpx 不阻塞事件循环。
    """
    import os

    if os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"):
        return None  # 已有系统代理, 不叠加
    import httpx

    for gw in ("192.168.16.1", "172.18.0.1"):
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                resp = await client.get(f"http://{gw}:17890/")
                if resp.status_code == 200:
                    return f"http://{gw}:17890"
        except Exception:
            continue
    return None


async def _tool_fetch_url(url: str, max_chars: int = 12000) -> str:
    """原生 URL 阅读工具 (httpx, 免 playwright): 抓任意网页/文档为纯文本。

    GitHub 仓库链接自动走 raw README 快速通道; 其余 URL 浏览器 UA 直抓,
    HTML 剥壳为纯文本。容器内经宿主代理 (GFW) 兜底。失败返回可读错误
    (模型读到后自行调整)。
    """
    import re

    import httpx

    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return f"错误: 无效 URL {url!r} (需要 http/https 开头)。"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,text/markdown,text/plain,*/*;q=0.8",
    }
    proxy = await _proxy_url()

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20.0, follow_redirects=True, proxy=proxy)

    # GitHub 仓库/子路径 → raw README 快速通道 (无 JS, 最可靠)
    m = re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", url)
    if m:
        owner, repo = m.group(1), m.group(2)
        for branch in ("HEAD", "main", "master"):
            for readme in ("README.md", "Readme.md", "readme.md"):
                raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme}"
                try:
                    async with _client() as client:
                        r = await client.get(raw, headers=headers)
                    if r.status_code == 200:
                        return (
                            f"[GitHub {owner}/{repo} README (branch={branch})]\n"
                            + str(r.text)[:max_chars]
                        )
                except Exception:
                    continue
        return (
            f"GitHub 仓库 {owner}/{repo} 无法直接读取 README (raw 通道失败)。"
            "可尝试 browser_run 交互抓取, 或访问仓库页面链接。"
        )
    try:
        async with _client() as client:
            r = await client.get(url, headers=headers)
    except Exception as exc:
        return f"抓取失败: {type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return f"抓取失败: HTTP {r.status_code} @ {url}"
    ctype = (r.headers.get("content-type") or "").lower()
    text = r.text or ""
    text = _html_to_text(text, max_chars) if "html" in ctype else text.strip()[:max_chars]
    return text or "(页面无文本内容)"


async def _tool_browser_run(
    url: str,
    action: str = "extract_text",
    selector: str | None = None,
    text: str | None = None,
    timeout_ms: int = 30000,
) -> str:
    """Playwright 无头浏览器: 访问 URL 并执行单个动作。"""
    try:
        from veya.oprim.browser import (
            action_click,
            action_extract_html,
            action_extract_text,
            action_navigate,
            action_screenshot,
            action_type,
        )
        from veya.oskill.browser import BrowserSession
    except ImportError as exc:  # pragma: no cover — 依赖缺失
        raise ToolExecutionError(f"playwright 未安装,无法执行 browser_run: {exc}") from exc

    session = BrowserSession(headless=True)
    try:
        await session.start()
        navigate = await session.execute_sequence(
            [action_navigate(url, wait_until="domcontentloaded")]
        )
        if not navigate or not getattr(navigate[-1], "success", True):
            raise ToolExecutionError(f"browser_run: 导航失败 {url}")
        actions = {
            "extract_text": lambda: action_extract_text(selector),
            "extract_html": lambda: action_extract_html(selector),
            "screenshot": lambda: action_screenshot(selector),
            "click": lambda: action_click(selector or "body"),
            "type": lambda: action_type(selector or "input", text or ""),
        }
        if action not in actions:
            raise ToolExecutionError(
                f"browser_run: 未知 action '{action}'. Available: {', '.join(actions)}"
            )
        results = await session.execute_sequence([actions[action]()])
        last = results[-1]
        return json.dumps(
            {
                "url": last.page_url or url,
                "title": last.page_title,
                "text": last.text[:4000],
                "screenshot_base64": (last.screenshot_base64[:200] + "...")
                if last.screenshot_base64
                else "",
                "success": last.success,
            },
            ensure_ascii=False,
        )
    finally:
        with contextlib.suppress(Exception):  # stop 失败不掩盖主结果
            await asyncio.wait_for(session.stop(), timeout=10)


# ── 2. 委派给 Genesis (3O 核心研发) ────────────────────────────────
_genesis_factory: Callable[[], Any] | None = None


def set_genesis_factory(factory: Callable[[], Any] | None) -> None:
    """注入 Genesis 构造工厂(测试替换 / 延迟构造)。None 恢复默认。"""
    global _genesis_factory
    _genesis_factory = factory


def _make_genesis_agent() -> Any:
    """默认: 从 .env / 环境变量构造 Genesis(专属 key 已配置时)。"""
    from server.agents.genesis_agent import GenesisAgent

    try:
        from config.loader import _load_dotenv

        _load_dotenv()
    except Exception:
        pass
    return GenesisAgent(library_root=_DEFAULT_LIBRARY_ROOT)


async def _tool_delegate_to_genesis(requirement_json: str) -> str:
    """唤醒 Genesis Agent,把已确认的 PRD 交给 3O 引擎执行。"""
    try:
        requirement = json.loads(requirement_json)
        if not isinstance(requirement, (dict, list)):
            raise ValueError("requirement_json 必须是 JSON 对象或数组")
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(
            f"delegate_to_genesis: requirement_json 不是合法 JSON: {exc}"
        ) from exc

    factory = _genesis_factory or _make_genesis_agent
    try:
        agent = factory()
    except ValueError as exc:
        raise ToolExecutionError(
            f"Genesis 未就绪(专属 API Key 未配置): {exc}. 请先在 .env 设置 GENESIS_API_KEY。"
        ) from exc

    mission = json.dumps(requirement, ensure_ascii=False)
    result = await agent.handle_mission(mission)
    if result.get("status") != "success":
        raise ToolExecutionError(
            f"Genesis mission failed: {result.get('error', 'unknown')} "
            f"(steps={result.get('steps')}, ledger={len(agent.memory.memory['element_ledger'])})"
        )
    return json.dumps(
        {
            "status": "success",
            "response": result.get("response", ""),
            "steps": result.get("steps"),
            "ledger": agent.memory.memory["element_ledger"],
        },
        ensure_ascii=False,
    )


# ── 3. 文件系统 / 代码理解 ─────────────────────────────────────────
def _tool_read_file_ast(filepath: str) -> str:
    """读取本地文件的 AST 骨架: 理解结构而不撑爆上下文窗口。"""
    from veya.ast import extract_skeleton

    path = _resolve_path(filepath)
    if path.is_dir():
        raise ToolExecutionError(f"path '{filepath}' 是目录不是文件 — 请指定具体 .py 文件路径")
    source = path.read_text(encoding="utf-8", errors="replace")
    return str(extract_skeleton(source, filepath))


def _resolve_write_path(filepath: str, *, must_exist: bool = False) -> Path:
    """写文件根: 可写区 (VEYA_WRITE_ROOT, 默认 ~/.veya/work — veya-data 卷, 重启不丢)。

    与读根 (VEYA_WORKSPACE, 项目/代码只读) 分离: 主脑「存储文件」写到这里,
    读文件照旧读项目代码。防逃逸同 _resolve_path。
    """
    root = Path(os.environ.get("VEYA_WRITE_ROOT", str(Path.home() / ".veya" / "work"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    p = Path(filepath)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if p != root and root not in p.parents:
        raise ToolExecutionError(f"path '{filepath}' escapes write root '{root}'")
    if must_exist and not p.exists():
        raise ToolExecutionError(f"file not found: {filepath}")
    return p


def _tool_write_file(filepath: str, content: str, overwrite: bool = True) -> str:
    """写文本文件到可写工作区 (~/.veya/work, 主脑「存储文件」入口, 零代码执行)。

    路径限定可写根内 (防逃逸, 与 read_file_ast 同策略);
    overwrite=false 时已存在则报错, 避免误覆盖。
    """
    path = _resolve_write_path(filepath, must_exist=False)
    if path.exists() and not overwrite:
        raise ToolExecutionError(f"文件已存在: {filepath} (overwrite=false, 不会覆盖)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), encoding="utf-8")
    return f"✅ 已写入 {path} ({len(str(content))} 字符)。可用 read_file_ast / grep 继续理解。"


def _tool_read_hashline(filepath: str, max_lines: int = 2000) -> str:
    """Read a file with per-line LINE#hash tags for stale-safe edits."""
    from server.hashline import render

    path = _resolve_path(filepath)
    if path.is_dir():
        raise ToolExecutionError(f"path '{filepath}' 是目录不是文件")
    source = path.read_text(encoding="utf-8", errors="replace")
    cap = max(1, min(int(max_lines or 2000), 8000))
    return f"[hashline {path}]\n" + str(render(source, max_lines=cap))


def _tool_edit_hashline(
    filepath: str,
    start_tag: str,
    new_text: str,
    end_tag: str | None = None,
) -> str:
    """Replace a LINE#hash span. Fails if the file drifted since read_hashline."""
    from server.hashline import HashlineError, apply

    path = _resolve_path(filepath)
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        rec = apply(source, start_tag=start_tag, end_tag=end_tag, new_text=new_text)
    except HashlineError as exc:
        raise ToolExecutionError(str(exc)) from exc
    path.write_text(rec["content"], encoding="utf-8")
    return (
        f"hashline-edited {filepath} lines {rec['start_line']}-{rec['end_line']} "
        f"({rec['replaced_lines']} → {rec['new_lines']} lines). "
        "Re-read with read_hashline before another edit."
    )


def _tool_runtime_calls_ingest(text: str = "", traces_json: str = "") -> str:
    """Ingest a traceback or JSON stacks into the observed CALLS overlay."""
    from server.runtime_calls import ingest

    rec = ingest(text=text, traces_json=traces_json)
    if not rec.get("ok"):
        raise ToolExecutionError(rec.get("error") or "ingest failed")
    return json.dumps(rec, ensure_ascii=False)


def _tool_runtime_calls_query(symbol: str, direction: str = "both") -> str:
    from server.runtime_calls import query

    return json.dumps(query(symbol, direction=direction or "both"), ensure_ascii=False)


async def _tool_code_blast_radius(symbols: str, depth: int = 2) -> str:
    """Static callers/callees plus observed runtime CALLS (ingest traces first)."""
    from server.codebase_memory import get_connector

    names = [s.strip() for s in (symbols or "").replace(",", " ").split() if s.strip()]
    if not names:
        raise ToolExecutionError("symbols required (comma/space separated)")
    connector = get_connector()
    if connector.ready:
        try:
            radius = await connector.blast_radius(names, depth=max(1, int(depth or 2)))
            return json.dumps(radius, ensure_ascii=False)
        except Exception as exc:
            static_err = str(exc)
    else:
        static_err = "codebase-memory-mcp not ready"
    from server.runtime_calls import merge_into_radius

    radius = merge_into_radius(
        {
            "symbols": names,
            "callers": [],
            "callees": [],
            "total_affected": 0,
            "static_error": static_err,
        },
        names,
    )
    return json.dumps(radius, ensure_ascii=False)


def _tool_ast_grep_search(pattern: str, path: str = ".", lang: str | None = None) -> str:
    from server.ast_grep_tool import search

    target = str(_resolve_path(path, must_exist=True))
    rec = search(pattern, path=target, lang=lang)
    if not rec.get("ok"):
        raise ToolExecutionError(rec.get("error") or "ast-grep search failed")
    return json.dumps(rec, ensure_ascii=False)[:16000]


def _tool_ast_grep_rewrite(
    pattern: str,
    rewrite: str,
    path: str,
    lang: str | None = None,
    update: bool = False,
) -> str:
    from server.ast_grep_tool import search

    target = str(_resolve_path(path, must_exist=True))
    rec = search(pattern, path=target, lang=lang, rewrite=rewrite, update=bool(update))
    if not rec.get("ok"):
        raise ToolExecutionError(rec.get("error") or "ast-grep rewrite failed")
    mode = "applied" if update else "dry-run"
    rec["mode"] = mode
    return json.dumps(rec, ensure_ascii=False)[:16000]


def _tool_grep(pattern: str, glob: str | None = None, root: str | None = None) -> str:
    """在项目内搜索代码(ripgrep),定位定义与引用。"""
    from server.assembly import ripgrep_search

    search_root = (
        str(_resolve_path(root, must_exist=True)) if root else str(_resolve_workspace_root())
    )
    try:
        hits = ripgrep_search(pattern, root=search_root, glob=glob)
    except FileNotFoundError as exc:
        raise ToolExecutionError("ripgrep (rg) 未安装,无法执行 grep") from exc
    if not hits:
        return f"no matches for {pattern!r}"
    lines = []
    for hit in hits[:40]:
        data = hit.get("data", {})
        path = (data.get("path") or {}).get("text", "?")
        line_no = data.get("line_number", "?")
        text = (data.get("lines") or {}).get("text", "").rstrip("\n")
        lines.append(f"{path}:{line_no}: {text}")
    return "\n".join(lines)


def _tool_list_files(path: str = ".") -> str:
    """列出工作区文件(排除噪音目录)。"""
    root = _resolve_workspace_root()
    target = root if path in (".", "") else _resolve_path(path, must_exist=False)
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
    allowed_roots = [root, *_resolve_extra_roots()]
    display_root = next((r for r in allowed_roots if target == r or r in target.parents), root)
    lines = []
    count = 0
    for p in sorted(target.rglob("*")):
        if any(part in excluded for part in p.parts):
            continue
        try:
            rel = p.relative_to(display_root)
        except ValueError:
            continue
        lines.append(f"{rel}/" if p.is_dir() else f"{rel} ({p.stat().st_size}b)")
        count += 1
        if count >= 200:
            lines.append("... (truncated)")
            break
    return "\n".join(lines) or "(empty)"


# ── 4. 代码执行 / 测试 (3O 隔离沙箱) ───────────────────────────────
def _normalize_sandbox_command(command: str) -> str:
    """沙箱只有 python3/pip3 (无 python/pip 软链)。把模型常用的整词 python/pip 归一到
    python3/pip3 (不动已带 3 的), 省掉 'python: not found' 反复空转烧轮次。
    """
    import re

    command = re.sub(r"(?<![\w./-])python(?!3)(?=\s|$)", "python3", command)
    return re.sub(r"(?<![\w./-])pip(?!3)(?=\s|$)", "pip3", command)


async def _tool_run_in_sandbox(code: str | None = None, command: str | None = None) -> str:
    """在统一沙箱合同里执行代码。策略由 isolation_policy(chat_verify, profile) 决定。"""
    import sys

    from server.auth import current_user
    from veya.platform import load

    if not code and not command:
        raise ToolExecutionError(
            "run_in_sandbox requires either 'code' (python source) or 'command' (shell)"
        )
    omodul = load("omodul")
    owner_id = str(current_user().get("user_id") or "")
    with omodul.sandbox_scope("chat_verify", owner_id=owner_id) as session:
        if not session.ok:
            raise ToolExecutionError(session.error or "sandbox_create failed")
        if code:
            rec = session.exec([sys.executable, "-c", code], timeout_s=30)
        else:
            assert command is not None  # guaranteed by the not-code-and-not-command check above
            rec = session.exec(
                ["bash", "-lc", _normalize_sandbox_command(command)],
                timeout_s=30,
            )
        isolation = rec.get("isolation") or session.isolation
        note = f"isolation={isolation} network_blocked={rec.get('block_network', session.block_network)}"
        if rec.get("exit_code") != 0:
            raise ToolExecutionError(
                f"exit_code={rec.get('exit_code')} ({note})\n"
                f"stdout:\n{rec.get('stdout', '')}\n"
                f"stderr:\n{rec.get('stderr', '')}"
            )
        return f"exit_code=0 ({note})\n{rec.get('stdout', '')}"


# ── 5. Genesis 记忆账本查询 ────────────────────────────────────────
def _tool_search_genesis_ledger(query: str) -> str:
    """查询 Genesis 的永久记忆账本: 3O 库里已有哪些元素(锻造前先查, 避免重复造轮子)。"""
    from server.agents.genesis_memory import GenesisMemory

    memory = GenesisMemory()
    hits = memory.search_elements(query)
    if not hits:
        return f"Genesis 账本中没有与 '{query}' 匹配的元素"
    return json.dumps(hits, ensure_ascii=False, indent=2)


# ── 6. 量化交火协议 (控制面/数据面分离) ─────────────────────────────
def _tool_get_market_data_schema(asset_id: str) -> str:
    """元数据注入: 只把 Schema + 前 5 行喂给大模型(全量数据绝不进上下文)。"""
    from server.quant_coprocessor import get_market_data_schema as _schema

    try:
        return str(_schema(asset_id))
    except FileNotFoundError as exc:
        raise ToolExecutionError(str(exc)) from exc


async def _tool_run_backtest_coprocessor(
    strategy_code: str, asset_id: str, start_date: str, end_date: str
) -> str:
    """时序协处理器: 在隔离沙箱中对海量数据执行策略, 只返回浓缩指标 + 图表 JSON。

    前置防线: 静态不变量校验 (oprim._lookahead_scan AST 硬扫描) —
    未来函数/未来行索引等硬违规 (verdict=block) 直接拦截, 不进沙箱。
    """
    from server.static_invariant import VeyaStaticInvariant

    gate = VeyaStaticInvariant.check(strategy_code, filename=f"{asset_id}_strategy")
    if gate["verdict"] == "block":
        return json.dumps(
            {
                "status": "blocked",
                "reason": "静态不变量校验拦截 (look-ahead/leakage)",
                "violations": gate["violations"],
            },
            ensure_ascii=False,
        )

    from server.quant_coprocessor import QuantCoprocessor

    coprocessor = QuantCoprocessor()
    return str(
        await coprocessor.execute_strategy(
            strategy_code=strategy_code,
            asset_id=asset_id,
            start_date=start_date,
            end_date=end_date,
        )
    )


# ================= 挂载 =================
master_tools.register(
    name="fetch_url",
    description=(
        "Read any URL (web page / article / docs / GitHub repo) as plain text. "
        "Use this when the user pastes a link or asks about something on the web — "
        "GitHub repo links auto-read the README. Lightweight httpx fetch (no browser); "
        "use browser_run only when you need interaction (click / login / JS pages)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标 URL (http/https)"},
            "max_chars": {"type": "integer", "description": "可选, 返回文本上限 (默认 12000)"},
        },
        "required": ["url"],
    },
    func=_tool_fetch_url,
    max_result_chars=16000,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="browser_run",
    description=(
        "Control a headless browser to visit a URL, scrape data, or interact with a webpage. "
        "Use this when the user asks for latest news, social media posts, or live web data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标 URL"},
            "action": {
                "type": "string",
                "description": "extract_text | extract_html | screenshot | click | type (default extract_text)",
            },
            "selector": {"type": "string", "description": "CSS/text 选择器 (optional)"},
            "text": {"type": "string", "description": "type action 的输入文本 (optional)"},
            "timeout_ms": {"type": "integer", "description": "导航超时 (optional)"},
        },
        "required": ["url"],
    },
    func=_tool_browser_run,
)

master_tools.register(
    name="delegate_to_genesis",
    description=(
        "Trigger the 3O Engine Genesis workflow. USE THIS ONLY WHEN the user explicitly "
        "confirms a Requirement Document (PRD) to build mathematical models, operators, "
        "or core system components. Genesis is the sovereign 3O librarian with permanent memory."
    ),
    parameters={
        "type": "object",
        "properties": {
            "requirement_json": {
                "type": "string",
                "description": "The approved PRD as a JSON string",
            }
        },
        "required": ["requirement_json"],
    },
    func=_tool_delegate_to_genesis,
)

master_tools.register(
    name="write_file",
    description=(
        "Write/save text content to a file inside the workspace (主脑「存储文件」入口, "
        "zero code execution). Use this when the user asks to save/store/persist content "
        "to a file. NEVER use run_in_sandbox to write files — write_file is the file-write "
        "tool. After writing, use read_file_ast / grep to understand the file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filepath": {"type": "string", "description": "path relative to workspace root"},
            "content": {"type": "string", "description": "text content to write"},
            "overwrite": {
                "type": "boolean",
                "description": "default true; false = refuse if file exists",
            },
        },
        "required": ["filepath", "content"],
    },
    func=_tool_write_file,
)

master_tools.register(
    name="read_hashline",
    description=(
        "Read a workspace file with per-line LINE#xxxxxxxx content-hash tags. "
        "Use this BEFORE surgical edits. Then call edit_hashline citing those tags. "
        "If the file changed, tags no longer match and the edit is rejected — "
        "re-read instead of guessing old_string. Prefer this over rewriting the whole file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filepath": {"type": "string", "description": "path relative to workspace root"},
            "max_lines": {
                "type": "integer",
                "description": "cap (default 2000) to protect context",
            },
        },
        "required": ["filepath"],
    },
    func=_tool_read_hashline,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="edit_hashline",
    description=(
        "Replace a span identified by LINE#hash tags from read_hashline. "
        "start_tag is required; end_tag optional (defaults to one line). "
        "Refuses the write if hashes no longer match the live file (stale edit). "
        "Do not invent tags — copy them from the latest read_hashline output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filepath": {"type": "string"},
            "start_tag": {
                "type": "string",
                "description": "LINE#xxxxxxxx of the first line to replace",
            },
            "end_tag": {
                "type": "string",
                "description": "LINE#xxxxxxxx of the last line (inclusive); omit for one line",
            },
            "new_text": {
                "type": "string",
                "description": "replacement text (may be multiple lines)",
            },
        },
        "required": ["filepath", "start_tag", "new_text"],
    },
    func=_tool_edit_hashline,
)

master_tools.register(
    name="runtime_calls_ingest",
    description=(
        "Ingest a Python traceback, pytest --tb=short, or JSON stacks into the "
        "observed CALLS overlay (runtime edges static graph cannot see: dispatch/"
        "reflection/tests). Then query with runtime_calls_query or code_blast_radius. "
        "MCP ingest_traces is still a stub; this overlay is the live store."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "traceback / pytest short tb"},
            "traces_json": {
                "type": "string",
                "description": "JSON: [{caller,callee,file,line}] or [{frames:[{func,file,line}]}]",
            },
        },
    },
    func=_tool_runtime_calls_ingest,
)

master_tools.register(
    name="runtime_calls_query",
    description="Query observed runtime CALLS for a symbol (callers/callees from ingested traces).",
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {
                "type": "string",
                "enum": ["both", "callers", "callees"],
                "description": "default both",
            },
        },
        "required": ["symbol"],
    },
    func=_tool_runtime_calls_query,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="code_blast_radius",
    description=(
        "Impact set for symbols: static codebase-memory callers/callees UNION "
        "ingested runtime CALLS. Use before a risky edit. Runtime-only names are "
        "listed separately (dispatch/reflection that tree-sitter missed)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbols": {
                "type": "string",
                "description": "comma or space separated function/class names",
            },
            "depth": {"type": "integer", "description": "static trace depth, default 2"},
        },
        "required": ["symbols"],
    },
    func=_tool_code_blast_radius,
)

master_tools.register(
    name="ast_grep_search",
    description=(
        "Structural search with ast-grep (AST pattern, not regex). "
        "Fails honestly if ast-grep is not installed. Pair with edit_hashline "
        "for stale-safe writes; use ast_grep_rewrite for pattern rewrite (dry-run default)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "ast-grep pattern, e.g. 'print($A)'"},
            "path": {
                "type": "string",
                "description": "file or dir relative to workspace (default .)",
            },
            "lang": {
                "type": "string",
                "description": "python/ts/go/... ; inferred from suffix if omitted",
            },
        },
        "required": ["pattern"],
    },
    func=_tool_ast_grep_search,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="ast_grep_rewrite",
    description=(
        "Structural rewrite with ast-grep. Default dry-run (preview hits). "
        "Set update=true to apply on disk (workspace-jailed). Install ast-grep if missing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "rewrite": {"type": "string", "description": "replacement pattern, e.g. 'log($A)'"},
            "path": {"type": "string", "description": "file or dir relative to workspace"},
            "lang": {"type": "string"},
            "update": {
                "type": "boolean",
                "description": "false=preview (default), true=write files",
            },
        },
        "required": ["pattern", "rewrite", "path"],
    },
    func=_tool_ast_grep_rewrite,
)

master_tools.register(
    name="read_file_ast",
    description=(
        "Read the AST skeleton of a local file (signatures + line ranges, no bodies) to "
        "understand its structure without blowing up the context window."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filepath": {"type": "string", "description": "path relative to workspace root"}
        },
        "required": ["filepath"],
    },
    func=_tool_read_file_ast,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="grep",
    description="Search code in the workspace with ripgrep to locate definitions and usages.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "regex pattern"},
            "glob": {"type": "string", "description": "rg glob filter, e.g. '*.py' (optional)"},
            "root": {
                "type": "string",
                "description": "subdirectory relative to workspace root (optional)",
            },
        },
        "required": ["pattern"],
    },
    func=_tool_grep,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="list_files",
    description="List files under a directory of the workspace (noise dirs excluded).",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "directory relative to workspace root (optional)",
            }
        },
    },
    func=_tool_list_files,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="run_in_sandbox",
    description=(
        "Run python code (or a shell command) in the unified 3O sandbox (chat_verify=process: "
        "CPU/memory/time limits; network is NOT blocked). ONLY for executing/verifying snippets. "
        "This tool exists to produce evidence for a fact you're not certain of (does this "
        "import work, does this snippet behave as expected) — it is not a default step, and "
        "not needed just because a question involves code or is hard to reason about. "
        "The sandbox has `python3` (NOT `python`) and the stdlib `unittest` (pytest is NOT "
        "installed) — run tests with `python3 -m unittest discover` or `python3 -m unittest "
        "<module>`, never `python ...` or `pytest`. "
        "DO NOT use it for file operations — writing files → write_file, reading/understanding "
        "files → read_file_ast / grep / mcp_codebase. If the user just wants to save content "
        "or understand a file, use those instead of running anything."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "python source to execute (optional)"},
            "command": {"type": "string", "description": "shell command to execute (optional)"},
        },
    },
    func=_tool_run_in_sandbox,
)

master_tools.register(
    name="search_genesis_ledger",
    description=(
        "Query Genesis's permanent memory ledger for existing 3O elements "
        "(check before requesting new operator development to avoid duplicates)."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "keyword, e.g. '均线' or 'ema'"}},
        "required": ["query"],
    },
    func=_tool_search_genesis_ledger,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="get_market_data_schema",
    description=(
        "Quant protocol step 1: fetch ONLY the schema + first 5 rows of market data for an asset "
        "(columns, dtypes, sample). Use this BEFORE writing any backtest strategy code — "
        "the full dataset is NEVER exposed to you; it is computed in the sandbox."
    ),
    parameters={
        "type": "object",
        "properties": {
            "asset_id": {"type": "string", "description": "asset symbol, e.g. 'AAPL' or 'BTCUSDT'"}
        },
        "required": ["asset_id"],
    },
    func=_tool_get_market_data_schema,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="run_backtest_coprocessor",
    description=(
        "Quant protocol step 2: execute a strategy in the isolated quant sandbox against the FULL "
        "dataset (millions of rows) and return ONLY condensed metrics (total_return / sharpe_ratio / "
        "max_drawdown) plus downsampled echarts chart data. Your strategy code MUST define "
        "run_strategy(df) -> df with 'daily_return' and 'cum_return' columns. "
        "NEVER try to load or compute market data yourself — you are the strategy expresser, "
        "the coprocessor is the calculator."
    ),
    parameters={
        "type": "object",
        "properties": {
            "strategy_code": {
                "type": "string",
                "description": "python code defining run_strategy(df)",
            },
            "asset_id": {"type": "string"},
            "start_date": {"type": "string", "description": "e.g. '2022-01-01'"},
            "end_date": {"type": "string", "description": "e.g. '2024-12-31'"},
        },
        "required": ["strategy_code", "asset_id", "start_date", "end_date"],
    },
    func=_tool_run_backtest_coprocessor,
    max_result_chars=40000,  # 浓缩 JSON(含 500 点图表数据)必须完整回喂
)

# ================= 状态内核 (ARCHITECTURE_STATE_KERNEL Phase 1) ============
# 主脑零改动: 只新增能力工具, 模型自主调用。状态复用 plan_todo 的 plan JSON
# (单一真相源), 补 Quota/Claim/Gate 控制面对象。
from server.state_kernel import gate_check, quota_should_run, todo_claim  # noqa: E402

master_tools.register(
    name="system_quota_should_run",
    description=(
        "控制面判断「该不该动」: 读当前活跃计划状态, 返回 {should_run, action, "
        "reason}。action=deliver(有可推进 todo) / repair(有 blocked 需解除) / "
        "wait(无可推进, 等外部或新指令)。长程任务/无人值守/被唤醒时先调本工具 "
        "判断本轮是否值得执行, 避免空转。可传 plan_id 指定计划, 空则找最近活跃计划。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {
                "type": "string",
                "description": "可选。指定计划 id; 空=自动找最近活跃计划。",
            },
        },
    },
    func=quota_should_run,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="system_todo_claim",
    description=(
        "认领计划中的一个 todo (claim + 可回收 lease): 标记 in_progress 并设置 "
        "TTL 租约 (默认 45 分钟, 过期自动释放, fail-closed)。done/blocked 不能认领; "
        "他人有效租约内不可抢占。多 agent/跨轮分工时用本工具声明『谁在做这件事』。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "description": "计划 id (create_plan 返回)。"},
            "todo_id": {"type": "string", "description": "待办 id (plan_status 可见)。"},
            "lease_minutes": {
                "type": "integer",
                "description": "可选。租约分钟数, 默认 45, 上限 1440。",
            },
        },
        "required": ["plan_id", "todo_id"],
    },
    func=todo_claim,
)

master_tools.register(
    name="system_gate_check",
    description=(
        "检查一个 scoped 决策门 (gate) 是否满足: 返回 {gate_open, scope, "
        "blocking_todos}。gate_scope 描述该门约束 (如 CI 通过/依赖 X 完成)。"
        "只检查 scope 相关 todo, 不冻结计划全局 (其他 todo 仍可推进)。"
        "执行不可逆/高风险步骤前先调本工具确认前置门已开。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "description": "计划 id。"},
            "gate_scope": {
                "type": "string",
                "description": "该门约束的描述/关键词 (如 'CI 通过' 或依赖 todo 名)。",
            },
        },
        "required": ["plan_id", "gate_scope"],
    },
    func=gate_check,
    side_effect=SideEffect.PURE_READ,
)

# ================= 状态内核 Phase 2+3 (Spend / Terminal Gate / 边界扫描) ====
from server.state_kernel import boundary_scan, quota_spend_slot, terminal_gate_check  # noqa: E402

master_tools.register(
    name="system_quota_spend_slot",
    description=(
        "验证后记账 (spend): 为一次『已完成的控制面推进』记一笔 (幂等, effect_id "
        "唯一不双扣)。spend = 有效推进的账, 不是『模型被唤醒过』的计数器 — "
        "dry-run / 只读 / 静默轮询不要调用。完成一个 todo (update_todo done + "
        "验证) 后调用, effect_id 填执行回执/验证摘要 hash。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "description": "计划 id。"},
            "todo_id": {"type": "string", "description": "已 done 的 todo id。"},
            "effect_id": {
                "type": "string",
                "description": "本次执行效果唯一标识 (如验证摘要/产物 hash)。",
            },
            "note": {"type": "string", "description": "可选。记账说明。"},
        },
        "required": ["plan_id", "todo_id", "effect_id"],
    },
    func=quota_spend_slot,
)

master_tools.register(
    name="system_terminal_gate_check",
    description=(
        "检查动作是否属 terminal (不可逆/发布/部署/删除/合并/推送) — 若属则须"
        "人工审批, 不可自行执行 (返回 requires_approval=true)。可传 plan_id+scope "
        "同时检查 plan gate。执行任何不可逆/对外生效动作前先调用本工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "要执行的动作描述 (如 'git push 到 main' / '发布到生产')。",
            },
            "plan_id": {
                "type": "string",
                "description": "可选。关联计划 id (同时检查 plan gate)。",
            },
            "scope": {"type": "string", "description": "可选。gate scope 关键词。"},
        },
        "required": ["action"],
    },
    func=terminal_gate_check,
    side_effect=SideEffect.PURE_READ,
)

master_tools.register(
    name="system_boundary_scan",
    description=(
        "文件级公私边界扫描: 检查目录内 git-tracked 文件 (即公开面) 是否含敏感"
        "内容 (api key/secret/token/私钥/密码/.env)。公开仓库只放 schema/示例/消毒"
        "文档; 真实状态与凭据只放 git-ignored 目录。提交/发布前先扫描防泄露。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "可选。要扫描的目录 (默认当前工作区)。"},
        },
    },
    func=boundary_scan,
    side_effect=SideEffect.PURE_READ,
)

# ================= 3O Wayfinding + StatefulProcedure ======================
# 目标模糊时先探路 (wayfind_*: 认领 ticket → 写决策, 收敛 frontier/fog),
# 收敛完成后编译成可执行 Runbook 并用 stateful_* 跑 (checked transition,
# 失败留在原节点)。业务逻辑在 omodul.wayfinding / obase.orchestrator, 这里
# 只挂号 —— 见 server/wayfinding_tools.py。
from server.wayfinding_tools import (  # noqa: E402
    stateful_current,
    stateful_goto,
    stateful_history,
    stateful_start,
    wayfind_add_fog,
    wayfind_add_ticket,
    wayfind_chart,
    wayfind_claim,
    wayfind_compile_runbook,
    wayfind_complete,
    wayfind_decisions,
    wayfind_frontier,
    wayfind_graduate_fog,
    wayfind_resolve,
    wayfind_rule_out_of_scope,
    wayfind_to_spec,
    wayfind_wire_blocking,
)

master_tools.register(
    name="wayfind_chart",
    description=(
        "开一张新的探路图: 目标还模糊/需要先收敛范围时用这个, 不要直接动手做。"
        "返回 map_id, 后续所有 wayfind_*/stateful_* 都要传它。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "destination": {"type": "string", "description": "1-2 句话说清楚往哪个方向探。"},
            "notes": {"type": "string", "description": "可选。领域背景/已知偏好/约束。"},
        },
        "required": ["destination"],
    },
    func=wayfind_chart,
)

master_tools.register(
    name="wayfind_add_ticket",
    description="给探路图加一张待澄清问题的 ticket。type: research/prototype/grilling/task。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "wayfind_chart 返回的 map_id。"},
            "title": {"type": "string", "description": "ticket 标题 (人看的短名)。"},
            "question": {"type": "string", "description": "具体要澄清/解决什么。"},
            "ticket_type": {
                "type": "string",
                "description": "research(可自行调查)/prototype(要出个东西给人看)/"
                "grilling(纯对话澄清, 不能替人决定)/task(阻塞性前置工作), 默认 task。",
            },
        },
        "required": ["map_id", "title", "question"],
    },
    func=wayfind_add_ticket,
)

master_tools.register(
    name="wayfind_wire_blocking",
    description="声明 to_ticket 依赖 from_ticket 先解决 (未关闭前 to 不出现在 frontier)。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "from_ticket": {"type": "string", "description": "前置 ticket id。"},
            "to_ticket": {"type": "string", "description": "被阻塞的 ticket id。"},
        },
        "required": ["map_id", "from_ticket", "to_ticket"],
    },
    func=wayfind_wire_blocking,
)

master_tools.register(
    name="wayfind_frontier",
    description="看当前能认领的 ticket (open+未阻塞+未认领)。探路循环每轮先看这个。",
    parameters={
        "type": "object",
        "properties": {"map_id": {"type": "string", "description": "map_id。"}},
        "required": ["map_id"],
    },
    func=wayfind_frontier,
)

master_tools.register(
    name="wayfind_claim",
    description="认领一张 ticket。已被别人认领会失败 (不可抢占); 认领后才能 resolve。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "ticket_id": {"type": "string", "description": "要认领的 ticket id。"},
            "claimed_by": {"type": "string", "description": "可选。认领者标识, 默认 veya。"},
        },
        "required": ["map_id", "ticket_id"],
    },
    func=wayfind_claim,
)

master_tools.register(
    name="wayfind_resolve",
    description=(
        "解决一张已认领的 ticket, 写下决策 (gist 会进 decisions_so_far, 之后编译成 "
        "Runbook 节点)。必须先 wayfind_claim 才能 resolve。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "ticket_id": {"type": "string", "description": "已认领的 ticket id。"},
            "resolution": {"type": "string", "description": "完整说明: 做了什么/为什么。"},
            "gist": {"type": "string", "description": "一句话结论。"},
        },
        "required": ["map_id", "ticket_id", "resolution", "gist"],
    },
    func=wayfind_resolve,
)

master_tools.register(
    name="wayfind_rule_out_of_scope",
    description="把一张 ticket 标记为不在本次范围内: 关闭且不会再出现在 frontier。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "ticket_id": {"type": "string", "description": "要排除的 ticket id。"},
            "reason": {"type": "string", "description": "为什么排除。"},
        },
        "required": ["map_id", "ticket_id", "reason"],
    },
    func=wayfind_rule_out_of_scope,
)

master_tools.register(
    name="wayfind_add_fog",
    description="记一块还说不清楚的模糊地带, 之后用 wayfind_graduate_fog 拆成具体 ticket。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "patch": {"type": "string", "description": "模糊地带的描述。"},
        },
        "required": ["map_id", "patch"],
    },
    func=wayfind_add_fog,
)

master_tools.register(
    name="wayfind_graduate_fog",
    description="把一块模糊地带拆成具体 ticket (每个标题一张 task 类型 ticket)。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "patch": {
                "type": "string",
                "description": "要拆解的 fog patch (须与 wayfind_add_fog 一致)。",
            },
            "new_ticket_titles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "拆出的新 ticket 标题列表。",
            },
        },
        "required": ["map_id", "patch", "new_ticket_titles"],
    },
    func=wayfind_graduate_fog,
)

master_tools.register(
    name="wayfind_decisions",
    description="列出这张探路图目前已经写下的所有决策。",
    parameters={
        "type": "object",
        "properties": {"map_id": {"type": "string", "description": "map_id。"}},
        "required": ["map_id"],
    },
    func=wayfind_decisions,
)

master_tools.register(
    name="wayfind_complete",
    description="frontier 和 fog 都清空时把地图标记为 completed; 没清空会告诉你还剩什么。",
    parameters={
        "type": "object",
        "properties": {"map_id": {"type": "string", "description": "map_id。"}},
        "required": ["map_id"],
    },
    func=wayfind_complete,
)

master_tools.register(
    name="wayfind_to_spec",
    description=(
        "把已清空探路图的决策收敛成一份 spec-pack requirements(产出人能读的文档, "
        "不是直接编译成可执行 Runbook——那是 wayfind_compile_runbook)。决策多/"
        "要给人看/要接 goal_run 批量执行时选这条。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "已清空(is_clear)的 map_id。"},
            "slug": {
                "type": "string",
                "description": "可选: 指定 spec-pack slug (默认由地图标题生成)。",
            },
        },
        "required": ["map_id"],
    },
    func=wayfind_to_spec,
)

master_tools.register(
    name="wayfind_compile_runbook",
    description="把已完成探路图的决策编译成 Runbook 预览 (不会自动开始执行)。",
    parameters={
        "type": "object",
        "properties": {"map_id": {"type": "string", "description": "已 complete 的 map_id。"}},
        "required": ["map_id"],
    },
    func=wayfind_compile_runbook,
)

master_tools.register(
    name="stateful_start",
    description="从一张已完成的探路图编译 Runbook, 开始 (或用同一 run_id 续跑) 执行。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "已 complete 的 map_id。"},
            "run_id": {"type": "string", "description": "可选。不传则用 map-<map_id>。"},
        },
        "required": ["map_id"],
    },
    func=stateful_start,
)

master_tools.register(
    name="stateful_current",
    description="看某次执行当前停在哪个节点, 节点 prompt 是什么, 能转去哪些节点。",
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "run_id": {"type": "string", "description": "stateful_start 用过的 run_id。"},
        },
        "required": ["map_id", "run_id"],
    },
    func=stateful_current,
)

master_tools.register(
    name="stateful_goto",
    description=(
        "尝试把执行转移到 target 节点。每个节点都有一条 checklist check ('Decision "
        "\\'<title>\\' applied / verified') 门住转移 —— 真的落实了该决策后, 把这条 "
        "checklist 文案原样传进 confirm_items 才能过; 不传或没确认会留在原节点并说明原因。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "description": "map_id。"},
            "run_id": {"type": "string", "description": "run_id。"},
            "target": {
                "type": "string",
                "description": "目标节点 id (stateful_current 里的 allowed_next)。",
            },
            "confirm_items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "已确认的 checklist 文案列表 (原样照抄节点 prompt 里的决策标题)。",
            },
            "workspace_root": {
                "type": "string",
                "description": "可选。command 类 check 的执行根目录。",
            },
        },
        "required": ["map_id", "run_id", "target"],
    },
    func=stateful_goto,
)

master_tools.register(
    name="stateful_history",
    description="看某次执行的转移历史 (最近 tail 条)。",
    parameters={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "run_id。"},
            "tail": {"type": "integer", "description": "可选。返回条数, 默认 20。"},
        },
        "required": ["run_id"],
    },
    func=stateful_history,
)

# ================= Team Mode — 点对点协作(oh-my-openagent 内化) ===========
# 邮箱 + 共享任务列表(claim) + 协商式关闭。真实限制: board.py 卡片跑外部 CLI
# 子进程, 塞不进 veya 工具, 这套是给主脑自己协调多个 session/工作项用的——
# 见 server/team_coord.py 文件头。
from server.team_tools import (  # noqa: E402
    team_approve_shutdown,
    team_create,
    team_delete,
    team_list,
    team_read_messages,
    team_reject_shutdown,
    team_send_message,
    team_shutdown_request,
    team_status,
    team_task_create,
    team_task_get,
    team_task_list,
    team_task_update,
)

master_tools.register(
    name="team_create",
    description="建一个协作组(邮箱+共享任务列表)。lead 会自动加入 members。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名(唯一)。"},
            "description": {"type": "string", "description": "可选: 协作组用途。"},
            "lead": {"type": "string", "description": "可选: 领队成员 id。"},
            "member_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选: 初始成员 id 列表。",
            },
        },
        "required": ["name"],
    },
    func=team_create,
)

master_tools.register(
    name="team_delete",
    description="解散协作组。还有成员没完成关闭协商(除发起者外)且 force=False 会被拒绝。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "requested_by": {
                "type": "string",
                "description": "可选: 发起解散的成员 id(不计入阻塞检查)。",
            },
            "force": {"type": "boolean", "description": "可选: 强制解散, 忽略未完成协商的成员。"},
        },
        "required": ["name"],
    },
    func=team_delete,
)

master_tools.register(
    name="team_list",
    description="列出所有未解散的协作组及其任务概况。",
    parameters={"type": "object", "properties": {}},
    func=team_list,
)

master_tools.register(
    name="team_send_message",
    description="发一条消息。to_member 留空 = 广播给全体成员。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "from_member": {"type": "string", "description": "发送者成员 id。"},
            "content": {"type": "string", "description": "消息内容。"},
            "to_member": {"type": "string", "description": "可选: 接收者成员 id, 留空广播。"},
        },
        "required": ["name", "from_member", "content"],
    },
    func=team_send_message,
)

master_tools.register(
    name="team_read_messages",
    description="读某个成员的邮箱(发给他的 + 广播的)。默认只看未读, 读过会标记已读。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "member_id": {"type": "string", "description": "要读取邮箱的成员 id。"},
            "unread_only": {"type": "boolean", "description": "可选: 默认 true, 只看未读。"},
        },
        "required": ["name", "member_id"],
    },
    func=team_read_messages,
)

master_tools.register(
    name="team_task_create",
    description="加一个共享任务(open 状态, 谁都能来 claim)。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "title": {"type": "string", "description": "任务标题。"},
            "description": {"type": "string", "description": "可选: 任务详情。"},
        },
        "required": ["name", "title"],
    },
    func=team_task_create,
)

master_tools.register(
    name="team_task_list",
    description="列出共享任务, 可按状态过滤(open/claimed/done)。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "status_filter": {"type": "string", "description": "可选: open/claimed/done。"},
        },
        "required": ["name"],
    },
    func=team_task_list,
)

master_tools.register(
    name="team_task_get",
    description="看一个任务的详情(含 note)。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "task_id": {"type": "string", "description": "任务 id。"},
        },
        "required": ["name", "task_id"],
    },
    func=team_task_get,
)

master_tools.register(
    name="team_task_update",
    description="更新任务状态(status=claimed 时必须带 claimed_by, 已被别人认领会拒绝)/认领人/备注。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "task_id": {"type": "string", "description": "任务 id。"},
            "status": {"type": "string", "description": "可选: open/claimed/done。"},
            "claimed_by": {"type": "string", "description": "可选: status=claimed 时的认领人。"},
            "note": {"type": "string", "description": "可选: 进度备注。"},
        },
        "required": ["name", "task_id"],
    },
    func=team_task_update,
)

master_tools.register(
    name="team_shutdown_request",
    description="请求某成员关闭(不是立刻关, 要走 team_approve_shutdown/team_reject_shutdown)。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "member_id": {"type": "string", "description": "要关闭的成员 id。"},
            "reason": {"type": "string", "description": "可选: 关闭原因。"},
        },
        "required": ["name", "member_id"],
    },
    func=team_shutdown_request,
)

master_tools.register(
    name="team_approve_shutdown",
    description="批准某成员的关闭请求。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "member_id": {"type": "string", "description": "成员 id。"},
        },
        "required": ["name", "member_id"],
    },
    func=team_approve_shutdown,
)

master_tools.register(
    name="team_reject_shutdown",
    description="拒绝某成员的关闭请求, 成员状态回到 active。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "协作组名。"},
            "member_id": {"type": "string", "description": "成员 id。"},
            "reason": {"type": "string", "description": "可选: 拒绝原因。"},
        },
        "required": ["name", "member_id"],
    },
    func=team_reject_shutdown,
)

master_tools.register(
    name="team_status",
    description="汇总视图: 成员状态 + 任务计数 + 消息数。",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "协作组名。"}},
        "required": ["name"],
    },
    func=team_status,
)

# ================= Wayfinding — GitHub Issues 后端 =========================
# 跟上面本地事件溯源版 wayfind_* 并存: map=GitHub issue, ticket=原生
# sub-issue, blocking=原生 issue dependency — 人能直接在 GitHub 网页上看到
# frontier, 不用调工具查。见 server/wayfinding_github_tools.py。
from server.wayfinding_github_tools import (  # noqa: E402
    wayfind_gh_add_fog,
    wayfind_gh_add_ticket,
    wayfind_gh_chart,
    wayfind_gh_claim,
    wayfind_gh_complete,
    wayfind_gh_decisions,
    wayfind_gh_frontier,
    wayfind_gh_graduate_fog,
    wayfind_gh_resolve,
    wayfind_gh_rule_out_of_scope,
    wayfind_gh_wire_blocking,
)

master_tools.register(
    name="wayfind_gh_chart",
    description=(
        "在指定 GitHub 仓库开一张新的探路图 (issue, label wayfinder:map)。目标模糊/"
        "需要先收敛范围时用这个。返回 map 的 issue number, 后续 wayfind_gh_* 都要传它。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "目标仓库, 'owner/name' 格式。"},
            "destination": {"type": "string", "description": "1-2 句话说清楚往哪个方向探。"},
            "notes": {"type": "string", "description": "可选。领域背景/已知偏好/约束。"},
        },
        "required": ["repo", "destination"],
    },
    func=wayfind_gh_chart,
)

master_tools.register(
    name="wayfind_gh_add_ticket",
    description="给探路图加一张 ticket (GitHub 原生 sub-issue)。type: research/prototype/grilling/task。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {
                "type": "integer",
                "description": "wayfind_gh_chart 返回的地图 issue number。",
            },
            "title": {"type": "string", "description": "ticket 标题。"},
            "question": {"type": "string", "description": "具体要澄清/解决什么。"},
            "ticket_type": {
                "type": "string",
                "description": "research/prototype/grilling/task, 默认 task。",
            },
        },
        "required": ["repo", "map_number", "title", "question"],
    },
    func=wayfind_gh_add_ticket,
)

master_tools.register(
    name="wayfind_gh_wire_blocking",
    description="声明 to_number 依赖 from_number 先解决 (GitHub 原生 blocked-by, 网页上直接可见)。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "from_number": {"type": "integer", "description": "前置 ticket 的 issue number。"},
            "to_number": {"type": "integer", "description": "被阻塞的 ticket 的 issue number。"},
        },
        "required": ["repo", "from_number", "to_number"],
    },
    func=wayfind_gh_wire_blocking,
)

master_tools.register(
    name="wayfind_gh_frontier",
    description="看当前能认领的 ticket (open+未阻塞+未认领)。探路循环每轮先看这个。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {"type": "integer", "description": "地图 issue number。"},
        },
        "required": ["repo", "map_number"],
    },
    func=wayfind_gh_frontier,
)

master_tools.register(
    name="wayfind_gh_claim",
    description="认领一张 ticket (指派给自己); 已被别人认领会失败。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "ticket_number": {"type": "integer", "description": "要认领的 ticket issue number。"},
            "login": {"type": "string", "description": "可选。指派给谁, 默认当前认证账号。"},
        },
        "required": ["repo", "ticket_number"],
    },
    func=wayfind_gh_claim,
)

master_tools.register(
    name="wayfind_gh_resolve",
    description=(
        "解决一张已认领的 ticket: 评论+关闭 issue, 决策写进地图的 Decisions so far。"
        "必须先 wayfind_gh_claim 才能 resolve。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {"type": "integer", "description": "地图 issue number。"},
            "ticket_number": {"type": "integer", "description": "已认领的 ticket issue number。"},
            "resolution": {
                "type": "string",
                "description": "完整说明: 做了什么/为什么, 发进 issue 评论。",
            },
            "gist": {"type": "string", "description": "一句话结论。"},
        },
        "required": ["repo", "map_number", "ticket_number", "resolution", "gist"],
    },
    func=wayfind_gh_resolve,
)

master_tools.register(
    name="wayfind_gh_rule_out_of_scope",
    description="把一张 ticket 标记为不在本次范围内: 关闭并记入地图的 Out of scope。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {"type": "integer", "description": "地图 issue number。"},
            "ticket_number": {"type": "integer", "description": "要排除的 ticket issue number。"},
            "reason": {"type": "string", "description": "为什么排除。"},
        },
        "required": ["repo", "map_number", "ticket_number", "reason"],
    },
    func=wayfind_gh_rule_out_of_scope,
)

master_tools.register(
    name="wayfind_gh_add_fog",
    description="记一块还说不清楚的模糊地带, 之后用 wayfind_gh_graduate_fog 拆成具体 ticket。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {"type": "integer", "description": "地图 issue number。"},
            "patch": {"type": "string", "description": "模糊地带的描述。"},
        },
        "required": ["repo", "map_number", "patch"],
    },
    func=wayfind_gh_add_fog,
)

master_tools.register(
    name="wayfind_gh_graduate_fog",
    description="把一块模糊地带拆成具体 ticket (每个标题一张 task 类型 sub-issue)。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {"type": "integer", "description": "地图 issue number。"},
            "patch": {
                "type": "string",
                "description": "要拆解的 fog patch (须与 wayfind_gh_add_fog 一致)。",
            },
            "new_ticket_titles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "拆出的新 ticket 标题列表。",
            },
        },
        "required": ["repo", "map_number", "patch", "new_ticket_titles"],
    },
    func=wayfind_gh_graduate_fog,
)

master_tools.register(
    name="wayfind_gh_decisions",
    description="列出这张探路图目前已经写下的所有决策 (从地图 issue body 读)。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {"type": "integer", "description": "地图 issue number。"},
        },
        "required": ["repo", "map_number"],
    },
    func=wayfind_gh_decisions,
)

master_tools.register(
    name="wayfind_gh_complete",
    description="frontier 和 fog 都清空时关闭地图 issue; 没清空会说明还剩什么。",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库, 'owner/name'。"},
            "map_number": {"type": "integer", "description": "地图 issue number。"},
        },
        "required": ["repo", "map_number"],
    },
    func=wayfind_gh_complete,
)

# ================= 长程任务预算/进度治理 (GoalKernel 接主链) ===============
# goal_start 是唯一入口: 调用后当前 session 的每一轮起自动做预算追踪
# (server/coordinator_master.py 的 _default_long_task_factory 从
# server/goal_session_map 查到 goal_id 才生效); 不调这个的会话完全不受影响。
from server.goal_tools import goal_add_todo, goal_start, goal_status  # noqa: E402

master_tools.register(
    name="goal_start",
    description=(
        "开一个长程任务目标: 之后本 session 每一轮自动做预算追踪, 超支时那一轮"
        "直接暂停不再调用 LLM。适合会连续跑很多轮、需要控制花费的任务; 普通"
        "对话不用调这个, 调了也不影响其他没开目标的会话。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "目标标题。"},
            "budget_usd": {"type": "number", "description": "可选。预算上限 (美元), 默认 5。"},
        },
        "required": ["title"],
    },
    func=goal_start,
)

master_tools.register(
    name="goal_add_todo",
    description="给当前 session 关联的长程任务加一个 todo (下一轮的进度提示会指向它)。",
    parameters={
        "type": "object",
        "properties": {
            "todo_id": {"type": "string", "description": "todo 的短 id (自己起, 后续引用用它)。"},
            "title": {"type": "string", "description": "todo 标题。"},
            "blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选。依赖的其它 todo_id 列表。",
            },
        },
        "required": ["todo_id", "title"],
    },
    func=goal_add_todo,
)

master_tools.register(
    name="goal_status",
    description=(
        "看当前 session 关联的长程任务进度 (todo/gate/预算)。可选传 todo_id+status "
        "顺便更新一个 todo 的状态 (如标记 done)。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "todo_id": {"type": "string", "description": "可选。要顺便更新状态的 todo id。"},
            "status": {"type": "string", "description": "可选。open/done/blocked/deferred。"},
            "note": {"type": "string", "description": "可选。更新说明。"},
        },
    },
    func=goal_status,
)

# ================= graph-engineer 式多引擎编排 (自纠正循环) ==============
from server.graph_engineer import graph_cycle  # noqa: E402

master_tools.register(
    name="system_graph_cycle",
    description=(
        "【触发条件】用户要求复杂功能多轮打磨/代码审查后再交付/重构/高风险任务 "
        "(auth/支付/删除/并发) 时优先考虑本工具; 一般简单任务直接回答或轻量工具即可。"
        "在计划的未完成 todo 上跑「实现→质量门→批判→仲裁→修复→验证」自纠正循环, "
        "不同引擎分离角色 — 实现引擎写代码, 批判引擎只读审查 (不让写的人自评)。"
        "完整机制 (graph-engineer 式): PRE-FLIGHT 安全检查 (workdir git clean/分支), "
        "QUALITY GATE 机械门 (quality_gate 命令, lint/type/build, 禁 mutating), "
        "DEBATE 三分类 (valid/debatable 反证/false-positive), VERIFY 功能验证 "
        "(verify_command 或验收判断, 失败分类根因回批判), Anti-loop cutoff 防振荡。"
        "参数: plan_id (create_plan 返回); implement_engine/critique_engine 可指定 "
        "(claude/codex/grok/pi, 默认 codex 实现 + claude 批判); max_iterations 默认 3; "
        "workdir (工作目录, PRE-FLIGHT 检查+命令执行); quality_gate (机械检查命令, "
        "check-only); verify_command (功能测试命令); preflight (默认 true)。"
        "适合: 复杂功能需要多轮打磨、质量要求高的任务。注意: 会调用外部引擎 CLI "
        "(可能产生订阅费用/耗时)。每步状态写入 plan_todo (看板可视化)。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "description": "计划 id (create_plan 返回)。"},
            "implement_engine": {
                "type": "string",
                "description": "可选。实现引擎 (claude/codex/grok/pi), 默认 codex。",
            },
            "critique_engine": {
                "type": "string",
                "description": "可选。批判引擎 (默认 claude, 与实现引擎不同更佳)。",
            },
            "max_iterations": {
                "type": "integer",
                "description": "可选。每 todo 修复轮次上限, 默认 3, 最大 5。",
            },
            "workdir": {
                "type": "string",
                "description": "可选。引擎工作目录 (PRE-FLIGHT 检查 + 质量门/验证命令执行目录)。",
            },
            "quality_gate": {
                "type": "string",
                "description": "可选。机械检查命令 (lint/type/build, 必须 check-only, 禁 mutating)。",
            },
            "verify_command": {
                "type": "string",
                "description": "可选。功能测试/验收命令 (与机械门分离, 失败分类根因回批判)。",
            },
            "preflight": {
                "type": "boolean",
                "description": "可选。PRE-FLIGHT 安全检查开关, 默认 true (git clean/分支警告)。",
            },
            "mode": {
                "type": "string",
                "description": "可选。full (从零实现, 默认) / refactor (已有代码重构, 不改变行为)。",
            },
            "elevated": {
                "type": "boolean",
                "description": "可选。Elevated assurance: None=auto (高风险任务如 auth/支付/删除/并发自动开), True=强制 3 lens+终局 challenger, False=关。",
            },
        },
        "required": ["plan_id"],
    },
    func=graph_cycle,
)


from server.graph_engineer import graph_review  # noqa: E402

master_tools.register(
    name="system_graph_review",
    description=(
        "【触发条件】用户要求审查/评估/检查现有实现、上线前复核、接手陌生代码时 "
        "优先考虑本工具 (比 graph_cycle 轻量, 只读不写)。"
        "Review-only 模式: 只读审查计划中的实现, 不写代码不修 bug, 输出审查报告 "
        "(功能/边界问题 + 安全隐患 + 建议) 写入 todo evidence, todo 标 reviewed。"
        "绝不调用实现引擎、绝不修改任何文件 (read-only 硬保证)。"
        "参数: plan_id (create_plan 返回); critique_engine 可选 (默认 claude); "
        "workdir 可选 (工作目录)。适合: 上线前审查、接手陌生代码的快速评估。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "string", "description": "计划 id (create_plan 返回)。"},
            "critique_engine": {
                "type": "string",
                "description": "可选。审查引擎 (claude/codex/grok/pi), 默认 claude。",
            },
            "workdir": {"type": "string", "description": "可选。工作目录 (审查上下文)。"},
        },
        "required": ["plan_id"],
    },
    func=graph_review,
)

# ================= 统一流水线 (Graft + OpenRSI + ReasoningBank 总装) =========
# evolve_solution: 沙盒验证式演化搜索替代单次盲写。装配层, 只组装 3O 主库能力。
from server import drawio_tool as _drawio_tool  # noqa: E402
from server import graft_autocontext as _graft_autocontext  # noqa: E402
from server import unified_pipeline as _unified_pipeline  # noqa: E402
from server import wechat_article_pipeline as _wechat_article_pipeline  # noqa: E402

_unified_pipeline.register(master_tools)
_graft_autocontext.register(master_tools)
_wechat_article_pipeline.register(master_tools)
_drawio_tool.register(master_tools)


def _register_wechat_discover(master_tools: Any) -> None:
    """公众号创作资源 discover 工具 (3O oskill.wechat_resources 装配).

    让主脑面对主题/布局/prompt 不确定时先 discover 再调用
    produce_wechat_article (md2wechat do-not-guess 原则)。
    oskill 未挂载时注册一个报错工具而非静默缺失。
    """
    if master_tools.has("wechat_discover"):
        return

    async def _wechat_discover(kind: str = "") -> str:
        try:
            from veya.platform import oskill as _load_oskill

            _load_oskill()
            from oskill.wechat_resources import register_wechat_resources

            catalog = register_wechat_resources()
            if kind:
                items = [
                    {"name": r.name, "description": r.description} for r in catalog.discover(kind)
                ]
                return f"{kind} 资源 ({len(items)}):\n" + "\n".join(
                    f"- {i['name']}: {i['description']}" for i in items
                )
            cap = catalog.capabilities()
            lines = [f"公众号创作能力目录 — 类型: {', '.join(cap['kinds'])}"]
            for k, count in sorted(cap["counts"].items()):
                lines.append(f"- {k}: {count} 个")
            lines.append(
                "用法: wechat_discover(kind='theme'|'layout'|'prompt'|'wechat-flow') "
                "查看具体资源; 主题/布局可配合 produce_wechat_article 的 requirements 使用。"
            )
            return "\n".join(lines)
        except Exception as exc:  # oskill 未挂载/导入失败 → 明确报错
            return f"wechat_discover: 3O 主库 oskill 不可用: {type(exc).__name__}: {exc}"

    master_tools.register(
        name="wechat_discover",
        description=(
            "公众号创作能力资源目录 (Discovery-First): 查询可用主题 theme / 布局模块 "
            "layout / 创作 prompt / 端到端流程 wechat-flow。USE THIS when the user "
            "wants a WeChat article and has not chosen a theme/layout/prompt, or asks "
            "what wechat capabilities exist — run this first instead of guessing. "
            "Pass kind to filter (theme|layout|prompt|wechat-flow), empty = overview."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["", "theme", "layout", "prompt", "wechat-flow"],
                    "description": "资源类型过滤, 空串返回能力总览",
                }
            },
        },
        func=_wechat_discover,
        max_result_chars=4000,
    )


_register_wechat_discover(master_tools)


# ── 内化能力 (2026-08-16): 决策账本 + 上下文图 ──────────────────────
# Semantica Decision Intelligence / Context Graph 轻量内化 (server/decision_ledger.py,
# server/context_graph.py)。工具形态暴露, 模型自主选择调用 — 零程序路由, 不裁藏。


def _register_internalized_tools(mt: Any) -> None:
    from server import context_graph as cg_mod
    from server import decision_ledger as dl_mod

    # ── decision_record: 把一次决策落成可审计记录 (一等公民) ──
    # ── ask_user: OpenMausBot 提问卡片内化 (bot 执行中向用户提问) ──
    async def _ask_user(question: str, options: list[str] | None = None) -> str:
        from server.user_control import ask_question

        return str(await ask_question(question, options))

    # ── agent_loop_run: omodul.AgentLoop 作为系统工具暴露 ──
    # 2026-08-17 架构澄清: MasterAgent ReAct 是唯一面向用户的主链 (全量工具面,
    # 模型自主判断, 冻结架构不改)；omodul.AgentLoop 不是第二条主链, 而是
    # MasterAgent 可选调用的一个工具 —— 需要"自己反复摸索多轮才能收敛"的隔离
    # 子任务时才用 (比如反复读代码+跑沙箱验证直到通过), 跑在自己的临时会话/
    # 工具面里, 完成后把结果文本带回主链, 不接管用户请求、不产生第二套持久
    # 会话历史。tool_group 由调用方 (MasterAgent, 全量视野) 决定给子任务开
    # 哪个专项能力组 — 裁剪的是子任务的执行边界, 不是主脑的认知面。
    async def _agent_loop_run(
        task: str,
        tool_group: str | None = None,
        max_rounds: int = 15,
        context_ref: str = "",
        acceptance: list[dict[str, Any]] | None = None,
        budget_usd: float | None = None,
        deadline: str | None = None,
    ) -> str:
        from runtime.execution.delegate_runtime import DelegateRuntime
        from runtime.execution.models import DelegateRequest, SpawnBudget
        from runtime.execution.spawn_guard import SpawnGuard
        from server.agent_loop_bridge import run_strict_chat

        depth = _delegation_depth_ctx.get()
        if tool_group and tool_group not in _GROUP_DESCRIPTIONS:
            raise ToolExecutionError(
                f"未知 tool_group '{tool_group}'。可用: {', '.join(sorted(_GROUP_DESCRIPTIONS))}"
            )
        sid = f"agent-loop-tool-{uuid.uuid4().hex}"
        depth_token = _delegation_depth_ctx.set(depth + 1)
        from server.events import current_task_id, event_store, fire_step

        parent_task_id = current_task_id()

        def _runtime_event(event: dict[str, Any]) -> None:
            with contextlib.suppress(Exception):
                event_store.append(
                    {
                        "topic": event.get("type", "delegate.updated"),
                        "task_id": parent_task_id,
                        "trace_id": parent_task_id or sid,
                        "actor": "master",
                        "payload": {
                            "child_session_id": sid,
                            **{key: value for key, value in event.items() if key != "type"},
                        },
                    }
                )
            # Mirror the same runtime fact into the active SSE stream.  The
            # canonical store remains the durable projection; this callback
            # only supplies live UI visibility.
            fire_step(event)

        guard = _spawn_guard_ctx.get()
        guard_token = None
        if guard is None:
            guard = SpawnGuard(SpawnBudget(), on_event=_runtime_event)
            guard_token = _spawn_guard_ctx.set(guard)
        if tool_group:
            _session_enabled_groups[sid] = {tool_group}
        try:
            child_task = task
            if acceptance:
                child_task += "\n\nAcceptance contract:\n" + "\n".join(
                    f"- {item.get('description') or item.get('id', '')}" for item in acceptance
                )
            if context_ref:
                child_task += f"\n\nContext reference: {context_ref}"
            request = DelegateRequest(
                delegate_id=sid,
                parent_task_id=parent_task_id or "master",
                parent_trace_id=parent_task_id or sid,
                objective=task,
                context_ref=context_ref or None,
                capability_scope=[tool_group] if tool_group else [],
                acceptance=cast("list[Any]", acceptance or []),
                depth=depth,
                estimated_tokens=max(1, min(int(max_rounds or 15), 40)) * 4096,
                budget_usd=budget_usd,
                timeout_s=5400,
                workspace=context_ref or ".",
            )

            async def _run_child(_cancel_event: asyncio.Event) -> dict[str, Any]:
                return await run_strict_chat(
                    child_task,
                    session_id=sid,
                    max_rounds=max(1, min(int(max_rounds or 15), 40)),
                    tool_schemas=mt.get_resident_schemas(session_id=sid),
                    tool_executor=mt.execute,
                    budget_usd=budget_usd,
                    deadline=deadline,
                )

            delegate_result = await DelegateRuntime(guard, on_event=_runtime_event).run(
                request, _run_child
            )
        finally:
            _session_enabled_groups.pop(sid, None)
            _delegation_depth_ctx.reset(depth_token)
            if guard_token is not None:
                _spawn_guard_ctx.reset(guard_token)

        # Keep deterministic acceptance as a result field.  The MasterAgent
        # remains the only component that decides how to explain it to the
        # user; a child never owns the final answer.
        if acceptance:
            from pathlib import Path

            from runtime.execution.models import AcceptanceResult
            from server.acceptance import evaluate_acceptance

            workspace = context_ref if Path(context_ref or ".").exists() else "."
            acceptance_for_eval = cast(
                "list[Any] | None",
                acceptance,
            )
            delegate_result.acceptance_results = [
                AcceptanceResult.from_value(item)
                for item in evaluate_acceptance(acceptance_for_eval, workspace=workspace)
            ]
        return json.dumps(delegate_result.to_dict(), ensure_ascii=False)

    async def _decision_record(
        category: str,
        scenario: str,
        reasoning: str = "",
        outcome: str = "",
        confidence: float = 0.0,
        parent_id: str | None = None,
    ) -> str:
        """记录一次决策, 返回决策 id。parent_id 建立因果链 (后续可 trace/impact)。"""
        did = dl_mod.ledger.record_decision(
            category,
            scenario,
            reasoning=reasoning,
            outcome=outcome,
            confidence=confidence,
            parent_id=parent_id,
            source="master_tool",
        )
        return f"decision recorded: {did}"

    # ── decision_query: 查先例 / 因果链 / 影响 / 策略门 / 摘要 ──
    async def _decision_query(
        action: str,
        decision_id: str = "",
        query: str = "",
        category: str | None = None,
        limit: int = 5,
    ) -> str:
        """查询决策账本: trace=因果链 / similar=先例检索 / impact=影响图 /
        rules=策略门检查 / export=审计导出 / summary=最近摘要。"""
        a = action.strip().lower()
        if a == "trace" and decision_id:
            return str(dl_mod.trace_decision_chain(decision_id, limit=limit))
        if a == "similar" and query:
            return str(dl_mod.find_similar_decisions(query, category=category, limit=limit))
        if a == "impact" and decision_id:
            return str(dl_mod.analyze_decision_impact(decision_id))
        if a == "rules":
            return str(dl_mod.check_decision_rules({}))
        if a == "export":
            return str(dl_mod.export_ledger(limit=limit or None))
        if a == "summary":
            return str(dl_mod.ledger.summary(limit=limit or 8))
        return "unknown action: use trace|similar|impact|rules|export|summary"

    # ── graph_store: 图写入 (实体/关系/软删) ──
    async def _graph_store(
        op: str,
        node_id: str = "",
        kind: str = "",
        name: str = "",
        rel: str = "",
        other_id: str = "",
        props: str = "",
    ) -> str:
        """写上下文图: op=upsert_node(建/更新实体) | add_edge(加关系边) |
        remove_node(软删)。props 为可选 JSON 字符串。"""
        import json as _json

        p: dict = {}
        if props:
            try:
                p = _json.loads(props)
            except _json.JSONDecodeError:
                return f"props 不是合法 JSON: {props[:100]}"
        o = op.strip().lower()
        if o == "upsert_node":
            if not (node_id and kind and name):
                return "upsert_node 需要 node_id/kind/name"
            cg_mod.graph.upsert_node(node_id, kind, name, p)
            return f"node upserted: {node_id} ({kind} {name})"
        if o == "add_edge":
            if not (node_id and rel and other_id):
                return "add_edge 需要 node_id/rel/other_id"
            cg_mod.graph.add_edge(node_id, rel, other_id, p)
            return f"edge added: {node_id} -[{rel}]-> {other_id}"
        if o == "remove_node":
            if not node_id:
                return "remove_node 需要 node_id"
            cg_mod.graph.remove_node(node_id)
            return f"node removed (soft): {node_id}"
        return "unknown op: use upsert_node|add_edge|remove_node"

    # ── graph_query: 图读取 (邻居遍历/时点快照/摘要) ──
    async def _graph_query(
        op: str,
        node_id: str = "",
        hops: int = 1,
        timestamp: str = "",
        limit: int = 8,
    ) -> str:
        """读上下文图: neighbors=图遍历 (hops 跳子图) / state_at=时点快照 /
        summary=图摘要。回答「什么相连、为什么、怎么连」类问题用 neighbors。"""
        o = op.strip().lower()
        if o == "neighbors":
            if not node_id:
                return "neighbors 需要 node_id"
            return str(cg_mod.neighbors(node_id, hops=hops))
        if o == "state_at":
            if not timestamp:
                return "state_at 需要 ISO 时间戳"
            return str(cg_mod.state_at(timestamp))
        if o == "summary":
            return str(cg_mod.summary(limit=limit))
        return "unknown op: use neighbors|state_at|summary"

    for spec in (
        dict(
            name="ask_user",
            description=(
                "执行中需要用户输入/选择时提问 (OpenMausBot 提问卡片内化): 卡片在聊天里"
                "弹出, 用户文字回答后回填给模型。适合歧义/二选一/需要用户拍板的场景 —"
                "不要用它问可自行搜索确认的事。带 options 时用户可点选或自定义。"
                "用户不答会收到明确提示, 用合理默认假设继续。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问的问题 (≤2000 字)"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选, 给用户的候选项 (最多 6 个)",
                    },
                },
                "required": ["question"],
            },
            func=_ask_user,
            max_result_chars=1000,
        ),
        dict(
            name="decision_record",
            description=(
                "记录一次决策为可审计账本条目 (一等公民): 类别/场景/推理/结果/置信度 + "
                "因果 parent。适合在完成重要判断、审批、派工后调用 — 之后可用 "
                "decision_query 追踪因果链、找先例、分析影响。只记录不判断。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "决策类别, 如 project_task/vendor/approve",
                    },
                    "scenario": {"type": "string", "description": "场景/请求描述"},
                    "reasoning": {"type": "string", "description": "推理依据"},
                    "outcome": {
                        "type": "string",
                        "description": "结果, 如 completed/blocked/approved",
                    },
                    "confidence": {"type": "number", "description": "置信度 0-1"},
                    "parent_id": {"type": "string", "description": "可选, 因果父决策 id"},
                },
                "required": ["category", "scenario"],
            },
            func=_decision_record,
            max_result_chars=500,
        ),
        dict(
            name="decision_query",
            description=(
                "查询决策账本: trace=沿因果链上溯到根, similar=语义先例检索 "
                "(给 query), impact=某决策影响的下游统计, rules=低置信度策略门, "
                "export=审计导出, summary=最近记录摘要。审计/复盘/找先例时用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["trace", "similar", "impact", "rules", "export", "summary"],
                    },
                    "decision_id": {"type": "string"},
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["action"],
            },
            func=_decision_query,
            max_result_chars=6000,
            side_effect=SideEffect.PURE_READ,
        ),
        dict(
            name="graph_store",
            description=(
                "写上下文图 (实体-关系记忆): upsert_node 建/更新实体, add_edge 加关系边, "
                "remove_node 软删。与 graph_query 配合, 把重要实体/关系沉淀为可遍历记忆。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["upsert_node", "add_edge", "remove_node"]},
                    "node_id": {"type": "string"},
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "rel": {"type": "string"},
                    "other_id": {"type": "string"},
                    "props": {"type": "string"},
                },
                "required": ["op"],
            },
            func=_graph_store,
            max_result_chars=500,
        ),
        dict(
            name="graph_query",
            description=(
                "读上下文图: neighbors=从某实体出发 hops 跳遍历子图 (回答什么相连/为什么/怎么连), "
                "state_at=某时刻图快照, summary=图概览。实体间连接类问题优先于向量检索。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["neighbors", "state_at", "summary"]},
                    "node_id": {"type": "string"},
                    "hops": {"type": "integer"},
                    "timestamp": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["op"],
            },
            func=_graph_query,
            max_result_chars=8000,
            side_effect=SideEffect.PURE_READ,
        ),
        dict(
            name="agent_loop_run",
            description=(
                "委托 omodul.AgentLoop 在隔离会话/工具面里执行一个结构化子任务, 完成后把"
                "结果文本带回来 (不会暂停等你确认, 也不会污染当前对话历史)。适合: 需要自己"
                "反复摸索多轮才能收敛的隔离子流程 (比如反复读代码+跑沙箱验证直到通过)。"
                "多数需求应优先直接调用具体工具, 或用 project_ask 派给 hicode; 只有需要给"
                "一个子任务单独开一段'自己摸索多轮'的隔离空间时才用这个。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "子任务目标, 自然语言描述, 写清楚验收标准。",
                    },
                    "tool_group": {
                        "type": "string",
                        "enum": sorted(_GROUP_DESCRIPTIONS),
                        "description": (
                            "子任务隔离工具面里额外解锁的专项组 (可选)。不传则子任务只有"
                            "意图理解/派工/监督/审查这几个常驻工具。"
                        ),
                    },
                    "max_rounds": {
                        "type": "integer",
                        "description": "子任务最多轮次, 默认 15, 上限 40。",
                    },
                    "context_ref": {
                        "type": "string",
                        "description": "可选的共享上下文/工作区引用，不会把子任务历史写入父会话。",
                    },
                    "acceptance": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "可选验收契约；确定性条件在子任务返回时评估。",
                    },
                    "budget_usd": {"type": "number", "minimum": 0},
                    "deadline": {
                        "type": "string",
                        "description": "可选 ISO-8601 deadline；超时会终止子任务。",
                    },
                },
                "required": ["task"],
            },
            func=_agent_loop_run,
            max_result_chars=8000,
        ),
    ):
        if not mt.has(spec["name"]):
            mt.register(
                name=spec["name"],
                description=spec["description"],
                parameters=spec["parameters"],
                func=spec["func"],
                max_result_chars=spec.get("max_result_chars", 4000),
                side_effect=spec.get("side_effect"),
            )


_register_internalized_tools(master_tools)

# ================= VAOM 只读查询: harness 历史表现 + 项目经验教训 ==========
# P5 落地(PR-25 最小可行版本, 见 docs/dev/rfc-01-vaom.md)。2026-08-23 用户已
# 按 ARCHITECTURE_STABLE.md §4 明确批准。纯只读、无副作用, 数据来自 P2/P3 已
# 经在跑的旁路记录(server/capability_model.py::performance_store,
# server/memory_controller.py::memory_controller)——见 server/vaom_query_tools.py。
from server.vaom_query_tools import (  # noqa: E402
    harness_performance_query,
    memory_recall_project_lessons,
)

master_tools.register(
    name="harness_performance_query",
    description=(
        "查某个执行者(hicode/dsh/builtin)在历史任务里的真实表现(成功率/样本量)。"
        "不传 harness_id 时返回全部已知执行者的对比。数据来自 goal_run 任务验收结果的"
        "真实累积, 不是宣传/文档——样本量很小或没有数据时会如实说明, 不要当作决定性证据。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "harness_id": {"type": "string", "description": "可选。hicode | dsh | builtin。"},
            "task_archetype": {
                "type": "string",
                "description": "可选。目前粒度很粗, 只有一个桶 'goal_run_task'。",
            },
        },
    },
    func=harness_performance_query,
)

master_tools.register(
    name="memory_recall_project_lessons",
    description=(
        "召回过往任务执行中积累的经验教训(比如'先跑迁移脚本再动代码'这类被反复验证过"
        "的做法)。是关键词匹配, 不是语义检索——查不到不代表不存在, 只是措辞没对上。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "可选。关键词。"},
            "scope": {
                "type": "string",
                "description": "可选。user | project | repo | global。",
            },
        },
    },
    func=memory_recall_project_lessons,
)


# ============ tool_search: 主脑瘦身模式下的按需能力发现 ============
def _search_tokens(text: str) -> set[str]:
    """分词: 拉丁词 (≥2 字符) + CJK 二元组 (相邻双字), 覆盖中英混排的工具描述。

    工具名/描述以中文为主, 纯 [a-z0-9_] 分词会丢掉全部中文 → 中文 query 排不出来;
    对 CJK 取滑动二元组 (如 '沙箱'→{沙箱}), 比单字更准, 又不必引分词库。
    """
    text = text.lower()
    tokens = {t for t in re.findall(r"[a-z0-9_]+", text) if len(t) > 1}
    cjk = re.findall(r"[一-鿿]+", text)
    for run in cjk:
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _tool_search(query: str, top_k: int = 5) -> str:
    """按模型显式查询返回工具 schema，不修改主链可见工具面。"""
    top_k = max(1, min(int(top_k or 5), 20))
    q = (query or "").lower().strip()
    q_tokens = _search_tokens(q)
    scored: list[tuple[int, str, dict]] = []
    for schema in master_tools._schemas:
        fn = schema["function"]
        name = fn["name"]
        if name == "tool_search":
            continue
        hay = f"{name} {fn.get('description', '')}".lower()
        hay_tokens = _search_tokens(hay)
        score = 0
        if q and q in hay:
            score += 5
        score += len(q_tokens & hay_tokens)
        scored.append((score, name, fn))
    scored.sort(key=lambda x: (-x[0], x[1]))
    hits = [row for row in scored if row[0] > 0][:top_k]
    if not hits:  # 无命中也给前几个, 避免模型空手
        hits = scored[:top_k]
    names = [row[1] for row in hits]
    return json.dumps(
        {
            "unlocked": [],
            "already_available": names,
            "tools": [row[2] for row in hits],
            "note": "主链始终暴露全量工具；本结果仅提供显式检索到的 schema。",
        },
        ensure_ascii=False,
    )


# ── Personal Runtime Memory capabilities ───────────────────────────────
# These callbacks remain model-invoked capabilities.  They do not pre-search
# or inject memory into the MasterAgent context.
from runtime.personal import PersonalRuntimeError, get_personal_runtime  # noqa: E402


def _personal_request_ids() -> tuple[str, str | None, str | None, str | None]:
    from server import auth as auth_mod
    from server.events import current_event_context

    user_id = str(auth_mod.current_user().get("user_id") or "anonymous")
    context = current_event_context()
    return user_id, context.get("session_id"), context.get("task_id"), context.get("trace_id")


def _personal_user_visible(record: dict[str, Any], user_id: str) -> bool:
    """User-scoped personal records never cross the authenticated boundary."""
    return record.get("scope_type") != "user" or str(record.get("scope_id")) == str(user_id)


async def _tool_memory_search(
    query: str = "",
    *,
    scope: str | None = None,
    scope_id: str | None = None,
    memory_type: str | None = None,
    limit: int = 20,
    min_confidence: float = 0.0,
    include_superseded: bool = False,
) -> dict:
    """搜索带来源、置信度和生命周期的 Personal MemoryRecord v2。"""
    user_id, _, _, _ = _personal_request_ids()
    if scope in {None, "global", "user"}:
        scope_id = user_id
        scope = "user"
    if scope == "project" and not scope_id:
        scope_id = os.environ.get("VEYA_WORKSPACE", "default")
    scope_type = {
        "global": "user",
        "user": "user",
        "project": "workspace",
        "workspace": "workspace",
        "session": "session",
    }.get(scope or "", scope)
    results = await get_personal_runtime().search_memory(
        query,
        scope_type=scope_type,
        scope_id=scope_id,
        memory_type=memory_type,
        limit=limit,
        min_confidence=min_confidence,
        include_superseded=include_superseded,
    )
    return {"results": results, "count": len(results), "authority": "execution-runtime"}


async def _tool_memory_write(
    content: str,
    *,
    type: str = "semantic",
    scope: str = "project",
    entities: list[str] | None = None,
    keywords: list[str] | None = None,
    provenance: str = "",
    trust_level: str = "unknown",
    scope_type: str | None = None,
    scope_id: str | None = None,
    memory_type: str | None = None,
    source_event_ids: list[str] | None = None,
    source_session_ids: list[str] | None = None,
    source_task_ids: list[str] | None = None,
    tags: list[str] | None = None,
    confidence: float | None = None,
    commit: bool = False,
) -> dict:
    """创建候选记忆；只有显式 commit 才成为 active fact。"""
    user_id, session_id, task_id, trace_id = _personal_request_ids()
    canonical_scope = scope_type or {
        "global": "user",
        "user": "user",
        "project": "workspace",
        "workspace": "workspace",
        "session": "session",
    }.get(scope, "workspace")
    if canonical_scope == "user":
        canonical_id = user_id
    elif canonical_scope == "session":
        canonical_id = scope_id or session_id or "anonymous-session"
    else:
        canonical_id = scope_id or os.environ.get("VEYA_WORKSPACE", "default")
    event = await get_personal_runtime().record_event(
        "memory.explicit_write",
        {
            "content": content,
            "memory_type": memory_type or type,
            "tags": tags or [],
            "trust_level": trust_level,
        },
        trace_id=trace_id,
        session_id=session_id,
        task_id=task_id,
        workspace_id=canonical_id if canonical_scope == "workspace" else None,
    )
    candidate = await get_personal_runtime().create_memory_candidate(
        content,
        scope_type=canonical_scope,
        scope_id=canonical_id,
        memory_type=memory_type or type,
        source_event_ids=[*(source_event_ids or []), event["id"]],
        source_session_ids=source_session_ids or ([session_id] if session_id else []),
        source_task_ids=source_task_ids or ([task_id] if task_id else []),
        confidence=confidence if confidence is not None else 0.8,
        reason=provenance or "MasterAgent explicit capability call",
        provenance={
            "actor": user_id,
            "entrypoint": "memory_write",
            "entities": entities or [],
            "keywords": keywords or [],
        },
        trace_id=trace_id,
    )
    if not commit:
        return {"status": "candidate", "candidate": candidate}
    return {
        "status": "committed",
        "candidate": candidate,
        "commit": await get_personal_runtime().commit_memory_candidate(
            candidate["id"], trace_id=trace_id
        ),
    }


async def _tool_memory_correct(
    memory_id: str, content: str, *, provenance: str = "user correction"
) -> dict:
    """保留旧记录并创建 active replacement。"""
    user_id, session_id, task_id, trace_id = _personal_request_ids()
    try:
        record = await get_personal_runtime().get_memory(memory_id)
        if record is None or not _personal_user_visible(record, user_id):
            return {"status": "not_found", "memory_id": memory_id}
        return await get_personal_runtime().correct_memory(
            memory_id,
            content,
            source_session_ids=[session_id] if session_id else [],
            source_task_ids=[task_id] if task_id else [],
            reason=provenance,
            trace_id=trace_id,
        )
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc), "actor": user_id}


async def _tool_memory_supersede(
    memory_id: str, content: str, *, provenance: str = "explicit supersede"
) -> dict:
    return await _tool_memory_correct(memory_id, content, provenance=provenance)


async def _tool_memory_forget(memory_id: str) -> dict:
    try:
        user_id, _, _, _ = _personal_request_ids()
        record = await get_personal_runtime().get_memory(memory_id)
        if record is None or not _personal_user_visible(record, user_id):
            return {"status": "not_found", "memory_id": memory_id}
        return await get_personal_runtime().forget_memory(memory_id)
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


async def _tool_memory_get(memory_id: str) -> dict:
    user_id, _, _, _ = _personal_request_ids()
    record = await get_personal_runtime().get_memory(memory_id, include_sources=True)
    if record and not _personal_user_visible(record, user_id):
        record = None
    return (
        {"status": "ok", "record": record}
        if record
        else {"status": "not_found", "memory_id": memory_id}
    )


async def _tool_memory_explain(memory_id: str) -> dict:
    user_id, _, _, _ = _personal_request_ids()
    record = await get_personal_runtime().get_memory(memory_id)
    if record is None or not _personal_user_visible(record, user_id):
        return {"status": "not_found", "memory_id": memory_id}
    source = await get_personal_runtime().show_memory_source(memory_id)
    return {"status": "ok", **source} if source else {"status": "not_found", "memory_id": memory_id}


if not master_tools.has("memory_search"):
    master_tools.register(
        name="memory_search",
        description=(
            "搜索长期记忆库。关键词过滤 content / entities / keywords, 可选按 scope 过滤。"
            "返回匹配的 MemoryRecord 列表。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词(内容/实体/关键词)"},
                "scope": {
                    "type": "string",
                    "description": "可选: 仅搜指定 scope(project/global等)",
                },
                "scope_id": {
                    "type": "string",
                    "description": "可选 user/workspace/session scope id",
                },
                "memory_type": {
                    "type": "string",
                    "description": "episodic|semantic|procedural|preference|decision",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "include_superseded": {
                    "type": "boolean",
                    "description": "审计查询时包含 superseded/forgotten",
                },
            },
            "required": ["query"],
        },
        func=_tool_memory_search,
        side_effect=SideEffect.PURE_READ,
    )

if not master_tools.has("memory_write"):
    master_tools.register(
        name="memory_write",
        description=(
            "创建带 provenance 的 MemoryCandidate；默认不会成为 active fact，显式 commit=true 才提交。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆内容"},
                "type": {
                    "type": "string",
                    "description": "类型: working|episodic|semantic|procedural, 默认 semantic",
                },
                "scope": {
                    "type": "string",
                    "description": "作用域: project|global|user, 默认 project",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "实体标签",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词标签",
                },
                "provenance": {"type": "string", "description": "来源说明"},
                "trust_level": {
                    "type": "string",
                    "description": "信任等级: unknown|L1|L2_verified|L3_cross_checked, 默认 unknown",
                },
                "scope_type": {
                    "type": "string",
                    "description": "canonical scope: user|workspace|session",
                },
                "scope_id": {"type": "string", "description": "canonical scope id"},
                "memory_type": {
                    "type": "string",
                    "description": "episodic|semantic|procedural|preference|decision",
                },
                "source_event_ids": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source_session_ids": {"type": "array", "items": {"type": "string"}},
                "source_task_ids": {"type": "array", "items": {"type": "string"}},
                "commit": {
                    "type": "boolean",
                    "description": "显式确认后提交为 active fact，默认 false",
                },
            },
            "required": ["content"],
        },
        func=_tool_memory_write,
        side_effect=SideEffect.LOCAL_WRITE,
    )

if not master_tools.has("memory_correct"):
    master_tools.register(
        name="memory_correct",
        description=("修正一条长期记忆：旧记录保留为 superseded，新 replacement 成为 active。"),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "要修正的记忆 ID"},
                "content": {"type": "string", "description": "新内容"},
                "provenance": {"type": "string", "description": "修正来源说明"},
            },
            "required": ["memory_id", "content"],
        },
        func=_tool_memory_correct,
        side_effect=SideEffect.LOCAL_WRITE,
    )

if not master_tools.has("memory_supersede"):
    master_tools.register(
        name="memory_supersede",
        description=(
            "supersede 一条记忆：旧记录保留，新 replacement 成为 active。"
            "返回 old_id / new_id / status。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "要 supersede 的记忆 ID"},
                "content": {"type": "string", "description": "新记忆内容"},
                "provenance": {"type": "string", "description": "supersede 来源说明"},
            },
            "required": ["memory_id", "content"],
        },
        func=_tool_memory_supersede,
        side_effect=SideEffect.LOCAL_WRITE,
    )

if not master_tools.has("memory_forget"):
    master_tools.register(
        name="memory_forget",
        description=("将一条记忆软删除为 forgotten（保留审计记录，不再被正常检索返回）。"),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "要删除的记忆 ID"},
            },
            "required": ["memory_id"],
        },
        func=_tool_memory_forget,
        side_effect=SideEffect.LOCAL_WRITE,
    )

if not master_tools.has("memory_get"):
    master_tools.register(
        name="memory_get",
        description="获取单条记忆的完整详情。",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆 ID"},
            },
            "required": ["memory_id"],
        },
        func=_tool_memory_get,
        side_effect=SideEffect.PURE_READ,
    )

if not master_tools.has("memory_explain"):
    master_tools.register(
        name="memory_explain",
        description="解释一条记忆的来源: provenance / source_episode_ids / source_artifact_ids / trust_level。",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆 ID"},
            },
            "required": ["memory_id"],
        },
        func=_tool_memory_explain,
        side_effect=SideEffect.PURE_READ,
    )

if not master_tools.has("memory_show_source"):
    master_tools.register(
        name="memory_show_source",
        description="查看 MemoryRecord 的来源事件、provenance 和缺失来源。",
        parameters={
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
        func=_tool_memory_explain,
        side_effect=SideEffect.PURE_READ,
    )


# ── Personal Runtime Skill/Learning capabilities ────────────────────────
async def _tool_skill_search(
    query: str = "",
    *,
    scope_type: str | None = None,
    scope_id: str | None = None,
    include_candidates: bool = False,
    limit: int = 20,
) -> dict:
    user_id, _, _, _ = _personal_request_ids()
    if scope_type in {None, "user"}:
        scope_type = "user"
        scope_id = user_id
    return {
        "skills": await get_personal_runtime().search_skills(
            query,
            scope_type=scope_type,
            scope_id=scope_id,
            include_candidates=include_candidates,
            limit=limit,
        )
    }


async def _tool_skill_show(skill_id: str) -> dict:
    user_id, _, _, _ = _personal_request_ids()
    value = await get_personal_runtime().get_skill(skill_id, versions=True)
    if value and not _personal_user_visible(value, user_id):
        value = None
    return (
        {"status": "ok", "skill": value} if value else {"status": "not_found", "skill_id": skill_id}
    )


async def _tool_skill_create(
    name: str,
    description: str,
    *,
    scope_type: str = "user",
    scope_id: str | None = None,
    trigger_examples: list[str] | None = None,
    parameters_schema: dict[str, Any] | None = None,
    execution_type: str = "prompt",
    execution_ref: str = "",
) -> dict:
    user_id, session_id, task_id, trace_id = _personal_request_ids()
    sid = (
        user_id if scope_type == "user" else scope_id or os.environ.get("VEYA_WORKSPACE", "default")
    )
    event = await get_personal_runtime().record_event(
        "skill.teaching_instruction",
        {"name": name, "description": description},
        trace_id=trace_id,
        session_id=session_id,
        task_id=task_id,
        workspace_id=sid if scope_type == "workspace" else None,
    )
    return await get_personal_runtime().create_skill_candidate(
        name,
        description,
        scope_type=scope_type,
        scope_id=sid,
        trigger_examples=trigger_examples or [],
        parameters_schema=parameters_schema,
        execution_type=execution_type,
        execution_ref=execution_ref,
        source_event_ids=[event["id"]],
        source_task_ids=[task_id] if task_id else [],
        created_by=user_id,
        trace_id=trace_id,
    )


async def _tool_skill_confirm(skill_version_id: str) -> dict:
    try:
        user_id, _, _, _ = _personal_request_ids()
        version = await get_personal_runtime().get_skill_version(skill_version_id)
        if version is None or not _personal_user_visible(version, user_id):
            return {"status": "not_found", "skill_version_id": skill_version_id}
        return await get_personal_runtime().confirm_skill(skill_version_id)
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


async def _tool_skill_run(skill_id: str, params: dict[str, Any] | None = None) -> dict:
    user_id, _, task_id, trace_id = _personal_request_ids()
    try:
        skill = await get_personal_runtime().get_skill(skill_id)
        if skill is None or not _personal_user_visible(skill, user_id):
            return {"status": "not_found", "skill_id": skill_id}
        return await get_personal_runtime().run_skill(
            skill_id, params, task_id=task_id, trace_id=trace_id
        )
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


async def _tool_skill_update(
    skill_id: str,
    description: str,
    *,
    trigger_examples: list[str] | None = None,
    execution_type: str = "prompt",
    execution_ref: str = "",
) -> dict:
    user_id, _, _, _ = _personal_request_ids()
    skill = await get_personal_runtime().get_skill(skill_id)
    if skill is None:
        return {"status": "not_found", "skill_id": skill_id}
    if not _personal_user_visible(skill, user_id):
        return {"status": "not_found", "skill_id": skill_id}
    version = skill["versions"][0] if skill.get("versions") else {}
    return await get_personal_runtime().create_skill_candidate(
        skill.get("name", skill_id),
        description,
        scope_type=skill.get("scope_type", "user"),
        scope_id=skill.get("scope_id", "anonymous"),
        trigger_examples=trigger_examples or version.get("trigger_examples", []),
        parameters_schema=version.get("parameters_schema", {"type": "object", "properties": {}}),
        execution_type=execution_type,
        execution_ref=execution_ref,
        source_task_ids=[],
        created_by="user",
        parent_version=int(skill.get("current_version") or version.get("version") or 1),
    )


async def _tool_skill_rollback(skill_id: str, version: int) -> dict:
    try:
        user_id, _, _, _ = _personal_request_ids()
        skill = await get_personal_runtime().get_skill(skill_id)
        if skill is None or not _personal_user_visible(skill, user_id):
            return {"status": "not_found", "skill_id": skill_id}
        return await get_personal_runtime().rollback_skill(skill_id, version)
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


async def _tool_skill_deprecate(skill_id: str) -> dict:
    try:
        user_id, _, _, _ = _personal_request_ids()
        skill = await get_personal_runtime().get_skill(skill_id)
        if skill is None or not _personal_user_visible(skill, user_id):
            return {"status": "not_found", "skill_id": skill_id}
        return await get_personal_runtime().deprecate_skill(skill_id)
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


async def _tool_learning_candidate(
    pattern_id: str,
    observation: str,
    hypothesis: str,
    evidence_task_ids: list[str],
    *,
    evidence_trajectory_ids: list[str] | None = None,
    scope: str = "default",
    candidate_type: str = "policy_advisory",
    proposed_change: dict[str, Any] | None = None,
    confidence: float = 0.5,
    explicit_teaching: bool = False,
) -> dict:
    try:
        user_id, _, _, trace_id = _personal_request_ids()
        if scope in {"", "default", "user"}:
            scope = f"user:{user_id}"
        return await get_personal_runtime().create_learning_candidate(
            pattern_id=pattern_id,
            scope=scope,
            evidence_task_ids=evidence_task_ids,
            evidence_trajectory_ids=evidence_trajectory_ids or [],
            observation=observation,
            hypothesis=hypothesis,
            confidence=confidence,
            candidate_type=candidate_type,
            proposed_change=proposed_change or {},
            explicit_teaching=explicit_teaching,
            trace_id=trace_id,
        )
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


async def _tool_learning_eval(
    learning_id: str,
    baseline_ref: str,
    candidate_ref: str,
    result: dict[str, Any],
    passed: bool,
) -> dict:
    try:
        return await get_personal_runtime().record_learning_eval(
            learning_id,
            baseline_ref=baseline_ref,
            candidate_ref=candidate_ref,
            result=result,
            passed=passed,
        )
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


async def _tool_learning_rollback(learning_id: str, reason: str = "regression detected") -> dict:
    try:
        return await get_personal_runtime().rollback_learning(learning_id, reason=reason)
    except PersonalRuntimeError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}


if not master_tools.has("skill_search"):
    master_tools.register(
        "skill_search",
        "搜索带版本、信任和成功率的 Personal Skill。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "scope_type": {"type": "string"},
                "scope_id": {"type": "string"},
                "include_candidates": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        _tool_skill_search,
        side_effect=SideEffect.PURE_READ,
    )
if not master_tools.has("skill_show"):
    master_tools.register(
        "skill_show",
        "查看 Skill 的当前版本、来源、安全状态和版本历史。",
        {
            "type": "object",
            "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"],
        },
        _tool_skill_show,
        side_effect=SideEffect.PURE_READ,
    )
if not master_tools.has("skill_create"):
    master_tools.register(
        "skill_create",
        "创建 SkillCandidate 草案；不会静默激活可执行 Skill，需显式确认。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "scope_type": {"type": "string"},
                "scope_id": {"type": "string"},
                "trigger_examples": {"type": "array", "items": {"type": "string"}},
                "parameters_schema": {"type": "object"},
                "execution_type": {"type": "string"},
                "execution_ref": {"type": "string"},
            },
            "required": ["name", "description"],
        },
        _tool_skill_create,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("skill_confirm"):
    master_tools.register(
        "skill_confirm",
        "确认一个 SkillCandidate，执行静态安全门禁后激活版本。",
        {
            "type": "object",
            "properties": {"skill_version_id": {"type": "string"}},
            "required": ["skill_version_id"],
        },
        _tool_skill_confirm,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("skill_run"):
    master_tools.register(
        "skill_run",
        "执行当前 active/trusted Skill，并记录版本、结果、验收、产物和证据。",
        {
            "type": "object",
            "properties": {"skill_id": {"type": "string"}, "params": {"type": "object"}},
            "required": ["skill_id"],
        },
        _tool_skill_run,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("learning_rollback"):
    master_tools.register(
        "learning_rollback",
        "回滚已应用但出现回归的 Learning 记录；保留评估证据，不自动修改 Skill 或提示词。",
        {
            "type": "object",
            "properties": {
                "learning_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["learning_id"],
        },
        _tool_learning_rollback,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("skill_update"):
    master_tools.register(
        "skill_update",
        "创建 Skill 的新候选版本；不自动替换当前 active 版本。",
        {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "description": {"type": "string"},
                "trigger_examples": {"type": "array", "items": {"type": "string"}},
                "execution_type": {"type": "string"},
                "execution_ref": {"type": "string"},
            },
            "required": ["skill_id", "description"],
        },
        _tool_skill_update,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("skill_rollback"):
    master_tools.register(
        "skill_rollback",
        "将 Skill 回滚到指定的已审计版本。",
        {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
            },
            "required": ["skill_id", "version"],
        },
        _tool_skill_rollback,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("skill_deprecate"):
    master_tools.register(
        "skill_deprecate",
        "停用 Skill 但保留全部版本和运行审计。",
        {
            "type": "object",
            "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"],
        },
        _tool_skill_deprecate,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("skill_delete"):
    master_tools.register(
        "skill_delete",
        "将 Skill 标记 deprecated 以保留审计；不物理删除历史版本。",
        {
            "type": "object",
            "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"],
        },
        _tool_skill_deprecate,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("learning_candidate_create"):
    master_tools.register(
        "learning_candidate_create",
        "从至少三次独立任务或明确教学创建 LearningCandidate，不直接修改行为。",
        {
            "type": "object",
            "properties": {
                "pattern_id": {"type": "string"},
                "observation": {"type": "string"},
                "hypothesis": {"type": "string"},
                "evidence_task_ids": {"type": "array", "items": {"type": "string"}},
                "evidence_trajectory_ids": {"type": "array", "items": {"type": "string"}},
                "scope": {"type": "string"},
                "candidate_type": {"type": "string"},
                "proposed_change": {"type": "object"},
                "confidence": {"type": "number"},
                "explicit_teaching": {"type": "boolean"},
            },
            "required": ["pattern_id", "observation", "hypothesis", "evidence_task_ids"],
        },
        _tool_learning_candidate,
        side_effect=SideEffect.LOCAL_WRITE,
    )
if not master_tools.has("learning_eval"):
    master_tools.register(
        "learning_eval",
        "记录 baseline/candidate 离线或 replay eval；只有通过才可进入 validated gate。",
        {
            "type": "object",
            "properties": {
                "learning_id": {"type": "string"},
                "baseline_ref": {"type": "string"},
                "candidate_ref": {"type": "string"},
                "result": {"type": "object"},
                "passed": {"type": "boolean"},
            },
            "required": ["learning_id", "baseline_ref", "candidate_ref", "result", "passed"],
        },
        _tool_learning_eval,
        side_effect=SideEffect.LOCAL_WRITE,
    )


if not master_tools.has("tool_search"):
    master_tools.register(
        name="tool_search",
        description=(
            "按意图检索并解锁工具。主脑常驻工具面已精简为极少数; 需要其它能力 (读写文件/"
            "跑沙箱/派工/回测/视觉/MCP…) 时, 先用它按自然语言意图搜索, 命中工具会被解锁, "
            "下一轮即可直接原生调用。system prompt 里的工具菜单列出了全部可搜的能力名。"
            "一次可搜多个关键词 (如 '运行代码 沙箱 验证'), top_k 控制返回数。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "自然语言意图或关键词, 用于匹配工具名与描述。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "最多返回/解锁的工具数, 默认 5, 上限 20。",
                },
            },
            "required": ["query"],
        },
        func=_tool_search,
        max_result_chars=8000,
    )


# PR-01..04 local coding product surface.  This is an additive capability
# layer: it does not route requests or replace the single MasterAgent path.
_register_coding_tools(master_tools)
