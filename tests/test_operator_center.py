"""O2 Operator 测试 — 确定性分配 / 激励相容支付 / 死锁防线。

断言具体数值: 匈牙利 14.0 全局最优; 一价可被操纵而 VCG 在检验网格上不可操纵;
囚徒困境均衡不在帕累托前沿。
"""

from __future__ import annotations

import pytest

scipy = pytest.importorskip("scipy")

from veya.platform import oprim as load_oprim  # noqa: E402

oprim = load_oprim()
from server.operator_center import VeyaOperatorCenter  # noqa: E402

# 4 Worker × 4 Task, 代价矩阵故意让贪心失效
C = [[2, 3, 20, 20],
     [3, 9, 20, 20],
     [20, 20, 4, 9],
     [20, 20, 5, 4]]


def simple_problem(n=4, costs=None, skills=True):
    costs = costs if costs is not None else C
    return {
        "tasks": [{"id": f"t{j}", "requires_skills": ("py",) if skills else ()} for j in range(n)],
        "workers": [{"id": f"w{i}", "skills": ("py",) if skills else ()} for i in range(n)],
        "bids": [{"worker_id": f"w{i}", "task_id": f"t{j}", "cost": float(costs[i][j])}
                 for i in range(n) for j in range(n)],
    }


# =========================================================================
# 一、分配: 组合最优化 vs 拍卖/随机化
# =========================================================================

def test_hungarian_global_optimum_14():
    r = VeyaOperatorCenter.dispatch(simple_problem())
    assert r["ok"] is True
    a = r["allocation"]
    assert a["method"] == "hungarian"
    assert a["total_cost"] == 14.0
    # 确定性最优配对: (w0,t1),(w1,t0),(w2,t2),(w3,t3) —— 贪心会给 19
    assert sorted(a["pairs"]) == [["w0", "t1"], ["w1", "t0"], ["w2", "t2"], ["w3", "t3"]]


def test_hungarian_tiebreak_deterministic_and_order_independent():
    tie = [[1.0, 1.0], [1.0, 1.0]]
    p = simple_problem(2, tie)
    r1 = VeyaOperatorCenter.dispatch(p)["allocation"]["pairs"]
    r2 = VeyaOperatorCenter.dispatch(p)["allocation"]["pairs"]
    assert r1 == r2
    # 输入顺序打乱 → 结果不变
    shuffled = {
        "tasks": [{"id": "t1", "requires_skills": ()}, {"id": "t0", "requires_skills": ()}],
        "workers": [{"id": "w1", "skills": ()}, {"id": "w0", "skills": ()}],
        "bids": [{"worker_id": "w1", "task_id": "t1", "cost": 1.0},
                 {"worker_id": "w0", "task_id": "t0", "cost": 1.0},
                 {"worker_id": "w1", "task_id": "t0", "cost": 1.0},
                 {"worker_id": "w0", "task_id": "t1", "cost": 1.0}],
    }
    assert VeyaOperatorCenter.dispatch(shuffled)["allocation"]["pairs"] == r1


def test_skill_mismatch_unassigned():
    p = {"tasks": [{"id": "t0", "requires_skills": ("sql",)}],
         "workers": [{"id": "w0", "skills": ("py",)}],
         "bids": [{"worker_id": "w0", "task_id": "t0", "cost": 1.0}],
         "unassigned_penalty": 50.0}
    r = VeyaOperatorCenter.dispatch(p)
    assert r["allocation"]["unassigned"] == ["t0"]
    assert not r["ok"]
    assert any(e["code"] == "UNASSIGNED_TASK" for e in r["escalations"])


