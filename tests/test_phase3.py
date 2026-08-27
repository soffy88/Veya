"""Phase 3 测试 — 反脆弱闭环决策与在线因果策略演化。

门禁(按用户 Spec 第三条):
  1. 效用选择必须在已知 ΔP 下稳定选出正确最优动作;
  2. 在线更新后, 同一因果结构下的预测 P(fault) 必须向真实观测收敛;
  3. 闭环事务在「可修复故障」场景下, 干预后失败率显著下降;
  4. 连续敌对信号后自动 quarantined(阈值可配)。
"""

from __future__ import annotations

import json
import random

import pytest

from veya.platform import omodul as load_omodul
from veya.platform import oprim as load_oprim
from veya.platform import oskill as load_oskill

omodul = load_omodul()
oprim = load_oprim()
oskill = load_oskill()


# =========================================================================
# 一、最优干预选择 (期望效用)
# =========================================================================


def test_utility_selection_picks_correct_optimal():
    """已知 ΔP: 高 ΔP 且低成本者胜出。"""
    cands = [
        oprim.InterventionCandidate("restart_db", delta_p=0.30, cost=0.5, risk=0.1),
        oprim.InterventionCandidate("scale_up", delta_p=0.25, cost=0.2, risk=0.05),
        oprim.InterventionCandidate("flush_cache", delta_p=0.10, cost=0.05, risk=0.01),
    ]
    r = oprim.select_intervention(cands, lambda_cost=1.0, risk_aversion=1.0)
    # U: restart=0.30-0.5-0.1=-0.3; scale=0.25-0.2-0.05=0.0; flush=0.10-0.05-0.01=0.04
    assert r.best.action_id == "flush_cache"
    assert [c.action_id for c, _ in r.ranked] == ["flush_cache"]


def test_high_delta_high_cost_is_suppressed():
    """高 ΔP 但高成本 → 被 λ 压制。"""
    cands = [
        oprim.InterventionCandidate("cheap_but_small", delta_p=0.20, cost=0.02, risk=0.01),
        oprim.InterventionCandidate("expensive_but_big", delta_p=0.60, cost=0.9, risk=0.1),
    ]
    r = oprim.select_intervention(cands, lambda_cost=1.0)
    # U: cheap=0.20-0.02-0.01=0.17; expensive=0.60-0.9-0.1=-0.4
    assert r.best.action_id == "cheap_but_small"
    assert [c.action_id for c, _ in r.rejected] == ["expensive_but_big"]
    # 降低 λ → 高 ΔP 动作翻盘 (成本敏感性的显式权衡)
    r2 = oprim.select_intervention(cands, lambda_cost=0.1)
    assert r2.best.action_id == "expensive_but_big"


def test_negative_utility_dropped_no_least_bad():
    cands = [
        oprim.InterventionCandidate("bad", delta_p=0.05, cost=0.5, risk=0.2),
        oprim.InterventionCandidate("worse", delta_p=-0.1, cost=0.1, risk=0.1),
    ]
    r = oprim.select_intervention(cands)
    assert r.best is None  # 不输出 least-bad
    assert len(r.rejected) == 2


def test_selection_deterministic_and_order_independent():
    cands = [
        oprim.InterventionCandidate("a", delta_p=0.2, cost=0.0, risk=0.0),
        oprim.InterventionCandidate("b", delta_p=0.2, cost=0.0, risk=0.0),
    ]
    r1 = oprim.select_intervention(list(reversed(cands)))
    r2 = oprim.select_intervention(cands)
    assert r1.best.action_id == r2.best.action_id == "a"  # 平局 → id 字典序


def test_from_diagnosis_report_aliases():
    """Phase 2 诊断报告 → 候选转换 (容错键名)。"""
    report = {
        "candidates": [
            {"id": "restart", "ΔP": 0.3, "cost": 0.5, "risk": 0.1},
            {"action_id": "scale", "delta_p_success": 0.25, "cost": 0.2},
            {"name": "no_delta", "cost": 0.1},  # 无 ΔP → 跳过
        ],
    }
    cands = oprim.from_diagnosis_report(report)
    assert [c.action_id for c in cands] == ["restart", "scale"]
    assert cands[0].delta_p == 0.3
    # 也支持 interventions 键
    r2 = oprim.from_diagnosis_report({"interventions": [{"id": "x", "delta_p": 0.1}]})
    assert r2[0].action_id == "x"


