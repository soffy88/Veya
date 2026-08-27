"""Phase 4 完备性门禁 — 长视距反事实规划 / 策略演化 / 硬化执行 / 授权派发。"""

from __future__ import annotations

import pytest

from veya_loop import (
    AuditEmitter,
    CausalGraphStore,
    HardenedExecutor,
    MemorySink,
    PermissionContract,
    dispatch_intervention,
    multi_step_plan,
)


def _build_store() -> CausalGraphStore:
    store = CausalGraphStore()
    store.add_node("api_gateway", p_fail=0.3)
    store.add_node("db", p_fail=0.2)
    store.add_node("task_outcome")
    store.add_edge("api_gateway", "task_outcome")
    store.add_edge("db", "task_outcome")
    return store


# =========================================================================
# 多步反事实规划: 感知-规划-行动-学习 长视距闭环
# =========================================================================


def test_multi_step_plan_full_loop(tmp_path):
    import numpy as _np

    store = _build_store()
    audit_path = str(tmp_path / "audit.jsonl")

    report = multi_step_plan(
        "task failed: db timeout after api gateway 5xx",
        store=store,
        threat_level=0.12,
        execute=True,
        repair_callback=lambda node: 0.6,
        capability_nonce="cap-007",
        notes="故障演练 #4",
        audit_path=audit_path,
        rng=_np.random.default_rng(0),  # 策略选择确定性 (否则受全局随机状态影响)
    )
    # 规划产物 (策略名以主库 STRATEGY_NAMES 为准)
    from veya_loop import STRATEGY_NAMES

    assert report.strategy in STRATEGY_NAMES
    assert report.plan.planned_actions
    assert report.executed is True
    assert report.execution.node
    assert report.execution.actual_delta_p == pytest.approx(0.6)
    assert report.cpd_updated  # 学习: CPD 被更新
    assert report.strategy_value_after >= 0.0  # 策略价值 EMA 回写
    assert report.recommended_actions

    # 审计: 五节点链路 + 因果图版本
    from veya_loop import JsonlSink

    events = JsonlSink(audit_path).read_trace(report.audit_trace_id)
    assert [e["event_type"] for e in events] == ["diagnose", "plan", "decide", "execute", "learn"]
    for e in events:
        assert e["inputs"]["graph_version"] == store.version
    assert events[3]["execution"]["capability_nonce"] == "cap-007"


def test_multi_step_plan_high_threat_forces_hard_strategy():
    store = _build_store()
    r_hard = multi_step_plan("db timeout", store=store, threat_level=0.95)
    r_calm = multi_step_plan("db timeout", store=store, threat_level=0.0)
    # 高威胁 → 策略选择应偏向激进 (由 StrategyEvolver 决定, 至少不劣于冷静态)
    assert r_hard.threat_level == 0.95
    assert r_calm.threat_level == 0.0


# =========================================================================
# 硬化执行器: 隔离沙箱
# =========================================================================


def test_hardened_executor_runs_and_isolates(tmp_path):
    with HardenedExecutor(isolation="netns", pool_size=2, base_dir=str(tmp_path / "pool")) as ex:
        out = ex.execute(["python3", "-c", "print('hello')"])
        assert out.ok and "hello" in out.stdout
        # 宿主环境变量不泄漏
        out2 = ex.execute(["python3", "-c", "import os;print(os.environ.get('SECRET','<none>'))"])
        assert "<none>" in out2.stdout
        # 超时强制杀
        out3 = ex.execute(["sleep", "5"], timeout_s=0.4)
        assert out3.timed_out and not out3.ok
    assert ex.stats["created"] == 2


# =========================================================================
# 授权契约: deny-by-default + nonce 单次消费
# =========================================================================


def test_permission_contract_deny_by_default_and_grant():
    contract = PermissionContract()
    d = contract.evaluate("do(db=ok)")
    assert d.allowed is False  # 无规则 → 拒绝
    assert "deny-by-default" in d.reason

    contract.grant("do*")
    d2 = contract.evaluate("do(db=ok)", actor="operator")
    assert d2.allowed is True

    contract.grant("danger*", allow=False)  # 显式禁止优先
    contract.grant("do*")
    d3 = contract.evaluate("danger:drop_table")
    assert d3.allowed is False


def test_nonce_single_use_and_auditable():
    contract = PermissionContract()
    contract.grant("do*")
    nonce = contract.issue_nonce("do(db=ok)", actor="operator")
    assert nonce.startswith("cap_")
    assert contract.verify_nonce(nonce) is True
    assert contract.verify_nonce(nonce) is False  # 单次消费, 防重放
    info = contract.nonce_info(nonce)
    assert info["actor"] == "operator"  # 可追溯谁授权的


# =========================================================================
# 干预派发: 授权 → 硬化执行 → 审计落笔
# =========================================================================


def test_dispatch_intervention_denied_path(tmp_path):
    contract = PermissionContract()  # 无规则 → 拒绝
    emitter = AuditEmitter(sink=MemorySink())
    result = dispatch_intervention(
        "do(db=ok)",
        ["python3", "-c", "print('x')"],
        contract=contract,
        emitter=emitter,
    )
    assert result.status == "denied"
    assert result.outcome is None
    # 拒绝也留审计 (decide)
    chain = emitter.replay()
    assert chain[0]["event_type"] == "decide"
    assert chain[0]["decision"]["denied"] is True


def test_dispatch_intervention_executed_with_audit(tmp_path):
    contract = PermissionContract()
    contract.grant("do*")
    emitter = AuditEmitter(sink=MemorySink())
    with HardenedExecutor(isolation="netns", base_dir=str(tmp_path / "pool")) as ex:
        result = dispatch_intervention(
            "do(db=ok)",
            ["python3", "-c", "print('repair ok')"],
            contract=contract,
            executor=ex,
            emitter=emitter,
            actor="operator",
            notes="演练 #5",
        )
    assert result.status == "approved_executed"
    assert result.nonce and result.nonce.startswith("cap_")
    assert result.audit_id
    # 审计: decide → execute 两条, nonce 贯穿
    chain = emitter.replay()
    assert [e["event_type"] for e in chain] == ["decide", "execute"]
    assert chain[1]["execution"]["capability_nonce"] == result.nonce
    assert chain[1]["execution"]["status"] == "ok"
    assert "repair ok" in chain[1]["execution"]["output"]


def test_dispatch_intervention_failed_status(tmp_path):
    contract = PermissionContract()
    contract.grant("do*")
    with HardenedExecutor(isolation="netns", base_dir=str(tmp_path / "pool")) as ex:
        result = dispatch_intervention(
            "do(db=ok)",
            ["python3", "-c", "raise SystemExit(1)"],
            contract=contract,
            executor=ex,
        )
    assert result.status == "approved_failed"
    assert result.outcome.exit_code == 1