def test_milp_capacity_and_balance():
    p = {
        "tasks": [
            {"id": "t_etl", "demand": {"cpu": 4}, "requires_skills": ("sql",), "resources": ("db", "cache")},
            {"id": "t_api", "demand": {"cpu": 4}, "requires_skills": ("py",), "resources": ("cache",)},
            {"id": "t_ml", "demand": {"cpu": 6}, "requires_skills": ("py",), "resources": ("gpu", "db")},
            {"id": "t_rpt", "demand": {"cpu": 3}, "requires_skills": ("sql",), "resources": ("db",)},
        ],
        "workers": [
            {"id": "w0", "capacity": {"cpu": 16}, "skills": ("py", "sql"), "max_tasks": 3},
            {"id": "w1", "capacity": {"cpu": 8}, "skills": ("py",), "max_tasks": 3},
            {"id": "w2", "capacity": {"cpu": 8}, "skills": ("sql",), "max_tasks": 3},
        ],
        "bids": [
            {"worker_id": "w0", "task_id": "t_etl", "cost": 3},
            {"worker_id": "w0", "task_id": "t_api", "cost": 3},
            {"worker_id": "w0", "task_id": "t_ml", "cost": 4},
            {"worker_id": "w0", "task_id": "t_rpt", "cost": 3},
            {"worker_id": "w1", "task_id": "t_api", "cost": 5},
            {"worker_id": "w1", "task_id": "t_ml", "cost": 5},
            {"worker_id": "w2", "task_id": "t_etl", "cost": 6},
            {"worker_id": "w2", "task_id": "t_rpt", "cost": 6},
        ],
        "unassigned_penalty": 50.0,
    }
    r = VeyaOperatorCenter.dispatch(p, mode="capacity")
    a = r["allocation"]
    assert a["method"] == "milp"
    assert a["unassigned"] == []
    assert a["total_cost"] == 14.0
    # max_tasks 与容量被遵守
    for _wid, tids in a["by_worker"].items():
        assert len(tids) <= 3
    # 负载均衡压平 (balance_weight 是显式权衡, 不是免费)
    rb = VeyaOperatorCenter.dispatch(p, mode="capacity", balance_weight=5.0)
    max_load = max(len(v) for v in rb["allocation"]["by_worker"].values())
    assert max_load < max(len(v) for v in a["by_worker"].values())
    assert rb["allocation"]["total_cost"] > a["total_cost"]  # 均衡有成本


# =========================================================================
# 二、支付与激励相容
# =========================================================================

def test_payments_rules():
    p = simple_problem(2, [[10, 14], [12, 11]], skills=False)
    r = VeyaOperatorCenter.dispatch(p, payment_rule="first_price")
    assert r["payments"]["rule"] == "first_price"
    assert r["payments"]["payments"] == {"w0": 10.0, "w1": 11.0}

    r2 = VeyaOperatorCenter.dispatch(p, payment_rule="second_price")
    assert r2["payments"]["payments"] == {"w0": 12.0, "w1": 14.0}   # 次低报价

    r3 = VeyaOperatorCenter.dispatch(p, payment_rule="vcg")
    assert r3["payments"]["rule"] == "vcg"
    assert r3["payments"]["solves"] == 3                            # n+1 次求解
    assert r3["payments"]["total"] > r["payments"]["total"]         # VCG ≥ 一价


def test_strategyproof_first_price_manipulable_vcg_not():
    """暴力最优反应: 一价可被操纵, VCG 在检验网格上不可操纵。"""
    p = {
        "tasks": [{"id": "j1", "requires_skills": ("py",)},
                  {"id": "j2", "requires_skills": ("py",)}],
        "workers": [{"id": "wA", "skills": ("py",)},
                    {"id": "wB", "skills": ("py",)},
                    {"id": "wC", "skills": ("py",)}],
        "bids": [
            {"worker_id": "wA", "task_id": "j1", "cost": 10},
            {"worker_id": "wA", "task_id": "j2", "cost": 14},
            {"worker_id": "wB", "task_id": "j1", "cost": 12},
            {"worker_id": "wB", "task_id": "j2", "cost": 11},
            {"worker_id": "wC", "task_id": "j1", "cost": 18},
            {"worker_id": "wC", "task_id": "j2", "cost": 20},
        ],
    }
    rp = oprim.Problem(
        tasks=[oprim.Task(**t) for t in p["tasks"]],
        workers=[oprim.Worker(**w) for w in p["workers"]],
        bids=[oprim.Bid(**b) for b in p["bids"]],
    )
    r_fp = oprim.check_strategyproof(rp, oprim.first_price)
    assert r_fp.manipulable is True
    dev = r_fp.best_deviation
    assert dev.gain > 0
    assert dev.misreport > dev.truthful_bid          # 成本侧谎报方向 = 抬价

    r_vcg = oprim.check_strategyproof(rp, oprim.vcg)
    assert r_vcg.manipulable is False
    assert r_fp.probes == r_vcg.probes               # 同一检验网格


