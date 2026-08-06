"""Phase 2 完备性门禁 — 因果测谎仪 / do-calculus / 贝叶斯意图雷达 / 蜜罐反间谍。

断言具体数值与结构, 不是"跑通就行"。
"""

from __future__ import annotations

import pytest

from veya_loop import (
    BayesianBeliefUpdater,
    CausalGraphStore,
    adversarial_honeypot_observe,
    build_binary_failure_cpd_map,
    causal_fault_diagnose,
)

# =========================================================================
# 因果图存储: DAG 约束 + 结构版本号
# =========================================================================

def test_graph_store_dag_and_version():
    store = CausalGraphStore()
    v0 = store.version
    store.add_node("api", p_fail=0.3)
    store.add_node("db", p_fail=0.2)
    store.add_node("task_outcome")
    store.add_edge("api", "task_outcome")
    store.add_edge("db", "task_outcome")
    assert store.version == v0 + 5          # 3 节点 + 2 边
    assert store.nodes() == ["api", "db", "task_outcome"]
    # 成环被拒绝 (CausalGraphError)
    from obase.causal_graph_store import CausalGraphError
    with pytest.raises(CausalGraphError):
        store.add_edge("task_outcome", "api")
    assert store.version == v0 + 5          # 拒绝的边不 bump 版本


# =========================================================================
# 因果诊断: do-calculus 逐节点干预
# =========================================================================

def _build_faulty_store() -> CausalGraphStore:
    store = CausalGraphStore()
    store.add_node("api_gateway", p_fail=0.3)
    store.add_node("db", p_fail=0.2)
    store.add_node("task_outcome")
    store.add_edge("api_gateway", "task_outcome")
    store.add_edge("db", "task_outcome")
    return store


def test_causal_fault_diagnose_returns_report():
    store = _build_faulty_store()
    report = causal_fault_diagnose("task failed: db timeout", store=store)
    assert report.failure_context
    # 逐节点 do-calculus: 每个候选节点都有干预结果
    assert report.candidate_nodes
    for res in report.interventions:
        assert res.node_id
        assert res.delta_p_fault is not None
    # 根因候选 + 建议动作存在
    assert report.root_cause_candidates
    assert isinstance(report.recommended_actions, list)


def test_do_calculus_cpd_map_build():
    store = _build_faulty_store()
    cpd_map = build_binary_failure_cpd_map(store.get_graph())   # 需要 networkx 图
    assert "task_outcome" in cpd_map
    assert "api_gateway" in cpd_map
    assert "db" in cpd_map


# =========================================================================
# 贝叶斯意图雷达 (Bayesian ToM)
# =========================================================================

def test_bayesian_belief_update_and_dominates():
    updater = BayesianBeliefUpdater(["benign", "malicious"])
    assert updater.belief("malicious") == 0.5     # 均匀先验
    updater.update([0.1, 0.9])                     # 强恶意信号
    assert updater.belief("malicious") > 0.8
    # 连续信号突破阈值 → dominates
    for _ in range(3):
        updater.update([0.05, 0.95])
    assert updater.dominates("malicious", threshold=0.95)
    # 良性信号拉回: 连续良性观测后显著回落
    for _ in range(4):
        updater.update([0.95, 0.05])
    assert updater.belief("malicious") < 0.9


# =========================================================================
# 蜜罐反间谍
# =========================================================================

def test_honeypot_detects_credential_theft():
    """敌对代码尝试偷蜜罐密钥 → hostile 信号。"""
    evil = (
        "import os\n"
        "out = []\n"
        "for k in os.environ:\n"
        "    if 'HONEY' in k or 'KEY' in k:\n"
        "        out.append((k, os.environ[k]))\n"
        "print(out)\n"
    )
    obs = adversarial_honeypot_observe(evil, timeout=10.0)
    assert obs.is_hostile is True
    assert obs.injected_keys                    # 确认哪些密钥被触碰
    assert obs.escalation_payload is not None


def test_honeypot_benign_code_passes():
    good = "print('hello')\n"
    obs = adversarial_honeypot_observe(good, timeout=10.0)
    assert obs.is_hostile is False
