"""server.team_tools 测试 — Team Mode 的 MasterAgent 工具面(oh-my-openagent 内化，
见 memory project_veya_pi_gap_audit)。

覆盖: create→task_create→send_message/read_messages→task_update(claim)→
status→shutdown_request→approve→delete 的端到端链路, 以及失败分支返回可读
错误(不抛异常)。
"""

from __future__ import annotations

import pytest

import server.team_coord as team_coord
import server.team_tools as tt


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    store = team_coord.TeamStore(path=tmp_path / "teams.json")
    monkeypatch.setattr(team_coord, "_default_store", store)
    yield store


@pytest.mark.asyncio
async def test_create_and_list():
    out = await tt.team_create("explorers", description="d", lead="atlas", member_ids=["scout-1"])
    assert "✅" in out
    listed = await tt.team_list()
    assert "explorers" in listed


@pytest.mark.asyncio
async def test_create_duplicate_is_readable_not_exception():
    await tt.team_create("t1")
    out = await tt.team_create("t1")
    assert "✅" not in out
    assert "已存在" in out


@pytest.mark.asyncio
async def test_message_send_and_read_roundtrip():
    await tt.team_create("t1", member_ids=["a", "b"])
    out = await tt.team_send_message("t1", from_member="a", content="hi b", to_member="b")
    assert "✅" in out
    inbox = await tt.team_read_messages("t1", member_id="b")
    assert "hi b" in inbox
    # 读过之后再读, 没有新消息
    inbox2 = await tt.team_read_messages("t1", member_id="b")
    assert inbox2 == "没有新消息"


@pytest.mark.asyncio
async def test_task_claim_and_double_claim_rejected():
    await tt.team_create("t1", member_ids=["a", "b"])
    created = await tt.team_task_create("t1", title="scout auth")
    assert "✅" in created
    task_id = created.split()[2].rstrip(":")

    claim1 = await tt.team_task_update("t1", task_id, status="claimed", claimed_by="a")
    assert "✅" in claim1
    claim2 = await tt.team_task_update("t1", task_id, status="claimed", claimed_by="b")
    assert "✅" not in claim2
    assert "已被 a 认领" in claim2


@pytest.mark.asyncio
async def test_task_list_and_get():
    await tt.team_create("t1")
    created = await tt.team_task_create("t1", title="do X")
    task_id = created.split()[2].rstrip(":")

    listed = await tt.team_task_list("t1")
    assert "do X" in listed
    detail = await tt.team_task_get("t1", task_id)
    assert "do X" in detail


@pytest.mark.asyncio
async def test_shutdown_negotiation_flow():
    await tt.team_create("t1", member_ids=["a"])
    req = await tt.team_shutdown_request("t1", member_id="a", reason="done")
    assert "✅" in req

    # 还没 approve, 强制 delete 之外应该被拦
    denied = await tt.team_delete("t1")
    assert "✅" not in denied

    approved = await tt.team_approve_shutdown("t1", member_id="a")
    assert "✅" in approved

    # approve 之后成员不再是 active, force 也不需要了——但用 force 稳妥验证不再报错
    deleted = await tt.team_delete("t1", force=True)
    assert "✅" in deleted


@pytest.mark.asyncio
async def test_reject_shutdown_returns_to_active():
    await tt.team_create("t1", member_ids=["a"])
    await tt.team_shutdown_request("t1", member_id="a")
    out = await tt.team_reject_shutdown("t1", member_id="a", reason="still needed")
    assert "✅" in out
    status = await tt.team_status("t1")
    assert "a=active" in status


@pytest.mark.asyncio
async def test_status_aggregates():
    await tt.team_create("t1", member_ids=["a", "b"])
    await tt.team_task_create("t1", title="x")
    out = await tt.team_status("t1")
    assert "open=1" in out
    assert "a=active" in out and "b=active" in out


@pytest.mark.asyncio
async def test_missing_team_errors_are_readable():
    out = await tt.team_status("nope")
    assert "✅" not in out
    assert "不存在" in out
