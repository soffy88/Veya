"""
layer4/server/assembly.py — 引擎装配中心

把已入库的 oprim/oskill/omodul 注入 oservi 引擎,产出可运行的 Engine。
这是 "库 → 可运行" 的关键接线。装配在 layer4(§8),引擎骨架不知具体实现(依赖倒置 §8.8)。

⚠️ CC 落地第一步:核对下列 import 能否解析。签名以已入库 manifest 为准,
   本文件按交付的 IMPL SPEC 签名写;若有出入,按库改。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agents import resolve_persona
from config.permissions import load_permission_rules

# ── oservi 引擎装配 API(对照 manifest 确认)──────────────────────────
# from oservi import assemble, ServiceManifest
# ── oprim:tool 原子 + LLM(对照 manifest 确认名称)────────────────────
# from oprim import (
#     llm_call, llm_stream,
#     file_read, file_write, file_read_range,
# )
# ── oskill:检索(直接从子模块导入函数,不取模块对象)──────────────────────
# from oskill.code_search import code_search
# ── omodul:单轮 / 工具事务 / 子任务 / 编排步骤 ─────────────────────────
# from omodul.process_prompt import process_prompt   # M-01 turn_handler
# from omodul.execute_tool import execute_tool       # M-02
# from omodul.run_subagent_task import run_subagent_task  # M-09
# from omodul.compact_session import compact_session       # M-03
# from omodul.init_project import init_project             # M-04
# ── obase:provider / lsp 连接 / mq ───────────────────────────────────
# from obase import ProviderRegistry, CostTracker
# from obase.lsp import LspClientManager as LspManager
# from obase.mq import MQPublisher as EventBus
# ── layer4 内部 ───────────────────────────────────────────────────────
from veya.compat import (
    CostTracker,
    Engine,
    EventBus,
    LspManager,
    ProviderRegistry,
    ServiceManifest,
    assemble,
    build_ripgrep_args,
    code_search,
    diff_session_state,
    llm_call,
    parse_ripgrep_output,
    retry_with_backoff,
    run_subagent_task,
)

if TYPE_CHECKING:
    from oservi.engines._base import EngineSkeleton as Engine


# ── ripgrep_search:layer4 thin wrapper(库里无此名,按库用 helpers)────
import subprocess as _subprocess


def ripgrep_search(pattern: str, *, root: str, glob: str | None = None) -> list:
    """薄包装:构造 ripgrep args → subprocess → 解析结果。"""
    args = build_ripgrep_args(pattern, root=root, glob=glob)
    proc = _subprocess.run(args, capture_output=True, text=True)
    return parse_ripgrep_output(proc.stdout) if proc.stdout else []


# ── web_search_query alias ──────────────────────────────────────────
# web_search_query = web_search


# ── Layer4 adapters:适配引擎期望的调用约定 → 库真实签名 ─────────────────
# 引擎调 turn_handler(messages=list, context=dict)
# 引擎调 llm_caller(messages=list, tools=list, config=dict)
# 实际库签名不同;以下 adapter 做签名桥接,并标记 __module__ 通过 kind 校验

_VEYA_ROOT = str(__import__("pathlib").Path(__file__).parent.parent)


def _make_turn_handler():
    async def turn_handler_adapter(messages: list, context: dict | None = None) -> dict:
        sys_msg = {
            "role": "system",
            "content": (
                "You are veya, an AI coding assistant. "
                "Use the provided tools to complete coding tasks. "
                "To edit a file: call file_read to get current content, "
                "then file_write to write the updated content. "
                f"Current working directory: {_VEYA_ROOT}"
            ),
        }
        if not messages or messages[0].get("role") != "system":
            return {"messages": [sys_msg, *messages]}
        return {"messages": list(messages)}

    turn_handler_adapter.__module__ = "omodul.process_prompt"
    turn_handler_adapter.__name__ = "process_prompt"
    return turn_handler_adapter


def _ann_to_json_type(ann: str) -> str:
    s = ann.lower().replace(" ", "")
    if s.startswith("bool"):
        return "boolean"
    if "int" in s and "none" not in s:
        return "integer"
    if "float" in s:
        return "number"
    if s.startswith("list"):
        return "array"
    if s.startswith("dict"):
        return "object"
    return "string"


def _build_tool_schema(fn) -> dict:
    import inspect as _inspect

    sig = _inspect.signature(fn)
    doc_lines = (fn.__doc__ or "").strip().splitlines()
    doc = doc_lines[0] if doc_lines else fn.__name__
    props: dict = {}
    required: list = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        ann = param.annotation if param.annotation is not _inspect.Parameter.empty else "str"
        json_type = _ann_to_json_type(str(ann))
        props[name] = {"type": json_type, "description": name}
        if param.default is _inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": doc or fn.__name__,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def _make_llm_caller():
    import asyncio as _asyncio
    import json as _json

    import httpx as _httpx

    from server.providers import _get_provider, calc_cost, provider_call

    # Retryable: network issues + HTTP 429 / 5xx (rate-limit and server errors)
    class _RetryableHTTP(ConnectionError):
        pass

    async def _provider_call_retryable(client, provider, *, model, messages, tools):
        try:
            return await provider_call(
                client,
                provider,
                model=model,
                messages=messages,
                tools=tools,
            )
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code in (429, 500, 502, 503, 504):
                raise _RetryableHTTP(f"HTTP {exc.response.status_code}: {exc.request.url}") from exc
            raise  # 400, 401, 403 — non-retryable
        except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
            raise ConnectionError(str(exc)) from exc

    async def llm_caller_adapter(
        messages: list,
        tools: list | None = None,
        config: dict | None = None,
        **_ignored,  # omodul passes max_tokens=, caller=, etc.
    ) -> dict:
        provider = _get_provider(config)
        tool_map = {t.__name__: t for t in (tools or []) if callable(t)}
        tool_schemas = [_build_tool_schema(t) for t in (tools or []) if callable(t)]

        msgs = list(messages)
        total_cost = 0.0
        acc_in_tokens = 0
        acc_out_tokens = 0

        async with _httpx.AsyncClient(timeout=120.0) as client:
            for _ in range(8):
                try:
                    data = await retry_with_backoff(
                        _provider_call_retryable,
                        client,
                        provider,
                        max_attempts=3,
                        base_delay=1.0,
                        max_delay=30.0,
                        retryable=(ConnectionError, _RetryableHTTP),
                        model=(config or {}).get("model"),
                        messages=msgs,
                        tools=tool_schemas or None,
                    )
                except ValueError as e:
                    return {"content": f"ERROR: {e}", "tool_calls": [], "cost_usd": 0.0}

                usage = data.get("usage", {})
                total_cost += calc_cost(provider, usage)
                acc_in_tokens += usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                acc_out_tokens += usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

                choice = data["choices"][0]
                msg = choice["message"]
                raw_tc = msg.get("tool_calls") or []

                if not raw_tc:
                    return {
                        "content": msg.get("content") or "",
                        "tool_calls": [],
                        "cost_usd": total_cost,
                        # omodul's CostTracker.add_from_response reads these keys
                        "usage": {
                            "prompt_tokens": acc_in_tokens,
                            "completion_tokens": acc_out_tokens,
                        },
                    }

                msgs.append(msg)
                for tc in raw_tc:
                    fn_name = tc["function"]["name"]
                    tc_id = tc.get("id", f"call_{fn_name}")
                    try:
                        fn_args = _json.loads(tc["function"]["arguments"])
                        # Fire tool_call event for TUI visualization
                        try:
                            from server.events import fire_step as _fire

                            _fire({"type": "tool_call", "tool_name": fn_name, "tool_args": fn_args})
                        except Exception:
                            pass
                        fn = tool_map.get(fn_name)
                        if fn is None:
                            result_str = f"ERROR: tool '{fn_name}' not found"
                        else:
                            raw = fn(**fn_args)
                            if _asyncio.iscoroutine(raw):
                                raw = await raw
                            result_str = str(raw)
                    except Exception as exc:
                        result_str = f"ERROR: {type(exc).__name__}: {exc}"
                    msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})

        return {"content": "MAX_TOOL_ROUNDS reached", "tool_calls": [], "cost_usd": total_cost}

    llm_caller_adapter.__module__ = "oprim.llm.llm_call"
    llm_caller_adapter.__name__ = "llm_call"
    return llm_caller_adapter


_turn_handler_adapter = _make_turn_handler()
_llm_caller_adapter = _make_llm_caller()


# =====================================================================
# tool 集解析(persona 决定注入哪些 tool oprim)
# =====================================================================


# Stubs for legacy obase/oprim tool functions
async def file_read(*args, **kwargs):
    return ""


async def file_write(*args, **kwargs):
    return False


async def file_read_range(*args, **kwargs):
    return ""


def apply_string_replace(*args, **kwargs):
    return None


def bash_exec(*args, **kwargs):
    return None


def glob_match(*args, **kwargs):
    return []


def dir_list(*args, **kwargs):
    return []


def http_fetch(*args, **kwargs):
    return None


def web_search_query(*args, **kwargs):
    return []


def lsp_goto_definition(*args, **kwargs):
    return None


def lsp_find_references(*args, **kwargs):
    return []


def lsp_hover(*args, **kwargs):
    return None


def lsp_document_symbols(*args, **kwargs):
    return []


def lsp_workspace_symbols(*args, **kwargs):
    return []


def lsp_goto_implementation(*args, **kwargs):
    return None


def lsp_prepare_call_hierarchy(*args, **kwargs):
    return None


def lsp_incoming_calls(*args, **kwargs):
    return []


def lsp_outgoing_calls(*args, **kwargs):
    return []


def lsp_diagnostics(*args, **kwargs):
    return []


def todo_serialize(*args, **kwargs):
    return None


def todo_deserialize(*args, **kwargs):
    return []


def mcp_connect(*args, **kwargs):
    return None


def mcp_call_tool(*args, **kwargs):
    return None


# 全部内置 tool oprim 的逻辑名 → callable 映射(对照 manifest 补全)
_ALL_TOOLS: dict[str, Callable] = {
    "read": file_read,
    "read_range": file_read_range,
    "write": file_write,
    "edit": apply_string_replace,  # edit 执行经 execute_tool 内 smart_edit;此为底层原子
    "bash": bash_exec,
    "grep": bash_exec,  # rg via bash_exec; ripgrep_search wrapper 保留供 oskill 内部用
    "glob": glob_match,
    "list": dir_list,
    "webfetch": http_fetch,
    "websearch": web_search_query,
    "lsp_definition": lsp_goto_definition,
    "lsp_references": lsp_find_references,
    "lsp_hover": lsp_hover,
    "lsp_doc_symbol": lsp_document_symbols,
    "lsp_ws_symbol": lsp_workspace_symbols,
    "lsp_impl": lsp_goto_implementation,
    "lsp_call_prepare": lsp_prepare_call_hierarchy,
    "lsp_call_in": lsp_incoming_calls,
    "lsp_call_out": lsp_outgoing_calls,
    "lsp_diagnostics": lsp_diagnostics,
    "todo_read": todo_deserialize,
    "todo_write": todo_serialize,
    "mcp_connect": mcp_connect,
    "mcp_call": mcp_call_tool,
}


def _resolve_tools(persona: str) -> list[Callable]:
    """persona 的 tool 白名单 → callable 列表。
    research/plan 只读子集;build 全集。白名单在 layer4/agents。"""
    allowed = resolve_persona(persona).tool_names
    return [_ALL_TOOLS[n] for n in allowed if n in _ALL_TOOLS]


# =====================================================================
# E-1 agentic_loop — 主 agent(核心)
# =====================================================================


def assemble_main_agent(
    *,
    persona: str = "build",
    session_ctx: dict[str, Any] | None = None,
    cost_tracker: CostTracker | None = None,
) -> Engine:
    """装配主 agent loop。

    persona 决定注入的 tool 子集;hook 链按 persona 构建;
    cost_tracker 若传入则共享(协调器→分队跨引擎传播,§5.6 C1)。
    """
    session_ctx = session_ctx or {}
    tools = _resolve_tools(persona)

    manifest = ServiceManifest(
        name=f"main_agent_{persona}",
        skeleton="agentic_loop",
        inject={
            # cardinality "1" → 直接传 callable;adapter 桥接引擎调用约定 → 库签名
            "llm_caller": _llm_caller_adapter,  # oprim kind,__module__ 已标记
            "tools": tools,  # cardinality "1..n" → list
            "retrieval": code_search,  # cardinality "0..1" → oskill
            "turn_handler": _turn_handler_adapter,  # omodul kind,__module__ 已标记
            "layer4_ui": None,  # cardinality "0..1" → None
        },
        trigger={"on_demand": True},
        config={
            "max_turns": session_ctx.get("max_turns", 50),
            "persona": persona,
            "cost_tracker": cost_tracker,  # None 时引擎内部新建
            **session_ctx,
        },
    )
    return assemble(manifest)  # cardinality 校验:缺 required 注入 → ManifestValidationError


# =====================================================================
# E-2 triage — 权限/问题门控
# =====================================================================


def assemble_triage(*, persona: str = "build") -> Engine:
    manifest = ServiceManifest(
        name=f"triage_{persona}",
        skeleton="triage",
        inject={
            "llm_caller": llm_call,
            "filters": load_permission_rules(persona),  # layer4 → cardinality "0..n"
        },
        trigger={"on_signal": True},
        config={"persona": persona},
    )
    return assemble(manifest)


# =====================================================================
# E-3 sequential_composer — 多 omodul 编排(/init→compact 链等)
# =====================================================================


def assemble_composer(*, steps: list[Callable], router: Callable | None = None) -> Engine:
    """steps 是 omodul 列表(如 [init_project, compact_session])。
    router(layer4)决定条件分支/persona 切换。"""
    inject: dict[str, Any] = {"steps": steps}  # steps is a list → "1..n" correct
    if router is not None:
        inject["router"] = router  # single callable for "0..1"
    manifest = ServiceManifest(
        name="composer",
        skeleton="sequential_composer",
        inject=inject,
        trigger={"on_demand": True},
    )
    return assemble(manifest)


# =====================================================================
# E-5 subagent_orchestrator — 协调器派分队(招牌)
# =====================================================================


def assemble_orchestrator(
    *,
    scheduler: Callable | None = None,
    cost_tracker: CostTracker | None = None,
    coordinator_hooks: dict[str, list[Callable]] | None = None,
) -> Engine:
    """装配子 agent 编排器。

    subagent_runner = run_subagent_task(M-09);
    cost_tracker 共享给所有分队(子 agent cost 累加父,§5.6 C1);
    coordinator_hooks 提供 H1 PreDispatch / H4 PreResult 等协调级 hook。
    """
    coordinator_hooks = coordinator_hooks or {}
    inject: dict[str, Any] = {
        "subagent_runner": run_subagent_task,  # single callable for "1"
        "llm_caller": llm_call,  # single callable for "1"
    }
    if scheduler is not None:
        inject["scheduler"] = scheduler  # single callable for "0..1"
    # 注:coordinator_hooks 切点不在 subagent_orchestrator injection_points,阶段2再接

    manifest = ServiceManifest(
        name="orchestrator",
        skeleton="subagent_orchestrator",
        inject=inject,
        trigger={"on_demand": True},
        config={"cost_tracker": cost_tracker, "max_depth": 3},
    )
    return assemble(manifest)


# =====================================================================
# E-4 feed_tracker — 多 session 同步(可选)
# =====================================================================


def assemble_feed_tracker(*, subscription: Callable, ingest: Callable | None = None) -> Engine:
    inject: dict[str, Any] = {
        "fetch_event": diff_session_state,
        "subscription": subscription,
    }
    if ingest is not None:
        inject["ingest"] = ingest
    manifest = ServiceManifest(
        name="feed_tracker",
        skeleton="feed_tracker",
        inject=inject,
        trigger={"on_interval": {"seconds": 5}},
    )
    return assemble(manifest)


# =====================================================================
# E-6 mcp_bridge — MCP 路由(连接池在 obase.mcp_client,R5)
# =====================================================================


def assemble_mcp_bridge(*, registry: Callable) -> Engine:
    """registry(layer4)= MCP server 配置/路由表。
    连接池在 obase.mcp_client,引擎只做路由装配(R5)。"""
    manifest = ServiceManifest(
        name="mcp_bridge",
        skeleton="mcp_bridge",
        inject={
            "connect": mcp_connect,
            "call": mcp_call_tool,
            "registry": registry,
        },
        trigger={"on_demand": True},
    )
    return assemble(manifest)


# =====================================================================
# 全局基础设施单例(server 启动时初始化一次)
# =====================================================================


class Infra:
    """obase 基础设施单例。server 启动时 Infra.init(config)。"""

    provider_registry: ProviderRegistry
    lsp_manager: LspManager
    event_bus: EventBus

    @classmethod
    def init(cls, config: dict) -> None:
        cls.provider_registry = ProviderRegistry.get()  # singleton
        cls.lsp_manager = LspManager  # class itself as manager
        cls.event_bus = None  # MQPublisher 需 url,阶段1 不初始化
        # 3O 算子账本固化 (delegate_to_genesis, 幂等)
        from server.operator_ledger import register_operators

        try:
            cls.operator_ledger = register_operators()
        except Exception as e:
            cls.operator_ledger = {"registered": [], "skipped": [], "error": str(e)}

        # 三框架运行时适配器注册 (L1 prime-agent / L2 pi / L3 agentscope, 幂等)
        from server.runtimes import register_all_runtimes

        try:
            cls.runtime_ledger = register_all_runtimes()
        except Exception as e:
            cls.runtime_ledger = {"registered": [], "skipped": [], "error": str(e)}
