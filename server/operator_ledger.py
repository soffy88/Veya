"""server/operator_ledger.py — 4 算子账本固化 (delegate_to_genesis)。

把四件套能力注册为 3O 正式算子 (obase.agent_registry.AgentRegistry, 单一来源):
  browser_use_agent       外网行为层 (自然语言驱动浏览器)
  agent_reach_channel     外网数据层 (多渠道抓取 MCP 桥)
  codebase_memory_graph   内网代码智能层 (调用链/BlastRadius/死代码)
  officecli_doc_engine    交付物生产层 (docx/xlsx/pptx 渲染-观察-修复)

注册类型: agent (AgentRegistry) + tool (ToolRegistry 经 to_openai_tool 喂 LLM)。
幂等: 已注册条目跳过 (RegistryConflictError 捕获), 支持装配期重复调用。
"""

from __future__ import annotations

from typing import Any

from obase.agent_registry import AgentRegistry, RegistryConflictError

_LEDGER: dict[str, dict[str, str]] = {
    "browser_use_agent": {
        "layer": "外网行为层",
        "desc": "自然语言驱动浏览器执行目标 (browser-use Agent; LLM 消耗 + 真实网络, 不跑沙箱)",
        "skill": "browser_use",
    },
    "agent_reach_channel": {
        "layer": "外网数据层",
        "desc": "多平台内容读取 (YouTube/推特/Reddit/B站/小红书/雪球), agent-reach MCP 桥",
        "skill": "agent_reach",
    },
    "codebase_memory_graph": {
        "layer": "内网代码智能层",
        "desc": "代码库调用链/BlastRadius/死代码查询 (CodebaseMemoryConnector)",
        "skill": None,
    },
    "officecli_doc_engine": {
        "layer": "交付物生产层",
        "desc": "Office 文档生产 (docx/xlsx/pptx 创建/编辑/渲染; 写路径白名单 + 审计)",
        "skill": "officecli",
    },
}


# =========================================================================
# 运行时立项账本 (三框架集成: prime-agent / pi / agentscope)
# PRD: docs/prd/AGENT_RUNTIMES_PRD.md — 状态 pending (待确认后实施 L1→L3)
# =========================================================================

RUNTIME_LEDGER: dict[str, dict[str, str]] = {
    "prime_agent_runtime": {
        "layer": "内核运行时 (L1)",
        "desc": "prime-agent RLM 适配器: AgentRuntime 协议, 持久内核 + checkpoint/cognitive 自我优化",
        "status": "pending",
    },
    "pi_bridge": {
        "layer": "工具链桥 (L2)",
        "desc": "pi CLI subprocess 桥: plugin_tool 包装 + provider 平级路由 + code_sandbox 隔离",
        "status": "pending",
    },
    "agentscope_bridge": {
        "layer": "平台编排桥 (L3)",
        "desc": "agentscope 双向翻译: Event Bus / 中间件↔hooks / MCP 互注册 / Skill Hub 同步",
        "status": "pending",
    },
}


def runtime_ledger_summary() -> list[dict[str, Any]]:
    """运行时立项全貌 (立档查询)。"""
    return [
        {"name": name, "layer": meta["layer"], "status": meta["status"],
         "description": meta["desc"]}
        for name, meta in RUNTIME_LEDGER.items()
    ]


# =========================================================================
# 算子实现 (薄适配: 技能包/连接器 → 账本)
# =========================================================================

async def browser_use_agent(goal: str, url: str = "", max_steps: int = 10) -> dict[str, Any]:
    """外网行为层: 自然语言驱动浏览器 (browser-use)。"""
    return await _call_skill_async("browser_use", goal=goal, url=url, max_steps=max_steps)


async def agent_reach_channel(channel: str, url: str, limit: int = 20) -> dict[str, Any]:
    """外网数据层: 多平台内容读取 (agent-reach MCP 桥)。"""
    return await _call_skill_async("agent_reach", channel=channel, url=url, limit=limit)


async def officecli_doc_engine(op: str, input: str = "", output: str = "",
                               data_json: str = "",
                               options: dict[str, Any] | None = None) -> dict[str, Any]:
    """交付物生产层: Office 文档创建/编辑/渲染 (写路径白名单 + 审计)。"""
    return await _call_skill_async("officecli", op=op, input=input, output=output,
                                   data_json=data_json, options=options)


async def codebase_memory_graph(query: str, *, depth: int = 2,
                                kind: str = "call_graph") -> dict[str, Any]:
    """内网代码智能层: 调用链/BlastRadius/死代码 (CodebaseMemoryConnector)。"""
    try:
        from server.codebase_memory import CodebaseMemoryConnector

        connector = CodebaseMemoryConnector()
        await connector.start()
        try:
            if kind == "call_graph":
                results = await connector.search_code(query, limit=20)
            elif kind == "blast_radius":
                results = await connector.blast_radius(query, depth=depth)
            elif kind == "dead_code":
                results = await connector.dead_code(query)
            else:
                return {"ok": False, "error": f"未知 kind: {kind}"}
            return {"ok": True, "kind": kind, "results": results[:20]}
        finally:
            await connector.close()
    except Exception as e:
        return {"ok": False, "error": f"codebase_memory 不可用: {e}"}


async def _call_skill_async(name: str, **kwargs: Any) -> dict[str, Any]:
    """async 版技能调用 (executor 本身 async)。"""
    from server.skill_hub import skill_hub as _hub

    executor = _hub._executors.get(name)
    if executor is None:
        return {"ok": False, "error": f"技能包 '{name}' 未挂载 (检查 ~/.veya/skills/)"}
    try:
        result = await executor(**kwargs)
    except Exception as e:
        return {"ok": False, "error": f"{name} 执行失败: {e}"}
    if isinstance(result, str):
        import json

        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"ok": True, "output": result[:4000]}
    return result


# =========================================================================
# 账本注册 (幂等)
# =========================================================================

def register_operators(registry: AgentRegistry | None = None) -> dict[str, Any]:
    """把 4 算子固化进 3O 账本。返回 {registered, skipped}。"""
    reg = registry or AgentRegistry()
    registered: list[str] = []
    skipped: list[str] = []

    entries: list[tuple[str, Any, str]] = [
        ("browser_use_agent", browser_use_agent, _LEDGER["browser_use_agent"]["desc"]),
        ("agent_reach_channel", agent_reach_channel, _LEDGER["agent_reach_channel"]["desc"]),
        ("codebase_memory_graph", codebase_memory_graph, _LEDGER["codebase_memory_graph"]["desc"]),
        ("officecli_doc_engine", officecli_doc_engine, _LEDGER["officecli_doc_engine"]["desc"]),
    ]
    for name, func, desc in entries:
        try:
            reg.register("agent", name, func, desc=desc)
            registered.append(name)
        except RegistryConflictError:
            skipped.append(name)

    return {"registered": registered, "skipped": skipped,
            "total": len(_LEDGER), "ledger": _LEDGER}


def ledger_summary() -> list[dict[str, Any]]:
    """账本全貌 (PRD 附录/健康检查用)。"""
    return [
        {"name": name, "layer": meta["layer"], "skill": meta["skill"],
         "description": meta["desc"]}
        for name, meta in _LEDGER.items()
    ]


__all__ = [
    "_LEDGER",
    "agent_reach_channel",
    "browser_use_agent",
    "codebase_memory_graph",
    "ledger_summary",
    "officecli_doc_engine",
    "register_operators",
]
