"""内化能力端到端集成测试 (2026-08-16)。

覆盖 5 个内化工具 (ask_user / decision_record / decision_query / graph_store /
graph_query) 的完整链路: schema 可见性 → 注册表按名取函数 (与 LLM tool_call
同路径) → SQLite 落库 → 因果链/先例/策略门/时点快照 → 提问卡片路由回填。

与 tests/test_internalized_graph.py (单元级, 直接调底层类) 互补; 本文件是
集成级, 走真实注册表面。

数据隔离: monkeypatch 将 ledger/graph 模块单例指向 tmp_path, 绝不触碰
~/.veya 生产库。VEYA_QUESTION_TIMEOUT_S 亦通过 patch 模块全局控制。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import server.user_control as uc
from server import context_graph as cg_mod
from server import decision_ledger as dl_mod
from server.context_graph import ContextGraph
from server.decision_ledger import DecisionLedger
from server.routes.legacy_agent import AgentAnswerRequest, agent_answer
from server.tool_registry import master_tools

TOOLS = ("ask_user", "decision_record", "decision_query", "graph_store", "graph_query")


def _patch_dbs(tmp_path: Path, monkeypatch) -> None:
    """把模块单例指向临时库 (工具层与模块级函数均经模块属性访问, patch 即生效)。"""
    dl = DecisionLedger(tmp_path / "dl.db")
    cg = ContextGraph(tmp_path / "cg.db")
    monkeypatch.setattr(dl_mod, "ledger", dl)
    monkeypatch.setattr(cg_mod, "graph", cg)


async def _call(tool_name: str, **kwargs) -> str:
    """与 LLM tool_call 一致: 注册表按名取已注册函数并调用。"""
    fn = master_tools._functions.get(tool_name)
    assert fn is not None, f"tool {tool_name} not registered"
    return await fn(**kwargs)


def _utc_now() -> str:
    """与 server 内部 _now() 同格式 (秒级 Z), 保证时点快照字符串可比。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 0. 工具面可见性 ────────────────────────────────────────────────


def test_tool_surface_registered(tmp_path: Path, monkeypatch) -> None:
    _patch_dbs(tmp_path, monkeypatch)
    names = [s.get("function", {}).get("name") for s in master_tools._schemas]
    for t in TOOLS:
        assert t in names, f"{t} 不在 master 工具面"
        assert master_tools._functions.get(t) is not None, f"{t} 未注册实现"


# ── 1. 决策账本: record → trace / impact / similar / rules / export ──


async def test_decision_ledger_chain_e2e(tmp_path: Path, monkeypatch) -> None:
    _patch_dbs(tmp_path, monkeypatch)

    r1 = await _call(
        "decision_record",
        category="project_task",
        scenario="端到端验证决策账本工具链",
        reasoning="按 AGENTS.md: 未证明能工作不算完成",
        outcome="completed",
        confidence=0.95,
    )
    id_a = r1.split(":")[-1].strip()
    assert id_a.startswith("dl_")

    r2 = await _call(
        "decision_record",
        category="approve",
        scenario="批准内化工具上线",
        reasoning="实现完整且 10 用例通过",
        outcome="approved",
        confidence=0.92,
        parent_id=id_a,
    )
    id_b = r2.split(":")[-1].strip()

    r3 = await _call(
        "decision_record",
        category="deploy",
        scenario="部署到生产容器",
        reasoning="线上环境待重启验证",
        outcome="blocked",
        confidence=0.55,
        parent_id=id_b,
    )
    id_c = r3.split(":")[-1].strip()

    r4 = await _call(
        "decision_record",
        category="approve",
        scenario="低置信度决策(应触发策略门)",
        reasoning="证据不足",
        outcome="pending",
        confidence=0.3,
        parent_id=id_a,
    )
    id_d = r4.split(":")[-1].strip()

    # trace: 因果链上溯到根 (最新在前)
    trace = await _call("decision_query", action="trace", decision_id=id_c)
    chain = eval(trace)
    assert [x["id"] for x in chain] == [id_c, id_b, id_a]

    # impact: 直接下游统计 (不含孙节点)
    impact = eval(await _call("decision_query", action="impact", decision_id=id_a))
    assert impact["direct_children"] == 2
    assert {c["id"] for c in impact["children"]} == {id_b, id_d}

    # similar: 关键词先例检索
    similar = eval(await _call("decision_query", action="similar", query="批准 上线", limit=3))
    assert similar and similar[0]["id"] == id_b

    # rules: conf < 0.5 触发 warn
    rules = eval(await _call("decision_query", action="rules"))
    assert rules[0]["rule"] == "low_confidence"
    assert rules[0]["matched"] == 1 and rules[0]["status"] == "warn"

    # export: 全量审计导出
    exported = eval(await _call("decision_query", action="export", limit=10))
    assert len(exported) == 4

    # summary: 最近记录 (含低置信度标记)
    summary = await _call("decision_query", action="summary")
    assert "最近 4 条" in summary and id_d in summary


