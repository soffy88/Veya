"""Phase 3 行为测试矩阵 — 期望效用边界 / CPD 更新收敛 / 审计链路回放。

与 test_phase3_loop.py 的区别: 那里验证"闭环能跑", 这里验证"数值与语义精确":
  1. 效用公式 — U = ΔP − λ·C − ρ·risk 精确数值;
  2. 权衡边界 — λ 偏好低成本, ρ 惩罚高风险, 并列确定性 tiebreak;
  3. drop_negative — u==0 恰好被丢弃的边界语义;
  4. CPD 收敛 — Dirichlet 平滑不落 0/1, EMA 收敛, strength 语义, version 单调;
  5. 审计 — trace 隔离, replay 确定性, Memory/Jsonl 一致性, 派发链路事件序。
"""

from __future__ import annotations

import pytest

from veya_loop import (
    AuditEmitter,
    CategoricalCPD,
    InterventionCandidate,
    JsonlSink,
    MemorySink,
    PermissionContract,
    dispatch_intervention,
    expected_utility,
    select_intervention,
    update_cpd,
)

# =========================================================================
# 1. 期望效用: 精确公式 + 权衡边界
# =========================================================================

def test_expected_utility_exact_formula():
    c = InterventionCandidate("a", delta_p=0.5, cost=0.2, risk=0.1)
    # U = ΔP − λ·C − ρ·risk
    assert expected_utility(c, lambda_cost=1.0, risk_aversion=1.0) == pytest.approx(0.2)
    assert expected_utility(c, lambda_cost=2.0, risk_aversion=1.0) == pytest.approx(0.0)
    assert expected_utility(c, lambda_cost=1.0, risk_aversion=5.0) == pytest.approx(-0.2)


def test_lambda_prefers_cheap_when_high():
    cheap = InterventionCandidate("cheap", delta_p=0.3, cost=0.01)
    strong = InterventionCandidate("strong", delta_p=0.6, cost=0.5)
    # λ 大: 成本权重压过收益
    sel = select_intervention([strong, cheap], lambda_cost=10.0)
    assert sel.best is not None and sel.best.action_id == "cheap"
    # λ 小: 高收益胜出
    sel2 = select_intervention([strong, cheap], lambda_cost=0.1)
    assert sel2.best is not None and sel2.best.action_id == "strong"


def test_risk_aversion_penalizes_risky():
    safe = InterventionCandidate("safe", delta_p=0.4, cost=0.1, risk=0.0)
    risky = InterventionCandidate("risky", delta_p=0.5, cost=0.1, risk=1.0)
    sel = select_intervention([risky, safe], risk_aversion=5.0)
    assert sel.best is not None and sel.best.action_id == "safe"
    sel2 = select_intervention([risky, safe], risk_aversion=0.0)
    assert sel2.best is not None and sel2.best.action_id == "risky"


def test_selection_tiebreak_deterministic():
    """并列效用 → ΔP 降序 → cost 升序 → id 字典序; 同输入同输出。"""
    c1 = InterventionCandidate("b", delta_p=0.5, cost=0.2)
    c2 = InterventionCandidate("a", delta_p=0.5, cost=0.2)
    c3 = InterventionCandidate("c", delta_p=0.5, cost=0.1)
    r1 = select_intervention([c1, c2, c3], drop_negative=False)
    r2 = select_intervention([c2, c3, c1], drop_negative=False)   # 乱序输入
    assert [c.action_id for c, _ in r1.ranked] == \
           [c.action_id for c, _ in r2.ranked], "排序必须与输入顺序无关"
    # 效用相同: 按 cost 升序 → c 排第一
    assert r1.ranked[0][0].action_id == "c"


def test_drop_negative_boundary_at_zero():
    """u == 0 恰好被丢弃 (drop_negative 用 u > 0 判定)。"""
    zero = InterventionCandidate("zero", delta_p=0.2, cost=0.2)   # u = 0.0
    pos = InterventionCandidate("pos", delta_p=0.3, cost=0.1)
    sel = select_intervention([zero, pos], drop_negative=True)
    assert sel.best is not None and sel.best.action_id == "pos"
    assert [c.action_id for c, _ in sel.rejected] == ["zero"]
    # drop_negative=False: zero 进入 ranked
    sel2 = select_intervention([zero], drop_negative=False)
    assert len(sel2.ranked) == 1 and sel2.ranked[0][0].action_id == "zero"


def test_selection_empty_and_all_negative():
    assert select_intervention([]).best is None
    bad = [InterventionCandidate("x", delta_p=0.1, cost=1.0),   # u = -0.9
           InterventionCandidate("y", delta_p=0.0, cost=0.5)]   # u = -0.5
    sel = select_intervention(bad)
    assert sel.best is None
    assert len(sel.rejected) == 2


# =========================================================================
# 2. CPD 更新: 收敛 / 平滑 / strength / version
# =========================================================================

def test_dirichlet_smoothing_never_reaches_01():
    """Dirichlet 先验平滑: 全是 fault 观测, p_fault 也 < 1 (不落极端)。"""
    cpd = CategoricalCPD.uniform(["success", "fault"], parents=["mode"])
    for _ in range(2000):
        cpd = update_cpd(cpd, "degraded", "fault")
    p = cpd.p_fault("degraded")
    assert 0.95 < p < 1.0, f"Dirichlet 平滑应逼近但不到 1, 实际 {p}"


