"""4 算子账本门禁 — delegate_to_genesis 固化验证。

PRD: docs/prd/OPERATORS_PRD.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "3O" / "obase"))

from obase.agent_registry import AgentRegistry

from server.operator_ledger import (
    _LEDGER,
    agent_reach_channel,
    browser_use_agent,
    codebase_memory_graph,
    ledger_summary,
    officecli_doc_engine,
    register_operators,
)

# =========================================================================
# 账本注册
# =========================================================================

def test_register_operators_idempotent():
    """4 算子注册进 AgentRegistry; 重复调用零冲突 (幂等)。"""
    reg = AgentRegistry()
    r1 = register_operators(reg)
    assert set(r1["registered"]) == set(_LEDGER)
    assert r1["total"] == 4

    r2 = register_operators(reg)
    assert r2["registered"] == []
    assert set(r2["skipped"]) == set(_LEDGER)  # 已注册跳过

    # 账本可查询
    entry = reg.get("agent", "officecli_doc_engine")
    assert entry is not None and "Office" in entry["desc"]


def test_ledger_summary_shape():
    summary = ledger_summary()
    assert len(summary) == 4
    layers = {s["layer"] for s in summary}
    assert layers == {"外网行为层", "外网数据层", "内网代码智能层", "交付物生产层"}


# =========================================================================
# 算子行为 (依赖缺失 → 结构化失败, 不崩溃)
# =========================================================================

@pytest.mark.asyncio
async def test_officecli_operator_missing_binary():
    import shutil

    if shutil.which("officecli"):
        pytest.skip("officecli 已安装")

    result = await officecli_doc_engine("read", input="x.docx")
    assert result["ok"] is False
    assert "未安装" in result["error"]


@pytest.mark.asyncio
async def test_agent_reach_operator_unreachable():
    """sidecar 未启动 → 结构化错误 (不崩溃)。"""
    result = await agent_reach_channel("youtube_transcript", "https://example.com/v")
    assert result["ok"] is False
    assert "未挂载" in result["error"] or "不可达" in result["error"] or "失败" in result["error"]


@pytest.mark.asyncio
async def test_browser_use_operator_unavailable():
    """browser_use 未装 → 结构化安装指引 (经技能包)。"""
    import shutil

    if shutil.which("officecli") is None and False:
        pass
    try:
        import browser_use  # noqa: F401
    except ImportError:
        result = await browser_use_agent("test goal")
        assert result["ok"] is False
        assert ("未安装" in result["error"] or "未挂载" in result["error"]
                or "失败" in result["error"])
    else:
        pytest.skip("browser_use 已安装")


@pytest.mark.asyncio
async def test_codebase_memory_operator_graceful():
    """codebase_memory: 任意状态返回结构化 dict (不抛)。"""
    result = await codebase_memory_graph("find", kind="call_graph")
    assert isinstance(result, dict)
    assert "ok" in result


# =========================================================================
# 三框架运行时立项账本
# =========================================================================

def test_runtime_ledger_registered():
    """三框架立项: prime-agent / pi / agentscope, 状态 pending。"""
    from server.operator_ledger import RUNTIME_LEDGER, runtime_ledger_summary

    assert set(RUNTIME_LEDGER) == {
        "prime_agent_runtime", "pi_bridge", "agentscope_bridge"}
    summary = runtime_ledger_summary()
    assert all(s["status"] == "pending" for s in summary)
    # 层归属
    layers = {s["layer"] for s in summary}
    assert layers == {"内核运行时 (L1)", "工具链桥 (L2)", "平台编排桥 (L3)"}
