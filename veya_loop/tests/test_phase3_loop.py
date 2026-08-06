"""Phase 3 完备性门禁 — 反脆弱闭环 / 在线因果更新 / 威胁演化 / 决策审计。"""

from __future__ import annotations

import random

import pytest

from veya_loop import (
    AuditEmitter,
    CategoricalCPD,
    JsonlSink,
    closed_loop_intervene,
    threat_model_evolve,
    update_cpd,
)

# =========================================================================
# 在线 CPD 更新: 收敛 + 列级隔离
# =========================================================================

def test_cpd_dirichlet_converges_and_version_bumps():
    rng = random.Random(42)
    cpd = CategoricalCPD.uniform(["success", "fault"], parents=["mode"])
    for _ in range(500):
        fault = rng.random() < 0.3
        cpd = update_cpd(cpd, "degraded", "fault" if fault else "success")
    assert 0.25 <= cpd.p_fault("degraded") <= 0.35
    assert cpd.version == 501                    # 每次更新自增 (审计原料)


def test_cpd_column_isolation():
    cpd = CategoricalCPD(child_states=["success", "fault"],
                         counts={"degraded": [3.0, 7.0], "healthy": [9.0, 1.0]},
                         parents=["mode"])
    cpd2 = update_cpd(cpd, "degraded", "success", strength=10.0)
    assert cpd2.p_fault("healthy") == pytest.approx(0.1)      # 未动
    assert cpd2.p_fault("degraded") < cpd.p_fault("degraded")  # 该行已更新


# =========================================================================
# 闭环事务: 可修复故障 → 失败率显著下降
# =========================================================================

class FaultyEnv:
    """真环境: degraded 下 P(fault)=0.7, healthy 下 P(fault)=0.1, 干预 85% 生效。"""

    def __init__(self, seed: int = 0, apply_prob: float = 0.85):
        self.rng = random.Random(seed)
        self.apply_prob = apply_prob

    def __call__(self, action_id: str, target_value: str) -> dict:
        effective = target_value if self.rng.random() < self.apply_prob else "degraded"
        p_fault = 0.7 if effective == "degraded" else 0.1
        return {"success": self.rng.random() >= p_fault, "parent_config": effective}


@pytest.mark.asyncio
async def test_closed_loop_failure_rate_drops(tmp_path):
    from veya_loop import ClosedLoopConfig, ClosedLoopInput

    env = FaultyEnv(seed=11)
    cpd = CategoricalCPD(child_states=["success", "fault"],
                         counts={"degraded": [4.0, 6.0], "healthy": [8.0, 2.0]},
                         parents=["mode"])
    interventions = [
        {"action_id": "do_mode=healthy", "target_value": "healthy", "cost": 0.1, "risk": 0.0},
        {"action_id": "do_mode=degraded", "target_value": "degraded", "cost": 0.0, "risk": 0.0},
    ]
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)

    for _ in range(300):
        inp = ClosedLoopInput(cpd, interventions=interventions)
        cfg = ClosedLoopConfig(simulate=False, execute_fn=env, rounds=1,
                               baseline_config="degraded")
        result = await closed_loop_intervene(cfg, inp, out)
        cpd = CategoricalCPD.from_dict(result["cpd_after"])

    assert result["executed_failure_rate"] < 0.3          # 显著低于不干预的 0.7
    assert abs(cpd.p_fault("degraded") - 0.7) < 0.08      # 双向收敛
    assert abs(cpd.p_fault("healthy") - 0.1) < 0.08


# =========================================================================
# 威胁模型演化: 蜜罐信号 → 后验 → 隔离
# =========================================================================

@pytest.mark.asyncio
async def test_threat_model_quarantine_and_threshold(tmp_path):
    from veya_loop import ThreatModelConfig, ThreatModelInput

    signals = [
        {"kind": "probe", "severity": 0.8},
        {"kind": "credential_stuffing", "severity": 0.9},
        {"kind": "payload_injection", "severity": 0.9},
    ]
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    r = await threat_model_evolve(ThreatModelConfig(quarantine_threshold=0.7),
                                  ThreatModelInput(signals, entity="attacker-42"), out)
    assert r["status"] == "quarantined"
    assert r["hostile_prob"] >= 0.7
    # 后验随信号序列单调攀升
    hostile_trail = [s["posterior"][2] for s in r["signal_trail"]]
    assert hostile_trail == sorted(hostile_trail)


# =========================================================================
# 决策审计: 统一 Schema + 链路回放
# =========================================================================

def test_audit_emitter_full_chain(tmp_path):
    sink = JsonlSink(str(tmp_path / "audit.jsonl"))
    em = AuditEmitter(sink=sink, trace_id="trace-42")
    em.diagnose(inputs={"graph_version": 3, "cpd_version": 5, "threat_level": 0.12})
    em.decide(decision={"chosen_strategy": "aggressive_repair",
                        "utilities": {"do(x)": 0.21}})
    em.execute(execution={"primitive": "circuit_break", "status": "ok",
                          "capability_nonce": "cap_abc"})
    em.learn(learning={"cpd_version_after": 6})

    chain = em.replay()
    assert [e["event_type"] for e in chain] == ["diagnose", "decide", "execute", "learn"]
    assert all(e["trace_id"] == "trace-42" for e in chain)
    # JSONL 可独立回读
    events = JsonlSink(str(tmp_path / "audit.jsonl")).read_trace("trace-42")
    assert len(events) == 4
    assert events[0]["inputs"]["cpd_version"] == 5


def test_audit_event_type_whitelist():
    from veya_loop import AuditEvent

    with pytest.raises(ValueError):
        AuditEvent(event_type="hack", trace_id="t")
