"""L3 反事实诊断测试 — 显式噪声 SCM 三步法。

门禁:
  1. SCM 构造: noisy-OR 参数精确恢复 (b / t, 含 1-D 根表);
  2. 确定性传播: 给定 U 赋值, 故障状态唯一且与证据相容判定正确;
  3. Abduction: 根节点观测 fault → P(U=1|e)=1.0 (可精确断言);
  4. **L3 vs L2 对照: 本次真凶识别** — L2 说修 db, L3 说修 external_api;
  5. 事务端到端: ranking/reports/abduction 结构与确定性。
"""

from __future__ import annotations

import numpy as np
import pytest
from pgmpy.factors.discrete import TabularCPD

from veya_loop import (
    CausalGraphStore,
    CounterfactualDiagnosisReport,
    StructuralSCM,
    counterfactual_diagnose,
)

# =========================================================================
# 工具: 手构不对称 noisy-OR CPD — 让 L2 偏好 db、L3 偏好 external_api
#   external_api: 根, P(fault)=0.3, 传输 t=0.9 (高传染)
#   db:           根, P(fault)=0.8, 传输 t=0.5 (高发但低传染)
#   task_outcome: base=0.05
# =========================================================================

_EXT_P = 0.3
_DB_P = 0.8
_TASK_BASE = 0.05
_T_EXT, _T_DB = 0.9, 0.5


def _p_task_fault(api_fault: bool, db_fault: bool) -> float:
    return 1.0 - (1.0 - _TASK_BASE) * (1.0 - _T_EXT * int(api_fault)) * (
        1.0 - _T_DB * int(db_fault)
    )


def _build_store() -> CausalGraphStore:
    store = CausalGraphStore()
    store.add_node("external_api", p_fail=_EXT_P)
    store.add_node("db", p_fail=_DB_P)
    store.add_node("task_outcome")
    store.add_edge("external_api", "task_outcome")
    store.add_edge("db", "task_outcome")
    return store


def _custom_cpds(dag) -> dict:
    """手构 CPD: 根 1-D 表 + task_outcome 多维表 (last axis = db)。"""
    cpds = {
        "external_api": TabularCPD(
            "external_api",
            2,
            [[1 - _EXT_P], [_EXT_P]],
            state_names={"external_api": ["ok", "fault"]},
        ),
        "db": TabularCPD("db", 2, [[1 - _DB_P], [_DB_P]], state_names={"db": ["ok", "fault"]}),
    }
    vals = np.zeros((2, 4))  # 2D: 行=变量状态, 列=父组合 (api 慢变)
    for a in (0, 1):
        for d in (0, 1):
            vals[1, a * 2 + d] = _p_task_fault(bool(a), bool(d))
            vals[0, a * 2 + d] = 1.0 - vals[1, a * 2 + d]
    cpds["task_outcome"] = TabularCPD(
        "task_outcome",
        2,
        vals,
        evidence=["external_api", "db"],
        evidence_card=[2, 2],
        state_names={
            "task_outcome": ["ok", "fault"],
            "external_api": ["ok", "fault"],
            "db": ["ok", "fault"],
        },
    )
    return cpds


def _scm():
    store = _build_store()
    dag = store.get_graph()
    return StructuralSCM.from_graph(dag, _custom_cpds(dag)), dag


# =========================================================================
# 一、SCM 构造与参数恢复
# =========================================================================


def test_scm_construction_and_noisy_or_recovery():
    scm, _ = _scm()
    # 根节点 (1-D 表): b = P(fault), 无父
    assert scm.nodes["external_api"].base == pytest.approx(_EXT_P, abs=1e-6)
    assert scm.nodes["db"].base == pytest.approx(_DB_P, abs=1e-6)
    assert scm.nodes["external_api"].parents == []

    # task_outcome (多维表): base 与逐父传输精确恢复
    task = scm.nodes["task_outcome"]
    assert set(task.parents) == {"external_api", "db"}
    assert task.base == pytest.approx(_TASK_BASE, abs=1e-6)
    assert task.transmissions["external_api"] == pytest.approx(_T_EXT, abs=1e-6)
    assert task.transmissions["db"] == pytest.approx(_T_DB, abs=1e-6)


def test_deterministic_propagation_and_consistency():
    scm, _ = _scm()
    state = scm.fault_state({n: False for n in scm.nodes})
    assert all(not v for v in state.values())
    # external_api 的 U=1 → external_api fault → 传播到 task_outcome
    state2 = scm.fault_state({"external_api": True, "db": False, "task_outcome": False})
    assert state2["external_api"] is True and state2["task_outcome"] is True
    # 相容性判定
    assert scm.consistent(
        {"external_api": True, "db": False, "task_outcome": False}, {"task_outcome": "fault"}
    )
    assert not scm.consistent(
        {"external_api": True, "db": False, "task_outcome": False}, {"task_outcome": "ok"}
    )


# =========================================================================
# 二、Abduction: 精确边际溯因 (twin-network 枚举, 捕捉 explaining-away)
# =========================================================================


def test_abduction_root_fault_pinpoints_noise():
    store = CausalGraphStore()
    store.add_node("y", p_fail=0.5)
    scm = StructuralSCM.from_graph(store.get_graph(), None)
    assert scm.abduct({"y": "fault"}, sweeps=3)["y"] == 1.0
    assert scm.abduct({"y": "ok"}, sweeps=3)["y"] == 0.0


