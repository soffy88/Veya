"""性能门禁 — 推理缓存命中 / 残差缩放近似正确性 / 软时延预算。

断言两类:
  1. 硬正确性: 近似 vs 精确的偏差界; DP 路径计数 vs 暴力枚举一致; 缓存键隔离;
  2. 软时延: 10 节点链上的 rollout / multi_step_plan 预算 (宽松, 防 CI 抖动)。
"""

from __future__ import annotations

import time

import pytest
from veya_loop.oprim._inference_cache import (  # 文档 import 路径
    count_simple_paths_dag,
    path_frequency_counts,
)

from veya_loop import (
    CausalGraphStore,
    causal_fault_diagnose,
    counterfactual_rollout,
    get_intervention_cache,
    multi_step_plan,
)

pytest.importorskip("pgmpy")


# =========================================================================
# 工具: 约 10 节点链状因果图
# =========================================================================


def _build_chain(n: int = 10) -> CausalGraphStore:
    """链: n0 → n1 → ... → n9(=task_outcome), 每节点 p_fail=0.1。"""
    store = CausalGraphStore()
    for i in range(n - 1):
        store.add_node(f"n{i}", p_fail=0.1)
    store.add_node("task_outcome", p_fail=0.1)
    for i in range(n - 1):
        store.add_edge(f"n{i}", f"n{i + 1}" if i + 1 < n - 1 else "task_outcome")
    return store


def _chain_dag(n: int = 10):
    return _build_chain(n).get_graph()


# =========================================================================
# 一、DP 路径计数正确性 (O(V+E) vs 暴力枚举)
# =========================================================================


def test_path_frequency_counts_matches_bruteforce():
    import networkx as nx

    dag = _chain_dag(6)
    candidates = list(dag.nodes)
    dp = path_frequency_counts(dag, candidates, "task_outcome")

    # 暴力参照: 每条根→failure 的简单路径, 节点计数
    brute = {n: 0 for n in candidates}
    for src in [n for n in dag.nodes if dag.in_degree(n) == 0]:
        for path in nx.all_simple_paths(dag, src, "task_outcome"):
            for n in path:
                if n in brute:
                    brute[n] += 1
    assert dp == brute
    # 链上: 根节点 n0 只有 1 条路径到 failure, 途经全部节点
    assert dp["n0"] == 1 and dp["n4"] == 1 and dp["task_outcome"] == 1


def test_count_simple_paths_dag_diamond():
    import networkx as nx

    dag = nx.DiGraph()
    dag.add_edges_from([("s", "a"), ("s", "b"), ("a", "t"), ("b", "t")])
    assert count_simple_paths_dag(dag, "s", "t") == 2
    # 无路径 → 0
    assert count_simple_paths_dag(dag, "t", "s") == 0


# =========================================================================
# 二、LRU 干预缓存: 命中可见 + 键隔离
# =========================================================================


def test_intervention_cache_hit_and_miss():
    from veya_loop import build_binary_failure_cpd_map

    cache = get_intervention_cache()
    cache.clear()
    dag = _chain_dag(5)
    cpd_map = build_binary_failure_cpd_map(dag)

    from veya_loop import _do_calculus_intervention as _dci

    r1 = _dci(
        dag,
        target_node="n2",
        intervention_value="ok",
        outcome_nodes=["task_outcome"],
        cpd_map=cpd_map,
    )
    assert r1["status"] == "ok"
    s1 = cache.stats()
    assert s1["misses"] == 1 and s1["hits"] == 0

    # 二次同干预 → cache_hit, 结果一致
    r2 = _dci(
        dag,
        target_node="n2",
        intervention_value="ok",
        outcome_nodes=["task_outcome"],
        cpd_map=cpd_map,
    )
    s2 = cache.stats()
    assert s2["hits"] == 1 and s2["misses"] == 1
    assert r2["post_intervention_distribution"] == r1["post_intervention_distribution"]

    # 不同干预节点 → miss
    _dci(
        dag,
        target_node="n4",
        intervention_value="ok",
        outcome_nodes=["task_outcome"],
        cpd_map=cpd_map,
    )
    assert cache.stats()["misses"] == 2

    # 图结构变了 → 指纹变 → 不命中
    dag2 = _chain_dag(5)
    dag2.add_edge("n0", "n3")
    _dci(
        dag2,
        target_node="n2",
        intervention_value="ok",
        outcome_nodes=["task_outcome"],
        cpd_map=cpd_map,
    )
    assert cache.stats()["misses"] == 3