# =========================================================================
# 二、在线 CPD 更新 (Dirichlet / EMA)
# =========================================================================


def test_dirichlet_update_converges_to_truth():
    """500 次真实观测 (P(fault)=0.3) → P(fault) 收敛到 ~0.3。"""
    rng = random.Random(42)
    cpd = oskill.CategoricalCPD.uniform(["success", "fault"], parents=["mode"])
    for _ in range(500):
        fault = rng.random() < 0.3
        cpd = oskill.dirichlet_update(cpd, "degraded", "fault" if fault else "success")
    p = cpd.p_fault("degraded")
    assert 0.25 <= p <= 0.35, f"P(fault)={p} 未收敛到 0.3"
    # 均匀先验: 伪计数 strength=1, 首次成功观测后 P(fault)=1/3
    cpd0 = oskill.CategoricalCPD.uniform(["success", "fault"])
    cpd0 = oskill.dirichlet_update(cpd0, "degraded", "success")
    assert cpd0.p_fault("degraded") == pytest.approx(1 / 3)
    with pytest.raises(KeyError):
        oskill.CategoricalCPD.uniform(["success", "fault"]).p_fault("ghost")


def test_cpd_column_isolation():
    """列级更新: 更新 degraded 行不影响 healthy 行。"""
    cpd = oskill.CategoricalCPD(
        child_states=["success", "fault"],
        counts={"degraded": [3.0, 7.0], "healthy": [9.0, 1.0]},
        parents=["mode"],
    )
    cpd2 = oskill.dirichlet_update(cpd, "degraded", "success", strength=10.0)
    assert cpd2.p_fault("healthy") == pytest.approx(0.1)  # 未动
    assert cpd2.p_fault("degraded") < cpd.p_fault("degraded")  # 该行已更新


def test_ema_tracks_drift_faster():
    """概念漂移: 环境 P(fault) 从 0.2 翻转到 0.8, EMA 比 Dirichlet 更快跟手。"""
    rng = random.Random(7)
    d = oskill.CategoricalCPD.uniform(["success", "fault"])
    e = oskill.CategoricalCPD.uniform(["success", "fault"])
    # 阶段 1: 200 次 P(fault)=0.2
    for _ in range(200):
        fault = rng.random() < 0.2
        d = oskill.dirichlet_update(d, "m", "fault" if fault else "success", strength=1.0)
        e = oskill.ema_update(e, "m", "fault" if fault else "success", alpha=0.1)
    # 阶段 2: 100 次 P(fault)=0.8 (漂移)
    for _ in range(100):
        fault = rng.random() < 0.8
        d = oskill.dirichlet_update(d, "m", "fault" if fault else "success", strength=1.0)
        e = oskill.ema_update(e, "m", "fault" if fault else "success", alpha=0.1)
    p_d, p_e = d.p_fault("m"), e.p_fault("m")
    assert p_e > p_d  # EMA 对漂移响应更快
    assert 0.3 < p_d < 0.8 and 0.5 < p_e < 0.9


def test_cpd_json_roundtrip():
    cpd = oskill.CategoricalCPD(
        child_states=["success", "fault"], counts={"degraded": [3.0, 7.0]}, parents=["mode"]
    )
    restored = oskill.CategoricalCPD.from_dict(json.loads(json.dumps(cpd.to_dict())))
    assert restored.p_fault("degraded") == pytest.approx(0.7)
    assert restored.child_states == ["success", "fault"]


# =========================================================================
# 三、闭环事务: 可修复故障 → 失败率显著下降
# =========================================================================


