"""T4/T5/T7: causal 规划/诊断 + 审计关联（SPEC §11）。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_t4_plan_goal_returns_report(client):
    """plan/goal → ranked_actions + trace_id（mock store 可用即通过）。"""
    r = await client.post(
        "/v1/loop/plan/goal",
        json={
            "goal": "完成支付模块",
            "criteria": "支持 3 种支付方式, 全链路测试通过",
        },
    )
    if r.status_code == 503:
        pytest.skip("veya_loop 因果符号不可用 (VEYA_LOOP_OPTIONAL)")
    assert r.status_code == 200
    report = r.json()
    assert report["trace_id"]
    assert isinstance(report["ranked_actions"], list)
    assert report["execute"] is False  # 默认不执行


@pytest.mark.asyncio
async def test_t5_diagnose_root_causes(client):
    """plan/diagnose → root_causes 结构合法。"""
    r = await client.post(
        "/v1/loop/plan/diagnose",
        json={
            "symptom": "用户登录偶发 500",
            "context": {"service": "auth", "window": "1h"},
        },
    )
    if r.status_code == 503:
        pytest.skip("veya_loop 因果符号不可用 (VEYA_LOOP_OPTIONAL)")
    assert r.status_code == 200
    report = r.json()
    assert isinstance(report["root_causes"], list)
    assert report["trace_id"]


@pytest.mark.asyncio
async def test_t7_audit_trace_correlation(client, audit):
    """审计文件含 plan 与 diagnose 行，trace_id 可关联。"""
    r = await client.post("/v1/loop/plan/goal", json={"goal": "审计目标"})
    if r.status_code == 503:
        pytest.skip("veya_loop 因果符号不可用")
    trace_id = r.json()["trace_id"]
    entries = audit.by_trace(trace_id)
    assert any(e["phase"] == "plan" for e in entries)
    assert entries[0]["decision_made"]["goal"] == "审计目标"


@pytest.mark.asyncio
async def test_audit_phases_validated(audit):

    with pytest.raises(ValueError):
        audit.append(phase="bogus", trace_id="x", decision_made={})


def test_plan_service_missing_veya_loop_raises():
    """无 veya_loop → 明确错误（503 语义）。"""
    import sys

    # 模拟 veya_loop 不可用: 从 sys.modules 摘除并拦截导入
    saved = sys.modules.get("veya_loop")
    sys.modules["veya_loop"] = None
    try:
        from app.domain.causal.service import plan_for_goal

        with pytest.raises(RuntimeError):
            plan_for_goal("目标")
    finally:
        if saved is not None:
            sys.modules["veya_loop"] = saved