# ── 2. 上下文图: store → neighbors → 软删 → 时点快照 ────────────────


async def test_context_graph_e2e(tmp_path: Path, monkeypatch) -> None:
    _patch_dbs(tmp_path, monkeypatch)

    for nid, kind, name in [
        ("veya", "system", "veya agent platform"),
        ("semantica", "upstream", "Semantica"),
        ("openmausbot", "upstream", "OpenMausBot"),
        ("decision_ledger", "module", "决策账本"),
    ]:
        await _call("graph_store", op="upsert_node", node_id=nid, kind=kind, name=name)
    await _call(
        "graph_store", op="add_edge", node_id="veya", rel="borrowed_from", other_id="semantica"
    )
    await _call(
        "graph_store", op="add_edge", node_id="veya", rel="borrowed_from", other_id="openmausbot"
    )
    await _call(
        "graph_store",
        op="add_edge",
        node_id="semantica",
        rel="inspired",
        other_id="decision_ledger",
    )

    nb = eval(await _call("graph_query", op="neighbors", node_id="veya", hops=2))
    assert {"semantica", "openmausbot", "decision_ledger"} <= set(nb["nodes"])

    t_before = _utc_now()
    await asyncio.sleep(1.1)  # 保证 deleted_at > t_before
    await _call("graph_store", op="remove_node", node_id="openmausbot")

    nb2 = eval(await _call("graph_query", op="neighbors", node_id="veya", hops=2))
    assert "openmausbot" not in nb2["nodes"]  # 软删后当前视角不可见

    snap = eval(await _call("graph_query", op="state_at", timestamp=t_before))
    assert snap["alive_nodes"] == 4  # 删除前时点快照仍含 openmausbot

    snap_now = eval(await _call("graph_query", op="state_at", timestamp=_utc_now()))
    assert snap_now["alive_nodes"] == 3

    gsum = await _call("graph_query", op="summary")
    assert "3 节点" in gsum


# ── 3. ask_user 提问卡片: 工具 → 事件 → 路由回填 → 工具返回 ──────────


async def test_ask_user_answer_roundtrip(tmp_path: Path, monkeypatch) -> None:
    _patch_dbs(tmp_path, monkeypatch)
    uc._pending_questions.clear()

    task = asyncio.create_task(
        _call("ask_user", question="端到端测试: 用什么库?", options=["临时库", "生产库"])
    )
    # 模拟前端: 收到 SSE agent_question 事件 → 拿到 request_id → POST /api/v1/agent/answer
    rid = None
    for _ in range(100):
        await asyncio.sleep(0.05)
        if uc._pending_questions:
            rid = next(iter(uc._pending_questions))
            break
    assert rid is not None, "agent_question 事件未挂起 (request_id 未生成)"

    resp = await agent_answer(AgentAnswerRequest(request_id=rid, answer="用临时库, 不碰生产数据"))
    assert resp.get("ok") is True

    ans = await asyncio.wait_for(task, timeout=8)
    assert ans == "用临时库, 不碰生产数据"


async def test_ask_user_timeout_hint(tmp_path: Path, monkeypatch) -> None:
    _patch_dbs(tmp_path, monkeypatch)
    monkeypatch.setattr(uc, "_QUESTION_TIMEOUT_S", 1.0)  # 不等默认 300s

    ans = await _call("ask_user", question="没人回答的问题")
    assert "did not answer" in ans and "默认假设" in ans
    assert uc._pending_questions == {}  # 超时后清理挂起项
