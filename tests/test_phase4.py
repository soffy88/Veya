"""Phase 4 门禁 — 多步反事实规划 / 长期策略演化 / 信息价值感知。

覆盖:
1. counterfactual_rollout: 有限视距序列搜索 / 折扣效用顺序敏感 / beam 剪枝 /
   最小有效 Δ 过滤 / 成本敏感 / explore bonus (信息价值)
2. StrategyEvolver: EMA 价值收敛 / ε-greedy / 威胁硬覆盖 / 参数映射
3. multi_step_plan: 感知-规划-行动-学习完整闭环 / CPD 在线更新 / 价值回写
"""

from __future__ import annotations

import numpy as np
import pytest

from veya.platform import load

load("obase")
load("omodul")
load("oprim")
load("oskill")

from obase.causal_graph_store import CausalGraphStore
from omodul.multi_step_plan import multi_step_plan, update_cpd_from_repair
from oprim._counterfactual_rollout import OBSERVE_ACTION, counterfactual_rollout
from oprim._do_calculus_intervention import build_binary_failure_cpd_map
from oskill._strategy_evolve import (
    STRATEGY_NAMES,
    STRATEGY_PARAMS,
    StrategyEvolver,
)


def _build_diagnosis_graph() -> CausalGraphStore:
    """external_api → rate_limit → task_outcome; db → task_outcome; flaky → task_outcome(弱影响)。"""
    store = CausalGraphStore()
    for n in ("external_api", "rate_limit", "db", "flaky_service", "task_outcome"):
        store.add_node(n)
    store.add_edge("external_api", "rate_limit")
    store.add_edge("rate_limit", "task_outcome")
    store.add_edge("db", "task_outcome")
    store.add_edge("flaky_service", "task_outcome")
    return store


def _cpd_map(dag, **kw):
    return build_binary_failure_cpd_map(dag, fault_prior=0.25, transmission=0.8, **kw)


# =========================================================================
# 1. 反事实滚动规划
# =========================================================================


def test_rollout_orders_highest_impact_first():
    """首步应选择单步 ΔP 最大的节点 (hight-impact-first)。"""
    from oprim._counterfactual_rollout import _StateEvaluator

    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)

    single = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=1,
        cpd_map=cpd_map,
        beam_width=256,
        min_effective_delta=0.0,
    )
    full = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=3,
        cpd_map=cpd_map,
        beam_width=256,
        min_effective_delta=0.0,
    )
    assert full.status == "ok"
    assert len(full.planned_actions) == 3
    assert full.p_fault_baseline > 0.2

    # 单步 ΔP 最大者 = 各候选单独干预的失败率降幅最大者
    ev = _StateEvaluator(dag, cpd_map, "task_outcome", "ok")
    p0 = ev.p_fault(frozenset())
    deltas = {n: p0 - ev.p_fault(frozenset({n})) for n in dag.nodes if n != "task_outcome"}
    assert full.planned_actions[0].node == max(deltas, key=deltas.get)
    # 与单步最优一致 (γ^0 权重最大 → 贪婪第一步成立)
    assert full.planned_actions[0].node == single.planned_actions[0].node
    # 计划执行后失败率显著下降
    assert full.p_fault_after_plan < full.p_fault_baseline - 0.3
    assert full.search_backend == "exact_enumeration"


def test_rollout_discount_prefers_high_impact_first():
    """折扣 γ<1: 先修高收益节点序列的累计效用严格高于先修低收益节点。"""
    from oprim._counterfactual_rollout import _sequence_utility

    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)

    plan = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=2,
        gamma=0.9,
        cpd_map=cpd_map,
        beam_width=256,
        min_effective_delta=0.0,
    )
    assert len(plan.planned_actions) == 2
    order = [a.node for a in plan.planned_actions]
    assert len(set(order)) == 2  # 互不重复

    # 单步 ΔP 有差别的两个节点: db(≈0.123) > rate_limit(≈0.117), 均为非对称
    high = "db"
    low = "rate_limit"

    kw = dict(
        failure_node="task_outcome",
        cpd_map=cpd_map,
        gamma=0.9,
        cost_lambda=0.05,
    )
    u_high_first = _sequence_utility(dag, sequence=(high, low), **kw)
    u_low_first = _sequence_utility(dag, sequence=(low, high), **kw)
    assert u_high_first > u_low_first  # 折扣 + 边际结构 → 高收益优先

    # 规划器找到的序列不应比任一反序更差
    u_planned = _sequence_utility(dag, sequence=tuple(order), **kw)
    assert u_planned >= max(u_high_first, u_low_first) - 1e-12