def test_abduction_parent_fault_explains_child():
    scm, _ = _scm()
    u = scm.abduct({"task_outcome": "fault", "external_api": "fault"}, sweeps=5)
    assert u["external_api"] == 1.0  # 观测到的根故障必然是其噪声
    # 精确溯因捕捉 explaining-away: task=fault 已被 external_api 传染解释,
    # 故 task 自身 leak 后验 ≈ 先验但略高 (0.0530 vs 0.05);
    # db 后验也略高于先验 (0.8081 vs 0.8) —— 均值场近似会误判成"保持先验"。
    assert u["task_outcome"] == pytest.approx(0.053022, abs=1e-4)
    assert u["db"] == pytest.approx(0.808059, abs=1e-4)


# =========================================================================
# 三、L3 vs L2: 识别**本次**真凶
# =========================================================================


def test_l3_identifies_this_failure_culprit():
    """证据: task=fault + external_api=fault (本次由 external_api 引起)。

    L2 (平均口径): 修 db 压降更大 (0.69 vs 0.43) — 会误导;
    L3 (锚定本次噪声): 修 db 几乎无效 (task 仍经 external_api 故障, 0.905),
    修 external_api 显著 (0.43) — 正确识别本次真凶。
    """
    scm, _ = _scm()
    u = scm.abduct({"task_outcome": "fault", "external_api": "fault"})

    l2_db = scm.l2_p_fault(["db"], "task_outcome")
    l2_api = scm.l2_p_fault(["external_api"], "task_outcome")
    l3_db = scm.l3_p_fault(["db"], "task_outcome", u)
    l3_api = scm.l3_p_fault(["external_api"], "task_outcome", u)

    # L2 口径: 平均情形下修 db 压降更大 (p_fail·t 更高)
    assert (1.0 - l2_db) > (1.0 - l2_api)
    # L3 口径: 本次修 db 几乎压不住 (external_api 仍故障)
    assert l3_db > 0.8
    assert l3_api < 0.5
    assert (1.0 - l3_api) > (1.0 - l3_db) + 0.2  # L3 判定与 L2 相反


def test_counterfactual_diagnose_ranking_and_reports():
    store = _build_store()
    diag = counterfactual_diagnose(
        store=store,
        failure_node="task_outcome",
        factual_evidence={"task_outcome": "fault", "external_api": "fault"},
        cpd_map=_custom_cpds(store.get_graph()),
    )
    assert isinstance(diag, CounterfactualDiagnosisReport)
    assert diag.ranking[0] == "external_api"  # 本次真凶排第一
    assert set(diag.ranking) == {"external_api", "db"}

    # reports 与 ranking 同序, 三层对照齐全
    assert [r.node for r in diag.reports] == diag.ranking
    top = diag.reports[0]
    assert top.node == "external_api"
    assert top.factual_p_fault == 1.0
    assert top.l2_p_fault_after_do >= 0.0
    assert top.ranking_score == top.l3_delta
    # 溯因摘要 (审计原料)
    assert diag.abduction["external_api"] == 1.0


def test_counterfactual_diagnose_deterministic():
    store = _build_store()
    cpd = _custom_cpds(store.get_graph())
    d1 = counterfactual_diagnose(
        store=store,
        failure_node="task_outcome",
        factual_evidence={"task_outcome": "fault", "external_api": "fault"},
        cpd_map=cpd,
    )
    d2 = counterfactual_diagnose(
        store=store,
        failure_node="task_outcome",
        factual_evidence={"task_outcome": "fault", "external_api": "fault"},
        cpd_map=cpd,
    )
    assert d1.ranking == d2.ranking
    assert [r.as_dict() for r in d1.reports] == [r.as_dict() for r in d2.reports]


def test_l3_delta_zero_for_irrelevant_candidate():
    store = _build_store()
    diag = counterfactual_diagnose(
        store=store,
        failure_node="task_outcome",
        factual_evidence={"task_outcome": "fault", "external_api": "fault"},
        cpd_map=_custom_cpds(store.get_graph()),
    )
    db_report = next(r for r in diag.reports if r.node == "db")
    assert db_report.l3_delta < 0.2  # 本次修 db 几乎无效果
    assert db_report.l2_delta > db_report.l3_delta  # 但平均口径下它显得有用


# =========================================================================
# 四、精确推断验证: reconvergent diamond 上 == pgmpy, 且严格优于均值场
# =========================================================================


def test_exact_inference_matches_pgmpy_and_beats_meanfield_on_diamond():
    """diamond (x→a,x→b,a→y,b→y): a,b 经 x 相关。

    精确 twin-network 枚举必须 == pgmpy VE 精确观测边际;
    而均值场乘积式 (假设 a⊥b) 在此 reconvergent DAG 上可测量地偏 ——
    证明这次"近似→精确"升级确有其效。
    """
    import networkx as nx
    from pgmpy.inference import VariableElimination
    from pgmpy.models import DiscreteBayesianNetwork

    from veya_loop import build_binary_failure_cpd_map

    g = nx.DiGraph()
    g.add_edges_from([("x", "a"), ("x", "b"), ("a", "y"), ("b", "y")])
    cpds = build_binary_failure_cpd_map(g, fault_prior=0.3, transmission=0.6, base_failure=0.05)
    scm = StructuralSCM.from_graph(g, cpds)

    exact = scm.exact_intervene({}, [], "y")  # 精确枚举 P(y=fault) (无干预/无证据)

    model = DiscreteBayesianNetwork(list(g.edges()))
    for c in cpds.values():
        model.add_cpds(c)
    q = VariableElimination(model).query(["y"], show_progress=False)
    states = list(q.state_names["y"])
    p_pgmpy = float(q.values.ravel()[states.index("fault")])

    meanfield = scm.propagate({n: nd.base for n, nd in scm.nodes.items()})["y"]

    assert exact == pytest.approx(p_pgmpy, abs=1e-9)  # 精确枚举 == pgmpy 金标准
    assert abs(meanfield - p_pgmpy) > 1e-4  # 均值场确实偏 (升级有意义)
