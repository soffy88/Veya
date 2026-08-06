"""P1 神经符号能力面行为测试 (经 veya_loop 装配面调用主库机制)。

覆盖: 四闸门 (校验/编译/可满足/MUS) · 分配+VCG · 死锁 · 博弈 · PUCT · 快照。
守护目标: 装配不只是"符号可达", 机制链路在真实输入下可跑通。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import veya_loop as v

# ---------------------------------------------------------------------------
# O1 四闸门: 校验 → 编译 → MaxSMT 可满足 → MUS
# ---------------------------------------------------------------------------

def _ir(vars_: list[dict], constraints: list[dict]) -> dict:
    return {"version": "o1.ir/v1", "intent": "test",
            "vars": vars_, "constraints": constraints}


def test_gate_validate_compile_feasible() -> None:
    ir = v.parse_ir(_ir(
        [{"name": "x", "type": "int", "lo": 0, "hi": 10}],
        [{"id": "c1", "kind": "hard", "intent": "x>=1",
          "expr": {"op": ">=", "args": [{"var": "x"}, {"lit": 1}]}}]))
    assert v.validate(ir) == []
    compiled = v.compile_ir(ir)
    feas = v.check_feasible(compiled)
    assert feas.status == "sat"


def test_gate_unsat_detected() -> None:
    ir = v.parse_ir(_ir(
        [{"name": "x", "type": "int", "lo": 0, "hi": 10}],
        [{"id": "c1", "kind": "hard", "intent": "x<=0",
          "expr": {"op": "<=", "args": [{"var": "x"}, {"lit": 0}]}},
         {"id": "c2", "kind": "hard", "intent": "x>=1",
          "expr": {"op": ">=", "args": [{"var": "x"}, {"lit": 1}]}}]))
    assert v.validate(ir) == []
    feas = v.check_feasible(v.compile_ir(ir))
    assert feas.status == "unsat"


def test_gate_mus_shrink() -> None:
    """核心 {a,b,c}: 仅当 a∧b 同时在场时不可满足 → MUS 必须收缩到 {a,b}。"""
    core = ["a", "b", "c"]

    def oracle(subset: list[str]) -> str:
        return "unsat" if {"a", "b"} <= set(subset) else "sat"

    res = v.shrink_to_mus(oracle, core)
    assert sorted(res.mus) == ["a", "b"]


def test_gate_backtranslate_render() -> None:
    ir = v.parse_ir(_ir(
        [{"name": "x", "type": "int", "lo": 0, "hi": 10}],
        [{"id": "c1", "kind": "hard", "intent": "x>=1",
          "expr": {"op": ">=", "args": [{"var": "x"}, {"lit": 1}]}}]))
    # 回译闸门: 意图 ↔ 渲染相似度比对 (diff_all 内部处理 var_map)
    reports = v.diff_all(ir)
    assert reports and all(0.0 <= r.similarity <= 1.0 for r in reports)


# ---------------------------------------------------------------------------
# O2 分配 + VCG 支付 + 策略证明
# ---------------------------------------------------------------------------

def _problem() -> v.Problem:
    return v.Problem(
        tasks=[v.Task("t1", {"s1": 1.0}), v.Task("t2", {"s1": 1.0})],
        workers=[v.Worker("a", {"s1": 5.0}), v.Worker("b", {"s1": 4.0})],
        bids=[v.Bid("a", "t1", 0.2), v.Bid("a", "t2", 0.2),
              v.Bid("b", "t1", 0.25), v.Bid("b", "t2", 0.25)],
    )


def test_allocate_vcg_welfare() -> None:
    p = _problem()
    alloc = v.assign_one_to_one(p)
    assert alloc is not None
    assert alloc.pairs, "分配结果为空 (检查 bids 报价构造)"
    assert len(alloc.pairs) == 2
    assert v.welfare(p, alloc) > 0.0
    pay = v.vcg(p, alloc)
    assert pay is not None
    # VCG 支付非负 (无外部性下)
    assert all(x >= 0.0 for x in pay.payments.values())


def test_vcg_strategyproof_check() -> None:
    p = _problem()
    alloc = v.assign_one_to_one(p)
    report = v.check_strategyproof(p, v.vcg, allocator=v.assign_one_to_one)
    assert report is not None
    assert report.manipulable is False  # VCG 下真实报价是弱占优


# ---------------------------------------------------------------------------
# O2 死锁: 等待图环 + 预检 + 受害者选择
# ---------------------------------------------------------------------------

def test_deadlock_cycle_and_precheck() -> None:
    wfg = v.WaitForGraph()
    wfg.add_wait("p1", "p2", resource="r1")
    wfg.add_wait("p2", "p1", resource="r2")
    assert wfg.cycles() == [["p1", "p2"]]
    # 预检: 加边 p2→p1 会成环; 加边 p3→p1 不会
    assert wfg.would_deadlock("p2", "p1") is True
    assert wfg.would_deadlock("p3", "p1") is False
    # 受害者: 代价最小者
    assert wfg.victim(["p1", "p2"], cost={"p1": 9.0, "p2": 1.0}) == "p2"


# ---------------------------------------------------------------------------
# 博弈论: 纯纳什 / 囚徒困境
# ---------------------------------------------------------------------------

def test_pure_nash_prisoners_dilemma() -> None:
    g = v.prisoners_dilemma()
    nash = v.pure_nash(g)
    assert nash, "囚徒困境必须存在纯纳什均衡"
    i, j = nash[0]
    # 经典结果: 双双背叛 (最后一格)
    assert (i, j) == (g.A.shape[0] - 1, g.A.shape[1] - 1)


def test_pareto_vs_nash_report() -> None:
    g = v.prisoners_dilemma()
    text = v.nash_vs_pareto_report(g)
    assert isinstance(text, str) and text


# ---------------------------------------------------------------------------
# O3 沙箱推演: PUCT 搜索 (自实现 WorldModel) + 快照
# ---------------------------------------------------------------------------

@dataclass
class CountModel:
    """世界: 状态是整数; 动作 +1 (先验 0.7) 或 +2 (先验 0.3); 奖励 = 状态/10。"""

    def key(self, state: int):
        return state

    def actions(self, state: int):
        return [(1, 0.7), (2, 0.3)]

    def step(self, state: int, action: int) -> int:
        return state + action

    def terminal(self, state: int) -> bool:
        return state >= 8

    def reward(self, state: int) -> float:
        return min(1.0, state / 10.0)


def test_puct_search_finds_path() -> None:
    mcts = v.MCTS(CountModel(), c_puct=1.4, seed=0)
    action = mcts.search(0, budget=64)
    assert action in (1, 2)
    path = v.best_path(mcts, 0, max_len=8)
    assert path and all(a in (1, 2) for a in path)


def test_snapshot_commit_checkout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        (work / "sub").mkdir(parents=True)
        (work / "f1.txt").write_text("hello", encoding="utf-8")
        (work / "sub" / "f2.txt").write_text("world", encoding="utf-8")

        store = v.SnapshotStore(str(Path(tmp) / "snaps"))
        digest = store.commit(str(work))
        assert store.exists(digest)

        # 修改后 digest 变化; checkout 恢复原状
        (work / "f1.txt").write_text("changed", encoding="utf-8")
        assert store.commit(str(work)) != digest

        restore = Path(tmp) / "restore"
        store.checkout(digest, str(restore))
        assert (restore / "f1.txt").read_text(encoding="utf-8") == "hello"
        assert (restore / "sub" / "f2.txt").read_text(encoding="utf-8") == "world"


# ---------------------------------------------------------------------------
# P3 期望效用: drop_negative 语义
# ---------------------------------------------------------------------------

def test_expected_utility_drop_negative() -> None:
    cands = [v.InterventionCandidate("cheap", delta_p=0.5, cost=0.1),
             v.InterventionCandidate("wasteful", delta_p=0.3, cost=10.0)]
    sel = v.select_intervention(cands, lambda_cost=1.0, drop_negative=True)
    assert sel.best is not None and sel.best.action_id == "cheap"
    assert sel.rejected, "负效用候选必须进入 rejected"

    sel2 = v.select_intervention(cands, lambda_cost=1.0, drop_negative=False)
    assert sel2.best is not None and sel2.best.action_id == "cheap"
    assert len(sel2.ranked) == 2