def test_ema_converges_and_alpha_bounds():
    """确定性交替序列 (f/s/f/s...) → EMA 收敛到 0.5 无噪声。

    注意: EMA 有效样本 ≈ 1/α, 随机序列断言容差必须 ≥ 统计噪声 σ≈0.15,
    因此用确定性序列验证收敛精确性。
    """
    cpd = CategoricalCPD.uniform(["success", "fault"], parents=["mode"])
    for i in range(2000):
        state = "fault" if i % 2 == 0 else "success"   # 确定性 50/50
        cpd = update_cpd(cpd, "degraded", state, mode="ema", alpha=0.1)
    # 交替序列的 EMA 稳态在两个相位间振荡: 0.5263 (fault 后) / 0.4737 (success 后)
    assert abs(cpd.p_fault("degraded") - 0.5) < 0.05
    # 全 fault 序列 → 单调逼近 1 (EMA 方向正确)
    cpd2 = CategoricalCPD.uniform(["success", "fault"], parents=["mode"])
    for _ in range(500):
        cpd2 = update_cpd(cpd2, "degraded", "fault", mode="ema", alpha=0.1)
    assert cpd2.p_fault("degraded") > 0.99
    with pytest.raises(ValueError):
        update_cpd(cpd, "degraded", "fault", mode="ema", alpha=0.0)   # α 必须 > 0
    with pytest.raises(ValueError):
        update_cpd(cpd, "degraded", "fault", mode="ema", alpha=1.5)


def test_strength_controls_update_magnitude():
    cpd = CategoricalCPD(child_states=["success", "fault"],
                         counts={"degraded": [50.0, 50.0]}, parents=["mode"])
    weak = update_cpd(cpd, "degraded", "success", strength=1.0)
    strong = update_cpd(cpd, "degraded", "success", strength=100.0)
    assert strong.p_fault("degraded") < weak.p_fault("degraded")
    # 强更新更接近极端 (50/50 → success 拉向高 success 概率)
    assert strong.p_fault("degraded") < 0.45
    assert weak.p_fault("degraded") > 0.45


def test_cpd_version_monotonic():
    cpd = CategoricalCPD.uniform(["s", "f"], parents=["m"])
    v = cpd.version
    for _ in range(10):
        cpd = update_cpd(cpd, "m", "s")
        assert cpd.version > v
        v = cpd.version


# =========================================================================
# 3. 审计链路: trace 隔离 / replay 确定性 / sink 一致性 / 派发事件序
# =========================================================================

def test_audit_trace_isolation():
    em = AuditEmitter(sink=MemorySink())
    em2 = AuditEmitter(sink=em.sink if hasattr(em, "sink") else None, trace_id="t2")
    em.decide(decision={"chosen_strategy": "a"})
    em2.decide(decision={"chosen_strategy": "b"})
    assert len(em.replay()) == 1
    assert em.replay()[0]["decision"]["chosen_strategy"] == "a"


def test_audit_replay_deterministic_order():
    em = AuditEmitter(sink=MemorySink())
    em.diagnose(inputs={"v": 1})
    em.decide(decision={"chosen_strategy": "x"})
    em.learn(learning={"v": 2})
    assert [e["event_type"] for e in em.replay()] == ["diagnose", "decide", "learn"]
    assert [e["event_type"] for e in em.replay()] == ["diagnose", "decide", "learn"]


def test_audit_memory_jsonl_consistency(tmp_path):
    mem = MemorySink()
    em = AuditEmitter(sink=mem)
    em.decide(decision={"chosen_strategy": "s"}, context={"notes": "n"})
    em.execute(execution={"primitive": "p", "status": "ok"})

    js = JsonlSink(str(tmp_path / "a.jsonl"))
    em2 = AuditEmitter(sink=js)
    em2.decide(decision={"chosen_strategy": "s"}, context={"notes": "n"})
    em2.execute(execution={"primitive": "p", "status": "ok"})

    mem_events = mem.read_trace(em.trace_id)
    js_events = js.read_trace(em2.trace_id)
    assert len(mem_events) == len(js_events) == 2
    for m, j in zip(mem_events, js_events, strict=True):
        assert m["event_type"] == j["event_type"]
        assert m["decision"] == j["decision"]


def test_dispatch_audit_chain_event_order():
    """dispatch_intervention 挂审计: 授权 → decide → execute, 事件序 + nonce 对应。"""
    contract = PermissionContract()
    contract.grant("do:*")
    sink = MemorySink()
    em = AuditEmitter(sink=sink)

    # 允许路径 (无执行器 → 仅派发); action 命名规范: 前缀:目标
    res = dispatch_intervention("do:reboot", ["echo", "ok"],
                                contract=contract, emitter=em)
    assert res.status == "approved_dispatched" and res.nonce
    events = em.replay()
    assert [e["event_type"] for e in events] == ["decide", "execute"]
    ex = events[1]["execution"]
    assert ex["primitive"] == "do:reboot"
    assert ex["capability_nonce"] == res.nonce          # 审计与派发 nonce 一致

    # 拒绝路径: 只写 decide (denied), 不写 execute
    sink2 = MemorySink()
    em2 = AuditEmitter(sink=sink2)
    res2 = dispatch_intervention("danger:drop", ["rm", "-rf", "/"],
                                 contract=contract, emitter=em2)
    assert res2.status == "denied"
    ev2 = em2.replay()
    assert [e["event_type"] for e in ev2] == ["decide"]
    assert ev2[0]["decision"]["denied"] is True
