"""server.goal_tools tests — goal_start/goal_add_todo/goal_status against a
real (file-backed, isolated tmp dir) GoalKernel event stream. No mocking of
GoalKernel/LongTaskDriver themselves — those are exercised for real.
"""

from __future__ import annotations

import pytest

from server import goal_session_map as gsm
from server.goal_tools import goal_add_todo, goal_start, goal_status
from server.tool_registry import _current_master_session


@pytest.fixture(autouse=True)
def isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gsm, "GOAL_LOOPS_DIR", tmp_path / "loops")
    import server.goal_tools as gt

    monkeypatch.setattr(gt, "GOAL_LOOPS_DIR", tmp_path / "loops")


@pytest.fixture
def session():
    token = _current_master_session.set("sess-1")
    yield "sess-1"
    _current_master_session.reset(token)


@pytest.mark.asyncio
async def test_goal_start_without_session_is_readable_error():
    out = await goal_start("some goal")
    assert "✅" not in out
    assert "session" in out


@pytest.mark.asyncio
async def test_goal_start_returns_confirmation_and_persists_association(session):
    out = await goal_start("重构结算模块", budget_usd=3.0)
    assert "✅" in out
    assert gsm.get_goal_id(session) is not None


@pytest.mark.asyncio
async def test_goal_status_before_goal_start_is_readable_error(session):
    out = await goal_status()
    assert "✅" not in out
    assert "还没开长程任务" in out


@pytest.mark.asyncio
async def test_goal_status_reports_budget_and_empty_todos(session):
    await goal_start("重构结算模块", budget_usd=3.0)
    out = await goal_status()
    assert "重构结算模块" in out
    assert "0 open" in out
    assert "3.0000" in out


@pytest.mark.asyncio
async def test_goal_add_todo_then_status_shows_it(session):
    await goal_start("重构结算模块", budget_usd=3.0)
    add_out = await goal_add_todo("t1", "拆服务")
    assert "✅" in add_out
    status_out = await goal_status()
    assert "1 open" in status_out
    assert "t1:拆服务" in status_out


@pytest.mark.asyncio
async def test_goal_status_can_mark_todo_done(session):
    await goal_start("重构结算模块", budget_usd=3.0)
    await goal_add_todo("t1", "拆服务")
    out = await goal_status(todo_id="t1", status="done")
    assert "1/1 done" in out
    assert "0 open" in out


@pytest.mark.asyncio
async def test_second_session_is_independent(monkeypatch):
    token1 = _current_master_session.set("sess-a")
    await goal_start("goal A", budget_usd=1.0)
    _current_master_session.reset(token1)

    token2 = _current_master_session.set("sess-b")
    out = await goal_status()
    _current_master_session.reset(token2)
    assert "还没开长程任务" in out