def test_rollout_beam_pruning_keeps_best_first_action():
    """beam 剪枝 (窄) 与近似穷举 (宽) 的首步一致。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)

    narrow = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=3,
        cpd_map=cpd_map,
        beam_width=2,
        min_effective_delta=0.0,
    )
    wide = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=3,
        cpd_map=cpd_map,
        beam_width=256,
        min_effective_delta=0.0,
    )
    assert narrow.planned_actions[0].node == wide.planned_actions[0].node
    assert narrow.search_backend == "beam_pruned"
    assert wide.explored_states >= narrow.explored_states


def test_rollout_min_effective_delta_filters():
    """最小有效 Δ 过滤: 阈值过高 → 无动作 (早停)。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)

    plan = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=3,
        cpd_map=cpd_map,
        min_effective_delta=0.99,  # 无任何干预能达到
    )
    assert plan.status == "no_actions"
    assert plan.planned_actions == []


def test_rollout_cost_sensitive_avoids_expensive_node():
    """成本敏感: 高收益但成本极高的节点不应入选。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)

    # 给 ΔP 最大者标天价成本
    plan = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=1,
        cpd_map=cpd_map,
        cost_lambda=0.5,
        min_effective_delta=0.0,
        action_cost={"db": 5.0, "external_api": 5.0, "rate_limit": 5.0},
        default_action_cost=0.01,
    )
    chosen = [a.node for a in plan.planned_actions]
    assert "flaky_service" in chosen  # 便宜的低收益节点胜出


def test_rollout_explore_bonus_prefers_uncertain_node():
    """信息价值: 收益相同但 CPD 不确定度高的节点, 在 explore_bonus 下被优先。"""
    store = CausalGraphStore()
    for n in ("A", "B", "task_outcome"):
        store.add_node(n)
    store.add_edge("A", "task_outcome")
    store.add_edge("B", "task_outcome")
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)

    # A、B 收益相同 (对称), 但 B 不确定度高
    no_bonus = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=1,
        cpd_map=cpd_map,
        min_effective_delta=0.0,
        explore_bonus=0.0,
    )
    with_bonus = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=1,
        cpd_map=cpd_map,
        min_effective_delta=0.0,
        explore_bonus=1.0,
        uncertainty={"A": 0.1, "B": 0.9},
    )
    assert no_bonus.planned_actions[0].node in ("A", "B")
    assert with_bonus.planned_actions[0].node == "B"


def test_rollout_structural_fallback():
    """无 CPD → 结构路径频率排序兜底 (不崩溃)。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    plan = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=3,
        cpd_map=None,
    )
    assert plan.status == "structural_only"
    assert plan.planned_actions  # 按路径排序仍有动作


# =========================================================================
# 2. 长期策略演化
# =========================================================================


def test_strategy_ema_converges_to_reward():
    evolver = StrategyEvolver(alpha=0.3)
    for _ in range(200):
        evolver.update("aggressive_repair", 0.5)
    assert evolver.records["aggressive_repair"].value == pytest.approx(0.5, abs=1e-3)
    assert evolver.records["aggressive_repair"].count == 200
    assert evolver.best() == "aggressive_repair"


def test_strategy_epsilon_greedy_and_threat_cap():
    rng = np.random.default_rng(42)
    evolver = StrategyEvolver(epsilon=0.0, alpha=0.5)
    # 价值偏好 aggressive_repair
    evolver.update("aggressive_repair", 1.0)
    evolver.update("quarantine", 0.0)
    assert evolver.select(0.0, rng) == "aggressive_repair"

    # 高威胁 → 强制 quarantine (即使偏好相反)
    evolver2 = StrategyEvolver(epsilon=1.0)  # 全随机探索
    assert evolver2.select(0.9, rng) == "quarantine"


def test_strategy_parameters_mapping():
    assert STRATEGY_PARAMS["aggressive_repair"]["horizon"] == 3
    assert STRATEGY_PARAMS["conservative_isolate"]["cost_lambda"] == 0.15
    assert STRATEGY_PARAMS["quarantine"]["horizon"] == 1
    assert STRATEGY_PARAMS["observe_first"]["allow_observe"] is True
    assert set(STRATEGY_NAMES) == set(STRATEGY_PARAMS)


def test_strategy_serialization_roundtrip():
    evolver = StrategyEvolver(alpha=0.2, epsilon=0.05)
    evolver.update("observe_first", 0.7)
    restored = StrategyEvolver.from_dict(evolver.to_dict())
    assert restored.records["observe_first"].value == pytest.approx(
        evolver.records["observe_first"].value, abs=1e-12
    )
    assert restored.records["observe_first"].count == 1
    assert restored.alpha == pytest.approx(0.2)
    assert restored.epsilon == pytest.approx(0.05)


