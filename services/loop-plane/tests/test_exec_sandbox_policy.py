"""T6: exec 硬化策略（SPEC §11）: sandbox + 未知 tool → failed/permission_denied。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_t6_dispatch_unknown_tool(client):
    """未知 tool → permission_denied。"""
    r = await client.post("/v1/loop/exec/dispatch", json={
        "mode": "sandbox", "tool_name": "nonexistent", "args": {},
    })
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is False
    assert result["permission"] == "permission_denied"
    assert "未知 tool" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_known_tool_sandbox(client):
    """echo 适配器 sandbox 模式可执行。"""
    r = await client.post("/v1/loop/exec/dispatch", json={
        "mode": "sandbox", "tool_name": "echo", "args": {"text": "hi"},
    })
    assert r.status_code == 200
    result = r.json()
    assert result["ok"] is True
    assert result["output"] == {"echo": "hi"}

    # 异步结果可查
    run_id = result["run_id"]
    r2 = await client.get(f"/v1/loop/exec/runs/{run_id}")
    assert r2.json()["run_id"] == run_id


@pytest.mark.asyncio
async def test_dispatch_forbidden_python_m(client):
    """sandbox 禁止 python -m 任意路径。"""
    r = await client.post("/v1/loop/exec/dispatch", json={
        "mode": "sandbox", "tool_name": "echo",
        "args": {"text": "x", "cmd": "python -m pip install requests"},
    })
    assert r.json()["permission"] == "permission_denied"
    assert "python -m" in r.json()["error"]


def test_adapter_registry_whitelist(exec_service):
    """白名单列表 + needs 收缩。"""
    adapters = exec_service.registry.list()
    names = [a["name"] for a in adapters]
    assert "echo" in names

    # needs=2 的适配器在 sandbox 下必须被收缩拒绝
    exec_service.registry.register("canary_op", lambda: "x", needs=2)
    r = exec_service.dispatch(mode="sandbox", tool_name="canary_op", args={})
    assert r["permission"] == "permission_denied"
    assert "权限不足" in r["error"]

    # shadow (1) 也拒绝 needs=2
    r2 = exec_service.dispatch(mode="shadow", tool_name="canary_op", args={})
    assert r2["permission"] == "permission_denied"

    # live_canary (2) 放行
    r3 = exec_service.dispatch(mode="live_canary", tool_name="canary_op", args={})
    assert r3["ok"] is True


def test_exec_writes_audit_and_events(exec_service, audit, store):
    """T7 扩展: execute 节点审计 + ActionFailed 事件 + trace_id 关联。"""
    trace = "trc_exec_1"
    r = exec_service.dispatch(
        mode="sandbox", tool_name="nonexistent", args={}, trace_id=trace,
        audit=audit, store=store,
    )
    assert r["permission"] == "permission_denied"
    entries = audit.by_trace(trace)
    assert any(e["phase"] == "execute" for e in entries)
    assert entries[0]["decision_made"]["tool"] == "nonexistent"
    events = store.stream(aggregate_type="Run", aggregate_id=r["run_id"])
    assert events[0]["event_type"] == "ActionFailed"