class FaultyEnv:
    """真环境: degraded 下 P(fault)=0.7, healthy 下 P(fault)=0.1。

    apply_prob < 1 模拟「干预并非总能生效」—— 部分轮次系统仍停留在 degraded,
    让 degraded 行也获得真实观测 (否则因果上永远看不到 degraded —— 被阻断的
    配置不产生数据, 这是正确的因果学习行为)。
    """

    def __init__(self, seed: int = 0, apply_prob: float = 0.85):
        self.rng = random.Random(seed)
        self.calls: list[str] = []
        self.apply_prob = apply_prob

    def __call__(self, action_id: str, target_value: str) -> dict:
        self.calls.append(action_id)
        effective = target_value if self.rng.random() < self.apply_prob else "degraded"
        p_fault = 0.7 if effective == "degraded" else 0.1
        # 返回实现态: 干预未生效时系统实际停留在 degraded
        return {"success": self.rng.random() >= p_fault, "parent_config": effective}


@pytest.mark.asyncio
async def test_closed_loop_failure_rate_drops(tmp_path):
    """闭环 300 轮: 模型学会选 healthy → 执行失败率显著下降 + CPD 向真相收敛。"""
    env = FaultyEnv(seed=11)
    # 初始 CPD: 轻微偏置的先验 (未完全知道真相)
    cpd = oskill.CategoricalCPD(
        child_states=["success", "fault"],
        counts={"degraded": [4.0, 6.0], "healthy": [8.0, 2.0]},
        parents=["mode"],
    )

    # 显式干预候选: do(mode=healthy) / do(mode=degraded), ΔP 交给 CPD 现算
    interventions = [
        {"action_id": "do_mode=healthy", "target_value": "healthy", "cost": 0.1, "risk": 0.0},
        {"action_id": "do_mode=degraded", "target_value": "degraded", "cost": 0.0, "risk": 0.0},
    ]
    for _ in range(300):
        result = await _run_closed_loop(tmp_path, cpd, interventions, env, rounds=1)
        cpd = oskill.CategoricalCPD.from_dict(result["cpd_after"])  # 在线积累

    assert result["executed_failure_rate"] < 0.3  # 显著低于不干预的 0.7
    # CPD 向真实频率收敛
    assert abs(cpd.p_fault("degraded") - 0.7) < 0.08
    assert abs(cpd.p_fault("healthy") - 0.1) < 0.08
    # 模型学会了选 healthy (绝大多数轮次执行的是健康干预)
    assert env.calls.count("do_mode=healthy") > env.calls.count("do_mode=degraded") * 5


async def _run_closed_loop(tmp_path, cpd, interventions, env, rounds=1):
    """直接走 omodul 事务 (每轮独立临时输出目录)。"""
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    inp = omodul.ClosedLoopInput(cpd, interventions=interventions)
    cfg = omodul.ClosedLoopConfig(
        simulate=False, execute_fn=env, rounds=rounds, baseline_config="degraded"
    )
    return await omodul.closed_loop_intervene(cfg, inp, out / f"r{len(list(out.iterdir()))}")


@pytest.mark.asyncio
async def test_closed_loop_no_action_when_no_positive_utility(tmp_path):
    """两个配置观测到的 P(fault) 相同 → 所有 ΔP≈0 → 无正效用 → 不执行。"""
    cpd = oskill.CategoricalCPD(
        child_states=["success", "fault"],
        counts={"degraded": [1.0, 1.0], "healthy": [1.0, 1.0]},  # 都是 0.5
        parents=["mode"],
    )
    interventions = [
        {"action_id": "do_mode=healthy", "target_value": "healthy", "cost": 0.0, "risk": 0.0},
    ]
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    inp = omodul.ClosedLoopInput(cpd, interventions=interventions)
    cfg = omodul.ClosedLoopConfig(simulate=True, baseline_config="degraded")
    result = await omodul.closed_loop_intervene(cfg, inp, out)
    assert result["status"] == "no_action"


