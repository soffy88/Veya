"""G5: obase.authz — 权限规则引擎 + 交互式确认门测试。

覆盖：规则匹配（allow/deny/ask/通配）、三态决策、persona 默认规则、
gate 挂起/批准/拒绝/超时/待决列表、compat 单源委托。
"""

import asyncio

import pytest

from veya.obase.authz import (
    InteractivePermissionGate,
    PermissionDecision,
    evaluate_permission,
    match_permission_rule,
)


# ── 规则匹配 ──────────────────────────────────────────────────────────
def test_match_allow():
    assert match_permission_rule(["allow:*"], "bash") == "allow"
    assert match_permission_rule(["allow:read", "deny:write"], "read") == "allow"


def test_match_deny():
    assert match_permission_rule(["deny:write", "allow:*"], "write") == "deny"


def test_match_ask():
    assert match_permission_rule(["ask:bash"], "bash") == "ask"


def test_match_no_rule():
    assert match_permission_rule(["allow:read"], "bash") is None


def test_match_order_priority():
    # 先匹配先生效
    assert match_permission_rule(["deny:*", "allow:read"], "read") == "deny"


# ── 三态决策 ──────────────────────────────────────────────────────────
def test_evaluate_allow():
    r = evaluate_permission("read", persona="build")
    assert r["decision"] == PermissionDecision.ALLOW
    assert r["status"] == "decided"
    assert r["matched_rule"] == "allow"


def test_evaluate_deny_for_plan_persona():
    r = evaluate_permission("write", persona="plan")
    assert r["decision"] == PermissionDecision.DENY


def test_evaluate_ask_becomes_pending():
    r = evaluate_permission("bash", persona="build")  # build: ask:bash
    assert r["decision"] == PermissionDecision.PENDING
    assert r["status"] == "pending"


def test_evaluate_no_match_is_pending_security_default():
    r = evaluate_permission("ssh", persona="build", rules=["allow:read"])
    assert r["decision"] == PermissionDecision.PENDING


# ── InteractivePermissionGate ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_gate_allows_without_prompt():
    gate = InteractivePermissionGate()
    result = await gate.evaluate("read", persona="build", wait=True)
    assert result["decision"] == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_gate_denies_for_plan_persona():
    gate = InteractivePermissionGate(default_timeout=1.0)
    result = await gate.evaluate("write", persona="plan", wait=True)
    assert result["decision"] == PermissionDecision.DENY


@pytest.mark.asyncio
async def test_gate_pending_then_approve():
    gate = InteractivePermissionGate()
    result = await gate.evaluate("bash", persona="build", wait=False)
    assert result["decision"] == PermissionDecision.PENDING
    request_id = result["request_id"]
    assert len(gate.pending_requests()) == 1

    assert gate.approve(request_id)
    await asyncio.sleep(0.01)
    assert gate.pending_requests() == []  # 决出后移出队列


@pytest.mark.asyncio
async def test_gate_pending_then_deny():
    gate = InteractivePermissionGate()
    result = await gate.evaluate("bash", persona="build", wait=False)
    request_id = result["request_id"]
    assert gate.deny(request_id)
    await asyncio.sleep(0.01)
    assert gate.pending_requests() == []


@pytest.mark.asyncio
async def test_gate_await_decision_approve():
    gate = InteractivePermissionGate(default_timeout=5.0)
    result = await gate.evaluate("bash", persona="build", wait=False)
    request_id = result["request_id"]

    async def approver():
        await asyncio.sleep(0.05)
        gate.approve(request_id)

    task = asyncio.create_task(approver())
    decision = await gate.await_decision(request_id)
    await task
    assert decision["decision"] == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_gate_await_decision_timeout_auto_denies():
    gate = InteractivePermissionGate(default_timeout=0.1)
    result = await gate.evaluate("bash", persona="build", wait=False)
    request_id = result["request_id"]
    decision = await gate.await_decision(request_id, timeout=0.1)
    assert decision["decision"] == PermissionDecision.DENY
    assert "timeout" in decision["note"]


def test_gate_unknown_request_id():
    gate = InteractivePermissionGate()
    assert not gate.approve("nope")
    assert not gate.deny("nope")


def test_gate_on_pending_callback_notified():
    notified: list = []

    def on_pending(req):
        notified.append(req)

    gate = InteractivePermissionGate(on_pending=on_pending)
    result = asyncio.run(gate.evaluate("bash", persona="build", wait=False))
    assert len(notified) == 1
    assert notified[0].request_id == result["request_id"]


# ── compat 单源委托（§1.4 守卫） ──────────────────────────────────────
def test_compat_permission_evaluate_delegates():
    from veya.compat import permission_evaluate

    r = permission_evaluate("write", persona="plan")
    assert r["decision"] == PermissionDecision.DENY
    r2 = permission_evaluate("bash", persona="build")
    assert r2["decision"] == PermissionDecision.PENDING


def test_compat_match_delegates():
    from veya.compat import match_permission_rule as compat_match

    # 委托后：allow/ask 命中 → True；deny → False
    assert compat_match(["allow:*"], "read")
    assert compat_match(["ask:bash"], "bash")
    assert not compat_match(["deny:write"], "write")
