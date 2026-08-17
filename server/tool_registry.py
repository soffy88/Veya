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
import inspect
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.tool_guard import ToolDenied as _ToolDenied
from server.tool_guard import global_tool_guard as _tool_guard
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
    }
)

# 工具名 → 职能分组。未登记的名字兜底进 "other" (mcp_* 网关例外, 见 _group_for)。
_TOOL_GROUPS: dict[str, str] = {
    # code_exec: 直接读写代码/跑命令 — 多数场景应走 project_ask 派给 hicode,
    # 只有需要亲自核查/兜底时才启用。
    "write_file": "code_exec",
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
    "mcp_gateway": "兄弟服务网关 (mcp_hevi/mcp_stratum/mcp_od/mcp_codebase)。",
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
        configured_timeout = timeout_s
        if configured_timeout is None:
            configured_timeout = os.environ.get(_TOOL_TIMEOUT_ENV)
        self._default_timeout_s = parse_optional_timeout(
            configured_timeout, source=f"timeout_s/{_TOOL_TIMEOUT_ENV}"
        )
        self._tool_timeouts: dict[str, float | None] = {}
        # 并发安全工具集: 模块级白名单 + 注册时显式 opt-in。主库循环据此决定
        # 一批 tool_call 能否并发 (整批都在集内才并发, 否则顺序)。
        self._parallel_safe: set[str] = set(_PARALLEL_SAFE_TOOLS)

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
        """
        if not name or not callable(func):
            raise ValueError("register requires a non-empty tool name and a callable")
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

    def unregister(self, name: str) -> None:
        if name in self._functions:
            del self._functions[name]
            self._validators.pop(name, None)
            del self._descriptions[name]
            self._result_limits.pop(name, None)
            self._tool_timeouts.pop(name, None)
            self._parallel_safe.discard(name)
            self._schemas = [s for s in self._schemas if s["function"]["name"] != name]

    # ── 查询 ─────────────────────────────────────────────────────────
    def get_all_schemas(self) -> list[dict]:
        """暴露给大模型的全部认知描述 (Function Calling 协议)。"""
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
        _validate_arguments(name, kwargs, self._validators[name])
        # 统一守卫通道: 执行前过策略链 + 记决策轨迹 (缺省全放行, 零行为变化)。
        try:
            await _tool_guard.acheck(name, kwargs, source="master_tool")
        except _ToolDenied as denied:
            raise ToolExecutionError(str(denied)) from denied
        effective_timeout = parse_optional_timeout(
            timeout_s, source=f"Tool '{name}' execute timeout_s"
        )
        if timeout_s is None:
            effective_timeout = self._tool_timeouts.get(name, self._default_timeout_s)
        try:
            execution = _invoke_callback(func, kwargs)
            if effective_timeout is None:
                raw = await execution
            else:
                raw = await asyncio.wait_for(execution, timeout=effective_timeout)
            return _to_str(raw, limit=self._result_limits.get(name, 8000))
        except TimeoutError as exc:
            raise ToolExecutionError(
                f"tool '{name}' timed out after {effective_timeout:g}s"
            ) from exc
        except ToolExecutionError:
            raise
        except Exception as exc:
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
                            + r.text[:max_chars]
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
    return extract_skeleton(source, filepath)


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
    """在统一沙箱合同里执行代码（chat_verify = process 限额；网络并未阻断）。"""
    import sys

    from veya.platform import load

    if not code and not command:
        raise ToolExecutionError(
            "run_in_sandbox requires either 'code' (python source) or 'command' (shell)"
        )
    oprim = load("oprim")
    oskill = load("oskill")
    policy = oskill.isolation_policy("chat_verify")
    created = oprim.sandbox_create(
        isolation=policy["isolation"],
        block_network=False,
        memory="1g",
    )
    if not created.get("ok"):
        raise ToolExecutionError(created.get("error") or "sandbox_create failed")
    sid = created["sandbox_id"]
    try:
        if code:
            rec = oprim.sandbox_exec(sid, [sys.executable, "-c", code], timeout_s=30)
        else:
            rec = oprim.sandbox_exec(
                sid,
                ["bash", "-lc", _normalize_sandbox_command(command)],
                timeout_s=30,
            )
    finally:
        oprim.sandbox_destroy(sid)
    isolation = rec.get("isolation") or created.get("isolation")
    note = f"isolation={isolation} network_blocked={rec.get('block_network', False)}"
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
        return _schema(asset_id)
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
    return await coprocessor.execute_strategy(
        strategy_code=strategy_code,
        asset_id=asset_id,
        start_date=start_date,
        end_date=end_date,
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
)

master_tools.register(
    name="run_in_sandbox",
    description=(
        "Run python code (or a shell command) in the unified 3O sandbox (chat_verify=process: "
        "CPU/memory/time limits; network is NOT blocked). ONLY for executing/verifying snippets. "
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
from server import graft_autocontext as _graft_autocontext  # noqa: E402
from server import unified_pipeline as _unified_pipeline  # noqa: E402
from server import wechat_article_pipeline as _wechat_article_pipeline  # noqa: E402

_unified_pipeline.register(master_tools)
_graft_autocontext.register(master_tools)
_wechat_article_pipeline.register(master_tools)


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

        return await ask_question(question, options)

    # ── agent_loop_run: omodul.AgentLoop 作为系统工具暴露 ──
    # 2026-08-17 架构澄清: MasterAgent ReAct 是唯一面向用户的主链 (全量工具面,
    # 模型自主判断, 冻结架构不改)；omodul.AgentLoop 不是第二条主链, 而是
    # MasterAgent 可选调用的一个工具 —— 需要"自己反复摸索多轮才能收敛"的隔离
    # 子任务时才用 (比如反复读代码+跑沙箱验证直到通过), 跑在自己的临时会话/
    # 工具面里, 完成后把结果文本带回主链, 不接管用户请求、不产生第二套持久
    # 会话历史。tool_group 由调用方 (MasterAgent, 全量视野) 决定给子任务开
    # 哪个专项能力组 — 裁剪的是子任务的执行边界, 不是主脑的认知面。
    async def _agent_loop_run(
        task: str, tool_group: str | None = None, max_rounds: int = 15
    ) -> str:
        from server.agent_loop_bridge import run_strict_chat

        if tool_group and tool_group not in _GROUP_DESCRIPTIONS:
            raise ToolExecutionError(
                f"未知 tool_group '{tool_group}'。可用: {', '.join(sorted(_GROUP_DESCRIPTIONS))}"
            )
        sid = f"agent-loop-tool-{uuid.uuid4().hex}"
        if tool_group:
            _session_enabled_groups[sid] = {tool_group}
        try:
            result = await run_strict_chat(
                task,
                session_id=sid,
                max_rounds=max(1, min(int(max_rounds or 15), 40)),
                tool_schemas=mt.get_resident_schemas(session_id=sid),
                tool_executor=mt.execute,
            )
        finally:
            _session_enabled_groups.pop(sid, None)
        if result.get("status") != "success":
            raise ToolExecutionError(
                f"子任务未完成 (status={result.get('status')}): "
                f"{result.get('error') or result.get('final_answer') or ''}"
            )
        return result.get("final_answer") or "(子任务无输出)"

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
            return dl_mod.ledger.summary(limit=limit or 8)
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
            return cg_mod.summary(limit=limit)
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
            )


_register_internalized_tools(master_tools)
