"""Tests for W16 Project Brain: Model-callable tools and isolation (Group B)."""

from pathlib import Path

import pytest

from server.goal_run import bot_identity
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.project_brain.tools import (
    project_brief_read,
    project_brief_update,
    project_log_append,
    project_task_cancel,
    project_task_start,
    project_task_status,
    project_task_steer,
    register_tools,
)


@pytest.fixture
def brain_on(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "1")
    monkeypatch.setenv("VEYA_SUPERVISOR", "1")
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_SUPERVISOR_BINDING_PATH", str(tmp_path / "supervisor_bindings.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json"))
    yield


@pytest.fixture
def brain_off(monkeypatch):
    monkeypatch.setenv("VEYA_PROJECT_BRAIN", "0")
    yield


@pytest.fixture
def project_bot(brain_on, tmp_path: Path):
    bot_id = "proj-my-test-proj"
    project_root = tmp_path / "project_root"
    project_root.mkdir(parents=True, exist_ok=True)

    spec = BotSpec(
        bot_id=bot_id,
        owner_id="user_alice",
        name="Test Project Bot",
        workspace_path=str(project_root),
        kind="project",
    )
    BotRegistry().register(spec)

    token = bot_identity.bind_current_bot(bot_id)
    yield bot_id, project_root
    bot_identity.reset_current_bot(token)


@pytest.mark.asyncio
async def test_tools_flag_gated(brain_off):
    res = await project_brief_read()
    assert "已拒绝：项目主脑未启用" in res

    res = await project_brief_update("goal", "test")
    assert "已拒绝：项目主脑未启用" in res

    res = await project_task_start("do something")
    assert "已拒绝：项目主脑未启用" in res

    res = await project_task_status()
    assert "已拒绝：项目主脑未启用" in res

    res = await project_task_steer("task_1", "change this")
    assert "已拒绝：项目主脑未启用" in res

    res = await project_task_cancel("task_1")
    assert "已拒绝：项目主脑未启用" in res

    res = await project_log_append("test entry")
    assert "已拒绝：项目主脑未启用" in res


def test_register_tools_flag_gated(brain_off):
    class FakeRegistry:
        def __init__(self):
            self.tools = {}

        def has(self, name):
            return name in self.tools

        def register(self, name, desc, schema, fn, side_effect=None):
            self.tools[name] = fn

    reg = FakeRegistry()
    count = register_tools(reg)
    assert count == 0
    assert len(reg.tools) == 0


@pytest.mark.asyncio
async def test_isolation_no_bot_id(brain_on):
    # No bot bound
    res = await project_brief_read()
    assert "无法识别当前机器人身份" in res


@pytest.mark.asyncio
async def test_isolation_unregistered_bot(brain_on):
    token = bot_identity.bind_current_bot("unregistered_bot_123")
    try:
        res = await project_brief_read()
        assert "未在注册表登记" in res
    finally:
        bot_identity.reset_current_bot(token)


@pytest.mark.asyncio
async def test_isolation_bot_no_workspace(brain_on, tmp_path: Path):
    spec = BotSpec(bot_id="bot_no_ws", owner_id="user_bob", name="No WS")
    BotRegistry().register(spec)
    token = bot_identity.bind_current_bot("bot_no_ws")
    try:
        res = await project_brief_read()
        assert "未绑定项目工作区" in res
    finally:
        bot_identity.reset_current_bot(token)


@pytest.mark.asyncio
async def test_project_brief_read_and_update(project_bot):
    _bot_id, _project_root = project_bot

    # Update goal_scope
    res = await project_brief_update("goal_scope", "构建现代化数据平台")
    assert "已成功更新" in res

    # Read back all
    all_brief = await project_brief_read()
    assert "构建现代化数据平台" in all_brief

    # Read specific section
    goal_sec = await project_brief_read("goal")
    assert "构建现代化数据平台" in goal_sec

    # Update rules
    res = await project_brief_update("rules", "- when: test fails → do: retry [approval: ask]")
    assert "已成功更新" in res
    rules_sec = await project_brief_read("rules")
    assert "test fails" in rules_sec


@pytest.mark.asyncio
async def test_project_log_append(project_bot):
    _bot_id, project_root = project_bot

    res = await project_log_append("完成了数据库模式初始化。")
    assert "已记录项目日志" in res

    from server.project_store import ProjectStore

    logs = ProjectStore(project_root).read_log()
    assert "完成了数据库模式初始化。" in logs


@pytest.mark.asyncio
async def test_project_task_lifecycle(project_bot):
    _bot_id, _project_root = project_bot

    # Start task
    res = await project_task_start(
        "编写核心用户接口", report_every_minutes=5, stall_after_minutes=15
    )
    assert "已启动项目任务" in res
    assert "编写核心用户接口" in res

    # Extract task_id
    import re

    m = re.search(r"\[(ptask_[a-f0-9]+)\]", res)
    assert m is not None
    task_id = m.group(1)

    # Status check
    status_res = await project_task_status(task_id)
    assert "编写核心用户接口" in status_res
    assert task_id in status_res

    # Steer task
    steer_res = await project_task_steer(task_id, "使用 FastAPI 替换原本的 Flask")
    assert "已成功将修正指令投递至任务" in steer_res

    # Cancel task
    cancel_res = await project_task_cancel(task_id, reason="需求变更")
    assert "已取消并关闭巡检绑定" in cancel_res

    # Status check after cancel
    status_after = await project_task_status(task_id)
    assert "cancelled" in status_after


def test_register_tools_active(brain_on):
    class FakeRegistry:
        def __init__(self):
            self.tools = {}

        def has(self, name):
            return name in self.tools

        def register(self, name, desc, schema, fn, side_effect=None):
            self.tools[name] = {"fn": fn, "side_effect": side_effect, "desc": desc}

    reg = FakeRegistry()
    count = register_tools(reg)
    assert count == 7
    expected = {
        "project_brief_read",
        "project_brief_update",
        "project_task_start",
        "project_task_status",
        "project_task_steer",
        "project_task_cancel",
        "project_log_append",
    }
    assert set(reg.tools.keys()) == expected
