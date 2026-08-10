"""状态内核 + 计划/长文认知增强测试 (ARCHITECTURE_STATE_KERNEL + ef7b40e0)。

覆盖:
- plan_todo: create_plan / plan_status / update_todo (状态流转 + 证据链 + 防逃逸)
- state_kernel: quota_should_run / todo_claim / gate_check (Phase 1)
- state_kernel: quota_spend_slot / terminal_gate_check / boundary_scan (Phase 2+3)
- long_read: 分块 / 大纲 / focus (主脑长文导航)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from server.state_kernel import (
    boundary_scan,
    gate_check,
    quota_should_run,
    quota_spend_slot,
    terminal_gate_check,
    todo_claim,
)


def _plan_id(out: str) -> str:
    m = re.search(r"[a-z0-9-]{4,64}", out.split("计划 ")[1])
    assert m
    return m.group()


@pytest.mark.asyncio
async def test_plan_create_status_update(tmp_path, monkeypatch):
    """create_plan → plan_status → update_todo 状态流转 + 证据链。"""
    monkeypatch.setattr("server.plan_todo._PLANS_ROOT", tmp_path / "plans")
    from server.plan_todo import create_plan, plan_status, update_todo

    out = await create_plan("测试计划", [
        {"title": "写代码", "id": "t1"},
        {"title": "跑测试", "id": "t2", "depends_on": ["t1"]},
    ])
    pid = _plan_id(out)

    status = await plan_status(pid)
    assert "写代码" in status and "⬜" in status

    r = await update_todo(pid, "t1", "done", evidence="代码完成")
    assert "✅" in r
    status2 = await plan_status(pid)
    assert "代码完成" in status2  # 证据链可见
    # blocked 合法状态
    await update_todo(pid, "t2", "blocked", evidence="依赖未满足")
    assert "⛔" in await plan_status(pid)


@pytest.mark.asyncio
async def test_plan_rejects_bad_status_and_id(tmp_path, monkeypatch):
    monkeypatch.setattr("server.plan_todo._PLANS_ROOT", tmp_path / "plans")
    from server.plan_todo import create_plan, update_todo

    out = await create_plan("X", [{"title": "a", "id": "t1"}])
    pid = _plan_id(out)
    with pytest.raises(ValueError):
        await update_todo(pid, "t1", "bogus")
    with pytest.raises(ValueError):
        await update_todo("../etc/passwd", "t1", "done")


@pytest.mark.asyncio
async def test_quota_state_machine(tmp_path, monkeypatch):
    """quota: 可推进→deliver / 依赖未满足→repair / 全 done→wait。"""
    monkeypatch.setattr("server.plan_todo._PLANS_ROOT", tmp_path / "plans")
    from server.plan_todo import create_plan, update_todo

    out = await create_plan("Q", [
        {"title": "a", "id": "t1"},
        {"title": "b", "id": "t2", "depends_on": ["t1"]},
    ])
    pid = _plan_id(out)

    q1 = json.loads(await quota_should_run(pid))
    assert q1["should_run"] and q1["action"] == "deliver"

    await update_todo(pid, "t1", "done")
    q2 = json.loads(await quota_should_run(pid))
    assert q2["action"] == "deliver"

    await update_todo(pid, "t2", "done")
    q3 = json.loads(await quota_should_run(pid))
    assert q3["should_run"] is False and q3["action"] == "wait"


@pytest.mark.asyncio
async def test_claim_lease(tmp_path, monkeypatch):
    monkeypatch.setattr("server.plan_todo._PLANS_ROOT", tmp_path / "plans")
    from server.plan_todo import create_plan

    out = await create_plan("C", [{"title": "a", "id": "t1"}])
    pid = _plan_id(out)

    r1 = await todo_claim(pid, "t1", lease_minutes=45)
    assert "已认领" in r1
    # 重复认领 → 拒绝 (他人有效租约)
    r2 = await todo_claim(pid, "t1")
    assert "已被认领" in r2
    # done 不能认领
    from server.plan_todo import update_todo

    await update_todo(pid, "t1", "done")
    assert "已完成" in await todo_claim(pid, "t1")


@pytest.mark.asyncio
async def test_gate_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr("server.plan_todo._PLANS_ROOT", tmp_path / "plans")
    from server.plan_todo import create_plan, update_todo

    out = await create_plan("G", [
        {"title": "写代码", "id": "t1"},
        {"title": "跑测试", "id": "t2", "depends_on": ["t1"]},
    ])
    pid = _plan_id(out)

    g = json.loads(await gate_check(pid, "跑测试"))
    assert g["gate_open"] is False and g["blocking_todos"] == ["t1"]

    await update_todo(pid, "t1", "done")
    g2 = json.loads(await gate_check(pid, "跑测试"))
    assert g2["gate_open"] is True


@pytest.mark.asyncio
async def test_spend_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("server.plan_todo._PLANS_ROOT", tmp_path / "plans")
    from server.plan_todo import create_plan, update_todo

    out = await create_plan("S", [{"title": "a", "id": "t1"}])
    pid = _plan_id(out)
    await update_todo(pid, "t1", "done")

    r1 = await quota_spend_slot(pid, "t1", "eff-1", "验证通过")
    assert "记账" in r1
    r2 = await quota_spend_slot(pid, "t1", "eff-1", "重复")
    assert "幂等跳过" in r2
    # 未 done 不能 spend
    from server.plan_todo import create_plan as cp2

    out2 = await cp2("S2", [{"title": "b", "id": "t1"}])
    assert "不能 spend" in await quota_spend_slot(_plan_id(out2), "t1", "eff-2")


@pytest.mark.asyncio
async def test_terminal_gate():
    g = json.loads(await terminal_gate_check("git push 到 main"))
    assert g["requires_approval"] is True and g["authority_level"] == "terminal"
    g2 = json.loads(await terminal_gate_check("写文档"))
    assert g2["requires_approval"] is False


@pytest.mark.asyncio
async def test_boundary_scan(tmp_path):
    (tmp_path / "ok.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "secret.env").write_text(
        "API_KEY=sk-abc12345678901234567890", encoding="utf-8")
    b = json.loads(await boundary_scan(str(tmp_path)))
    assert b["risk_level"] == "high"
    assert any("secret.env" in f["path"] for f in b["sensitive_files"])


@pytest.mark.asyncio
async def test_long_read_chunks(tmp_path):
    from server.long_read import long_read

    doc = tmp_path / "doc.txt"
    doc.write_text("\n".join(f"第{i}章 内容{j}" for i in range(30) for j in range(15)),
                   encoding="utf-8")
    out = await long_read(str(doc))
    assert "chunk[0]" in out and "chunk[1]" in out
    assert "总行数" in out
    # 深入某块
    deep = await long_read(str(doc), chunk_id=1)
    assert "第" in deep