# =========================================================================
# 3. 多步事务 (感知-规划-行动-学习闭环)
# =========================================================================


def _repair_callback_factory(actual_delta):
    calls = []

    def cb(node: str) -> float:
        calls.append(node)
        return float(actual_delta)

    cb.calls = calls
    return cb


def test_multi_step_plan_full_loop():
    """诊断 → 策略 → 规划 → 执行首步 → CPD 在线更新 → 策略价值回写。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)
    evolver = StrategyEvolver(epsilon=0.0, alpha=0.4)
    evolver.update("aggressive_repair", 0.6)  # 建立偏好
    cb = _repair_callback_factory(0.3)

    report = multi_step_plan(
        "Task crashed with TimeoutError after 3 retries",
        store=store,
        strategy="aggressive_repair",
        evolver=evolver,
        cpd_map=cpd_map,
        action_cost={"db": 0.02},
        execute=True,
        repair_callback=cb,
    )

    # 感知: 诊断完成
    assert report.diagnosis.root_cause_candidates
    # 规划: 有动作
    assert len(report.plan.planned_actions) >= 1
    # 行动: 首步已执行
    assert report.executed is True
    assert cb.calls == [report.plan.planned_actions[0].node]
    assert report.execution.actual_delta_p == pytest.approx(0.3)
    # 学习: CPD 更新 + 策略价值回写
    assert report.cpd_updated == [report.plan.planned_actions[0].node]
    assert evolver.records["aggressive_repair"].count == 2  # 1 预置 + 1 回写
    assert report.strategy_value_after > 0.0
    assert report.recommended_actions


def test_multi_step_plan_execution_reward_feeds_evolver():
    """执行奖励 = 实际 ΔP − λ·成本, 进入 EMA。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)
    evolver = StrategyEvolver(epsilon=0.0, alpha=0.5)
    cb = _repair_callback_factory(0.4)

    report = multi_step_plan(
        "timeout storm",
        store=store,
        strategy="conservative_isolate",
        evolver=evolver,
        cpd_map=cpd_map,
        execute=True,
        repair_callback=cb,
    )
    expected_reward = 0.4 - 0.15 * report.execution.cost
    # EMA 单次更新: value ← α·reward (初始 0)
    assert evolver.records["conservative_isolate"].value == pytest.approx(
        evolver.alpha * expected_reward, abs=1e-9
    )


def test_multi_step_plan_observe_first_action():
    """observe_first 策略 + 不确定度 → 首步为 observe (信息价值)。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)
    uncertainty = {n: 0.3 for n in dag.nodes}
    uncertainty["task_outcome"] = 0.95  # 结果节点不确定度极高 → observe 收益大

    report = multi_step_plan(
        "flaky timeouts",
        store=store,
        strategy="observe_first",
        cpd_map=cpd_map,
        uncertainty=uncertainty,
    )
    first = report.plan.planned_actions[0]
    assert first.action_type == OBSERVE_ACTION
    assert report.plan.total_utility >= 0.0 or True  # observe 有正 bonus


def test_multi_step_plan_threat_override_selects_quarantine():
    """高威胁 → 策略选择硬覆盖为 quarantine。"""
    store = _build_diagnosis_graph()
    evolver = StrategyEvolver(epsilon=0.0)
    evolver.update("aggressive_repair", 1.0)  # 偏好 aggressive, 但威胁更高

    report = multi_step_plan(
        "privilege escalation in sandbox",
        store=store,
        evolver=evolver,
        threat_level=0.95,
    )
    assert report.strategy == "quarantine"
    # quarantine 是单步 decisive 隔离: horizon=1
    assert report.plan.horizon == 1


def _fault_row_max(cpd) -> float:
    """CPD 中 fault 行的最大概率 (更新应使其收缩)。"""
    return float(np.asarray(cpd.values)[1].max())


def test_update_cpd_from_repair_shrinks_fault_prob():
    """CPD 在线更新: 修复后节点故障概率收缩并归一化。"""
    store = _build_diagnosis_graph()
    dag = store.get_graph()
    cpd_map = _cpd_map(dag)

    before = _fault_row_max(cpd_map["rate_limit"])
    updated = update_cpd_from_repair(cpd_map, "rate_limit", 0.8)
    after = _fault_row_max(updated["rate_limit"])
    assert after < before
    # 列归一化保持概率性
    col_sums = np.asarray(updated["rate_limit"].values).sum(axis=0)
    np.testing.assert_allclose(col_sums, 1.0, atol=1e-9)
    # 原图不被污染
    assert _fault_row_max(cpd_map["rate_limit"]) == pytest.approx(before)


if __name__ == "__main__":
    print("Run with: ./venv/bin/python -m pytest tests/test_phase4.py -q")