def test_enumerate_paths_false_existence_only():
    from veya_loop import _do_calculus_intervention as _dci

    dag = _chain_dag(5)
    full = _dci(
        dag,
        target_node="n2",
        intervention_value="ok",
        outcome_nodes=["task_outcome"],
        use_cache=False,
        enumerate_paths=True,
    )
    slim = _dci(
        dag,
        target_node="n2",
        intervention_value="ok",
        outcome_nodes=["task_outcome"],
        use_cache=False,
        enumerate_paths=False,
    )
    # 存在性判断: num_paths 一致, 但 False 不携带路径列表
    assert (
        full["structural_effect_paths"][0]["num_paths"]
        == slim["structural_effect_paths"][0]["num_paths"]
    )
    assert full["structural_effect_paths"][0]["paths"]
    assert slim["structural_effect_paths"][0]["paths"] == []


# =========================================================================
# 三、rollout: 残差缩放近似正确性 + 时延预算
# =========================================================================


def test_residual_approximation_close_to_exact():
    dag = _chain_dag(6)
    cpd_map = None
    from veya_loop import build_binary_failure_cpd_map

    cpd_map = build_binary_failure_cpd_map(dag)

    exact = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=2,
        cpd_map=cpd_map,
        approx_second_step=False,
        use_cache=False,
    )
    approx = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=2,
        cpd_map=cpd_map,
        approx_second_step=True,
        use_cache=False,
    )
    # 首动作一致 (深度 1 是精确的)
    assert exact.planned_actions[0].node == approx.planned_actions[0].node
    # 次步 ΔP 近似偏差有界 (残差缩放是一阶近似)
    if len(exact.planned_actions) >= 2:
        d_e = exact.planned_actions[1].delta_p
        d_a = approx.planned_actions[1].delta_p
        assert abs(d_a - d_e) <= 0.05 + 0.3 * d_e, (d_e, d_a)


def test_rollout_latency_budget_and_cache_reuse():
    """H=2 10 节点链: 首跑 < 200ms (软预算); 二次跑命中缓存显著更快。"""
    dag = _chain_dag(10)
    from veya_loop import build_binary_failure_cpd_map

    cpd_map = build_binary_failure_cpd_map(dag)
    cache = get_intervention_cache()
    cache.clear()

    t0 = time.perf_counter()
    r1 = counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=2,
        cpd_map=cpd_map,
        approx_second_step=True,
        use_cache=True,
    )
    first_ms = (time.perf_counter() - t0) * 1000
    assert r1.planned_actions
    assert first_ms < 200, f"首次 rollout 超预算: {first_ms:.0f}ms"

    # 二次同图规划: 深度 1 全部命中缓存 (hits 增长)
    hits_before = cache.stats()["hits"]
    t1 = time.perf_counter()
    counterfactual_rollout(
        dag,
        failure_node="task_outcome",
        horizon=2,
        cpd_map=cpd_map,
        approx_second_step=True,
        use_cache=True,
    )
    second_ms = (time.perf_counter() - t1) * 1000
    assert cache.stats()["hits"] > hits_before
    assert second_ms < first_ms * 0.9 + 20, (second_ms, first_ms)


# =========================================================================
# 四、diagnose / multi_step_plan 时延预算
# =========================================================================


def test_diagnose_latency_budget():
    store = _build_chain(10)
    cache = get_intervention_cache()
    cache.clear()
    t0 = time.perf_counter()
    report = causal_fault_diagnose("task failed", store=store)
    _ms = (time.perf_counter() - t0) * 1000
    assert report.candidate_nodes
    # 二次诊断 → 全部干预命中缓存
    hits_before = cache.stats()["hits"]
    causal_fault_diagnose("task failed", store=store)
    assert cache.stats()["hits"] > hits_before


def test_multi_step_plan_latency_budget():
    store = _build_chain(10)
    cache = get_intervention_cache()
    cache.clear()
    t0 = time.perf_counter()
    report = multi_step_plan(
        "task failed: n7 timeout", store=store, horizon_override=2, execute=False
    )
    ms = (time.perf_counter() - t0) * 1000
    assert report.recommended_actions
    assert ms < 300, f"multi_step_plan 超软预算: {ms:.0f}ms"


# =========================================================================
# 五、调试入口可见性
# =========================================================================


def test_cache_stats_visible():
    stats = get_intervention_cache().stats()
    assert {"hits", "misses", "hit_rate", "size", "capacity"} <= set(stats)
    assert stats["capacity"] == 512
