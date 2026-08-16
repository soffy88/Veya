"""decision_ledger + context_graph — 内化单元测试 (2026-08-16)。"""

from __future__ import annotations

from pathlib import Path

from server.context_graph import ContextGraph
from server.decision_ledger import DecisionLedger


def _mk(tmp_path: Path):
    return DecisionLedger(tmp_path / "dl.db"), ContextGraph(tmp_path / "cg.db")


def test_record_and_get(tmp_path: Path):
    dl, _ = _mk(tmp_path)
    did = dl.record_decision(
        "project_task",
        "在当前目录创建 hello.py",
        reasoning="用户明确要求文件与内容",
        outcome="completed",
        confidence=0.98,
        source="project_ask",
        metadata={"task_id": "pa_1", "assignee": "hicode"},
    )
    d = dl.get_decision(did)
    assert d is not None
    assert d["category"] == "project_task"
    assert d["confidence"] == 0.98
    assert d["metadata"]["task_id"] == "pa_1"
    assert d["created_at"]


def test_trace_chain_and_impact(tmp_path: Path):
    dl, _ = _mk(tmp_path)
    a = dl.record_decision("understand", "优化一下这个项目", outcome="ask", confidence=0.3)
    b = dl.record_decision(
        "project_task", "写代码创建 u5_chain.txt", outcome="completed",
        confidence=0.9, parent_id=a,
    )
    c = dl.record_decision(
        "project_task", "写代码创建另一个文件", outcome="completed",
        confidence=0.85, parent_id=a,
    )
    chain = dl.trace_decision_chain(b)
    assert [x["id"] for x in chain] == [b, a]  # 最新在前, 上溯到根
    impact = dl.analyze_decision_impact(a)
    assert impact["direct_children"] == 2
    assert impact["by_outcome"]["completed"] == 2


def test_find_similar_decisions(tmp_path: Path):
    dl, _ = _mk(tmp_path)
    dl.record_decision("project_task", "创建 hello.py 文件", outcome="completed", confidence=0.9)
    dl.record_decision("project_task", "部署后端服务", outcome="blocked", confidence=0.6)
    hits = dl.find_similar_decisions("创建文件", category="project_task")
    assert hits and "hello.py" in hits[0]["scenario"]


def test_check_rules_and_export(tmp_path: Path):
    dl, _ = _mk(tmp_path)
    dl.record_decision("a", "低置信决策", outcome="blocked", confidence=0.2)
    dl.record_decision("a", "高置信决策", outcome="completed", confidence=0.95)
    rules = dl.check_decision_rules({"min_confidence": 0.5})
    assert rules[0]["rule"] == "low_confidence"
    assert rules[0]["matched"] == 1
    exported = dl.export_ledger(limit=10)
    assert len(exported) == 2
    assert all(isinstance(x["metadata"], dict) for x in exported)


def test_graph_upsert_edge_neighbors(tmp_path: Path):
    _, cg = _mk(tmp_path)
    cg.upsert_node("acme_corp", "Organization", "Acme Corp", {"industry": "SaaS"})
    cg.upsert_node("alice", "Person", "Alice Chen", {"role": "CTO"})
    cg.upsert_node("contract_001", "Contract", "Acme 合同", {"value": 2400000})
    cg.add_edge("alice", "works_for", "acme_corp", {"since": "2019-03-01"})
    cg.add_edge("acme_corp", "party_to", "contract_001", {"signed": "2024-01-15"})

    sub = cg.neighbors("alice", hops=2)
    assert set(sub["nodes"].keys()) == {"alice", "acme_corp", "contract_001"}
    assert len(sub["edges"]) == 2


def test_graph_temporal_snapshot(tmp_path: Path):
    _, cg = _mk(tmp_path)
    cg.upsert_node("n1", "Task", "任务一")
    cg.upsert_node("n2", "Task", "任务二")
    cg.remove_node("n1")
    s = cg.state_at("2030-01-01T00:00:00Z")
    assert s["alive_nodes"] == 1  # n1 已软删
    # 时点回放: 删除时间之前的状态
    past = cg.get_node("n1", as_of="2026-01-01T00:00:00Z")
    assert past is not None
    assert cg.get_node("n1") is None  # 当前视角已删


def test_graph_remove_and_upsert_revive(tmp_path: Path):
    _, cg = _mk(tmp_path)
    cg.upsert_node("n1", "Task", "旧名字")
    cg.remove_node("n1")
    cg.upsert_node("n1", "Task", "新名字")  # 复活
    d = cg.get_node("n1")
    assert d is not None and d["name"] == "新名字"


def test_summary_text(tmp_path: Path):
    dl, cg = _mk(tmp_path)
    dl.record_decision("project_task", "创建文件", outcome="completed", confidence=0.9)
    cg.upsert_node("t1", "Task", "任务一")
    assert "决策账本" in dl.summary()
    assert "上下文图" in cg.summary()


def test_ask_question_resolve_answer():
    """OpenMausBot 提问卡片内化: 提问 → 回答回填。"""
    import asyncio

    from server import user_control as uc

    async def main():
        task = asyncio.create_task(uc.ask_question("选 A 还是 B？", ["A", "B"]))
        await asyncio.sleep(0.05)  # 让提问事件发出
        # 找到待回答的提问 (唯一)
        assert len(uc._pending_questions) == 1
        rid = next(iter(uc._pending_questions))
        assert uc.resolve_answer(rid, "选 A") is True
        answer = await task
        assert answer == "选 A"
        # 未知 request_id 回填失败
        assert uc.resolve_answer("no-such-id", "x") is False

    asyncio.run(main())


def test_ask_question_timeout_uses_default(monkeypatch):
    """超时/用户不答 → 明确提示让模型用默认假设继续, 不阻断。"""
    import asyncio

    from server import user_control as uc

    import server.user_control as uc_mod
    monkeypatch.setattr(uc_mod, "_QUESTION_TIMEOUT_S", 0.05)

    async def main():
        answer = await uc.ask_question("要不要继续？")
        assert "did not answer" in answer
        assert "默认假设继续" in answer

    asyncio.run(main())