# =========================================================================
# 三、死锁三层防线
# =========================================================================

def test_resource_order_and_waitforgraph():
    order = oprim.ResourceOrder(["cache", "db", "gpu"])
    assert order.plan(["gpu", "db"]) == ["db", "gpu"]
    assert order.violates(["db", "cache"]) == ("db", "cache")
    assert order.violates(["cache", "db", "gpu"]) is None

    g = oprim.WaitForGraph()
    g.add_wait("wX", "wY", "db")
    assert g.cycles() == []
    assert g.would_deadlock("wY", "wX") is True      # 预检发现将成环
    g.add_wait("wY", "wX", "cache")
    cyc = g.cycles()
    assert len(cyc) == 1 and set(cyc[0]) == {"wX", "wY"}
    assert g.victim(cyc[0], {"wX": 5, "wY": 2}) == "wY"   # 受害者取代价最小者
    assert g.cycles() == cyc                        # 规范化后可复现


def test_lease_manager_three_layers():
    lm = oprim.LeaseManager(order=oprim.ResourceOrder(["cache", "db", "gpu"]),
                            default_ttl_s=10.0)
    assert lm.acquire("wX", "db", 0.0, priority=1).kind == "grant"
    assert lm.acquire("wY", "db", 1.0, priority=1).kind == "deny"      # 同优先级被拒
    assert lm.acquire("wZ", "db", 2.0, priority=9).kind == "grant"     # 高优先级抢占
    assert any(e.kind == "preempt" for e in lm.events)
    assert lm.acquire("wQ", "db", 100.0).kind == "grant"               # TTL 到期自动释放
    assert lm.release("wQ", "db").kind == "release"


def test_deadlock_risk_detected_in_pipeline():
    # 两个 worker 需要相反顺序的资源 → 全序化后应无环 (预防层)
    p = {
        "tasks": [
            {"id": "t1", "demand": {"cpu": 1}, "resources": ("db", "cache")},
            {"id": "t2", "demand": {"cpu": 1}, "resources": ("cache", "db")},
        ],
        "workers": [{"id": "w0", "capacity": {"cpu": 4}, "max_tasks": 2},
                    {"id": "w1", "capacity": {"cpu": 4}, "max_tasks": 2}],
        "bids": [{"worker_id": "w0", "task_id": "t1", "cost": 1},
                 {"worker_id": "w0", "task_id": "t2", "cost": 2},
                 {"worker_id": "w1", "task_id": "t1", "cost": 2},
                 {"worker_id": "w1", "task_id": "t2", "cost": 1}],
    }
    r = VeyaOperatorCenter.dispatch(p, resource_ranking=["cache", "db"])
    assert not any(e["code"] == "DEADLOCK_RISK" for e in r["escalations"])
    # 每个 worker 的申请顺序都符合全序
    for _wid, seq in r["acquisition_plan"].items():
        assert oprim.ResourceOrder(["cache", "db"]).violates(seq) is None


# =========================================================================
# 四、博弈论离线反例 + 全流程确定性
# =========================================================================

def test_pd_nash_not_pareto():
    pd = oprim.prisoners_dilemma()
    ne, po = oprim.pure_nash(pd), oprim.pareto_optimal(pd)
    assert ne == [(1, 1)]                          # 唯一均衡 (背叛, 背叛)
    assert (1, 1) not in po                        # 严格帕累托劣
    assert (0, 0) in po                            # (合作, 合作) 在前沿
    assert oprim.dominant_strategies(pd) == ([1], [1])


def test_decision_id_and_replay_key_reproducible():
    p = simple_problem()
    d1 = VeyaOperatorCenter.dispatch(p, payment_rule="vcg",
                                     resource_ranking=["cache", "db", "gpu"])
    d2 = VeyaOperatorCenter.dispatch(p, payment_rule="vcg",
                                     resource_ranking=["cache", "db", "gpu"])
    assert d1["decision_id"] == d2["decision_id"]
    assert d1["replay_key"] == d2["replay_key"]
    assert d1["payments"]["payments"] == d2["payments"]["payments"]
