"""T8 + server 转发测试: feature flag 切回旧路径仍可用（迁移期双轨）。"""

from __future__ import annotations

import pytest


def test_flag_off_uses_legacy_path(monkeypatch: pytest.MonkeyPatch):
    """未设置 flag → make_plan_func 返回旧 plan_todo 函数（T8）。"""
    monkeypatch.delenv("LOOP_PLANE_URL", raising=False)
    monkeypatch.setenv("LOOP_PLANE_INPROCESS", "false")
    from server.loop_plane_client import loop_plane_enabled, make_plan_func

    assert loop_plane_enabled() is False
    fn = make_plan_func("create_plan")
    import inspect

    # 旧函数: async 且来自 server.plan_todo
    assert inspect.iscoroutinefunction(fn)
    assert fn.__module__ == "server.plan_todo"


@pytest.mark.asyncio
async def test_flag_inprocess_forwards_create_plan(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """LOOP_PLANE_INPROCESS=true → create_plan 转发到 loop-plane（事件溯源）。"""
    monkeypatch.setenv("LOOP_PLANE_INPROCESS", "true")
    monkeypatch.setenv("LOOP_DATA_DIR", str(tmp_path / "loop"))
    from server.loop_plane_client import _get_client

    client = _get_client()
    text = await client.create_plan("转发目标", [{"id": "t1", "title": "步骤一"}])
    assert "已创建计划" in text
    plan_id = text.split("已创建计划 ")[1].splitlines()[0]
    # 事件已落盘（EventStore 文件存在）
    events_file = tmp_path / "loop" / "default" / "events.jsonl"
    assert events_file.exists()
    content = events_file.read_text(encoding="utf-8")
    assert "GoalCreated" in content
    # 状态查询转发
    status = await client.plan_status(plan_id)
    assert "转发目标" in status


def test_wire_loop_tools_registers_three(monkeypatch: pytest.MonkeyPatch):
    """loop_* 三个新工具注册（冻结架构: 只加工具）。"""
    from server.loop_plane_client import wire_loop_tools
    from server.tool_registry import master_tools

    added = wire_loop_tools()
    assert added >= 3 or all(master_tools.has(n) for n in
                             ("loop_plan_goal", "loop_diagnose", "loop_intervene"))
    for name in ("loop_plan_goal", "loop_diagnose", "loop_intervene"):
        assert master_tools.has(name), f"缺少工具 {name}"


@pytest.mark.asyncio
async def test_loop_tools_disabled_hint_without_flag(monkeypatch: pytest.MonkeyPatch):
    """flag 关闭 → 工具返回明确提示，不隐式启用（零副作用）。"""
    monkeypatch.delenv("LOOP_PLANE_URL", raising=False)
    monkeypatch.setenv("LOOP_PLANE_INPROCESS", "false")
    from server.loop_plane_client import loop_diagnose, loop_intervene, loop_plan_goal

    assert "未启用" in await loop_plan_goal("目标")
    assert "未启用" in await loop_diagnose("症状")
    assert "未启用" in await loop_intervene("sandbox", "echo", {})