@pytest.mark.asyncio
async def test_closed_loop_from_diagnosis_report(tmp_path):
    """Phase 2 诊断报告直接驱动闭环 (ΔP 来自报告, 不经 CPD 现算)。"""
    cpd = oskill.CategoricalCPD.uniform(["success", "fault"])
    diagnosis = {
        "candidates": [
            {"id": "do_mode=healthy", "ΔP": 0.6, "cost": 0.1, "risk": 0.0},
            {"id": "do_mode=degraded", "ΔP": 0.0, "cost": 0.0, "risk": 0.0},
        ],
    }
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    inp = omodul.ClosedLoopInput(cpd, diagnosis=diagnosis)
    cfg = omodul.ClosedLoopConfig(simulate=True, rounds=10, seed=3, baseline_config="degraded")
    result = await omodul.closed_loop_intervene(cfg, inp, out)
    assert result["status"] == "executed"
    assert len(result["executed"]) == 10
    # 全部选择 healthy (ΔP=0.6 唯一正效用)
    assert all(e["action_id"] == "do_mode=healthy" for e in result["executed"])
    assert result["fingerprint"]
    # 报告与持久化 CPD 落盘
    assert (out / "cpd_after.json").exists()
    assert result["report_path"]


# =========================================================================
# 四、威胁模型演化: 蜜罐信号 → 后验 → 隔离
# =========================================================================


@pytest.mark.asyncio
async def test_hostile_signals_lead_to_quarantine(tmp_path):
    """连续蜜罐敌对信号 → P(hostile) 攀升 → 自动 quarantined。"""
    signals = [
        {"kind": "probe", "severity": 0.8},
        {"kind": "credential_stuffing", "severity": 0.9},
        {"kind": "payload_injection", "severity": 0.9},
    ]
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    inp = omodul.ThreatModelInput(signals, entity="attacker-42")
    cfg = omodul.ThreatModelConfig(quarantine_threshold=0.7)
    r = await omodul.threat_model_evolve(cfg, inp, out)
    assert r["status"] == "quarantined"
    assert r["quarantined"] is True
    assert r["hostile_prob"] >= 0.7
    # 后验随信号序列单调攀升 (信号轨迹断言)
    hostile_trail = [s["posterior"][2] for s in r["signal_trail"]]
    assert hostile_trail == sorted(hostile_trail)


@pytest.mark.asyncio
async def test_benign_signals_stay_monitoring(tmp_path):
    signals = [{"kind": "benign_activity", "severity": 1.0}] * 5
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    inp = omodul.ThreatModelInput(signals, entity="normal-user")
    cfg = omodul.ThreatModelConfig(quarantine_threshold=0.7)
    r = await omodul.threat_model_evolve(cfg, inp, out)
    assert r["status"] == "monitoring"
    assert r["quarantined"] is False
    assert r["hostile_prob"] < 0.1


@pytest.mark.asyncio
async def test_threat_profile_persistence_roundtrip(tmp_path):
    """后验持久化 → 下一次作为先验 (威胁记忆闭环)。"""
    profile = tmp_path / "profile.json"
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)

    inp1 = omodul.ThreatModelInput(
        [{"kind": "exfiltration", "severity": 0.9}], entity="e1", profile_path=str(profile)
    )
    r1 = await omodul.threat_model_evolve(
        omodul.ThreatModelConfig(quarantine_threshold=0.9), inp1, out
    )
    assert (profile).exists()
    saved = json.loads(profile.read_text(encoding="utf-8"))
    assert saved["posterior"] == r1["posterior"]

    # 第二次: 不传 prior → 从画像读旧后验, 一个弱信号就能推到更高
    inp2 = omodul.ThreatModelInput(
        [{"kind": "probe", "severity": 0.6}], entity="e1", profile_path=str(profile)
    )
    r2 = await omodul.threat_model_evolve(
        omodul.ThreatModelConfig(quarantine_threshold=0.9), inp2, out
    )
    assert r2["hostile_prob"] > r1["hostile_prob"]  # 记忆让判断更锐利


@pytest.mark.asyncio
async def test_quarantine_threshold_configurable(tmp_path):
    """阈值可配: 同一串信号, 低阈值隔离、高阈值监控。"""
    signals = [{"kind": "probe", "severity": 1.0}, {"kind": "honeypot_trigger", "severity": 1.0}]
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    low = await omodul.threat_model_evolve(
        omodul.ThreatModelConfig(quarantine_threshold=0.5), omodul.ThreatModelInput(signals), out
    )
    high = await omodul.threat_model_evolve(
        omodul.ThreatModelConfig(quarantine_threshold=0.99), omodul.ThreatModelInput(signals), out
    )
    assert low["quarantined"] is True
    assert high["quarantined"] is False
